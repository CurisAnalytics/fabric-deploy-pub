"""Git utilities used by the deployer."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

__all__ = ["current_branch", "changed_paths_in", "latest_pr_commit"]

# How Azure DevOps and GitHub write a pull-request commit subject. ADO writes
# "Merged PR 123: ...", GitHub writes "Merge pull request #12 from ..." for a
# merge commit and "... (#12)" for a squash. A squash-merged PR leaves no merge
# commit at all, so the subject is the only signal.
_PR_SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^Merged PR \d+:"),
    re.compile(r"^Merge pull request #\d+"),
    re.compile(r"\(#\d+\)$"),
)

# How far back to look for the PR commit. A deploy follows a fetch of upstream
# changes, so the PR is near the tip; scanning further would risk picking up an
# unrelated older PR when the tip is plain local commits.
_PR_SEARCH_DEPTH = 50


def _git(repo_dir: Path | str, *args: str) -> str:
    """Run a git command in `repo_dir` and return its UTF-8 stdout, or raise RuntimeError."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError(
            "git was not found on PATH. fabdep runs it to read the current branch (pass "
            "--branch instead) and to find what --pr deploys."
        )
    try:
        result = subprocess.run(
            [git, *args],
            cwd=str(repo_dir),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else "Unknown error"
        raise RuntimeError(f"git {' '.join(args)} failed.\n{error_msg}") from e
    return result.stdout


def latest_pr_commit(repo_dir: Path | str) -> tuple[str, str] | None:
    """Find the most recent pull-request commit reachable from HEAD.

    Returns (sha, subject), or None if no PR-shaped commit is in the last
    `_PR_SEARCH_DEPTH` commits. A merge commit counts regardless of subject —
    that is what a non-squashed PR looks like once fetched.

    Args:
        repo_dir: Path to a directory inside a git repository.
    """
    out = _git(
        repo_dir,
        "log",
        f"-n{_PR_SEARCH_DEPTH}",
        "--format=%H%x00%P%x00%s",
        "HEAD",
    )
    for line in out.splitlines():
        sha, parents, subject = (line.split("\0", 2) + ["", ""])[:3]
        is_merge = len(parents.split()) > 1
        if is_merge or any(p.search(subject) for p in _PR_SUBJECT_PATTERNS):
            return sha, subject
    return None


def changed_paths_in(repo_dir: Path | str, commit: str) -> list[str]:
    """Paths under `repo_dir` that a commit changed relative to its first parent.

    For a merge commit that is everything the merged branch brought in; for a
    squash-merged PR (a single commit) it is that commit's own diff. Both are
    "what this PR changed" from the perspective of the branch being deployed.
    Paths are relative to `repo_dir`, which may be a subfolder of the git
    repository; changes outside it are left out.

    Args:
        repo_dir: Path to a directory inside a git repository.
        commit: Commit-ish to inspect.
    """
    parents = _git(repo_dir, "rev-list", "--parents", "-n1", commit).split()
    # rev-list prints "<sha> <parent>..." — no parent means a root commit.
    if len(parents) < 2:
        diff = ("diff-tree", "-r", "--root", "--no-commit-id", commit)
    else:
        diff = ("diff", f"{commit}^1", commit)
    # -z: paths unquoted and unescaped, whatever characters they hold.
    out = _git(repo_dir, *diff, "--name-only", "-z", "--relative")
    return [path for path in out.split("\0") if path]


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
    branch = _git(repo_dir, "branch", "--show-current").strip()
    if not branch:
        raise RuntimeError(
            "HEAD is detached, so there is no branch to choose the workspace by. "
            "Pass the branch name with --branch."
        )
    return branch
