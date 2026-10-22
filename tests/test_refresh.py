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


def test_extract_none_on_completed():
    assert extract_refresh_error({"status": "Completed"}) is None


def test_extract_none_on_missing_status():
    assert extract_refresh_error({}) is None


def test_extract_parses_service_exception_json():
    detail = {
        "status": "Failed",
        "serviceExceptionJson": json.dumps(
            {"errorCode": "DMTS_X", "errorDescription": "column missing"}
        ),
    }
    out = extract_refresh_error(detail)
    assert "DMTS_X" in out
    assert "column missing" in out


def test_extract_falls_back_to_messages():
    detail = {
        "status": "Failed",
        "messages": [{"code": "0xC11C0020", "message": "evicted"}],
    }
    out = extract_refresh_error(detail)
    assert "0xC11C0020" in out and "evicted" in out


def test_extract_prefers_refresh_attempt_over_thin_top_level():
    """Real PBI failures put the rich error in refreshAttempts[]; the top-level
    serviceExceptionJson is often just {"errorCode": ...}."""
    detail = {
        "status": "Failed",
        "serviceExceptionJson": json.dumps(
            {"errorCode": "ModelRefresh_ShortMessage_ProcessingError"}
        ),
        "refreshAttempts": [
            {
                "serviceExceptionJson": json.dumps(
                    {"errorCode": "0xC112", "errorDescription": "real cause here"}
                )
            }
        ],
    }
    out = extract_refresh_error(detail)
    assert "real cause here" in out
    assert "0xC112" in out


def test_extract_errorcode_only_does_not_echo_twice():
    """errorCode-only top-level must not return 'CODE: {json blob}'."""
    detail = {
        "status": "Failed",
        "serviceExceptionJson": json.dumps({"errorCode": "ProcessingError"}),
    }
    out = extract_refresh_error(detail)
    assert out == "ProcessingError"


def test_extract_messages_skips_warnings():
    detail = {
        "status": "Failed",
        "messages": [
            {"code": "W1", "message": "just a warning", "type": "Warning"},
            {"code": "E1", "message": "the real error", "type": "Error"},
        ],
    }
    out = extract_refresh_error(detail)
    assert "the real error" in out
    assert "just a warning" not in out


def test_extract_falls_back_to_status():
    assert "Failed" in extract_refresh_error({"status": "Failed"})


def test_extract_handles_malformed_service_exception_json():
    detail = {"status": "Failed", "serviceExceptionJson": "not json {"}
    assert extract_refresh_error(detail) == "not json {"


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
