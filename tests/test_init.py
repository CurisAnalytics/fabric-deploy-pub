"""`fabdep init`: a starter fabdep.yml whose every example is valid."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from fabric_deploy.cli import main
from fabric_deploy.config import FabdepConfig
from fabric_deploy.params import _manual_entries
from fabric_deploy.scaffold import STARTER_EXAMPLES, schema_text, starter_fabdep_yml

from .conftest import make_item

VALIDATOR = Draft202012Validator(json.loads(schema_text()))


def _init(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["fabdep", "init", *argv])
    main()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A workspace folder named "Contoso Sales" holding one item."""
    repo = tmp_path / "Contoso Sales"
    make_item(repo, "Sales", "Notebook")
    return repo


def _read(repo: Path, branch: str) -> FabdepConfig:
    """fabdep.yml as the loader reads it for `branch`, manual_parameters included."""
    config = FabdepConfig.from_path(repo, branch_override=branch)
    _ = _manual_entries(config.manual_parameters)
    return config


class TestInit:
    def test_names_the_workspace_after_the_directory(self, monkeypatch, capsys, repo: Path):
        _init(monkeypatch, str(repo))

        assert yaml.safe_load((repo / "fabdep.yml").read_text(encoding="utf-8")) == {
            "workspace_base_name": "Contoso Sales"
        }
        assert _read(repo, "dev").workspace_name == "Contoso Sales [DEV]"
        captured = capsys.readouterr()
        assert "Wrote" in captured.out
        for step in ("fabdep detect", "fabdep plan", "fabdep "):
            assert step in captured.out
        assert captured.err == ""

    def test_current_directory(self, monkeypatch, capsys, repo: Path):
        monkeypatch.chdir(repo)
        _init(monkeypatch)
        assert _read(repo, "main").workspace_base_name == "Contoso Sales"
        assert "Next steps:" in capsys.readouterr().out

    def test_next_steps_name_another_directory(self, monkeypatch, capsys, repo: Path):
        _init(monkeypatch, str(repo))
        assert f"Next steps, in {repo}:" in capsys.readouterr().out

    @pytest.mark.parametrize("name", ["Northwind", 'Sales: "EU" #1', "Ventes été", "{base}"])
    def test_base_name(self, monkeypatch, repo: Path, name: str):
        _init(monkeypatch, str(repo), "--base-name", name)
        assert _read(repo, "main").workspace_base_name == name

    def test_refuses_to_overwrite(self, monkeypatch, capsys, repo: Path):
        (repo / "fabdep.yml").write_text("workspace_base_name: Mine\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            _init(monkeypatch, str(repo))
        assert exc.value.code == 1
        assert "--force" in capsys.readouterr().err
        assert (repo / "fabdep.yml").read_text(encoding="utf-8") == "workspace_base_name: Mine\n"

    def test_force_overwrites(self, monkeypatch, repo: Path):
        (repo / "fabdep.yml").write_text("workspace_base_name: Mine\n", encoding="utf-8")
        _init(monkeypatch, str(repo), "--force")
        assert _read(repo, "main").workspace_base_name == "Contoso Sales"

    def test_warns_without_items(self, monkeypatch, capsys, tmp_path: Path):
        _init(monkeypatch, str(tmp_path))
        assert (tmp_path / "fabdep.yml").is_file()
        assert ".platform" in capsys.readouterr().err

    def test_a_missing_directory_is_a_usage_error(self, monkeypatch, capsys, tmp_path: Path):
        with pytest.raises(SystemExit) as exc:
            _init(monkeypatch, str(tmp_path / "missing"))
        assert exc.value.code == 2
        assert "no such directory" in capsys.readouterr().err


class TestStarter:
    """Each commented-out example, uncommented, is a fabdep.yml the loader and schema accept."""

    BASE = "workspace_base_name: Contoso Sales\n"

    @pytest.mark.parametrize(
        "example",
        [e for _, e in STARTER_EXAMPLES],
        ids=[e.split(":")[0] for _, e in STARTER_EXAMPLES],
    )
    def test_each_example_is_valid(self, tmp_path: Path, example: str):
        body = self.BASE + example
        assert VALIDATOR.is_valid(yaml.safe_load(body))
        (tmp_path / "fabdep.yml").write_text(body, encoding="utf-8")
        _ = _read(tmp_path, "main")

    def test_uncommented_whole_file_is_valid(self, tmp_path: Path):
        text = starter_fabdep_yml("Contoso Sales")
        for _, example in STARTER_EXAMPLES:
            commented = "".join(f"# {line}\n" for line in example.splitlines())
            assert commented in text
            text = text.replace(commented, example)
        data = yaml.safe_load(text)

        assert set(data) == set(json.loads(schema_text())["properties"])
        assert VALIDATOR.is_valid(data)
        (tmp_path / "fabdep.yml").write_text(text, encoding="utf-8")
        assert _read(tmp_path, "main").workspace_name == "Contoso Sales"

    def test_explains_editor_validation(self):
        text = starter_fabdep_yml("Contoso Sales")
        assert "fabdep schema -o fabdep.schema.json" in text
        assert "# yaml-language-server: $schema=./fabdep.schema.json" in text
