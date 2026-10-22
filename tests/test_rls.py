"""Unit tests for the RLS round-trip (rls.py).

Pure helpers (role extraction, diff classification, ref-role reconciliation)
are tested directly; the guard and `pull` orchestrators are tested with the
network surface (`_remote_roles`) mocked.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabric_deploy.rls import (
    MEMBERS_NOTE,
    Role,
    _reconcile_ref_roles,
    diff_roles,
    guard_deploy_rls,
    pull_rls,
    read_local_roles,
    roles_from_parts,
)
from fabric_deploy.workspace import WorkspaceDeployer

from .conftest import make_item

ROLE_RLS = (
    "role RLS\n\tmodelPermission: read\n\n\ttablePermission contact = [User Name] = USERNAME()\n"
)
ROLE_RLS_EDITED = ROLE_RLS.replace("USERNAME()", "USERPRINCIPALNAME()")
ROLE_FS = "role fs_rls\n\tmodelPermission: read\n"
ID_A = "\tannotation PBI_Id = 000000000000000000000000000000a1\n"
ID_B = "\tannotation PBI_Id = 000000000000000000000000000000b2\n"


def _make_model(tmp_path, name="M", roles=None, model_tmdl=None):
    """Create a `.SemanticModel` repo dir with an optional roles/ folder."""
    files = {"definition/model.tmdl": model_tmdl or "model Model\n\nref role RLS\n"}
    files.update({f"definition/roles/{fname}": text for fname, text in (roles or {}).items()})
    return make_item(tmp_path, name, "SemanticModel", files=files)


# --- extraction ----------------------------------------------------------


def test_roles_from_parts_extracts_name_and_filename():
    parts = {
        "definition/roles/RLS.tmdl": ROLE_RLS,
        "definition/model.tmdl": "model Model\nref role RLS\n",
        "definition/tables/contact.tmdl": "table contact\n",
    }
    roles = roles_from_parts(parts)
    assert set(roles) == {"RLS"}
    assert roles["RLS"].filename == "RLS.tmdl"


def test_read_local_roles_empty_without_roles_dir(tmp_path):
    d = _make_model(tmp_path, roles=None)
    assert read_local_roles(d) == {}


def test_read_local_roles_reads_files(tmp_path):
    d = _make_model(tmp_path, roles={"RLS.tmdl": ROLE_RLS, "fs_rls.tmdl": ROLE_FS})
    roles = read_local_roles(d)
    assert set(roles) == {"RLS", "fs_rls"}


def test_read_local_roles_tolerates_utf8_bom(tmp_path):
    """A BOM written by an editor must not read as a rule change."""
    d = _make_model(tmp_path, roles={"RLS.tmdl": ""})
    (d / "definition" / "roles" / "RLS.tmdl").write_bytes(b"\xef\xbb\xbf" + ROLE_RLS.encode())
    roles = read_local_roles(d)
    assert set(roles) == {"RLS"}
    assert not diff_roles(roles, roles_from_parts({"definition/roles/RLS.tmdl": ROLE_RLS})).changed


# --- diff ----------------------------------------------------------------


def _role(name, text):
    return {name: Role(name=name, filename=f"{name}.tmdl", raw=text)}


def test_diff_unchanged_when_identical():
    local = _role("RLS", ROLE_RLS)
    remote = _role("RLS", ROLE_RLS)
    d = diff_roles(local, remote)
    assert not d.changed
    assert d.unchanged == ["RLS"]


def test_diff_ignores_cosmetic_whitespace():
    """Trailing whitespace + surrounding blank lines must not read as a change."""
    local = _role("RLS", ROLE_RLS)
    remote = _role("RLS", "\n\n" + ROLE_RLS.replace("\n", "  \n") + "\n")
    assert not diff_roles(local, remote).changed


@pytest.mark.parametrize(
    ("local_text", "remote_text"),
    [
        pytest.param(ROLE_RLS + ID_A, ROLE_RLS + ID_B, id="different"),
        pytest.param(ROLE_RLS, ROLE_RLS + ID_B, id="repo-has-none"),
        pytest.param(ROLE_RLS + ID_A, ROLE_RLS, id="target-has-none"),
    ],
)
def test_diff_reports_pbi_id_change_apart_from_rules(local_text, remote_text):
    """Same rules, different PBI_Id: not a rule change, but still a change.

    Deploying a role under a new PBI_Id makes the service drop and recreate it.
    """
    d = diff_roles(_role("RLS", local_text), _role("RLS", remote_text))
    assert d.pbi_id_changed == ["RLS"]
    assert d.modified == []
    assert d.unchanged == []
    assert d.changed


def test_diff_same_pbi_id_is_unchanged():
    d = diff_roles(_role("RLS", ROLE_RLS + ID_A), _role("RLS", ROLE_RLS + ID_A))
    assert d.unchanged == ["RLS"]
    assert not d.changed


def test_diff_rule_change_with_new_pbi_id_is_both():
    d = diff_roles(_role("RLS", ROLE_RLS + ID_A), _role("RLS", ROLE_RLS_EDITED + ID_B))
    assert d.modified == ["RLS"]
    assert d.pbi_id_changed == ["RLS"]


def test_diff_detects_modified():
    d = diff_roles(_role("RLS", ROLE_RLS), _role("RLS", ROLE_RLS_EDITED))
    assert d.modified == ["RLS"]
    assert d.changed


def test_diff_only_local_and_only_remote():
    local = _role("RLS", ROLE_RLS)
    remote = _role("fs_rls", ROLE_FS)
    d = diff_roles(local, remote)
    assert d.only_local == ["RLS"]
    assert d.only_remote == ["fs_rls"]


# --- ref role reconciliation ---------------------------------------------


def test_reconcile_ref_roles_adds_and_drops():
    model = "model Model\n\nref role RLS\nref role stale\n"
    out = _reconcile_ref_roles(model, {"RLS", "new"})
    assert "ref role RLS" in out
    assert "ref role new" in out
    assert "ref role stale" not in out


def test_reconcile_ref_roles_appends_when_none_present():
    out = _reconcile_ref_roles("model Model\n", {"RLS"})
    assert "ref role RLS" in out


def test_reconcile_ref_roles_quotes_names_with_spaces():
    out = _reconcile_ref_roles("model Model\n", {"Finance Report Beta"})
    assert "ref role 'Finance Report Beta'" in out


# --- guard ---------------------------------------------------------------

NO_READ = object()  # the guard must not read the target at all


@pytest.mark.parametrize(
    ("repo_roles", "target", "allow", "scope", "blocked", "logged", "not_logged"),
    [
        pytest.param(
            {"RLS.tmdl": ROLE_RLS},
            _role("RLS", ROLE_RLS),
            False,
            None,
            None,
            ["reading live role definitions", "repo matches target"],
            [MEMBERS_NOTE],
            id="match-passes-without-members-note",
        ),
        pytest.param(
            {"RLS.tmdl": ROLE_RLS_EDITED},
            _role("RLS", ROLE_RLS),
            False,
            None,
            "Deploy blocked.*row-level security",
            ["deploy CHANGES"],
            [],
            id="rule-change-blocks",
        ),
        pytest.param(
            {"RLS.tmdl": ROLE_RLS},
            {**_role("RLS", ROLE_RLS), **_role("fs_rls", ROLE_FS)},
            False,
            None,
            "Deploy blocked",
            ["[deploy WIPES] role 'fs_rls'"],
            [],
            id="dropped-role-blocks",
        ),
        pytest.param(
            {"RLS.tmdl": ROLE_RLS + ID_A},
            _role("RLS", ROLE_RLS + ID_B),
            False,
            None,
            r"(?s)annotation PBI_Id.*recreate it.*wipes its member assignments",
            ["[deploy RECREATES] role 'RLS'"],
            ["deploy CHANGES"],
            id="pbi-id-change-blocks-as-membership-wipe",
        ),
        pytest.param(
            {"RLS.tmdl": ROLE_RLS},
            None,
            False,
            None,
            None,
            ["not in the target yet"],
            [],
            id="first-deploy-passes",
        ),
        pytest.param(
            {"RLS.tmdl": ROLE_RLS_EDITED},
            NO_READ,
            True,
            None,
            None,
            ["skipped", MEMBERS_NOTE],
            ["reading live role definitions"],
            id="allow-change-skips-the-read-but-says-so",
        ),
        pytest.param(
            None,
            {},
            False,
            None,
            None,
            ["reading live role definitions", "repo matches target"],
            [],
            id="no-rls-anywhere-passes",
        ),
        # A repo without roles/ deploys a model without roles: the target's go.
        pytest.param(
            None,
            _role("RLS", ROLE_RLS),
            False,
            None,
            "Deploy blocked",
            ["[deploy WIPES] role 'RLS'"],
            [],
            id="no-repo-roles-but-target-roles-blocks",
        ),
        pytest.param(
            None,
            NO_READ,
            True,
            None,
            None,
            ["skipped", MEMBERS_NOTE],
            [],
            id="no-repo-roles-allow-change-skips-but-says-so",
        ),
        pytest.param(
            {"RLS.tmdl": ROLE_RLS}, NO_READ, False, [], None, None, [], id="empty-scope-is-a-noop"
        ),
    ],
)
def test_guard(
    tmp_path, monkeypatch, caplog, repo_roles, target, allow, scope, blocked, logged, not_logged
):
    """Outcome, target reads and log lines of the deploy guard.

    `logged=None` means the guard must log nothing at all.
    """
    _make_model(tmp_path, name="M", roles=repo_roles)

    def _remote(ws, name):
        assert target is not NO_READ, "the guard must not read the target"
        return target

    monkeypatch.setattr("fabric_deploy.rls._remote_roles", _remote)
    log = logging.getLogger("test_rls_guard")
    with caplog.at_level(logging.INFO, logger=log.name):
        if blocked:
            with pytest.raises(RuntimeError, match=blocked) as exc:
                guard_deploy_rls("ws", tmp_path, scope, allow_change=allow, logger=log)
            assert MEMBERS_NOTE in str(exc.value)
        else:
            guard_deploy_rls("ws", tmp_path, scope, allow_change=allow, logger=log)

    messages = "\n".join(r.getMessage() for r in caplog.records)
    if logged is None:
        assert not caplog.records
    else:
        assert all(s in messages for s in logged), messages
    assert not any(s in messages for s in not_logged), messages


@pytest.mark.parametrize(
    ("item_types", "include_items", "expected"),
    [
        pytest.param(None, None, None, id="whole-repo"),
        pytest.param(["Notebook"], None, [], id="type-scope-without-models"),
        pytest.param(["Notebook"], ["M.SemanticModel"], [], id="model-named-but-type-excluded"),
        pytest.param(
            ["Notebook", "SemanticModel"], ["M.SemanticModel", "N.Notebook"], ["M"], id="named"
        ),
    ],
)
def test_deployer_guards_only_models_it_publishes(monkeypatch, item_types, include_items, expected):
    seen = {}
    monkeypatch.setattr("fabric_deploy.rls.guard_deploy_rls", lambda **kw: seen.update(kw))
    stand_in = SimpleNamespace(workspace_id="ws", _repo_dir_resolved=Path("."), logger=None)
    WorkspaceDeployer._guard_rls(stand_in, item_types, include_items, allow_rls_change=False)
    assert seen["model_names"] == expected


def test_guard_reads_models_concurrently(tmp_path, monkeypatch):
    """Live reads are independent round trips; they overlap instead of queueing."""
    names = ("A", "B", "C")
    for name in names:
        _make_model(tmp_path, name=name, roles={"RLS.tmdl": ROLE_RLS})
    # Every read waits until all are in flight; sequential reads break the barrier.
    barrier = threading.Barrier(len(names), timeout=10)

    def _remote(ws, name):
        barrier.wait()
        return read_local_roles(tmp_path / f"{name}.SemanticModel")

    monkeypatch.setattr("fabric_deploy.rls._remote_roles", _remote)
    guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=False)


# --- pull ----------------------------------------------------------------


@pytest.fixture
def patch_pull_env(monkeypatch):
    monkeypatch.setattr(
        "fabric_deploy.rls.FabdepConfig.from_path",
        classmethod(
            lambda cls, repo_dir, branch_override=None: SimpleNamespace(
                workspace_name="WS [MAIN]", auth=None
            )
        ),
    )
    monkeypatch.setattr("fabric_deploy.rls.apply_auth_settings", lambda *a, **k: None)
    monkeypatch.setattr("fabric_deploy.rls.get_workspace_id_by_name", lambda n: "ws-id")


def test_pull_in_sync_returns_false(tmp_path, monkeypatch, patch_pull_env):
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda ws, name: read_local_roles(tmp_path / "M.SemanticModel"),
    )
    assert pull_rls(tmp_path) is False


def test_pull_reports_drift_without_write(tmp_path, monkeypatch, patch_pull_env):
    d = _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles", lambda ws, name: _role("RLS", ROLE_RLS_EDITED)
    )
    assert pull_rls(tmp_path, write=False) is True
    # Working tree untouched on a diff-only run.
    assert (d / "definition" / "roles" / "RLS.tmdl").read_text() == ROLE_RLS


def test_pull_write_syncs_working_tree(tmp_path, monkeypatch, patch_pull_env):
    d = _make_model(
        tmp_path,
        name="M",
        roles={"RLS.tmdl": ROLE_RLS, "stale.tmdl": "role stale\n\tmodelPermission: read\n"},
        model_tmdl="model Model\n\nref role RLS\nref role stale\n",
    )
    # Target has an edited RLS and a brand-new role, and dropped 'stale'.
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda ws, name: {**_role("RLS", ROLE_RLS_EDITED), **_role("fs_rls", ROLE_FS)},
    )
    assert pull_rls(tmp_path, write=True) is True

    roles_dir = d / "definition" / "roles"
    assert "USERPRINCIPALNAME()" in (roles_dir / "RLS.tmdl").read_text()
    assert (roles_dir / "fs_rls.tmdl").exists()
    assert not (roles_dir / "stale.tmdl").exists()

    model = (d / "definition" / "model.tmdl").read_text()
    assert "ref role RLS" in model
    assert "ref role fs_rls" in model
    assert "ref role stale" not in model


def test_pull_write_adopts_target_pbi_id(tmp_path, monkeypatch, patch_pull_env):
    """Taking the target's PBI_Id is what lets the next deploy keep the role's members."""
    d = _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS + ID_A})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles", lambda ws, name: _role("RLS", ROLE_RLS + ID_B)
    )
    assert pull_rls(tmp_path, write=True) is True
    assert (d / "definition" / "roles" / "RLS.tmdl").read_text() == ROLE_RLS + ID_B


def test_pull_skips_absent_model(tmp_path, monkeypatch, patch_pull_env):
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr("fabric_deploy.rls._remote_roles", lambda ws, name: None)
    assert pull_rls(tmp_path) is False


def test_pull_requires_a_model(tmp_path, patch_pull_env):
    with pytest.raises(ValueError, match="No semantic model"):
        pull_rls(tmp_path)


def test_models_found_by_platform_type_not_folder_suffix(tmp_path, monkeypatch, patch_pull_env):
    """Items are what `.platform` says they are, whatever the folder is called."""
    d = _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    d.rename(tmp_path / "M")
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles", lambda ws, name: _role("RLS", ROLE_RLS_EDITED)
    )
    assert pull_rls(tmp_path) is True
