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
                workspace_name="WS [MAIN]", tenant=None
            )
        ),
    )
    monkeypatch.setattr("fabric_deploy.refresh.apply_tenant_pin", lambda *a, **k: None)
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


def test_frame_models_syncs_sql_endpoint(patch_api, monkeypatch, tmp_path):
    calls = {"sync": 0}
    monkeypatch.setattr(
        "fabric_deploy.refresh.list_workspace_items",
        lambda ws, t: [{"id": "ep", "displayName": "lh"}],
    )

    def _sync(ws, ep, timeout_s=600):
        calls["sync"] += 1
        return [{"tableName": "t", "status": "Success"}]

    monkeypatch.setattr("fabric_deploy.refresh.refresh_sql_endpoint_metadata", _sync)
    monkeypatch.setattr(
        "fabric_deploy.refresh.refresh_semantic_model",
        lambda ws, ds, timeout_s=900: {"status": "Completed"},
    )
    frame_models(tmp_path, models=["My Model"], sync=True)
    assert calls["sync"] == 1
