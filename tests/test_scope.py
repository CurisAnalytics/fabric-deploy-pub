"""The .platform index and deploy-scope resolution (scope.py)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fabric_cicd.constants import ACCEPTED_ITEM_TYPES

from fabric_deploy.scope import (
    RepoItem,
    find_in_item_files,
    repo_item_types,
    repo_items,
    resolve_item_scope,
    validate_include_items,
)

from .conftest import make_item

LOG = logging.getLogger("test.scope")


class TestRepoItems:
    """Items are what their .platform says, whatever the folder is called —
    a `*.SynapseNotebook/` dir with `type: Notebook` is a Notebook."""

    def test_reads_type_name_and_logical_id_shallowest_first(self, tmp_path: Path):
        make_item(tmp_path, "db", "SQLDatabase", folder="ERP/db.SQLDatabase")
        make_item(tmp_path, "foo", "Notebook", folder="foo.SynapseNotebook", logical_id="l-1")

        assert repo_items(tmp_path) == [
            RepoItem(tmp_path / "foo.SynapseNotebook", "Notebook", "foo", "l-1"),
            RepoItem(tmp_path / "ERP" / "db.SQLDatabase", "SQLDatabase", "db", None),
        ]
        assert [i.key for i in repo_items(tmp_path, "Notebook")] == ["foo.Notebook"]
        assert repo_item_types(tmp_path) == {"Notebook", "SQLDatabase"}

    def test_platform_files_are_utf8_with_or_without_bom(self, tmp_path: Path):
        for name, encoding in (("Ventes été", "utf-8"), ("Achats été", "utf-8-sig")):
            platform = json.dumps({"metadata": {"displayName": name, "type": "Notebook"}})
            make_item(tmp_path, name, "Notebook")
            (tmp_path / f"{name}.Notebook" / ".platform").write_bytes(platform.encode(encoding))
        assert [i.name for i in repo_items(tmp_path)] == ["Achats été", "Ventes été"]

    def test_skips_types_fabric_cicd_does_not_deploy(self, tmp_path: Path):
        make_item(tmp_path, "foo", "NotARealType")
        assert repo_items(tmp_path) == []

    @pytest.mark.parametrize(
        "platform",
        ["{not json", '{"metadata": {"type": "Notebook"}}', '{"metadata": []}', "[]"],
    )
    def test_unreadable_platform_is_skipped(self, tmp_path: Path, caplog, platform):
        """Silently, unless the caller passes a logger to warn on."""
        (tmp_path / "x.Notebook").mkdir()
        (tmp_path / "x.Notebook" / ".platform").write_text(platform)
        with caplog.at_level("WARNING"):
            assert repo_items(tmp_path) == []
            assert not caplog.records
            assert repo_items(tmp_path, logger=LOG) == []
        assert "x.Notebook" in caplog.text


def test_find_in_item_files_reads_item_files_only(tmp_path: Path):
    """fabdep.yml and files outside any item are not item files."""
    make_item(tmp_path, "nb", "Notebook", files={"notebook-content.py": "x = 'abc'\n"})
    (tmp_path / "fabdep.yml").write_text("abc\n")
    (tmp_path / "loose").mkdir()
    (tmp_path / "loose" / "file.txt").write_text("abc def\n")

    assert find_in_item_files(tmp_path, ["abc", "def"]) == {
        "abc": {"nb.Notebook/notebook-content.py"},
        "def": set(),
    }


@pytest.mark.parametrize(
    ("item_types", "exclude_types", "include_items", "expected"),
    [
        (None, None, None, None),
        (["Notebook", "Report"], None, None, ["Notebook", "Report"]),
        (["notebook", "REPORT"], None, None, ["Notebook", "Report"]),  # canonical case
        (["Notebook"], ["Notebook", "Report"], None, ["Notebook"]),  # --item-types wins
        (["SemanticModel"], None, ["Foo.Notebook"], ["SemanticModel"]),
        (None, None, ["Foo.Notebook", "Bar.Report"], ["Notebook", "Report"]),
        (None, None, ["Foo.Notebook", "PlainName", "Bar.NotAType"], ["Notebook"]),
        (None, ["Notebook"], None, [t for t in ACCEPTED_ITEM_TYPES if t != "Notebook"]),
    ],
)
def test_resolve_item_scope(item_types, exclude_types, include_items, expected):
    assert resolve_item_scope(item_types, exclude_types, include_items, LOG) == expected


def test_resolve_item_scope_warns_about_unknown_types(caplog):
    with caplog.at_level("WARNING"):
        resolve_item_scope(["Notebook", "BogusType"], None, None, LOG)
    assert "BogusType" in caplog.text


class TestValidateIncludeItems:
    """--include-items is matched on display name; a name that matches nothing
    must raise instead of silently deploying zero items."""

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        make_item(tmp_path, "Fill Lakehouse", "Notebook")
        make_item(tmp_path, "Load", "Notebook", folder="load_dir.SynapseNotebook")
        return tmp_path

    @pytest.mark.parametrize(
        ("requested", "matched"),
        [
            (["Fill Lakehouse.Notebook"], ["Fill Lakehouse.Notebook"]),
            (["Fill Lakehouse.Notebook", "Ghost.Notebook"], ["Fill Lakehouse.Notebook"]),
            (["Load.Notebook"], ["Load.Notebook"]),
        ],
    )
    def test_returns_the_matched_items(self, repo, requested, matched):
        assert validate_include_items(requested, repo, LOG) == matched

    @pytest.mark.parametrize("requested", [["Ghost.Notebook"], ["load_dir.Notebook"]])
    def test_raises_when_nothing_matches(self, repo, requested):
        with pytest.raises(RuntimeError, match="matched no items"):
            validate_include_items(requested, repo, LOG)
