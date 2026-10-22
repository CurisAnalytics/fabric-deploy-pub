"""The workspace repo's items and the deploy scope.

Pure functions over a workspace repo directory, no deployer state: the
`.platform` index every module reads items from, `-i` validation, item-type
scoping and the `--pr` path-to-item mapping.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import get_close_matches
from json import loads
from pathlib import Path

from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

__all__ = [
    "RepoItem",
    "find_in_item_files",
    "items_for_changed_paths",
    "repo_item_types",
    "repo_items",
    "resolve_item_scope",
    "validate_include_items",
]


@dataclass(frozen=True)
class RepoItem:
    """One item of a workspace repo, as its `.platform` file declares it.

    fabric-cicd discovers items the same way: a directory holding a `.platform`
    is an item, its type and name are the file's `metadata.type` and
    `metadata.displayName`, whatever the directory is called.
    """

    path: Path  # the item directory
    type: str
    name: str  # displayName
    logical_id: str | None  # config.logicalId

    @property
    def key(self) -> str:
        """'<displayName>.<type>', the form fabric-cicd's items_to_include takes."""
        return f"{self.name}.{self.type}"


def repo_items(
    repo_dir: Path, item_type: str | None = None, logger: logging.Logger | None = None
) -> list[RepoItem]:
    """The repo's items of a type fabric-cicd deploys, shallowest directory first.

    Args:
        repo_dir: Workspace repo directory.
        item_type: Only items of this type.
        logger: Warned about each `.platform` that can't be read; without one
            those are skipped silently (fabric-cicd rejects them at deploy).
    """
    items: list[RepoItem] = []
    for platform_file in sorted(repo_dir.rglob(".platform"), key=lambda p: (len(p.parts), str(p))):
        try:
            data = loads(platform_file.read_text(encoding="utf-8-sig"))
            meta, config = data["metadata"], data.get("config") or {}
            kind, name = meta["type"], meta["displayName"]
            if not (isinstance(kind, str) and isinstance(name, str) and name):
                raise ValueError("metadata.type and metadata.displayName must be strings")
        except (OSError, ValueError, KeyError, TypeError) as e:
            if logger:
                logger.warning(f"Failed to read item metadata from {platform_file}: {e}")
            continue
        if kind not in ACCEPTED_ITEM_TYPES or (item_type and kind != item_type):
            continue
        logical_id = config.get("logicalId") if isinstance(config, dict) else None
        items.append(
            RepoItem(
                path=platform_file.parent,
                type=kind,
                name=name,
                logical_id=logical_id if isinstance(logical_id, str) else None,
            )
        )
    return items


def find_in_item_files(repo_dir: Path, values: Iterable[str]) -> dict[str, set[str]]:
    """Repo-relative item files that contain each literal in `values`.

    Item files are the files inside item directories, the set fabric-cicd's
    find_replace operates on. Repo-root files such as fabdep.yml are left out
    so a rule can't match itself. One walk covers every value.
    """
    matches: dict[str, set[str]] = {v: set() for v in values}
    if not matches:
        return matches
    files = {f for item in repo_items(repo_dir) for f in item.path.rglob("*") if f.is_file()}
    for path in sorted(files):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(repo_dir))
        for value, found_in in matches.items():
            if value in content:
                found_in.add(rel)
    return matches


def repo_item_types(repo_dir: Path) -> set[str]:
    """The item types the repo has."""
    return {item.type for item in repo_items(repo_dir)}


def items_for_changed_paths(
    changed_paths: list[str], repo_dir: Path
) -> tuple[list[str], list[str]]:
    """Map changed file paths onto the items that own them.

    Each item is a directory containing a `.platform`; a changed file belongs
    to the nearest such ancestor. Files outside any item (fabdep.yml, README,
    .gitignore) and files whose item directory no longer exists (the PR
    deleted it) match nothing.

    Args:
        changed_paths: Repo-relative POSIX paths from a git diff.
        repo_dir: Workspace repo directory.

    Returns:
        (item ids as '<displayName>.<ItemType>' in stable order, unmatched paths)
    """
    index = {item.path.relative_to(repo_dir).as_posix(): item.key for item in repo_items(repo_dir)}

    matched: list[str] = []
    unmatched: list[str] = []
    for path in changed_paths:
        posix = Path(path).as_posix()
        # The deepest item directory owns the file.
        owner = max(
            (d for d in index if posix == d or posix.startswith(f"{d}/")),
            key=len,
            default=None,
        )
        if owner is None:
            unmatched.append(path)
        elif index[owner] not in matched:
            matched.append(index[owner])

    return matched, unmatched


def resolve_item_scope(
    item_types: list[str] | None,
    exclude_types: list[str] | None,
    include_items: list[str] | None,
) -> list[str] | None:
    """Normalize, validate, and combine item-type filters into a single scope.

    Returns the list of item types that fabric-cicd should deploy, or None if
    no scope filtering is requested.

    Combines (in priority order):
    1. Explicit `item_types` (after canonical-case normalization).
    2. `exclude_types` → all ACCEPTED_ITEM_TYPES minus excludes.
    3. `include_items` (e.g. "Foo.Notebook") → derive scope from suffixes
       if no other scope was provided.
    """
    canonical = {t.lower(): t for t in ACCEPTED_ITEM_TYPES}

    def _normalize(types: list[str] | None) -> list[str] | None:
        if not types:
            return types
        return [canonical.get(t.lower(), t) for t in types]

    item_types = _normalize(item_types)
    exclude_types = _normalize(exclude_types)
    for item_type in [*(item_types or []), *(exclude_types or [])]:
        if item_type not in ACCEPTED_ITEM_TYPES:
            hint = get_close_matches(item_type, ACCEPTED_ITEM_TYPES, n=1)
            raise ValueError(
                f"Unknown item type '{item_type}'"
                + (f" (did you mean '{hint[0]}'?)" if hint else "")
                + f". fabric-cicd deploys: {', '.join(ACCEPTED_ITEM_TYPES)}"
            )

    if item_types:
        return item_types
    if exclude_types:
        return [t for t in ACCEPTED_ITEM_TYPES if t not in exclude_types]

    # Derive scope from include_items (Name.Type format)
    if include_items:
        derived = {
            item.rsplit(".", 1)[1]
            for item in include_items
            if "." in item and item.rsplit(".", 1)[1] in ACCEPTED_ITEM_TYPES
        }
        if derived:
            return sorted(derived)

    return None


def validate_include_items(
    include_items: list[str], repo_dir: Path, logger: logging.Logger
) -> list[str]:
    """Resolve `--include-items` against the repo's display names.

    fabric-cicd silently skips any `items_to_include` entry whose display name
    isn't in the repo — so a single typo (or matching the directory name
    instead of the displayName) means nothing publishes and orphan cleanup is
    disabled, with no error. This makes that loud:

    - Unmatched entries are warned about, with the available items listed.
    - If NOTHING matches, raise instead of running a no-op deploy.

    Returns the matched subset (what should actually be published).
    """
    index = {item.key for item in repo_items(repo_dir)}
    matched = [it for it in include_items if it in index]
    missing = [it for it in include_items if it not in index]

    if missing:
        logger.warning(
            f"{len(missing)} requested item(s) not found by display name "
            f"(fabric-cicd matches displayName, not directory name): "
            f"{', '.join(missing)}"
        )
        logger.warning(f"Available items: {', '.join(sorted(index)) or '(none)'}")

    if not matched:
        raise RuntimeError(
            "--include-items matched no items by display name, so nothing "
            f"would deploy. Requested: {', '.join(include_items)}. "
            "Names are '<displayName>.<ItemType>' — check the displayName in "
            "each item's .platform, not the folder name."
        )
    return matched
