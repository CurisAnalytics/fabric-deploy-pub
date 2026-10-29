"""Unit tests for the Direct Lake framing orchestrator (refresh.py).

The pure helpers (error extraction, table-sync triage) are tested directly;
the orchestrator is tested with every network call mocked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fabric_deploy.refresh import (
    extract_refresh_error,
    failed_table_syncs,
    frame_models,
)

# --- failed_table_syncs --------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        # Only Failure rows are returned; Success/NotRun are not failures.
        (
            [
                {"tableName": "ok", "status": "Success"},
                {"tableName": "skipped", "status": "NotRun"},
                {"tableName": "bad", "status": "Failure", "error": {"message": "token problem"}},
            ],
            [("bad", "token problem")],
        ),
        ([{"tableName": "t", "status": "Success"}], []),
    ],
)
def test_failed_table_syncs(statuses, expected):
    assert failed_table_syncs(statuses) == expected


# --- extract_refresh_error -----------------------------------------------


def _exc(**fields):
    return json.dumps(fields)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        pytest.param({"status": "Completed"}, None, id="completed"),
        pytest.param({}, None, id="no-status"),
        pytest.param(
            {"status": "Failed", "serviceExceptionJson": _exc(errorCode="X", errorDescription="d")},
            "X: d",
            id="service-exception-json",
        ),
        # The top-level serviceExceptionJson is often just {"errorCode": ...};
        # the per-attempt one carries the description.
        pytest.param(
            {
                "status": "Failed",
                "serviceExceptionJson": _exc(errorCode="ProcessingError"),
                "refreshAttempts": [
                    {"serviceExceptionJson": _exc(errorCode="0xC112", errorDescription="cause")}
                ],
            },
            "0xC112: cause",
            id="refresh-attempt-beats-thin-top-level",
        ),
        pytest.param(
            {"status": "Failed", "serviceExceptionJson": _exc(errorCode="ProcessingError")},
            "ProcessingError",
            id="error-code-only-not-echoed-twice",
        ),
        pytest.param(
            {"status": "Failed", "serviceExceptionJson": "not json {"},
            "not json {",
            id="malformed-service-exception-json",
        ),
        pytest.param(
            {"status": "Failed", "messages": [{"code": "0xC11C0020", "message": "evicted"}]},
            "0xC11C0020 evicted",
            id="messages",
        ),
        pytest.param(
            {
                "status": "Failed",
                "messages": [
                    {"code": "W1", "message": "a warning", "type": "Warning"},
                    {"code": "E1", "message": "the error", "type": "Error"},
                ],
            },
            "E1 the error",
            id="messages-skip-warnings",
        ),
        pytest.param({"status": "Failed"}, "Refresh status: Failed", id="bare-status"),
    ],
)
def test_extract_refresh_error(detail, expected):
    assert extract_refresh_error(detail) == expected


# --- frame_models (mocked) -----------------------------------------------


@pytest.fixture
def patch_api(monkeypatch):
    """Mock the whole network surface frame_models touches."""
    monkeypatch.setattr(
        "fabric_deploy.refresh.FabdepConfig.from_path",
        classmethod(
            lambda cls, repo_dir, branch_override=None: SimpleNamespace(
                workspace_name="WS [MAIN]", auth=None
            )
        ),
    )
    monkeypatch.setattr("fabric_deploy.refresh.apply_auth_settings", lambda *a, **k: None)
    monkeypatch.setattr("fabric_deploy.refresh.get_workspace_id_by_name", lambda name: "ws-id")
    monkeypatch.setattr(
        "fabric_deploy.refresh.find_item_by_name",
        lambda ws, t, name=None: {"id": "ds-id", "displayName": name or "M"},
    )


def test_frame_models_success(patch_api, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_semantic_model",
        lambda ws, ds, timeout_s=900: {"status": "Completed"},
    )
    details = frame_models(tmp_path, models=["My Model"], sync=False)
    assert details == [{"status": "Completed"}]


def test_frame_models_raises_first_framing_error(patch_api, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_semantic_model",
        lambda ws, ds, timeout_s=900: {
            "status": "Failed",
            "serviceExceptionJson": json.dumps({"errorCode": "E", "error": "boom"}),
        },
    )
    with pytest.raises(RuntimeError, match="Framing failed.*boom"):
        frame_models(tmp_path, models=["My Model"], sync=False)


def test_frame_models_strips_semanticmodel_suffix(patch_api, monkeypatch, tmp_path):
    """`refresh -i 'X.SemanticModel'` (deploy-style) resolves the same as 'X'."""
    seen: list[str] = []

    def _find(ws, t, name=None):
        seen.append(name)
        return {"id": "ds", "displayName": name}

    monkeypatch.setattr("fabric_deploy.refresh.find_item_by_name", _find)
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_semantic_model",
        lambda ws, ds, timeout_s=900: {"status": "Completed"},
    )
    frame_models(tmp_path, models=["Northwind Sales Model.SemanticModel"], sync=False)
    assert seen == ["Northwind Sales Model"]


def test_frame_models_requires_a_target(patch_api, tmp_path):
    # Empty repo + no explicit models -> nothing to frame.
    with pytest.raises(ValueError, match="No semantic model"):
        frame_models(tmp_path, sync=False)


EP1, EP2, EP3 = (f"00000000-0000-0000-0000-0000000000e{i}" for i in (1, 2, 3))


def _definition(mode: str, *endpoint_ids: str) -> dict[str, str]:
    """getDefinition parts for a model with one table in `mode` over the endpoints."""
    expressions = "".join(
        f"expression Source{i} =\n\t\tlet\n\t\t\tdatabase = Sql.Database("
        f'"contoso.datawarehouse.fabric.microsoft.com", "{ep}")\n\t\tin\n\t\t\tdatabase\n\n'
        for i, ep in enumerate(endpoint_ids)
    )
    table = f"table t\n\tpartition t = entity\n\t\tmode: {mode}\n\t\tsource\n\t\t\tentityName: t\n"
    return {"definition/expressions.tmdl": expressions, "definition/tables/t.tmdl": table}


@pytest.fixture
def frame_env(patch_api, monkeypatch):
    """Workspace with endpoints EP1-EP3; records syncs and refreshes in call order."""
    env = SimpleNamespace(definitions={}, synced=[], refreshed=[], definition_reads=[])
    monkeypatch.setattr(
        "fabric_deploy.refresh.find_item_by_name",
        lambda ws, t, name=None: {"id": name, "displayName": name},
    )

    def _get_definition(ws, item_id):
        env.definition_reads.append(item_id)
        return env.definitions[item_id]

    monkeypatch.setattr("fabric_deploy.refresh.get_semantic_model_definition", _get_definition)
    monkeypatch.setattr(
        "fabric_deploy.refresh.list_workspace_items",
        lambda ws, t: [{"id": ep, "displayName": f"lh{ep[-1]}"} for ep in (EP1, EP2, EP3)],
    )
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_sql_endpoint_metadata",
        lambda ws, ep, timeout_s=600: env.synced.append(ep) or [],
    )
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_semantic_model",
        lambda ws, ds, timeout_s=900: env.refreshed.append(ds) or {"status": "Completed"},
    )
    return env


def test_frame_models_syncs_every_endpoint_the_direct_lake_models_read(frame_env, tmp_path):
    frame_env.definitions = {
        "A": _definition("directLake", EP2),
        "B": _definition("directLake", EP1, EP2),
    }
    frame_models(tmp_path, models=["A", "B"], sync=True)
    assert frame_env.synced == [EP1, EP2]  # each once, EP3 is not read by either model
    assert frame_env.refreshed == ["A", "B"]


def test_frame_models_fails_on_a_failed_table_sync(frame_env, tmp_path, monkeypatch):
    """The model would refresh against stale tables, or fail on them."""
    frame_env.definitions = {"M": _definition("directLake", EP1)}
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_sql_endpoint_metadata",
        lambda ws, ep, timeout_s=600: [
            {"tableName": "orders", "status": "Success"},
            {"tableName": "customers", "status": "Failure", "error": {"message": "bad schema"}},
        ],
    )
    with pytest.raises(RuntimeError, match="(?s)lh1.*customers: bad schema"):
        frame_models(tmp_path, models=["M"], sync=True)
    assert frame_env.refreshed == []


def test_frame_models_import_model_refreshes_without_sync(frame_env, tmp_path, caplog):
    frame_env.definitions = {"M": _definition("import", EP1)}
    with caplog.at_level("INFO"):
        frame_models(tmp_path, models=["M"], sync=True)
    assert frame_env.synced == []
    assert frame_env.refreshed == ["M"]
    assert "no Direct Lake tables" in caplog.text


def test_frame_models_reports_endpoint_outside_the_workspace(frame_env, tmp_path, caplog):
    foreign = "00000000-0000-0000-0000-0000000000f1"
    frame_env.definitions = {"M": _definition("directLake", EP1, foreign)}
    with caplog.at_level("INFO"):
        frame_models(tmp_path, models=["M"], sync=True)
    assert frame_env.synced == [EP1]
    assert foreign in caplog.text


def test_frame_models_no_sync_reads_no_definition(frame_env, tmp_path):
    frame_env.definitions = {"M": _definition("directLake", EP1)}
    frame_models(tmp_path, models=["M"], sync=False)
    assert frame_env.definition_reads == []
    assert frame_env.synced == []
    assert frame_env.refreshed == ["M"]


def test_frame_models_defaults_to_repo_models_by_platform_type(patch_api, monkeypatch, tmp_path):
    """Without -i, every SemanticModel the repo's `.platform` files declare is framed."""
    for folder, name, item_type in [
        ("B.SemanticModel", "B", "SemanticModel"),
        ("a-model", "A", "SemanticModel"),
        ("lh.Lakehouse", "lh", "Lakehouse"),
    ]:
        (tmp_path / folder).mkdir()
        (tmp_path / folder / ".platform").write_text(
            json.dumps({"metadata": {"displayName": name, "type": item_type}})
        )
    refreshed: list[str] = []
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_semantic_model",
        lambda ws, ds, timeout_s=900: refreshed.append(ds) or {"status": "Completed"},
    )
    monkeypatch.setattr(
        "fabric_deploy.refresh.find_item_by_name",
        lambda ws, t, name=None: {"id": name, "displayName": name},
    )
    frame_models(tmp_path, sync=False)
    assert refreshed == ["A", "B"]
