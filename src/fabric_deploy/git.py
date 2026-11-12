"""Git utilities used by the deployer."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


__all__ = ["current_branch", "changed_paths_in", "commits_after", "latest_pr_commit"]

# How hosting services word the commit a pull request lands as. A squash merge
# leaves no merge commit, so the message is the only signal, and a merge commit
# without one of these is a local `git merge` or `git pull`, not a pull request.
_PR_SUBJECTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Merged PR \d+:"),  # Azure DevOps
    re.compile(r"^Merge pull request #\d+"),  # GitHub merge commit
    re.compile(r"\(#\d+\)$"),  # GitHub squash merge
    re.compile(r"\(pull request #\d+\)"),  # Bitbucket
)
_PR_BODY = re.compile(r"^See merge request \S+!\d+$", re.MULTILINE)  # GitLab

# How far back to look for the PR commit. A deploy follows a fetch of upstream
# changes, so the PR is near the tip; scanning further would risk picking up an
# unrelated older PR when the tip is plain local commits.
_PR_SEARCH_DEPTH = 50


def _git(repo_dir: Path | str, *args: str) -> str:
    """Run a git command in `repo_dir` and return stdout, or raise RuntimeError."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else "Unknown error"
        raise RuntimeError(f"git {' '.join(args)} failed.\n{error_msg}") from e
    return result.stdout


def latest_pr_commit(repo_dir: Path | str) -> tuple[str, str] | None:
    """Find the most recent pull-request commit on HEAD's first-parent history.

    Returns (sha, subject), or None if none of the last `_PR_SEARCH_DEPTH`
    commits carries a pull-request message (see `_PR_SUBJECTS`). Commits a
    merge brought in from another branch are not the deployed branch's own, so
    they are not searched, however they are worded.

    Args:
        repo_dir: Path to a directory inside a git repository.
    """
    out = _git(
        repo_dir,
        "log",
        f"-n{_PR_SEARCH_DEPTH}",
        "--first-parent",
        "-z",
        "--format=%H%x1f%s%x1f%b",
        "HEAD",
    )
    for record in out.split("\0"):
        sha, subject, body = (record.split("\x1f", 2) + ["", ""])[:3]
        if any(p.search(subject) for p in _PR_SUBJECTS) or _PR_BODY.search(body):
            return sha.strip(), subject
    return None


def commits_after(repo_dir: Path | str, commit: str) -> int:
    """How many first-parent commits HEAD has on top of `commit`."""
    return int(_git(repo_dir, "rev-list", "--count", "--first-parent", f"{commit}..HEAD").strip())


def changed_paths_in(repo_dir: Path | str, commit: str) -> list[str]:
    """Repo-relative paths a commit introduced, relative to its first parent.

    For a merge commit that is everything the merged branch brought in; for a
    squash-merged PR (a single commit) it is that commit's own diff. Both are
    "what this PR changed" from the perspective of the branch being deployed.

    Args:
        repo_dir: Path to a directory inside a git repository.
        commit: Commit-ish to inspect.
    """
    parents = _git(repo_dir, "rev-list", "--parents", "-n1", commit).split()
    # rev-list prints "<sha> <parent>..." — no parent means a root commit.
    if len(parents) < 2:
        out = _git(repo_dir, "show", "--name-only", "--format=", commit)
    else:
        out = _git(repo_dir, "diff", "--name-only", f"{commit}^1", commit)
    return [line.strip() for line in out.splitlines() if line.strip()]


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
