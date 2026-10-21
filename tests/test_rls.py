"""Unit tests for the RLS round-trip (rls.py).

Pure helpers (role extraction, diff classification, ref-role reconciliation)
are tested directly; the guard and `pull` orchestrators are tested with the
network surface (`_remote_roles`) mocked.
"""

from __future__ import annotations

import json
import logging
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

ROLE_RLS = "role RLS\n\tmodelPermission: read\n\n\ttablePermission contact = [User Name] = USERNAME()\n"
ROLE_RLS_EDITED = ROLE_RLS.replace("USERNAME()", "USERPRINCIPALNAME()")
ROLE_FS = "role fs_rls\n\tmodelPermission: read\n"


def _make_model(tmp_path, name="M", roles=None, model_tmdl=None):
    """Create a `.SemanticModel` repo dir with an optional roles/ folder."""
    d = tmp_path / f"{name}.SemanticModel"
    d.mkdir()
    (d / ".platform").write_text(
        json.dumps({"metadata": {"displayName": name, "type": "SemanticModel"}})
    )
    defn = d / "definition"
    defn.mkdir()
    (defn / "model.tmdl").write_text(model_tmdl or "model Model\n\nref role RLS\n")
    if roles:
        rdir = defn / "roles"
        rdir.mkdir()
        for fname, text in roles.items():
            (rdir / fname).write_text(text)
    return d


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


def test_diff_ignores_pbi_id_annotation():
    """Identical security rule, different per-env PBI_Id GUID -> not drift.

    Mirrors the real Woodgrove dev-vs-prod case: same tablePermission, different
    annotation PBI_Id. Must read as unchanged so a legit deploy isn't blocked.
    """
    base = (
        'role GOTHAM\n\tmodelPermission: read\n\n'
        '\ttablePermission V_REGIONS = [region] == "GOTHAM CITY"\n\n'
        "\tannotation PBI_Id = "
    )
    local = _role("GOTHAM", base + "0123456789abcdef0123456789abcdef")
    remote = _role("GOTHAM", base + "fedcba9876543210fedcba9876543210")
    assert not diff_roles(local, remote).changed


def test_diff_detects_real_rule_change_despite_pbi_id():
    """A genuine DAX change is still caught even with PBI_Id stripped."""
    a = _role("GOTHAM", 'role GOTHAM\n\ttablePermission t = [x] == "A"\n\tannotation PBI_Id = 1\n')
    b = _role("GOTHAM", 'role GOTHAM\n\ttablePermission t = [x] == "B"\n\tannotation PBI_Id = 2\n')
    assert diff_roles(a, b).modified == ["GOTHAM"]


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


def test_guard_skips_model_without_local_roles(tmp_path, monkeypatch):
    """A model with no roles in the repo must not trigger any API call."""
    _make_model(tmp_path, name="M", roles=None)

    def _boom(*a, **k):
        raise AssertionError("remote roles should not be fetched")

    monkeypatch.setattr("fabric_deploy.rls._remote_roles", _boom)
    guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=False)


def test_guard_empty_model_names_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )
    guard_deploy_rls("ws", tmp_path, model_names=[], allow_change=False)


def test_guard_passes_when_rls_matches(tmp_path, monkeypatch):
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles", lambda ws, name: read_local_roles(tmp_path / "M.SemanticModel")
    )
    guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=False)


def test_guard_blocks_on_drift(tmp_path, monkeypatch):
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS_EDITED})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles", lambda ws, name: _role("RLS", ROLE_RLS)
    )
    with pytest.raises(RuntimeError, match="Deploy blocked.*row-level security"):
        guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=False)


def test_guard_allows_drift_with_flag(tmp_path, monkeypatch):
    """--allow-rls-change skips the check outright — no live read, no block.

    The flag already decides the outcome (overwrite), so paying ~30s/model to
    diff the roles could only produce a warning about a settled decision.
    """
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS_EDITED})

    def _boom(*a, **k):
        raise AssertionError("remote roles should not be fetched")

    monkeypatch.setattr("fabric_deploy.rls._remote_roles", _boom)
    guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=True)


def test_guard_with_allow_change_announces_the_skip(tmp_path, monkeypatch, caplog):
    """Skipping is still reported — silence would read as 'checked, and clean'."""
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )
    with caplog.at_level(logging.INFO, logger="test_rls_guard"):
        guard_deploy_rls(
            "ws", tmp_path, model_names=None, allow_change=True,
            logger=logging.getLogger("test_rls_guard"),
        )
    assert any("skipped" in r.message for r in caplog.records)
    assert not any("reading live role definitions" in r.message for r in caplog.records)


def test_guard_with_allow_change_silent_without_repo_rls(tmp_path, monkeypatch, caplog):
    """No RLS in the repo -> nothing is being overwritten, so say nothing."""
    _make_model(tmp_path, name="M", roles=None)
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )
    with caplog.at_level(logging.INFO, logger="test_rls_guard"):
        guard_deploy_rls(
            "ws", tmp_path, model_names=None, allow_change=True,
            logger=logging.getLogger("test_rls_guard"),
        )
    assert not caplog.records


def test_guard_skips_when_model_absent_in_target(tmp_path, monkeypatch):
    """First deploy: the model isn't in the target yet -> nothing to protect."""
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr("fabric_deploy.rls._remote_roles", lambda ws, name: None)
    guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=False)


def test_guard_announces_fetch_cost_before_reading_target(tmp_path, monkeypatch, caplog):
    """The live-definition read is slow — say so before starting."""
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda ws, name: read_local_roles(tmp_path / "M.SemanticModel"),
    )
    with caplog.at_level(logging.INFO, logger="test_rls_guard"):
        guard_deploy_rls(
            "ws", tmp_path, model_names=None, allow_change=False,
            logger=logging.getLogger("test_rls_guard"),
        )
    assert any("reading live role definitions" in r.message for r in caplog.records)


def test_guard_clean_check_omits_members_note(tmp_path, monkeypatch, caplog):
    """When repo matches target there is no RLS change afoot — skip the note."""
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda ws, name: read_local_roles(tmp_path / "M.SemanticModel"),
    )
    with caplog.at_level(logging.INFO, logger="test_rls_guard"):
        guard_deploy_rls(
            "ws", tmp_path, model_names=None, allow_change=False,
            logger=logging.getLogger("test_rls_guard"),
        )
    assert not any(MEMBERS_NOTE in r.message for r in caplog.records)


def test_guard_permitted_change_logs_members_note(tmp_path, monkeypatch, caplog):
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS_EDITED})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles", lambda ws, name: _role("RLS", ROLE_RLS)
    )
    with caplog.at_level(logging.INFO, logger="test_rls_guard"):
        guard_deploy_rls(
            "ws", tmp_path, model_names=None, allow_change=True,
            logger=logging.getLogger("test_rls_guard"),
        )
    assert any(MEMBERS_NOTE in r.message for r in caplog.records)


def test_guard_no_local_rls_stays_silent(tmp_path, monkeypatch, caplog):
    """No repo RLS -> no API call, so no fetch-cost announcement either."""
    _make_model(tmp_path, name="M", roles=None)
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
    )
    with caplog.at_level(logging.INFO, logger="test_rls_guard"):
        guard_deploy_rls(
            "ws", tmp_path, model_names=None, allow_change=False,
            logger=logging.getLogger("test_rls_guard"),
        )
    assert not caplog.records


def test_guard_blocks_on_wipe(tmp_path, monkeypatch):
    """Repo has a role the target lost track of? No — repo drops one the target has."""
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles",
        lambda ws, name: {**_role("RLS", ROLE_RLS), **_role("fs_rls", ROLE_FS)},
    )
    with pytest.raises(RuntimeError, match="Deploy blocked"):
        guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=False)


# --- pull ----------------------------------------------------------------


@pytest.fixture
def patch_pull_env(monkeypatch):
    monkeypatch.setattr(
        "fabric_deploy.rls.FabdepConfig.from_path",
        classmethod(
            lambda cls, repo_dir, branch_override=None: SimpleNamespace(
                workspace_name="WS [MAIN]", tenant=None
            )
        ),
    )
    monkeypatch.setattr("fabric_deploy.rls.apply_tenant_pin", lambda *a, **k: None)
    monkeypatch.setattr("fabric_deploy.rls.get_workspace_id_by_name", lambda n: "ws-id")


def test_pull_in_sync_returns_false(tmp_path, monkeypatch, patch_pull_env):
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr(
        "fabric_deploy.rls._remote_roles", lambda ws, name: read_local_roles(tmp_path / "M.SemanticModel")
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


def test_pull_skips_absent_model(tmp_path, monkeypatch, patch_pull_env):
    _make_model(tmp_path, name="M", roles={"RLS.tmdl": ROLE_RLS})
    monkeypatch.setattr("fabric_deploy.rls._remote_roles", lambda ws, name: None)
    assert pull_rls(tmp_path) is False


def test_pull_requires_a_model(tmp_path, patch_pull_env):
    with pytest.raises(ValueError, match="No semantic model"):
        pull_rls(tmp_path)


def test_members_note_mentions_entra():
    assert "Entra" in MEMBERS_NOTE and "membership" in MEMBERS_NOTE.lower()


def test_guard_reads_models_concurrently(tmp_path, monkeypatch):
    """Live reads are independent round trips; they overlap instead of queueing."""
    import threading
    import time

    for name in ("A", "B", "C"):
        _make_model(tmp_path, name=name, roles={"RLS.tmdl": ROLE_RLS})
    active = 0
    peak = 0
    lock = threading.Lock()

    def _slow_remote(ws, name):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return read_local_roles(tmp_path / f"{name}.SemanticModel")

    monkeypatch.setattr("fabric_deploy.rls._remote_roles", _slow_remote)
    guard_deploy_rls("ws", tmp_path, model_names=None, allow_change=False)
    assert peak > 1
