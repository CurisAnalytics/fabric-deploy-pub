"""Unit tests for the --pr deploy filter: PR-commit discovery and path->item mapping.

Uses a real git repo in tmp_path (git plumbing is the thing under test, so
mocking it would test nothing), built by git that reads none of the machine's
global or system config, runs no hooks and commits as a fixed identity, so the
suite doesn't depend on the developer's git setup.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from fabric_deploy.git import changed_paths_in, commits_after, current_branch, latest_pr_commit
from fabric_deploy.scope import items_for_changed_paths
from fabric_deploy.workspace import PR_LATEST

from .conftest import make_item

# --- helpers ---------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    # A global core.hooksPath, init.templateDir or signing setup would otherwise
    # run the developer's hooks on these commits, or fail them.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=T",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / "README.md").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def _item(repo: Path, name: str, item_type: str, subdir: str = "") -> Path:
    folder = f"{subdir}/{name}.{item_type}" if subdir else None
    return make_item(repo, name, item_type, folder=folder)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


# --- PR commit discovery ---------------------------------------------------


class TestLatestPrCommit:
    def test_none_when_no_pr_commit(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        _commit(repo, "chore: plain local commit")
        assert latest_pr_commit(repo) is None

    @pytest.mark.parametrize(
        "message",
        [
            "Merged PR 412: add orders pipeline",  # Azure DevOps
            "Merge pull request #12 from contoso/feature",  # GitHub merge
            "feat: add orders pipeline (#12)",  # GitHub squash
            "Merged in feature/orders (pull request #12)",  # Bitbucket
            "Merge branch 'orders' into 'main'\n\nSee merge request contoso/sales!12",  # GitLab
        ],
    )
    def test_recognizes_host_pr_messages(self, tmp_path: Path, message: str):
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        sha = _commit(repo, message)
        found = latest_pr_commit(repo)
        assert found is not None
        assert found[0] == sha
        assert found[1] == message.split("\n")[0]

    @pytest.mark.parametrize(
        "message", ["Merge branch 'feature'", "Merge branch 'main' of https://example.com/sales"]
    )
    def test_plain_merge_commit_is_not_a_pr(self, tmp_path: Path, message: str):
        """A local merge (`git merge`, `git pull`) is not what a PR brought in."""
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        pr = _commit(repo, "Merged PR 1: earlier")
        _git(repo, "checkout", "-b", "feature")
        (repo / "b.txt").write_text("b")
        _commit(repo, "feat: work")
        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "-m", message, "feature")
        assert latest_pr_commit(repo) == (pr, "Merged PR 1: earlier")

    def test_commits_a_merged_branch_brought_in_are_not_the_pr_commit(self, tmp_path: Path):
        """Only the deployed branch's own history counts: a PR-worded commit that a
        local merge brought in from another branch is not this branch's PR."""
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        pr = _commit(repo, "Merged PR 1: earlier")
        _git(repo, "checkout", "-b", "feature")
        (repo / "b.txt").write_text("b")
        _commit(repo, "feat: cherry-picked work (#5)")
        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "-m", "Merge branch 'feature'", "feature")
        assert latest_pr_commit(repo) == (pr, "Merged PR 1: earlier")
        assert commits_after(repo, pr) == 1

    def test_most_recent_pr_wins(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        _commit(repo, "Merged PR 1: old")
        (repo / "b.txt").write_text("b")
        sha = _commit(repo, "Merged PR 2: new")
        found = latest_pr_commit(repo)
        assert found is not None
        assert found[0] == sha

    def test_commits_after_counts_what_head_has_on_top(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        pr = _commit(repo, "Merged PR 1: add a")
        assert commits_after(repo, pr) == 0
        for name in ("b", "c"):
            (repo / f"{name}.txt").write_text(name)
            _commit(repo, f"chore: direct push {name}")
        assert commits_after(repo, pr) == 2


# --- changed paths ---------------------------------------------------------


class TestChangedPaths:
    def test_squash_commit_reports_its_own_diff(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a")
        sha = _commit(repo, "Merged PR 1: add a")
        assert changed_paths_in(repo, sha) == ["a.txt"]

    def test_merge_commit_reports_everything_the_branch_brought(self, tmp_path: Path):
        repo = _repo(tmp_path)
        _git(repo, "checkout", "-b", "feature")
        (repo / "a.txt").write_text("a")
        _commit(repo, "one")
        (repo / "b.txt").write_text("b")
        _commit(repo, "two")
        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "-m", "Merged PR 9: feature", "feature")
        sha = _git(repo, "rev-parse", "HEAD").strip()
        assert sorted(changed_paths_in(repo, sha)) == ["a.txt", "b.txt"]

    def test_paths_are_relative_to_a_workspace_in_a_subfolder(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "ws" / "N.Notebook").mkdir(parents=True)
        (repo / "ws" / "N.Notebook" / "notebook-content.py").write_text("x")
        (repo / "docs").mkdir()
        (repo / "docs" / "notes.md").write_text("x")
        sha = _commit(repo, "Merged PR 2: notebook")
        assert changed_paths_in(repo / "ws", sha) == ["N.Notebook/notebook-content.py"]

    def test_unusual_file_names_come_back_verbatim(self, tmp_path: Path):
        repo = _repo(tmp_path)
        item = repo / 'Ventes "été".Notebook'
        item.mkdir()
        (item / "notebook-content.py").write_text("x")
        sha = _commit(repo, "Merged PR 3: ventes")
        assert changed_paths_in(repo, sha) == ['Ventes "été".Notebook/notebook-content.py']

    def test_root_commit_in_a_subfolder(self, tmp_path: Path):
        _git(tmp_path, "init", "-b", "main")
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        sha = _commit(tmp_path, "initial")
        assert changed_paths_in(tmp_path / "ws", sha) == ["a.txt"]

    def test_a_shallow_clone_is_an_error(self, tmp_path: Path):
        """A CI checkout of depth 1 hides the PR commit's parent; diffed as a root
        commit it would list every file in the repo."""
        (tmp_path / "origin").mkdir()
        repo = _repo(tmp_path / "origin")
        (repo / "a.txt").write_text("a")
        _commit(repo, "Merged PR 1: add a")
        clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(clone))
        with pytest.raises(RuntimeError, match="shallow clone.*fetch-depth"):
            changed_paths_in(clone, "HEAD")


class TestGitErrors:
    def test_missing_git_is_a_runtime_error(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        with pytest.raises(RuntimeError, match="git"):
            latest_pr_commit(tmp_path)
        with pytest.raises(RuntimeError, match="git"):
            current_branch(tmp_path)

    def test_outside_a_repository_suggests_branch(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match=r"(?s)not in a git repository.*--branch"):
            current_branch(tmp_path)

    def test_detached_head_suggests_branch(self, tmp_path: Path):
        repo = _repo(tmp_path)
        _git(repo, "checkout", "--detach")
        with pytest.raises(RuntimeError, match="--branch"):
            current_branch(repo)


# --- path -> item mapping --------------------------------------------------


class TestItemsForChangedPaths:
    def test_maps_files_to_owning_items(self, tmp_path: Path):
        _item(tmp_path, "Load GL", "Notebook")
        _item(tmp_path, "Finance Report", "Report")
        matched, unmatched = items_for_changed_paths(
            [
                "Load GL.Notebook/notebook-content.py",
                "Finance Report.Report/definition/pages/p1/visual.json",
            ],
            tmp_path,
        )
        assert sorted(matched) == ["Finance Report.Report", "Load GL.Notebook"]
        assert unmatched == []

    def test_display_name_wins_over_directory_name(self, tmp_path: Path):
        """fabric-cicd matches displayName, not the folder name."""
        item = tmp_path / "weird_dir_name.Notebook"
        item.mkdir()
        (item / ".platform").write_text(
            json.dumps({"metadata": {"displayName": "Real Name", "type": "Notebook"}})
        )
        matched, _ = items_for_changed_paths(
            ["weird_dir_name.Notebook/notebook-content.py"], tmp_path
        )
        assert matched == ["Real Name.Notebook"]

    def test_nested_item_beats_shallower_match(self, tmp_path: Path):
        """A workspace folder is not an item; the item inside it owns the file."""
        _item(tmp_path, "Deep", "Notebook", subdir="FINANCE REPORT")
        matched, unmatched = items_for_changed_paths(
            ["FINANCE REPORT/Deep.Notebook/notebook-content.py"], tmp_path
        )
        assert matched == ["Deep.Notebook"]
        assert unmatched == []

    def test_non_item_paths_are_unmatched(self, tmp_path: Path):
        _item(tmp_path, "N", "Notebook")
        matched, unmatched = items_for_changed_paths(
            ["fabdep.yml", "README.md", "N.Notebook/notebook-content.py"], tmp_path
        )
        assert matched == ["N.Notebook"]
        assert sorted(unmatched) == ["README.md", "fabdep.yml"]

    def test_deleted_item_directory_is_unmatched(self, tmp_path: Path):
        """A PR that deleted an item leaves nothing to publish — --pr never unpublishes."""
        matched, unmatched = items_for_changed_paths(
            ["Gone.Notebook/notebook-content.py"], tmp_path
        )
        assert matched == []
        assert unmatched == ["Gone.Notebook/notebook-content.py"]

    def test_each_item_listed_once(self, tmp_path: Path):
        _item(tmp_path, "N", "Notebook")
        matched, _ = items_for_changed_paths(
            ["N.Notebook/notebook-content.py", "N.Notebook/.platform"], tmp_path
        )
        assert matched == ["N.Notebook"]

    def test_unknown_item_type_is_unmatched(self, tmp_path: Path):
        item = tmp_path / "x.Bogus"
        item.mkdir()
        (item / ".platform").write_text(
            json.dumps({"metadata": {"displayName": "x", "type": "Bogus"}})
        )
        matched, unmatched = items_for_changed_paths(["x.Bogus/thing.json"], tmp_path)
        assert matched == []
        assert unmatched == ["x.Bogus/thing.json"]


# --- deployer wiring -------------------------------------------------------


@pytest.fixture
def deployer(tmp_path: Path, mock_fabric_api):
    from fabric_deploy.workspace import WorkspaceDeployer

    (tmp_path / "fabdep.yml").write_text('workspace_base_name: "ws"\n')
    _item(tmp_path, "Load GL", "Notebook")
    return WorkspaceDeployer(tmp_path, branch_override="dev")


class TestItemsFromPr:
    @pytest.fixture(autouse=True)
    def _head_is_the_pr_commit(self, monkeypatch):
        monkeypatch.setattr("fabric_deploy.workspace.commits_after", lambda _d, _c: 0)

    def test_resolves_to_the_items_the_pr_touched(self, deployer, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.workspace.latest_pr_commit",
            lambda _d: ("abc123def", "Merged PR 7: gl fix"),
        )
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in",
            lambda _d, _c: ["Load GL.Notebook/notebook-content.py", "README.md"],
        )
        assert deployer._items_from_pr(PR_LATEST) == ["Load GL.Notebook"]

    def test_head_past_the_pr_commit_is_an_error(self, deployer, monkeypatch):
        """After a rebase merge or a direct push, the newest PR-worded commit is an
        older PR: deploying its items would redeploy that PR, not what HEAD brought."""
        monkeypatch.setattr(
            "fabric_deploy.workspace.latest_pr_commit",
            lambda _d: ("abc123def", "Merged PR 7: gl fix"),
        )
        monkeypatch.setattr("fabric_deploy.workspace.commits_after", lambda _d, _c: 3)
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in",
            lambda _d, _c: pytest.fail("should stop before reading the older PR's changes"),
        )
        with pytest.raises(RuntimeError, match=r"3 commit\(s\) past.*--pr <commit>"):
            deployer._items_from_pr(PR_LATEST)

    def test_explicit_commit_skips_the_search(self, deployer, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.workspace.latest_pr_commit",
            lambda _d: pytest.fail("should not search when a commit is given"),
        )
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in",
            lambda _d, commit: (
                ["Load GL.Notebook/notebook-content.py"] if commit == "deadbee" else []
            ),
        )
        assert deployer._items_from_pr("deadbee") == ["Load GL.Notebook"]

    def test_raises_when_no_pr_commit_found(self, deployer, monkeypatch):
        monkeypatch.setattr("fabric_deploy.workspace.latest_pr_commit", lambda _d: None)
        with pytest.raises(RuntimeError, match="found no pull-request commit"):
            deployer._items_from_pr(PR_LATEST)

    def test_explicit_head_is_heads_own_commit(self, deployer, monkeypatch):
        """`--pr HEAD` names a commit; only a bare `--pr` searches for the PR commit."""
        monkeypatch.setattr(
            "fabric_deploy.workspace.latest_pr_commit",
            lambda _d: pytest.fail("should not search when a commit is given"),
        )
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in",
            lambda _d, commit: ["Load GL.Notebook/notebook-content.py"] if commit == "HEAD" else [],
        )
        assert deployer._items_from_pr("HEAD") == ["Load GL.Notebook"]

    def test_pr_that_touched_no_item_deploys_nothing(
        self, deployer, monkeypatch, fabric_cicd, caplog
    ):
        """A docs-only PR is a no-op, never a deploy of everything."""
        monkeypatch.setattr(
            "fabric_deploy.workspace.latest_pr_commit",
            lambda _d: ("abc123def", "Merged PR 7: docs"),
        )
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in", lambda _d, _c: ["README.md"]
        )
        monkeypatch.setattr(
            "fabric_deploy.workspace.create_workspace",
            lambda *_a: pytest.fail("created a workspace with nothing to deploy"),
        )
        scope = deployer.resolve_scope(pr=PR_LATEST)
        assert (scope.items, scope.include_items, scope.cleanup) == ([], [], False)

        with caplog.at_level("WARNING"):
            deployer.deploy(pr=PR_LATEST)
        assert "nothing to deploy" in caplog.text
        assert deployer.deployed_items == []
        fabric_cicd.workspace.assert_not_called()
        fabric_cicd.publish.assert_not_called()
        fabric_cicd.unpublish.assert_not_called()


def test_workspace_in_a_subfolder_of_the_git_repo(tmp_path: Path, mock_fabric_api):
    from fabric_deploy.workspace import WorkspaceDeployer

    repo = _repo(tmp_path)
    ws = repo / "workspace"
    ws.mkdir()
    (ws / "fabdep.yml").write_text('workspace_base_name: "ws"\n')
    item = _item(ws, "Load GL", "Notebook")
    _commit(repo, "chore: add workspace")
    (item / "notebook-content.py").write_text("x")
    (repo / "README.md").write_text("y")
    _commit(repo, "Merged PR 5: gl fix")

    deployer = WorkspaceDeployer(ws, branch_override="dev")

    assert deployer._items_from_pr(PR_LATEST) == ["Load GL.Notebook"]


def test_pr_and_include_items_conflict(deployer):
    with pytest.raises(RuntimeError, match="--pr and --include-items"):
        deployer.deploy(pr=PR_LATEST, include_items=["Load GL.Notebook"])
