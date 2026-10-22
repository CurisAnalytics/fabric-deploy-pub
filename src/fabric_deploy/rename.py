"""Rename Fabric workspace items based on their display names."""

import json
from pathlib import Path

from .log import get_logger

logger = get_logger(__name__)


def _format_path(path: Path) -> str:
    """Format path for display (show parent/name only)."""
    return f"{path.parent.name}/{path.name}"


def _rmtree(path: Path) -> None:
    """Remove directory tree using pathlib."""
    for child in path.iterdir():
        if child.is_dir():
            _rmtree(child)
        else:
            child.unlink()
    path.rmdir()


def rename_items_by_display_name(
    repo_dir: Path | str, dry_run: bool = True, clean: bool = False
) -> None:
    """
    Rename workspace item directories to match their display names.
    Searches for all .platform files in the workspace and renames parent directories
    to use lowercase, underscore-separated versions of the display names.
    Args:
        repo_dir: Path to workspace directory
        dry_run: If True, only shows what would be renamed without making changes
        clean: If True, applies .lower().replace(" ", "_") to directory names
    """
    src = Path(repo_dir).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Directory not found: {src}")

    targets = list(src.glob("**/*.*/.platform"))
    if not targets:
        logger.info(f"No items found in {src}")
        return
    logger.info(f"Found {len(targets)} item(s) to process")

    renamed_count, skipped_count = 0, 0
    for target in targets:
        try:
            platform_data = json.loads(target.read_text())
            display_name = platform_data["metadata"]["displayName"]
            new_stem = display_name.lower().replace(" ", "_") if clean else display_name
            parent = target.parent
            new_path = parent.with_stem(new_stem)

            if parent == new_path:
                logger.info(f"Skipping {_format_path(parent)} (already matches)")
                skipped_count += 1
                continue

            if dry_run:
                logger.info(f"{_format_path(parent)} -> {_format_path(new_path)}")
            else:
                if new_path.exists():
                    _rmtree(new_path)
                _ = parent.rename(new_path)
                logger.info(f"Renamed: {_format_path(parent)} -> {_format_path(new_path)}")
            renamed_count += 1
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Skipping {target}: Invalid .platform file ({e})")
            skipped_count += 1
        except Exception as e:
            logger.error(f"Error processing {target}: {e}")
            skipped_count += 1

    if dry_run:
        logger.info(
            f"\nDry run complete: {renamed_count} item(s) would be renamed, {skipped_count} skipped"
        )
        if renamed_count > 0:
            logger.info("Run with --execute flag to perform actual renames")
    else:
        logger.info(f"\nRename complete: {renamed_count} item(s) renamed, {skipped_count} skipped")
