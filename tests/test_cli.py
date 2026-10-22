"""CLI tests against the sample workspace fixture."""

import io
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fabric_deploy.cli import (
    format_deploy_options,
    handle_command_error,
    log_banner,
    main,
    parse_args,
)
from fabric_deploy.scope import repo_item_types
from fabric_deploy.workspace import WorkspaceDeployer

from .conftest import SAMPLE_DEV_WORKSPACE_ID, SAMPLE_LAKEHOUSE_ID

# Resolve the fabdep binary once. Prefer PATH; fall back to the venv that
# launched pytest (sys.executable's bin dir). Tests skip if neither exists.
_FABDEP = shutil.which("fabdep") or str(Path(sys.executable).parent / "fabdep")
_FABDEP_AVAILABLE = Path(_FABDEP).exists()
_REQUIRES_FABDEP = pytest.mark.skipif(
    not _FABDEP_AVAILABLE,
    reason="fabdep binary not installed on PATH or in sys.executable's bin dir",
)


def _parse(monkeypatch, *argv: str):
    monkeypatch.setattr(sys, "argv", ["fabdep", *argv])
    return parse_args()


def _run(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["fabdep", *argv])
    main()


class TestCLI:
    """Test fabdep CLI commands against test workspace."""

    @pytest.fixture
    def test_workspace_dir(self, sample_ws):
        return sample_ws

    @pytest.mark.integration
    def test_workspace_deployer(self, test_workspace_dir, monkeypatch):
        """Live: deploy the repo to its MAIN workspace one item type at a time."""
        monkeypatch.chdir(test_workspace_dir)
        deployer = WorkspaceDeployer(test_workspace_dir, branch_override="MAIN")
        for item_type in repo_item_types(test_workspace_dir):
            deployer.deploy(item_types=[item_type])

    @pytest.mark.integration
    @_REQUIRES_FABDEP
    def test_fabdep_no_args(self, test_workspace_dir):
        """Test running fabdep with no arguments exits without error.

        Runs a real deployment against the live workspace; integration-only.
        """
        result = subprocess.run(
            [_FABDEP],
            cwd=test_workspace_dir,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"

    @_REQUIRES_FABDEP
    def test_detect_command(self, test_workspace_dir):
        """Test fabdep detect --save command."""
        result = subprocess.run(
            [_FABDEP, "detect", "--save"],
            cwd=test_workspace_dir,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"

        # Verify output file was created
        detected_file = test_workspace_dir / "parameter.yml.detected"
        assert detected_file.exists(), "parameter.yml.detected not created"

        # Verify it contains expected parameters
        content = detected_file.read_text()
        assert SAMPLE_DEV_WORKSPACE_ID in content
        assert SAMPLE_LAKEHOUSE_ID in content
        assert "$workspace.$id" in content
        assert "$items.Lakehouse.fabdep_lakehouse.$id" in content

        # Clean up
        detected_file.unlink()


class TestDetectSourceWorkspaceFlag:
    """`fabdep detect --source-workspace-id` parses (repeatable) and runs."""

    def test_detect_parser_accepts_source_workspace_id(self, monkeypatch):
        args = _parse(monkeypatch, "detect", "--source-workspace-id", SAMPLE_DEV_WORKSPACE_ID)
        assert args.source_workspace_ids == [SAMPLE_DEV_WORKSPACE_ID]

    @_REQUIRES_FABDEP
    def test_detect_with_source_workspace_id_runs(self, sample_ws):
        result = subprocess.run(
            [
                _FABDEP,
                "detect",
                "--source-workspace-id",
                SAMPLE_DEV_WORKSPACE_ID,
            ],
            cwd=sample_ws,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "Source workspace anchor" in result.stdout


def test_detect_prints_its_results_to_stdout(monkeypatch, capsys, sample_ws):
    """`fabdep detect > ids.txt` captures the results; warnings stay on stderr."""
    _run(monkeypatch, "detect", str(sample_ws))
    captured = capsys.readouterr()
    assert "Detected " in captured.out
    assert f"Source value: {SAMPLE_LAKEHOUSE_ID}" in captured.out
    assert "Detected " not in captured.err


class TestBanner:
    @staticmethod
    def _options(monkeypatch, *argv: str) -> str:
        return format_deploy_options(_parse(monkeypatch, "deploy", *argv))

    def test_defaults(self, monkeypatch):
        assert self._options(monkeypatch) == "Options: (defaults)"

    def test_scoped_cross_branch_deploy(self, monkeypatch):
        line = self._options(monkeypatch, "-b", "prod", "-t", "SemanticModel")
        assert line == "Options: --branch prod --item-types SemanticModel"

    def test_all_flags(self, monkeypatch):
        line = self._options(
            monkeypatch,
            *("--auth", "env", "-b", "prod", "-N", "-L", "-n", "-F", "--frame"),
            *("-t", "Notebook", "DataPipeline", "-x", "Report", "-i", "My Model.SemanticModel"),
            *("--allow-rls-change", "--allow-unbound-udf"),
        )
        for needle in (
            "--auth env",
            "--branch prod",
            "--new-workspace",
            "--skip-lakehouse",
            "--no-cleanup",
            "--item-types Notebook DataPipeline",
            "--exclude-types Report",
            "--include-items 'My Model.SemanticModel'",
            "--fail-on-stale",
            "--frame",
            "--allow-rls-change",
            "--allow-unbound-udf",
        ):
            assert needle in line

    def test_pr(self, monkeypatch):
        assert self._options(monkeypatch, "--pr") == "Options: --pr"
        assert self._options(monkeypatch, "--pr", "abc1234") == "Options: --pr abc1234"

    def test_banner_names_fabdep(self, caplog):
        from fabric_deploy import __version__

        with caplog.at_level("INFO"):
            log_banner(logging.getLogger("fabric_deploy.test"), "Options: (defaults)")
        assert any(f"fabdep v{__version__}" in r.message for r in caplog.records)
        assert caplog.records[-1].message == "Options: (defaults)"


class TestHelp:
    def test_top_level_help_shows_the_deploy_options(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            _parse(monkeypatch, "-h")
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for flag in ("--item-types", "--include-items", "--branch", "--auth", "--frame", "--pr"):
            assert flag in out
        for command in ("auth", "detect", "refresh", "rls"):
            assert command in out

    @pytest.mark.parametrize("command", [["deploy"], ["refresh"], ["rls", "pull"]])
    def test_target_options_are_shared(self, monkeypatch, command):
        args = _parse(monkeypatch, *command, "-b", "prod", "-i", "M.SemanticModel", "--auth", "env")
        assert (args.branch, args.include_items, args.auth) == ("prod", ["M.SemanticModel"], "env")
        assert args.directory == Path(".")


MODEL = "SQL DB.SemanticModel"  # the sample workspace's model and notebook
NOTEBOOK = "Fill Lakehouse.Notebook"


class TestFrameScope:
    """`deploy --frame` refreshes only the semantic models the deploy published."""

    @pytest.fixture
    def framed(self, monkeypatch, sample_ws, mock_fabric_api, fabric_cicd) -> list[list[str]]:
        calls: list[list[str]] = []

        def frame_models(**kwargs: object) -> list[dict]:
            calls.append(kwargs["models"])  # type: ignore[arg-type]
            return []

        monkeypatch.setattr("fabric_deploy.rls._remote_roles", lambda _ws, _name: None)
        monkeypatch.setattr(
            "fabric_deploy.workspace.changed_paths_in",
            lambda _repo, _commit: [f"{MODEL}/definition/model.tmdl"],
        )
        monkeypatch.setattr("fabric_deploy.cli.frame_models", frame_models)
        monkeypatch.chdir(sample_ws)
        return calls

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            ([], [["SQL DB"]]),
            (["-i", MODEL, NOTEBOOK], [["SQL DB"]]),
            (["--pr", "abc1234"], [["SQL DB"]]),
            (["-t", "semanticmodel"], [["SQL DB"]]),
            (["-i", NOTEBOOK], []),
            (["-t", "Notebook"], []),
            (["-x", "SemanticModel"], []),
            (["-t", "Notebook", "-i", MODEL], []),
        ],
    )
    def test_frames_what_the_deploy_published(self, monkeypatch, framed, argv, expected):
        _run(monkeypatch, "deploy", "-b", "MAIN", "--frame", *argv)
        assert framed == expected

    def test_says_why_nothing_was_framed(self, monkeypatch, framed, caplog):
        with caplog.at_level("INFO"):
            _run(monkeypatch, "deploy", "-b", "MAIN", "--frame", "-i", NOTEBOOK)
        assert "no semantic model" in caplog.text

    def test_no_frame_without_the_flag(self, monkeypatch, framed):
        _run(monkeypatch, "deploy", "-b", "MAIN")
        assert framed == []


class TestRlsPullExitStatus:
    """Drift exits 3, as `plan` does when a guard would block; errors exit 1."""

    @pytest.mark.parametrize(
        ("changed", "argv", "code"),
        [
            (True, [], 3),
            (False, [], 0),
            (True, ["--write"], 0),
            (False, ["--write"], 0),
        ],
    )
    def test_exit_status(self, monkeypatch, tmp_path, changed, argv, code):
        monkeypatch.setattr("fabric_deploy.cli.pull_rls", lambda **_: changed)
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "rls", "pull", str(tmp_path), *argv)
            raise SystemExit(0)
        assert exc.value.code == code

    def test_an_error_exits_1(self, monkeypatch, tmp_path):
        def fail(**_: object) -> bool:
            raise ValueError("No semantic model to check.")

        monkeypatch.setattr("fabric_deploy.cli.pull_rls", fail)
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "rls", "pull", str(tmp_path))
        assert exc.value.code == 1


class TestRemovedCommands:
    @pytest.mark.parametrize(
        "argv",
        [
            ["rename"],
            ["rename", "--dry-run"],
            ["auth", "list"],
            ["auth", "switch", "contoso"],
            ["auth", "rename", "a", "b"],
            ["auth", "login", "contoso"],
            ["auth", "bind", "contoso"],
        ],
    )
    def test_say_they_were_removed(self, monkeypatch, capsys, argv):
        with pytest.raises(SystemExit) as exc:
            _parse(monkeypatch, *argv)
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.count("\n") == 1
        assert f"'{' '.join(argv[:2] if argv[0] == 'auth' else argv[:1])}'" in captured.err
        assert "removed" in captured.err
        assert "CHANGELOG" in captured.err

    @pytest.mark.parametrize(
        ("argv", "option"),
        [
            (["--max-retries", "3"], "--max-retries"),
            (["--max-retries=3"], "--max-retries"),
            (["-r", "3"], "-r"),
            (["deploy", ".", "-b", "dev", "-r", "3"], "-r"),
        ],
    )
    def test_removed_options_say_so(self, monkeypatch, capsys, argv, option):
        with pytest.raises(SystemExit) as exc:
            _parse(monkeypatch, *argv)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert err == f"fabdep: '{option}' was removed; see CHANGELOG.md.\n"


class TestUnknownCommand:
    @pytest.mark.parametrize(
        ("typo", "command"), [("refesh", "refresh"), ("detct", "detect"), ("pln", "plan")]
    )
    def test_a_typo_suggests_the_command(self, monkeypatch, capsys, tmp_path, typo, command):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _parse(monkeypatch, typo, "-b", "dev")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert err == f"fabdep: unknown command '{typo}' (did you mean '{command}'?)\n"

    def test_a_directory_named_like_a_command_is_deployed(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "refesh").mkdir()
        args = _parse(monkeypatch, "refesh")
        assert (args.command, args.directory) == ("deploy", Path("refesh"))

    def test_a_missing_directory_unlike_any_command(self, monkeypatch, capsys, tmp_path):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _parse(monkeypatch, "contoso-sales")
        assert exc.value.code == 2
        assert "no such directory: contoso-sales" in capsys.readouterr().err


class TestCommandErrors:
    @pytest.mark.parametrize(
        ("command", "target"),
        [("deploy", "WorkspaceDeployer"), ("detect", "ParameterDetector")],
    )
    def test_a_missing_file_is_not_a_usage_error(self, monkeypatch, capsys, command, target):
        def missing_az(**_: object) -> None:
            raise FileNotFoundError(2, "No such file or directory", "az")

        monkeypatch.setattr(f"fabric_deploy.cli.{target}", missing_az)
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, command)
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "No such file or directory: 'az'" in captured.err + captured.out
        assert "Usage:" not in captured.err + captured.out

    def test_missing_fabdep_yml(self, monkeypatch, capsys, tmp_path):
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "deploy", str(tmp_path))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "No fabdep.yml" in captured.err + captured.out
        assert "Usage:" not in captured.err + captured.out

    @pytest.mark.parametrize(
        "command", [["deploy"], ["detect"], ["refresh"], ["rls", "pull"], ["auth", "status"]]
    )
    def test_a_missing_directory_is_a_usage_error(self, monkeypatch, capsys, tmp_path, command):
        with pytest.raises(SystemExit) as exc:
            _parse(monkeypatch, *command, str(tmp_path / "missing"))
        assert exc.value.code == 2
        assert "no such directory" in capsys.readouterr().err

    def test_schema_that_cannot_be_written_is_a_clean_error(self, monkeypatch, capsys, tmp_path):
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "schema", "-o", str(tmp_path / "missing" / "fabdep.schema.json"))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "schema failed:" in captured.err
        assert "No such file or directory" in captured.err

    def test_errors_go_to_stderr(self, capsys):
        with pytest.raises(SystemExit):
            handle_command_error(
                RuntimeError("Failed to list workspaces (HTTP 403)."), "Deployment"
            )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Deployment failed: Failed to list workspaces (HTTP 403)." in captured.err
        assert "Contributor" in captured.err


def test_a_legacy_console_encoding_does_not_crash_output(monkeypatch, sample_ws):
    """A cp1252 console (Windows) can't encode '✓'; it prints a placeholder instead."""
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    _run(monkeypatch, "detect", "--save", str(sample_ws))
    stdout.flush()
    assert "? Saved" in stdout.buffer.getvalue().decode("cp1252")  # type: ignore[attr-defined]
