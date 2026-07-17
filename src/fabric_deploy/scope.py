"""Repo item introspection and deploy-scope resolution.

Pure functions over a workspace repo directory — no deployer state — so `-i`
validation, item-type scoping, and `.platform` scanning live in one place
instead of bloating WorkspaceDeployer.
"""

from __future__ import annotations

import logging
from json import loads
from pathlib import Path

from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

__all__ = [
    "repo_item_types",
    "resolve_item_scope",
    "validate_include_items",
]


def _platform_meta(platform_file: Path) -> tuple[str, str] | None:
    """Return (displayName, type) from a .platform file, or None if unreadable."""
    try:
        meta = loads(platform_file.read_text())["metadata"]
        return meta["displayName"], meta["type"]
    except Exception:
        return None


def repo_item_types(repo_dir: Path) -> set[str]:
    """Return the set of accepted Fabric item types found in the repo.

    Keyed on the `.platform` metadata `type` — how fabric-cicd discovers
    items — NOT the directory suffix. The two can differ: Synapse-migrated
    notebooks sit in `*.SynapseNotebook/` dirs whose .platform says
    `type: Notebook`.
    """
    item_types: set[str] = set()
    for platform_file in repo_dir.rglob(".platform"):
        meta = _platform_meta(platform_file)
        if meta and meta[1] in ACCEPTED_ITEM_TYPES:
            item_types.add(meta[1])
    return item_types


def _repo_items(repo_dir: Path) -> dict[str, str]:
    """Map '<displayName>.<ItemType>' -> item directory name for the repo.

    Keyed on the `.platform` displayName and type — which is what
    fabric-cicd's `items_to_include` matches on, NOT the directory name (the
    two can differ, and that mismatch is exactly the `-i` silent no-op
    footgun; a `*.SynapseNotebook/` dir is addressed as `<name>.Notebook`).
    """
    items: dict[str, str] = {}
    for platform_file in repo_dir.rglob(".platform"):
        meta = _platform_meta(platform_file)
        if not meta or meta[1] not in ACCEPTED_ITEM_TYPES:
            continue
        display, item_type = meta
        items[f"{display}.{item_type}"] = platform_file.parent.name
    return items


def resolve_item_scope(
    item_types: list[str] | None,
    exclude_types: list[str] | None,
    include_items: list[str] | None,
    logger: logging.Logger,
) -> list[str] | None:
    """Normalize, validate, and combine item-type filters into a single scope.

    Returns the list of item types that fabric-cicd should deploy, or None if
    no scope filtering is requested. Logs warnings for unrecognized types.

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

    def _warn_unknown(types: list[str] | None, label: str) -> None:
        if not types:
            return
        invalid = [t for t in types if t not in ACCEPTED_ITEM_TYPES]
        if invalid:
            logger.warning(f"Unrecognized item types in {label}: {', '.join(invalid)}")
            logger.warning(f"Supported types: {', '.join(ACCEPTED_ITEM_TYPES)}")

    item_types = _normalize(item_types)
    exclude_types = _normalize(exclude_types)
    _warn_unknown(item_types, "filter")
    _warn_unknown(exclude_types, "exclusion filter")

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
    index = _repo_items(repo_dir)
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
            "would deploy (the silent no-op fabdep used to hide). "
            f"Requested: {', '.join(include_items)}. "
            "Names are '<displayName>.<ItemType>' — check the displayName in "
            "each item's .platform, not the folder name."
        )
    return matched
