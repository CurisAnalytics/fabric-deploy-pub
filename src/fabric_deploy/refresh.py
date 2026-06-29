"""Direct Lake framing: SQL-endpoint sync -> refresh -> poll -> surface error.

After a Delta write or a semantic-model deploy, a Direct Lake model needs to
be re-framed before it serves correct data — and the SQL analytics endpoint
must be synced first or the refresh fails with "cannot access the source Delta
table". This module codifies that sequence (the hand-rolled ~6-step recipe)
behind `fabdep refresh` and `fabdep deploy --frame`.

A `Completed` refresh proves the model re-framed; it does NOT prove measures
evaluate.
"""

from __future__ import annotations

import json
import logging
from json import loads
from pathlib import Path

from .client import (
    find_item_by_name,
    get_workspace_id_by_name,
    list_workspace_items,
    refresh_semantic_model,
    refresh_sql_endpoint_metadata,
)
from .config import FabdepConfig
from .log import get_logger
from .identity import apply_tenant_pin

__all__ = [
    "extract_refresh_error",
    "failed_table_syncs",
    "frame_models",
]


def failed_table_syncs(statuses: list[dict]) -> list[tuple[str, str]]:
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


def _pick_service_exception(detail: dict) -> str | None:
    """Pick the most detailed serviceExceptionJson from a refresh detail.

    The per-attempt `refreshAttempts[].serviceExceptionJson` carries the rich
    error (with `errorDescription`); the top-level field is often just
    `{"errorCode": ...}`. Prefer the last attempt that has one, then top-level.
    """
    for attempt in reversed(detail.get("refreshAttempts") or []):
        if isinstance(attempt, dict) and attempt.get("serviceExceptionJson"):
            return attempt["serviceExceptionJson"]
    return detail.get("serviceExceptionJson")


def extract_refresh_error(detail: dict) -> str | None:
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


def _repo_display_names(repo_dir: Path, item_type: str) -> list[str]:
    """Display names of every `<name>.<item_type>` item in the repo."""
    names: list[str] = []
    for platform_file in repo_dir.rglob(".platform"):
        if not platform_file.parent.name.endswith(f".{item_type}"):
            continue
        try:
            names.append(loads(platform_file.read_text())["metadata"]["displayName"])
        except Exception:
            continue
    return names


def _resolve_sql_endpoint(
    workspace_id: str, repo_dir: Path, logger: logging.Logger
) -> str | None:
    """Best-effort resolve the SQL endpoint to sync.

    Matches the repo's lakehouse display name first; falls back to the sole
    endpoint in the workspace. Returns None (with a warning) when it can't be
    determined unambiguously — sync is skipped rather than failing the whole
    refresh.
    """
    endpoints = list_workspace_items(workspace_id, "SQLEndpoint")
    if not endpoints:
        logger.warning("No SQL analytics endpoint found in the workspace; skipping sync.")
        return None

    lakehouses = _repo_display_names(repo_dir, "Lakehouse")
    for lh in lakehouses:
        for ep in endpoints:
            if ep["displayName"].lower() == lh.lower():
                return ep["id"]

    if len(endpoints) == 1:
        return endpoints[0]["id"]

    logger.warning(
        "Could not determine which SQL endpoint to sync "
        f"({len(endpoints)} present, no name match) — skipping sync. "
        "Re-run with --no-sync, or sync manually."
    )
    return None


def frame_models(
    repo_dir: Path | str,
    branch_override: str | None = None,
    models: list[str] | None = None,
    sync: bool = True,
    timeout_s: int = 900,
    logger: logging.Logger | None = None,
) -> list[dict]:
    """Sync the SQL endpoint, then refresh + poll each target semantic model.

    Args:
        repo_dir: Workspace repo directory (for fabdep.yml + item discovery).
        branch_override: Override the git branch for workspace resolution.
        models: Semantic-model display names to frame. Defaults to every
            SemanticModel present in the repo.
        sync: Sync the SQL analytics endpoint metadata first (the staleness
            fix). Disable with --no-sync.
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

    targets = models or _repo_display_names(repo_dir, "SemanticModel")
    if not targets:
        raise ValueError(
            "No semantic model to frame. Pass -i '<displayName>' or run from a "
            "repo that contains a .SemanticModel item."
        )

    if sync:
        endpoint_id = _resolve_sql_endpoint(workspace_id, repo_dir, log)
        if endpoint_id:
            log.info("Syncing SQL analytics endpoint metadata...")
            statuses = refresh_sql_endpoint_metadata(workspace_id, endpoint_id)
            failures = failed_table_syncs(statuses)
            if failures:
                for name, msg in failures:
                    log.warning(f"  table sync failed: {name}: {msg}")
            else:
                log.info(f"  SQL endpoint synced ({len(statuses)} table(s)).")

    details: list[dict] = []
    for name in targets:
        item = find_item_by_name(workspace_id, "SemanticModel", name)
        log.info(f"Refreshing semantic model '{item['displayName']}'...")
        detail = refresh_semantic_model(workspace_id, item["id"], timeout_s=timeout_s)
        details.append(detail)

        err = extract_refresh_error(detail)
        if err:
            raise RuntimeError(
                f"Framing failed for '{item['displayName']}': {err}"
            )
        log.info(f"  Refresh {detail.get('status', 'Completed')}.")

    log.info(
        "Framing complete. NOTE: a Completed refresh proves the model re-framed, "
        "not that measures evaluate — verify a visual / DAX query."
    )
    return details
