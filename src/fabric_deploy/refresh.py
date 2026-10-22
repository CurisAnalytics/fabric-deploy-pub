"""Direct Lake framing: SQL-endpoint sync -> refresh -> poll -> surface error.

After a Delta write or a semantic-model deploy, a Direct Lake model needs to
be re-framed before it serves correct data — and a Direct Lake model that reads
a lakehouse through its SQL analytics endpoint needs that endpoint's metadata
synced first, or the refresh fails with "cannot access the source Delta table".
`fabdep refresh` and `fabdep deploy --frame` run that sequence; models without
Direct Lake tables are refreshed without the sync.

A `Completed` refresh proves the model re-framed; it does NOT prove measures
evaluate.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .client import (
    find_item_by_name,
    get_semantic_model_definition,
    get_workspace_id_by_name,
    list_workspace_items,
    refresh_semantic_model,
    refresh_sql_endpoint_metadata,
)
from .config import FabdepConfig
from .identity import apply_tenant_pin
from .log import get_logger
from .scope import repo_items

__all__ = [
    "extract_refresh_error",
    "failed_table_syncs",
    "frame_models",
]

_DIRECT_LAKE_PARTITION = re.compile(r"^\s*mode:\s*directLake\s*$", re.MULTILINE | re.IGNORECASE)
# Direct Lake on SQL reads a lakehouse via `Sql.Database("<host>", "<SQL endpoint id>")`.
_SQL_ENDPOINT_ID = re.compile(
    r'Sql\.Database\(\s*"[^"]*"\s*,\s*'
    r'"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"\s*\)',
    re.IGNORECASE,
)


def failed_table_syncs(statuses: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return (tableName, error message) for every non-successful table sync.

    `NotRun` is not a failure (the table was simply already current), so only
    `Failure` rows count.
    """
    failures: list[tuple[str, str]] = []
    for s in statuses:
        if s.get("status") == "Failure":
            err = s.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            failures.append((s.get("tableName", "<unknown>"), msg or "Failure"))
    return failures


def _pick_service_exception(detail: dict[str, Any]) -> str | None:
    """Pick the most detailed serviceExceptionJson from a refresh detail.

    The per-attempt `refreshAttempts[].serviceExceptionJson` carries the rich
    error (with `errorDescription`); the top-level field is often just
    `{"errorCode": ...}`. Prefer the last attempt that has one, then top-level.
    """
    for attempt in reversed(detail.get("refreshAttempts") or []):
        if isinstance(attempt, dict) and attempt.get("serviceExceptionJson"):
            return attempt["serviceExceptionJson"]
    return detail.get("serviceExceptionJson")


def extract_refresh_error(detail: dict[str, Any]) -> str | None:
    """Pull the first human-readable framing error out of a refresh detail.

    Returns None when the refresh succeeded. Prefers the structured
    serviceExceptionJson (per-attempt before top-level), then the first
    `Error`-typed message, then the bare status — so a Failed refresh always
    yields *something* actionable, and never just echoes the error code twice.
    """
    status = detail.get("status")
    if status in (None, "Completed"):
        return None

    raw = _pick_service_exception(detail)
    if raw:
        try:
            parsed = json.loads(raw)
            msg = parsed.get("errorDescription") or parsed.get("error")
            code = parsed.get("errorCode")
            if msg and code:
                return f"{code}: {msg}"
            return msg or code or str(raw)
        except (ValueError, TypeError):
            return str(raw)

    messages = detail.get("messages") or []
    # Entries carry type Error|Warning — surface an actual error, not a warning.
    errors = [m for m in messages if isinstance(m, dict) and m.get("type") == "Error"]
    chosen = errors or messages
    if chosen:
        first = chosen[0]
        if isinstance(first, dict):
            return f"{first.get('code', '')} {first.get('message', '')}".strip()
        return str(first)

    return f"Refresh status: {status}"


def _sql_endpoints_to_sync(
    workspace_id: str, models: list[dict[str, Any]], logger: logging.Logger
) -> list[dict[str, Any]]:
    """The workspace's SQL analytics endpoints that the Direct Lake models read.

    Taken from each model's deployed definition, so every lakehouse a model
    uses is found, under the ids the deploy actually wrote. Models without
    Direct Lake tables (Import, DirectQuery) contribute nothing.
    """
    wanted: set[str] = set()
    for item in models:
        tmdl = "\n".join(get_semantic_model_definition(workspace_id, item["id"]).values())
        if _DIRECT_LAKE_PARTITION.search(tmdl):
            wanted.update(ep.lower() for ep in _SQL_ENDPOINT_ID.findall(tmdl))
        else:
            logger.info(
                f"'{item['displayName']}' has no Direct Lake tables — refreshing it "
                "without a SQL endpoint sync."
            )
    if not wanted:
        return []

    endpoints = {ep["id"].lower(): ep for ep in list_workspace_items(workspace_id, "SQLEndpoint")}
    if foreign := sorted(wanted - endpoints.keys()):
        logger.info(
            f"Not syncing SQL endpoint(s) outside this workspace: {', '.join(foreign)} "
            "— sync them in their own workspace if their tables changed."
        )
    return [endpoints[ep] for ep in sorted(wanted & endpoints.keys())]


def frame_models(
    repo_dir: Path | str,
    branch_override: str | None = None,
    models: list[str] | None = None,
    sync: bool = True,
    timeout_s: int = 900,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Sync the SQL endpoints the models read, then refresh + poll each model.

    Args:
        repo_dir: Workspace repo directory (for fabdep.yml + item discovery).
        branch_override: Override the git branch for workspace resolution.
        models: Semantic-model display names to frame. Defaults to every
            SemanticModel present in the repo.
        sync: First sync the metadata of every SQL analytics endpoint the
            Direct Lake models read (found in their deployed definitions).
            Disable with --no-sync.
        timeout_s: Per-model refresh poll timeout.
        logger: Logger to use (one is created if omitted).

    Returns:
        One refresh-detail dict per model framed.

    Raises:
        ValueError: If no target models can be determined.
        RuntimeError: If a refresh fails (with the first framing error).
    """
    log = logger or get_logger(__name__)
    repo_dir = Path(repo_dir).resolve()

    config = FabdepConfig.from_path(repo_dir, branch_override=branch_override)
    apply_tenant_pin(config.tenant, log)
    workspace_id = get_workspace_id_by_name(config.workspace_name)
    log.info(f"Workspace: '{config.workspace_name}' ({workspace_id})")

    # `refresh -i` takes a display name, but `deploy -i` takes "Name.Type" —
    # tolerate a trailing ".SemanticModel" so the two share muscle memory.
    if models:
        models = [m.removesuffix(".SemanticModel") for m in models]

    targets = models or sorted(item.name for item in repo_items(repo_dir, "SemanticModel"))
    if not targets:
        raise ValueError(
            "No semantic model to frame. Pass -i '<displayName>' or run from a "
            "repo that contains a .SemanticModel item."
        )

    items = [find_item_by_name(workspace_id, "SemanticModel", name) for name in targets]

    if sync:
        for endpoint in _sql_endpoints_to_sync(workspace_id, items, log):
            log.info(f"Syncing SQL analytics endpoint '{endpoint['displayName']}'...")
            statuses = refresh_sql_endpoint_metadata(workspace_id, endpoint["id"])
            failures = failed_table_syncs(statuses)
            for table, msg in failures:
                log.warning(f"  table sync failed: {table}: {msg}")
            if not failures:
                log.info(f"  SQL endpoint synced ({len(statuses)} table(s)).")

    details: list[dict[str, Any]] = []
    for item in items:
        log.info(f"Refreshing semantic model '{item['displayName']}'...")
        detail = refresh_semantic_model(workspace_id, item["id"], timeout_s=timeout_s)
        details.append(detail)

        err = extract_refresh_error(detail)
        if err:
            raise RuntimeError(f"Framing failed for '{item['displayName']}': {err}")
        log.info(f"  Refresh {detail.get('status', 'Completed')}.")

    log.info(
        "Framing complete. NOTE: a Completed refresh proves the model re-framed, "
        "not that measures evaluate — verify a visual / DAX query."
    )
    return details
