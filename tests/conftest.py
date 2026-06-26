"""Shared test fixtures for fabric-deploy."""

from __future__ import annotations

from unittest.mock import patch

import pytest


FAKE_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
FAKE_CAPACITY_ID = "00000000-0000-0000-0000-0000000000c0"


@pytest.fixture
def mock_fabric_api(monkeypatch):
    """Patch the Fabric REST API surface used during deployer construction.

    Patches both `fabric_deploy.client.*` (the source) and
    `fabric_deploy.workspace.*` (the call site, since `workspace.py` imports
    these names directly). Returns a small handle so tests can override the
    workspace ID per test if needed.
    """

    class Handle:
        workspace_id: str = FAKE_WORKSPACE_ID
        capacity_id: str = FAKE_CAPACITY_ID

    handle = Handle()

    def _get_workspace_id_by_name(_name: str) -> str:
        return handle.workspace_id

    def _get_default_capacity_id() -> str:
        return handle.capacity_id

    def _create_workspace(workspace_name: str, capacity_id: str, description=None) -> str:
        return handle.workspace_id

    # Patch the call sites in workspace.py (imported by name)
    monkeypatch.setattr(
        "fabric_deploy.workspace.get_workspace_id_by_name",
        _get_workspace_id_by_name,
    )
    monkeypatch.setattr(
        "fabric_deploy.workspace.get_default_capacity_id",
        _get_default_capacity_id,
    )
    monkeypatch.setattr(
        "fabric_deploy.workspace.create_workspace",
        _create_workspace,
    )

    # Also patch the source in api.py for tests that import from there directly
    monkeypatch.setattr(
        "fabric_deploy.client.get_workspace_id_by_name",
        _get_workspace_id_by_name,
    )
    monkeypatch.setattr(
        "fabric_deploy.client.get_default_capacity_id",
        _get_default_capacity_id,
    )

    # Suppress fabric-cicd module-level logging side effects during construction
    with patch("fabric_deploy.workspace.patch_fabric_cicd_logger"):
        yield handle
