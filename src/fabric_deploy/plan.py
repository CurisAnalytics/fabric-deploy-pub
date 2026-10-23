"""`fabdep plan`: what a deploy would do, worked out with read calls only.

`plan_deploy` runs the deploy's own steps up to the point where it would
publish: it resolves the target workspace, the item scope, the find/replace
rules and every guard's verdict, and lists the workspace's items to tell
creates from updates and find the orphans cleanup would unpublish. It never
creates, updates, publishes or unpublishes anything. `format_plan` renders
the result for people, `plan_json` for machines (shape: `JSON_SHAPE`).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from fabric_cicd import constants

from .client import list_workspace_items
from .identity import credential_in_use
from .log import GREEN, RED, RESET, YELLOW, get_logger
from .params import Rule, for_environment
from .rls import RlsCheck
from .scope import RepoItem, repo_items
from .workspace import DeployScope, Verdict, WorkspaceDeployer

__all__ = [
    "BLOCKED_EXIT_CODE",
    "JSON_SHAPE",
    "Plan",
    "PlannedItem",
    "PlannedRule",
    "Target",
    "format_plan",
    "plan_deploy",
    "plan_json",
]

BLOCKED_EXIT_CODE = 3  # `fabdep plan` exits with it when a guard would block the deploy

JSON_SHAPE = """\
target      {branch, workspace, workspace_id (null: the workspace doesn't exist),
            create (whether the deploy would create it), capacity_id (of a
            workspace it would create), environment, credential, tenant (null:
            not pinned)}
items       [{name, type, action: create | update | unpublish}], in the order
            fabric-cicd would act on them
cleanup     {runs, reason: --no-cleanup | --include-items | --pr | null}
parameters  [{find_value, replace_value (null: no value for the environment, so
            not applied), origin: parameter.yml | <extend template> | fabdep.yml
            | detected, kind, items: published items whose files contain it}]
guards      {fail_on_stale, unbound_udf, rls}, each {verdict: pass | warn |
            block, message, findings}; rls also has models: [{model, status:
            new | unchanged | changed | skipped, adds, removes, changes,
            recreates}]
warnings    [what the deploy would warn about before publishing]
blocked     whether a guard would block the deploy"""

_LOGGER = "fabric_deploy.plan"
# fabric-cicd publishes item types in this order and unpublishes them in reverse.
_PUBLISH_ORDER = [item_type.value for item_type in constants.SERIAL_ITEM_PUBLISH_ORDER.values()]


@dataclass(frozen=True)
class Target:
    """Where the deploy would go."""

    branch: str
    workspace: str  # display name
    workspace_id: str | None  # None: the workspace doesn't exist
    create: bool  # whether the deploy would create it
    capacity_id: str | None  # the capacity a workspace the deploy creates gets
    environment: str  # fabric-cicd environment
    credential: str  # how fabdep signs in, e.g. "cli (default)"
    tenant: str | None  # the tenant fabdep.yml pins


@dataclass(frozen=True)
class PlannedItem:
    name: str
    type: str
    action: str  # "create", "update" or "unpublish"


@dataclass(frozen=True)
class PlannedRule:
    """A find_replace rule as it applies in the deploy's environment."""

    find_value: str
    replace_value: str | None  # None: no value for the environment, so it doesn't apply
    origin: str  # see params.Rule
    kind: str  # what detection took find_value to be; "" for parameter.yml entries
    items: tuple[str, ...]  # the published items whose files contain find_value


@dataclass(frozen=True)
class Plan:
    """What a deploy would do."""

    target: Target
    items: tuple[PlannedItem, ...]
    cleanup: str | None  # None: orphan cleanup runs; else the option that turns it off
    parameters: tuple[PlannedRule, ...]
    guards: dict[str, Verdict]  # by name, in the order the deploy checks them
    warnings: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        """Whether a guard would stop the deploy."""
        return any(verdict.status == "block" for verdict in self.guards.values())


def plan_deploy(
    repo_dir: Path | str,
    *,
    branch_override: str | None = None,
    new_workspace: bool = False,
    skip_lakehouse: bool = False,
    environment: str = "",
    no_cleanup: bool = False,
    item_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    include_items: list[str] | None = None,
    pr: str | None = None,
    fail_on_stale: bool = False,
    allow_rls_change: bool = False,
    allow_unbound_udf: bool = False,
) -> Plan:
    """What `fabdep deploy` with these options would do; see `WorkspaceDeployer`
    (an empty `environment` is fabdep.yml's for the branch).

    Read calls only. The warnings the deploy would log before publishing go
    into the plan instead of the console.

    Raises:
        Whatever the deploy would fail with before publishing (no such
        workspace, an `include_items` that names no item, ...).
    """
    with _collected_warnings() as warnings:
        deployer = WorkspaceDeployer(
            repo_dir,
            logger_name=_LOGGER,
            branch_override=branch_override,
            new_workspace=new_workspace,
            skip_lakehouse=skip_lakehouse,
            environment=environment,
            dry_run=True,
        )
        scope = deployer.resolve_scope(
            no_cleanup=no_cleanup,
            item_types=item_types,
            exclude_types=exclude_types,
            include_items=include_items,
            pr=pr,
        )
        # With nothing to deploy, the deploy stops before its guards, the
        # parameter file and creating the workspace.
        nothing = scope.nothing_to_deploy
        guards = (
            {name: Verdict("pass") for name in _GUARD_LABELS}
            if nothing
            else dict(
                deployer.guard_verdicts(scope, fail_on_stale, allow_rls_change, allow_unbound_udf)
            )
        )
        in_workspace = list_workspace_items(deployer.workspace_id) if deployer.workspace_id else []
        items = _items(scope, repo_items(deployer.config.repo_dir), in_workspace, deployer.logger)
        rules = (
            []
            if nothing
            else _planned_rules(deployer.parameter_rules(), deployer.environment, scope.items)
        )

    cleanup = None  # the option that turns orphan cleanup off, if one does
    if not scope.cleanup:
        cleanup = "--no-cleanup" if no_cleanup else "--pr" if pr else "--include-items"
    config = deployer.config
    create = deployer.creates_workspace and not nothing
    return Plan(
        target=Target(
            branch=config.branch,
            workspace=deployer.workspace_name,
            workspace_id=deployer.workspace_id or None,
            create=create,
            capacity_id=(deployer.capacity_id or None) if create else None,
            environment=deployer.environment,
            credential=credential_in_use(),
            tenant=config.auth.tenant if config.auth else None,
        ),
        items=tuple(items),
        cleanup=cleanup,
        parameters=tuple(rules),
        guards=guards,
        warnings=tuple(record.getMessage() for record in warnings),
    )


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pyright: ignore[reportImplicitOverride]
        self.records.append(record)


@contextmanager
def _collected_warnings() -> Generator[list[logging.LogRecord]]:
    """Hold back what the plan's deployer logs, keeping its warnings for the plan.

    Should planning fail, the warnings so far are printed after all: they may
    say why.
    """
    logger = get_logger(_LOGGER)
    collector = _Collector()
    logger.addHandler(collector)
    logger.propagate = False
    try:
        yield collector.records
    except BaseException:
        for record in collector.records:
            logging.getLogger(_LOGGER.split(".")[0]).handle(record)
        raise
    finally:
        logger.removeHandler(collector)
        logger.propagate = True


def _items(
    scope: DeployScope,
    repo: list[RepoItem],
    in_workspace: list[dict[str, Any]],
    logger: logging.Logger,
) -> list[PlannedItem]:
    """The items the deploy would create, update and unpublish, in fabric-cicd's order.

    As fabric-cicd decides: a repo item updates the workspace item of the same
    type and display name (exact match), else it is created. Orphan cleanup
    unpublishes the workspace's items of the in-scope types that the repo
    lacks, except the types fabric-cicd unpublishes only with a feature flag
    (Lakehouse, Warehouse, SQLDatabase, ...): those are warned about.
    """
    present = {
        (str(i["type"]), str(i["displayName"]))
        for i in in_workspace
        if i.get("type") and i.get("displayName")
    }
    order = {item_type: n for n, item_type in enumerate(_PUBLISH_ORDER)}
    planned = [
        PlannedItem(
            item.name, item.type, "update" if (item.type, item.name) in present else "create"
        )
        for item in sorted(scope.items, key=lambda i: (order.get(i.type, len(order)), i.name))
    ]
    if not scope.cleanup:
        return planned

    in_repo = {(item.type, item.name) for item in repo}
    for item_type in reversed(_PUBLISH_ORDER):
        if scope.item_types is not None and item_type not in scope.item_types:
            continue
        orphans = sorted(n for t, n in present if t == item_type and (t, n) not in in_repo)
        flag = constants.UNPUBLISH_FLAG_MAPPING.get(item_type)
        if orphans and flag and flag not in cast(set[str], constants.FEATURE_FLAG):
            logger.warning(
                f"Orphan cleanup leaves {item_type} {', '.join(map(repr, orphans))}: "
                f"fabric-cicd unpublishes {item_type} items only with its '{flag}' feature flag."
            )
            continue
        planned += [PlannedItem(name, item_type, "unpublish") for name in orphans]
    return planned


def _planned_rules(rules: list[Rule], environment: str, items: list[RepoItem]) -> list[PlannedRule]:
    """Each rule's replacement for `environment` and the published items it matches.

    Matched like fabric-cicd's find_replace (literal, or `is_regex` /
    `ignore_case`), within the rule's item_type / item_name filters; a
    file_path filter is not applied, nor a `$`-variable find_value resolved.
    """
    matchers = [_matcher(rule.entry) for rule in rules]
    found: list[set[str]] = [set() for _ in rules]
    for item in items:
        for path in sorted(p for p in item.path.rglob("*") if p.is_file()):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for rule, matches, hits in zip(rules, matchers, found, strict=True):
                if not matches or item.key in hits or not _applies_to(rule.entry, item):
                    continue
                if matches(text):
                    hits.add(item.key)

    planned: list[PlannedRule] = []
    for rule, hits in zip(rules, found, strict=True):
        replace = rule.entry.get("replace_value")
        value = (
            for_environment(cast(str | dict[str, str], replace), environment)
            if isinstance(replace, str | dict)
            else None
        )
        planned.append(
            PlannedRule(
                find_value=str(rule.entry.get("find_value")),
                replace_value=None if value is None else str(value),
                origin=rule.origin,
                kind=rule.kind,
                items=tuple(sorted(hits)),
            )
        )
    return planned


def _matcher(entry: dict[str, Any]) -> Callable[[str], bool] | None:
    find = entry.get("find_value")
    if not isinstance(find, str) or not find or find.startswith("$"):
        return None
    ignore_case = str(entry.get("ignore_case", "")).lower() == "true"
    if str(entry.get("is_regex", "")).lower() == "true":
        try:
            pattern = re.compile(find, re.IGNORECASE if ignore_case else 0)
        except re.error:
            return None  # fabric-cicd rejects it at deploy
        return lambda text: pattern.search(text) is not None
    if ignore_case:
        return lambda text: find.lower() in text.lower()
    return lambda text: find in text


def _applies_to(entry: dict[str, Any], item: RepoItem) -> bool:
    for key, value in (("item_type", item.type), ("item_name", item.name)):
        wanted = entry.get(key)
        wanted = [wanted] if isinstance(wanted, str) else wanted
        if isinstance(wanted, list) and wanted and value not in wanted:
            return False
    return True


# --- output ----------------------------------------------------------------

_COLOURS = {
    "create": GREEN,
    "update": YELLOW,
    "unpublish": RED,
    "pass": GREEN,
    "warn": YELLOW,
    "block": RED,
}
_GUARD_LABELS = {"fail_on_stale": "fail-on-stale", "unbound_udf": "unbound UDF", "rls": "RLS"}


def plan_json(plan: Plan) -> dict[str, Any]:
    """The plan as one JSON-serializable document, shaped as `JSON_SHAPE` says."""
    return {
        "target": asdict(plan.target),
        "items": [asdict(item) for item in plan.items],
        "cleanup": {"runs": plan.cleanup is None, "reason": plan.cleanup},
        "parameters": [{**asdict(rule), "items": list(rule.items)} for rule in plan.parameters],
        "guards": {name: _verdict_json(name, verdict) for name, verdict in plan.guards.items()},
        "warnings": list(plan.warnings),
        "blocked": plan.blocked,
    }


def _verdict_json(name: str, verdict: Verdict) -> dict[str, Any]:
    document: dict[str, Any] = {
        "verdict": verdict.status,
        "message": verdict.message,
        "findings": list(verdict.findings),
    }
    if name == "rls":
        document["models"] = [
            {"model": check.model, "status": check.status, **_changes(check)}
            for check in verdict.models
        ]
    return document


def _changes(check: RlsCheck) -> dict[str, list[str]]:
    """What the deploy would do to a model's roles: repo (local) vs target (remote)."""
    diff = check.diff
    return {
        "adds": diff.only_local if diff else [],
        "removes": diff.only_remote if diff else [],
        "changes": diff.modified if diff else [],
        "recreates": diff.pbi_id_changed if diff else [],
    }


def format_plan(plan: Plan, color: bool = False) -> str:
    """The plan for people: aligned sections, coloured only with `color`."""

    def paint(word: str, width: int = 0) -> str:
        code = _COLOURS.get(word) if color else None
        return f"{code}{word}{RESET}{' ' * (width - len(word))}" if code else word.ljust(width)

    target = plan.target
    if target.workspace_id:
        where = f"{target.workspace} ({target.workspace_id})"
    elif target.create:
        where = f"{target.workspace} (would be created on capacity {target.capacity_id})"
    else:
        where = f"{target.workspace} (does not exist; nothing to deploy, so not created)"
    rows = [
        ("Workspace", where),
        ("Branch", target.branch),
        ("Environment", target.environment),
        ("Credential", target.credential),
        ("Tenant", target.tenant or "not pinned"),
    ]
    lines = [f"{label:<13}{value}" for label, value in rows]

    counts = Counter(item.action for item in plan.items)
    summary = [f"{counts[a]} to {a}" for a in ("create", "update", "unpublish") if counts[a]]
    lines += ["", f"Items: {', '.join(summary) or 'nothing to publish'}"]
    width = max((len(item.type) for item in plan.items), default=0) + 2
    lines += [f"  {paint(i.action, 11)}{i.type:<{width}}{i.name}" for i in plan.items]
    if plan.cleanup:
        lines.append(f"  (orphan cleanup off: {plan.cleanup})")

    applied = sum(rule.replace_value is not None for rule in plan.parameters)
    lines += ["", f"Parameters for environment {target.environment}: {applied} rule(s) apply"]
    for rule in plan.parameters:
        replacement = rule.replace_value
        if replacement is None:
            replacement = f"(no value for {target.environment}: not applied)"
        source = f"{rule.origin}: {rule.kind}" if rule.kind else rule.origin
        found = f"in {', '.join(rule.items)}" if rule.items else "in no published item"
        lines += [f"  {rule.find_value} -> {replacement}", f"      {source}; {found}"]

    if plan.warnings:
        lines += ["", "Warnings"]
        for warning in plan.warnings:
            first, *rest = warning.splitlines() or [""]
            lines += [f"  - {first}", *(f"    {line}" for line in rest)]

    lines += ["", "Guards"]
    for name, verdict in plan.guards.items():
        lines.append(f"  {_GUARD_LABELS[name]:<15}{paint(verdict.status)}")
        lines += [f"      {_describe(check)}" for check in verdict.models]
        if verdict.status != "pass":
            lines += [f"      {line}" for line in verdict.message.splitlines()]

    blocked = [_GUARD_LABELS[name] for name, v in plan.guards.items() if v.status == "block"]
    lines.append("")
    if blocked:
        lines.append(
            f"{paint('Blocked')}: the {' and '.join(blocked)} guard would stop the deploy."
        )
    else:
        lines.append("The deploy would proceed.")
    return "\n".join(lines)


def _describe(check: RlsCheck) -> str:
    if check.status == "new":
        return f"{check.model}: new (not in the target)"
    if check.status == "skipped":
        return f"{check.model}: skipped (--allow-rls-change)"
    changes = "; ".join(
        f"{verb} {', '.join(roles)}" for verb, roles in _changes(check).items() if roles
    )
    return f"{check.model}: {check.status}" + (f" ({changes})" if changes else "")
