"""Row-level security (RLS) round-trip: read the target's role definitions, diff, guard.

RLS on a Fabric semantic model is two separate things in two stores:

* **Role definitions** (role names + DAX `tablePermission` filters) live *in*
  the model definition (`definition/roles/*.tmdl`, wired via `ref role` in
  `model.tmdl`). A from-git deploy **overwrites** these, so a role edited
  directly in the target is reverted by the next deploy.
* **Role memberships** (which users/groups are assigned) are Service state.
  They are NOT in the deploy payload and `getDefinition` cannot read them, so
  the repo can neither compare nor restore them. A deploy keeps a role's
  members only while the role keeps its name and `annotation PBI_Id`; the
  service recreates a role deployed under a different PBI_Id without them.

This module compares definitions, including the PBI_Id changes that cost a
role its members. It powers two entry points:

* ``fabdep rls pull`` — read the target model's live roles, diff against the
  repo, and (with ``--write``) pull them into the working tree for review.
* the deploy guard — before ``fabdep deploy`` publishes semantic models,
  compare each in-scope model's repo roles to the target's live roles and
  **block** the deploy on any difference. A model without ``definition/roles/``
  is checked too: deploying it removes every role the target has.
  ``--allow-rls-change`` skips the comparison entirely, since it declares the
  overwrite intentional before the check would run.
"""

from __future__ import annotations

import difflib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .client import (
    find_item_by_name,
    get_semantic_model_definition,
    get_workspace_id_by_name,
)
from .config import FabdepConfig
from .identity import apply_tenant_pin
from .log import get_logger
from .scope import _platform_meta

__all__ = [
    "MEMBERS_NOTE",
    "Role",
    "RlsDiff",
    "diff_roles",
    "read_local_roles",
    "roles_from_parts",
    "guard_deploy_rls",
    "pull_rls",
]

MEMBERS_NOTE = (
    "Note: role memberships (who is assigned to each role) are not part of the "
    "model definition, so they are not compared here and the repo cannot restore "
    "them. A deploy keeps a role's members only while the role keeps its name and "
    "annotation PBI_Id; a role that is removed, renamed or given a new PBI_Id "
    "loses them."
)

_ROLE_DECL = re.compile(r"^\s*role\s+(.+?)\s*$")
_REF_ROLE = re.compile(r"^(\s*)ref\s+role\s+(.+?)\s*$")
# A TMDL name is bare when it is a simple identifier; otherwise it is quoted.
_BARE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# PBI_Id identifies a role to the service; a role created separately in two
# environments gets a different one in each. Deploying a role under a different
# PBI_Id makes the service drop and recreate it, wiping its members, so it is
# compared on its own (Role.pbi_id) rather than as part of the rules.
_PBI_ID_ANNOTATION = re.compile(r"^\s*annotation\s+PBI_Id\s*=\s*(.*?)\s*$")


def _normalize_tmdl(text: str) -> str:
    """Normalize a role's rules for comparison: LF endings, no trailing/edge blank lines.

    Deliberately conservative — only cosmetic serialization noise (line
    endings, trailing whitespace, surrounding blank lines) is folded away.
    Indentation and DAX bodies are left intact so a real edit is never masked
    as "unchanged" (a false match would let a deploy silently overwrite the
    target). `annotation PBI_Id` is left out here and compared separately.
    """
    lines = [
        ln.rstrip()
        for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if not _PBI_ID_ANNOTATION.match(ln)
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _unquote_name(raw: str) -> str:
    """Strip TMDL single-quote quoting from a role name."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _quote_name(name: str) -> str:
    """Quote a role name for a TMDL `ref role` line if it isn't a bare id."""
    if _BARE_NAME.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _role_name_from_tmdl(text: str, fallback: str) -> str:
    """Parse the `role <name>` declaration; fall back to the file stem."""
    for line in text.splitlines():
        m = _ROLE_DECL.match(line)
        if m:
            return _unquote_name(m.group(1))
    return fallback


@dataclass(frozen=True)
class Role:
    """One RLS role definition, as stored in the model definition."""

    name: str
    filename: str  # basename within definition/roles/, e.g. "RLS.tmdl"
    raw: str  # original TMDL text (written verbatim on pull --write)

    @property
    def body(self) -> str:
        """Normalized rules text (without `annotation PBI_Id`) for comparison."""
        return _normalize_tmdl(self.raw)

    @property
    def pbi_id(self) -> str | None:
        """The role's `annotation PBI_Id` value, or None when it has none."""
        for line in self.raw.splitlines():
            if m := _PBI_ID_ANNOTATION.match(line):
                return m.group(1)
        return None


@dataclass
class RlsDiff:
    """Neutral difference between two role sets: `local` (repo) vs `remote` (target).

    Callers phrase the direction — the deploy guard reads it as "what the deploy
    would do to the target"; `rls pull` reads it as "what pulling would do to the
    working tree".
    """

    only_local: list[str]  # role names present only in the repo
    only_remote: list[str]  # role names present only in the target
    modified: list[str]  # names present in both, rules differ
    pbi_id_changed: list[str]  # names present in both, annotation PBI_Id differs
    unchanged: list[str]  # names present in both, rules and PBI_Id identical
    local: dict[str, Role]
    remote: dict[str, Role]

    @property
    def changed(self) -> bool:
        return bool(self.only_local or self.only_remote or self.modified or self.pbi_id_changed)


def roles_from_parts(parts: dict[str, str]) -> dict[str, Role]:
    """Extract RLS roles from a `getDefinition` part map (`{path: text}`)."""
    roles: dict[str, Role] = {}
    for path, text in parts.items():
        norm = path.replace("\\", "/")
        if "/roles/" not in norm or not norm.endswith(".tmdl"):
            continue
        filename = norm.rsplit("/", 1)[-1]
        name = _role_name_from_tmdl(text, filename[: -len(".tmdl")])
        roles[name] = Role(name=name, filename=filename, raw=text)
    return roles


def read_local_roles(model_dir: Path) -> dict[str, Role]:
    """Read RLS roles from a `.SemanticModel` repo directory."""
    roles_dir = model_dir / "definition" / "roles"
    if not roles_dir.is_dir():
        return {}
    roles: dict[str, Role] = {}
    for f in sorted(roles_dir.glob("*.tmdl")):
        text = f.read_text(encoding="utf-8-sig")
        name = _role_name_from_tmdl(text, f.stem)
        roles[name] = Role(name=name, filename=f.name, raw=text)
    return roles


def diff_roles(local: dict[str, Role], remote: dict[str, Role]) -> RlsDiff:
    """Compare two role sets by normalized rules text and by PBI_Id."""
    lk, rk = set(local), set(remote)
    both = lk & rk
    modified = {n for n in both if local[n].body != remote[n].body}
    pbi_id_changed = {n for n in both if local[n].pbi_id != remote[n].pbi_id}
    return RlsDiff(
        only_local=sorted(lk - rk),
        only_remote=sorted(rk - lk),
        modified=sorted(modified),
        pbi_id_changed=sorted(pbi_id_changed),
        unchanged=sorted(both - modified - pbi_id_changed),
        local=local,
        remote=remote,
    )


def _render_diff(
    diff: RlsDiff, add_label: str, remove_label: str, change_label: str, pbi_id_label: str
) -> str:
    """Render a role diff as a human-readable block with unified DAX diffs."""
    out: list[str] = []
    for name in diff.only_local:
        out.append(f"  [{add_label}] role '{name}'")
    for name in diff.only_remote:
        out.append(f"  [{remove_label}] role '{name}'")
    for name in diff.modified:
        out.append(f"  [{change_label}] role '{name}':")
        udiff = difflib.unified_diff(
            diff.remote[name].body.splitlines(),
            diff.local[name].body.splitlines(),
            fromfile=f"{name} (target)",
            tofile=f"{name} (repo)",
            lineterm="",
        )
        out.extend("      " + ln for ln in udiff)
    for name in diff.pbi_id_changed:
        out.append(
            f"  [{pbi_id_label}] role '{name}': annotation PBI_Id differs "
            f"(target {diff.remote[name].pbi_id or 'none'}, "
            f"repo {diff.local[name].pbi_id or 'none'})"
        )
    return "\n".join(out)


# --- shared collection ---------------------------------------------------


def _semantic_model_dirs(repo_dir: Path) -> dict[str, Path]:
    """Map SemanticModel display name -> its repo directory, by `.platform` type."""
    dirs: dict[str, Path] = {}
    for platform_file in repo_dir.rglob(".platform"):
        meta = _platform_meta(platform_file)
        if meta and meta[1] == "SemanticModel":
            dirs[meta[0]] = platform_file.parent
    return dirs


def _remote_roles(workspace_id: str, model_name: str) -> dict[str, Role] | None:
    """Fetch the target model's live RLS role definitions.

    Returns None when the model does not exist in the target yet (a first-time
    deploy) — there is no live RLS to protect or pull in that case.
    """
    try:
        item = find_item_by_name(workspace_id, "SemanticModel", model_name)
    except ValueError:
        return None  # model not present in the target workspace
    parts = get_semantic_model_definition(workspace_id, item["id"])
    return roles_from_parts(parts)


# --- deploy guard --------------------------------------------------------


def guard_deploy_rls(
    workspace_id: str,
    repo_dir: Path | str,
    model_names: list[str] | None,
    allow_change: bool,
    logger: logging.Logger | None = None,
) -> None:
    """Block a deploy that would change a target model's live RLS definitions.

    Every in-scope model's live roles are fetched and compared with the repo's;
    any difference (added, removed or modified role, or a changed PBI_Id, which
    wipes the role's members) blocks the deploy. A model with no
    `definition/roles/` folder counts as "no roles", so a target that has roles
    for it blocks too.

    `allow_change` skips the comparison outright: the operator has already
    decided to overwrite the target's roles, so the live read (slow — each
    model's definition is a full getDefinition round trip) can only produce a
    warning about a decision already made. Models are read in parallel.

    Assumes auth / tenant pin is already applied (the deployer does this).

    Args:
        workspace_id: Target workspace GUID (whatever this deploy targets).
        repo_dir: Workspace repo directory.
        model_names: SemanticModel display names in deploy scope; None = every
            model in the repo. Pass an empty list to skip the guard entirely.
        allow_change: When True, skip the check — the deploy is permitted to
            overwrite the target's live role definitions.
        logger: Logger to use.

    Raises:
        RuntimeError: If a deploy would change RLS and `allow_change` is False.
    """
    log = logger or get_logger(__name__)
    repo_dir = Path(repo_dir).resolve()

    dirs = _semantic_model_dirs(repo_dir)
    if model_names is not None:
        wanted = {m.removesuffix(".SemanticModel") for m in model_names}
        dirs = {n: d for n, d in dirs.items() if n in wanted}
    if not dirs:
        return

    guarded = {name: read_local_roles(model_dir) for name, model_dir in sorted(dirs.items())}

    # --allow-rls-change is the operator saying "overwrite the target's roles".
    # Reading them back first can't change that outcome, so don't pay for it.
    if allow_change:
        log.warning(
            f"RLS check skipped for {len(guarded)} model(s) (--allow-rls-change): "
            "the deploy replaces the target's live role definitions with the "
            f"repo's, removing any role the repo lacks. {MEMBERS_NOTE}"
        )
        return

    log.info(
        f"RLS guard: reading live role definitions for {len(guarded)} model(s) from the target..."
    )

    # Each live read is a full getDefinition round trip; they are independent,
    # so fetch them concurrently and report in name order.
    with ThreadPoolExecutor(max_workers=min(8, len(guarded))) as pool:
        remotes = dict(
            zip(
                guarded,
                pool.map(lambda name: _remote_roles(workspace_id, name), guarded),
                strict=True,
            )
        )

    blocked: list[str] = []
    recreates = False
    for name, local in guarded.items():
        remote = remotes[name]
        if remote is None:
            log.info(
                f"RLS check: '{name}' is not in the target yet — first deploy, "
                "no live RLS to protect."
            )
            continue

        diff = diff_roles(local, remote)
        if not diff.changed:
            log.info(f"RLS check: '{name}' — repo matches target ({len(local)} role(s)).")
            continue

        rendering = _render_diff(
            diff,
            add_label="deploy ADDS",
            remove_label="deploy WIPES",
            change_label="deploy CHANGES",
            pbi_id_label="deploy RECREATES",
        )
        log.error(f"RLS drift for '{name}' — deploy would change live RLS:\n{rendering}")
        blocked.append(name)
        recreates = recreates or bool(diff.pbi_id_changed)

    if blocked:
        raise RuntimeError(
            "Deploy blocked: it would change row-level security on "
            f"{len(blocked)} model(s): {', '.join(blocked)}.\n"
            "The target's live RLS differs from the repo (someone may have "
            "edited it directly). Reconcile with `fabdep rls pull` (--write to "
            "adopt the target's roles), or re-run deploy with --allow-rls-change "
            "to intentionally overwrite it.\n"
            + (
                "A role marked RECREATES has a different annotation PBI_Id in the "
                "repo: the deploy makes the service drop and recreate it, which "
                "wipes its member assignments. To keep them, give the repo's role "
                "the target's PBI_Id (`fabdep rls pull --write` adopts the target's "
                "role files).\n"
                if recreates
                else ""
            )
            + MEMBERS_NOTE
        )


# --- rls pull ------------------------------------------------------------


def _reconcile_ref_roles(model_tmdl: str, desired: set[str]) -> str:
    """Rewrite `ref role` lines in model.tmdl to exactly `desired`.

    Drops `ref role` lines for roles no longer present and appends any missing
    ones next to the existing block (or at end of file if there is none),
    preserving the indentation style already in use.
    """
    lines = model_tmdl.replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    ref_indent = ""
    last_ref_idx = -1
    present: set[str] = set()
    for line in lines:
        m = _REF_ROLE.match(line)
        if m:
            ref_indent = m.group(1)
            name = _unquote_name(m.group(2))
            if name not in desired:
                continue  # drop a stale ref role
            present.add(name)
            last_ref_idx = len(kept)
        kept.append(line)

    missing = sorted(desired - present)
    if not missing:
        return "\n".join(kept)

    new_lines = [f"{ref_indent}ref role {_quote_name(n)}" for n in missing]
    if last_ref_idx >= 0:
        kept[last_ref_idx + 1 : last_ref_idx + 1] = new_lines
    else:
        if kept and kept[-1] != "":
            kept.append("")
        kept.extend(new_lines)
    return "\n".join(kept)


def _apply_pull(model_dir: Path, diff: RlsDiff, log: logging.Logger) -> None:
    """Overwrite the working tree so its RLS matches the target (remote)."""
    roles_dir = model_dir / "definition" / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)

    for name in diff.only_local:  # target no longer has it -> delete locally
        f = roles_dir / diff.local[name].filename
        if f.exists():
            f.unlink()
            log.info(f"  deleted {f.relative_to(model_dir)}")

    # Write the target's version of every role that differs from it.
    for name in sorted({*diff.only_remote, *diff.modified, *diff.pbi_id_changed}):
        role = diff.remote[name]
        f = roles_dir / role.filename
        f.write_text(role.raw if role.raw.endswith("\n") else role.raw + "\n", encoding="utf-8")
        log.info(f"  wrote {f.relative_to(model_dir)}")

    model_tmdl = model_dir / "definition" / "model.tmdl"
    if model_tmdl.exists():
        rewritten = _reconcile_ref_roles(model_tmdl.read_text(encoding="utf-8"), set(diff.remote))
        model_tmdl.write_text(rewritten, encoding="utf-8")


def pull_rls(
    repo_dir: Path | str,
    branch_override: str | None = None,
    models: list[str] | None = None,
    write: bool = False,
    logger: logging.Logger | None = None,
) -> bool:
    """Read the target's RLS role definitions, diff against the repo, optionally pull.

    Args:
        repo_dir: Workspace repo directory (for fabdep.yml + model discovery).
        branch_override: Override the git branch for workspace resolution.
        models: SemanticModel display names to check; default = all in the repo.
        write: Overwrite the working tree's roles to match the target.
        logger: Logger to use.

    Returns:
        True if any model's RLS differed from the target (before any --write).

    Raises:
        ValueError: If no semantic model can be resolved.
    """
    log = logger or get_logger(__name__)
    repo_dir = Path(repo_dir).resolve()

    config = FabdepConfig.from_path(repo_dir, branch_override=branch_override)
    apply_tenant_pin(config.tenant, log)
    workspace_id = get_workspace_id_by_name(config.workspace_name)
    log.info(f"Workspace: '{config.workspace_name}' ({workspace_id})")

    dirs = _semantic_model_dirs(repo_dir)
    if models:
        wanted = {m.removesuffix(".SemanticModel") for m in models}
        dirs = {n: d for n, d in dirs.items() if n in wanted}
    if not dirs:
        raise ValueError(
            "No semantic model to check. Pass -i '<displayName>' or run from a "
            "repo that contains a .SemanticModel item."
        )

    any_changed = False
    for name, model_dir in sorted(dirs.items()):
        local = read_local_roles(model_dir)
        remote = _remote_roles(workspace_id, name)
        if remote is None:
            log.info(f"'{name}': not present in the target workspace — skipping.")
            continue

        diff = diff_roles(local, remote)

        if not diff.changed:
            log.info(f"'{name}': RLS in sync ({len(remote)} role(s)).")
            continue

        any_changed = True
        rendering = _render_diff(
            diff,
            add_label="pull DELETES",
            remove_label="pull ADDS",
            change_label="pull UPDATES",
            pbi_id_label="pull UPDATES",
        )
        log.info(f"'{name}': RLS differs from target —\n{rendering}")
        if write:
            _apply_pull(model_dir, diff, log)
            log.info(f"'{name}': working tree updated to match target — review and commit.")

    log.info(MEMBERS_NOTE)
    if any_changed and not write:
        log.info("Re-run with --write to pull the target's roles into the working tree.")
    return any_changed
