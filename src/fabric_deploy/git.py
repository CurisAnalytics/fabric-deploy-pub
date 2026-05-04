"""Git utilities used by the deployer."""

from __future__ import annotations

import subprocess
from pathlib import Path


__all__ = ["current_branch"]


def current_branch(repo_dir: Path | str) -> str:
    """Return the current git branch name for a working directory.

    Args:
        repo_dir: Path to a directory inside a git repository.

    Returns:
        Branch name (e.g. "main", "feature/xyz").

    Raises:
        RuntimeError: If git is unavailable, the directory is not a repo,
            or HEAD is detached.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else "Unknown error"
        raise RuntimeError(
            f"Failed to get git branch.\n{error_msg}\n\n"
            "Make sure you're in a git repository and git is installed."
        ) from e

    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("Not on a git branch (detached HEAD state)")
    return branch
