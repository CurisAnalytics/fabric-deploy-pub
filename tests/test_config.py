"""How fabdep.yml and the branch resolve to a target workspace."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fabric_deploy.config import FabdepConfig
from fabric_deploy.workspace import PR_LATEST, WorkspaceDeployer

from .conftest import make_item

BASE = "workspace_base_name: Base\n"
MAP = """\
workspace_base_name: Contoso Sales
environments:
  main:
    workspace: Contoso Sales
    environment: PROD
  dev:
    workspace: "{base} (Dev)"
  "release/*":
    workspace: "{base} [RELEASE]"
  "feature/*":
    create: true
  "feature/legacy-*":
    workspace: Legacy
  feature/pinned:
    workspace: Pinned
  "hotfix-?":
    workspace: "{base} - {branch}"
"""


def _config(repo: Path, body: str, branch: str = "dev") -> FabdepConfig:
    (repo / "fabdep.yml").write_text(body, encoding="utf-8")
    return FabdepConfig.from_path(repo, branch_override=branch)


class TestFabdepYml:
    def test_missing_fabdep_yml_raises_before_any_api_call(self, tmp_path: Path, monkeypatch):
        """The folder name is no stand-in for the workspace name."""
        monkeypatch.setattr(
            "fabric_deploy.workspace.get_workspace_id_by_name",
            lambda _name: pytest.fail("looked up a workspace without a fabdep.yml"),
        )
        with pytest.raises(
            FileNotFoundError, match="(?s)No fabdep.yml in .*`fabdep init`.*workspace_base_name"
        ):
            WorkspaceDeployer(tmp_path, branch_override="dev")

    def test_yaml_missing_workspace_base_name_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="must set workspace_base_name"):
            _config(tmp_path, "")

    def test_branch_override_skips_git(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "fabric_deploy.config.current_branch",
            lambda _repo: pytest.fail("consulted git despite branch_override"),
        )
        assert _config(tmp_path, BASE, "staging").workspace_name == "Base [STAGING]"

    def test_reads_utf8_with_a_byte_order_mark(self, tmp_path: Path):
        body = 'workspace_base_name: "Ventes été"\n'
        (tmp_path / "fabdep.yml").write_bytes(body.encode("utf-8-sig"))
        config = FabdepConfig.from_path(tmp_path, branch_override="dev")
        assert config.workspace_name == "Ventes été [DEV]"


class TestWorkspaceNames:
    def test_defaults_without_environments(self, tmp_path: Path):
        config = _config(tmp_path, BASE, "feature/x")
        assert config.workspace_name == "Base [FEATURE/X]"
        assert config.environment == "feature/x"
        assert config.create_workspace is False

    def test_workspace_name_template(self, tmp_path: Path):
        body = BASE + 'workspace_name_template: "{base} - {branch}"\n'
        assert _config(tmp_path, body).workspace_name == "Base - dev"

    def test_template_needs_no_base_when_it_uses_none(self, tmp_path: Path):
        body = 'workspace_name_template: "Analytics {BRANCH}"\n'
        assert _config(tmp_path, body).workspace_name == "Analytics DEV"

    def test_literal_braces(self, tmp_path: Path):
        body = BASE + 'workspace_name_template: "{{{base}}} {branch}"\n'
        assert _config(tmp_path, body).workspace_name == "{Base} dev"

    @pytest.mark.parametrize(
        ("branch", "workspace", "environment", "create"),
        [
            ("main", "Contoso Sales", "PROD", False),
            ("dev", "Contoso Sales (Dev)", "dev", False),
            ("release/2.0", "Contoso Sales [RELEASE]", "release/2.0", False),
            ("feature/x", "Contoso Sales [FEATURE/X]", "feature/x", True),
            # The first matching glob wins, even over a more specific later one ...
            ("feature/legacy-y", "Contoso Sales [FEATURE/LEGACY-Y]", "feature/legacy-y", True),
            # ... but an exact key beats every glob.
            ("feature/pinned", "Pinned", "feature/pinned", False),
            # `?` matches one character.
            ("hotfix-1", "Contoso Sales - hotfix-1", "hotfix-1", False),
            ("hotfix-12", "Contoso Sales [HOTFIX-12]", "hotfix-12", False),
            # Branch names are case-sensitive; no entry falls back to the template.
            ("Main", "Contoso Sales [MAIN]", "Main", False),
            ("hotfix", "Contoso Sales [HOTFIX]", "hotfix", False),
        ],
    )
    def test_environments_map(self, tmp_path: Path, branch, workspace, environment, create):
        config = _config(tmp_path, MAP, branch)
        assert (config.workspace_name, config.environment, config.create_workspace) == (
            workspace,
            environment,
            create,
        )

    def test_entry_without_settings_uses_the_template(self, tmp_path: Path):
        body = BASE + "environments:\n  dev:\n"
        assert _config(tmp_path, body).workspace_name == "Base [DEV]"

    def test_literal_names_need_no_base(self, tmp_path: Path):
        body = "environments:\n  dev:\n    workspace: Sales Dev\n  qa:\n    workspace: Sales QA\n"
        assert _config(tmp_path, body).workspace_name == "Sales Dev"
        # A branch without an entry still needs the base for the template.
        with pytest.raises(
            ValueError,
            match=r"(?s)no entry for branch 'main' \(entries: dev, qa\).*workspace_base_name",
        ):
            _config(tmp_path, body, "main")

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (BASE + "environments: [dev, main]\n", "environments must map branch names"),
            (BASE + "environments:\n  dev: Sales\n", "environments 'dev' must be a mapping"),
            (
                BASE + "environments:\n  dev:\n    workpsace: Sales\n",
                "unknown key 'workpsace'.*did you mean 'workspace'",
            ),
            (BASE + "environments:\n  dev:\n    workspace: 3\n", "'workspace' must be a non-empty"),
            (BASE + "environments:\n  dev:\n    environment: ''\n", "'environment' must be a non"),
            (BASE + "environments:\n  dev:\n    create: 'yes'\n", "'create' must be true or false"),
            (BASE + "environments:\n  1:\n    workspace: Sales\n", "quote"),
            (
                BASE + 'workspace_name_template: "{base} {brnch}"\n',
                r"\{brnch\}.*\{base\}, \{branch\}",
            ),
            (BASE + 'workspace_name_template: "{base} [{branch!r}]"\n', r"\{branch!r\}"),
            (BASE + 'workspace_name_template: "{base"\n', "workspace_name_template"),
            (BASE + "workspace_name_template: 3\n", "workspace_name_template must be"),
            (
                'environments:\n  "feature/*":\n    workspace: "{base}-{branch}"\n',
                r"must set workspace_base_name.*'feature/\*'",
            ),
            (BASE + "capacity_id: F64\n", "capacity_id must be a Fabric capacity ID"),
            (BASE + "capacity_id: 12345\n", "capacity_id must be a Fabric capacity ID"),
        ],
    )
    def test_invalid_settings(self, tmp_path: Path, body: str, message: str):
        with pytest.raises(ValueError, match=f"(?s){message}"):
            _config(tmp_path, body)


class TestSiblingWorkspaces:
    def _siblings(self, tmp_path: Path, body: str, names: list[str]) -> list[str]:
        config = _config(tmp_path, body)
        return [n for n in names if config.is_repo_workspace(n)]

    def test_template_without_environments(self, tmp_path: Path):
        names = [
            "Base [QA]",
            "base [qa]",
            "Base [DEV] [OLD]",
            "Base",
            "Base X [DEV]",
            "Other [DEV]",
        ]
        assert self._siblings(tmp_path, BASE, names) == ["Base [QA]", "base [qa]"]

    def test_custom_template(self, tmp_path: Path):
        body = BASE + 'workspace_name_template: "{base} - {BRANCH}"\n'
        assert self._siblings(tmp_path, body, ["Base - QA", "Base [QA]"]) == ["Base - QA"]

    def test_environments_name_the_siblings(self, tmp_path: Path):
        names = [
            "Contoso Sales",
            "Contoso Sales (Dev)",
            "Contoso Sales [RELEASE]",
            "Contoso Sales [FEATURE/ABC]",  # "feature/*" falls back to the template
            "Legacy",
            "Pinned",
            "Contoso Sales - hotfix-2",
            "Contoso Sales [QA]",  # a branch without an entry: not one the map names
            "Contoso Sales (Test)",
            "Contoso Sales - hotfix-22",
        ]
        assert self._siblings(tmp_path, MAP, names) == names[:7]

    @pytest.mark.parametrize(
        ("body", "open_template"),
        [
            (BASE, None),
            ('workspace_name_template: "{branch}"\n', "{branch}"),
            (BASE + 'workspace_name_template: "{BRANCH}{branch}"\n', "{BRANCH}{branch}"),
            ('environments:\n  "*":\n    workspace: "{BRANCH}"\n', "{BRANCH}"),
            (BASE + 'environments:\n  "feature/*":\n    workspace: "{branch}"\n', None),
            ('workspace_name_template: "{branch}"\nenvironments:\n  dev:\n', None),
        ],
    )
    def test_warns_when_the_branch_is_all_a_name_has(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, body: str, open_template
    ):
        """Every workspace name without a ']' would match, and count as the repo's."""
        with caplog.at_level("WARNING", logger="fabric_deploy"):
            _config(tmp_path, body)
        warnings = [r.getMessage() for r in caplog.records if "no text besides" in r.getMessage()]
        assert warnings == ([] if open_template is None else [warnings[0]])
        if open_template is not None:
            assert repr(open_template) in warnings[0]


class TestTargetWorkspace:
    """Which workspace a deployer targets, and when it creates it."""

    CREATED = "00000000-0000-0000-0000-0000000000cc"
    CAPACITY = "00000000-0000-0000-0000-0000000000c1"

    @pytest.fixture(autouse=True)
    def _an_item(self, tmp_path: Path) -> None:
        """A deploy of a folder without items is refused before it gets this far."""
        make_item(tmp_path, "Sales", "Notebook")

    @pytest.mark.parametrize(
        ("body", "branch", "new_workspace", "exists", "created_on"),
        [
            pytest.param(MAP, "dev", False, True, None, id="existing"),
            pytest.param(MAP, "feature/x", False, False, "default", id="create-entry-missing"),
            pytest.param(MAP, "feature/x", False, True, None, id="create-entry-exists"),
            pytest.param(
                BASE + f"capacity_id: {CAPACITY}\n", "dev", True, False, CAPACITY, id="-N-capacity"
            ),
            pytest.param(BASE, "dev", True, False, "default", id="-N-only-active-capacity"),
        ],
    )
    def test_resolves_or_creates_the_workspace(
        self,
        tmp_path: Path,
        mock_fabric_api,
        fabric_cicd,
        body,
        branch,
        new_workspace,
        exists,
        created_on,
    ):
        """A workspace to create is created by the deploy, not by building the deployer."""
        (tmp_path / "fabdep.yml").write_text(body)
        config = FabdepConfig.from_path(tmp_path, branch_override=branch)
        create = MagicMock(return_value=self.CREATED)
        find = MagicMock(return_value=mock_fabric_api.workspace_id if exists else None)
        with (
            patch("fabric_deploy.workspace.create_workspace", create),
            patch("fabric_deploy.workspace.find_workspace_id_by_name", find),
        ):
            d = WorkspaceDeployer(tmp_path, branch_override=branch, new_workspace=new_workspace)
            create.assert_not_called()
            d.deploy()

        if created_on is None:
            create.assert_not_called()
            assert d.workspace_id == mock_fabric_api.workspace_id
        else:
            capacity = mock_fabric_api.capacity_id if created_on == "default" else created_on
            create.assert_called_once_with(
                workspace_name=config.workspace_name, capacity_id=capacity
            )
            assert d.workspace_id == self.CREATED
            assert fabric_cicd.workspace.call_args.kwargs["workspace_id"] == self.CREATED

    @pytest.mark.parametrize(
        ("new_workspace", "options", "error"),
        [
            pytest.param(True, {"item_types": ["Notebok"]}, "Notebok", id="-N-unknown-type"),
            pytest.param(True, {"pr": PR_LATEST}, "no pull-request commit", id="-N-no-pr-commit"),
            pytest.param(
                False, {"include_items": ["Nope.Notebook"]}, "Nope", id="create-unknown-item"
            ),
            pytest.param(
                True,
                {"parameter.yml": "find_replace: x\n"},
                "parameter.yml",
                id="-N-bad-parameter-file",
            ),
        ],
    )
    def test_nothing_is_created_when_the_deploy_options_fail(
        self,
        tmp_path: Path,
        mock_fabric_api,
        fabric_cicd,
        monkeypatch,
        new_workspace,
        options,
        error,
    ):
        (tmp_path / "fabdep.yml").write_text(MAP)
        if user_file := options.pop("parameter.yml", None):
            (tmp_path / "parameter.yml").write_text(user_file)
        monkeypatch.setattr("fabric_deploy.workspace.latest_pr_commit", lambda _d: None)
        create = MagicMock(return_value=self.CREATED)
        with (
            patch("fabric_deploy.workspace.create_workspace", create),
            patch(
                "fabric_deploy.workspace.find_workspace_id_by_name", MagicMock(return_value=None)
            ),
        ):
            d = WorkspaceDeployer(
                tmp_path, branch_override="feature/x", new_workspace=new_workspace
            )
            with pytest.raises((ValueError, RuntimeError), match=error):
                d.deploy(**options)

        create.assert_not_called()
        fabric_cicd.workspace.assert_not_called()

    def test_unknown_workspace_fails(self, tmp_path: Path, mock_fabric_api, monkeypatch):
        (tmp_path / "fabdep.yml").write_text(BASE)

        def not_found(name: str) -> str:
            raise ValueError(f"Workspace not found: '{name}'")

        monkeypatch.setattr("fabric_deploy.workspace.get_workspace_id_by_name", not_found)
        with pytest.raises(ValueError, match=r"Workspace not found: 'Base \[DEV\]'"):
            WorkspaceDeployer(tmp_path, branch_override="dev")

    def test_new_workspace_refuses_an_existing_one(self, tmp_path: Path, mock_fabric_api):
        (tmp_path / "fabdep.yml").write_text(BASE)
        with (
            patch("fabric_deploy.workspace.find_workspace_id_by_name", MagicMock(return_value="x")),
            pytest.raises(ValueError, match="remove the -N flag"),
        ):
            WorkspaceDeployer(tmp_path, branch_override="dev", new_workspace=True)

    @pytest.mark.parametrize(
        ("body", "branch", "new_workspace", "message"),
        [
            pytest.param(BASE, "dev", True, "remove the -N flag", id="-N"),
            pytest.param(MAP, "feature/x", False, "create: true", id="create-entry"),
        ],
    )
    def test_workspace_that_appears_before_the_create(
        self, tmp_path: Path, mock_fabric_api, fabric_cicd, body, branch, new_workspace, message
    ):
        """Someone else created it after the deployer looked: only -N is to blame for -N."""
        (tmp_path / "fabdep.yml").write_text(body)
        exists = MagicMock(side_effect=ValueError("Workspace already exists: 'x'"))
        with (
            patch("fabric_deploy.workspace.create_workspace", exists),
            patch(
                "fabric_deploy.workspace.find_workspace_id_by_name", MagicMock(return_value=None)
            ),
        ):
            d = WorkspaceDeployer(tmp_path, branch_override=branch, new_workspace=new_workspace)
            with pytest.raises(ValueError, match=message) as raised:
                d.deploy()
        assert ("-N" in str(raised.value)) == new_workspace
        fabric_cicd.workspace.assert_not_called()


def test_environment_reaches_the_deployer(tmp_path: Path, mock_fabric_api):
    (tmp_path / "fabdep.yml").write_text(MAP)
    assert WorkspaceDeployer(tmp_path, branch_override="main").environment == "PROD"
    explicit = WorkspaceDeployer(tmp_path, branch_override="main", environment="QA")
    assert explicit.environment == "QA"
