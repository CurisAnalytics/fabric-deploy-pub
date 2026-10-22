"""Shared test fixtures for fabric-deploy."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path
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

    def _create_workspace(workspace_name: str, capacity_id: str) -> str:
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
    monkeypatch.setattr("fabric_deploy.workspace.list_workspaces", lambda: [])

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


SAMPLE_WORKSPACE = Path(__file__).parent / "fixtures" / "sample-workspace"
# Ids the sample notebook's legacy lakehouse metadata carries.
SAMPLE_DEV_WORKSPACE_ID = "00000000-0000-0000-0000-0000000000de"
SAMPLE_LAKEHOUSE_ID = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def sample_ws(tmp_path) -> Path:
    """A private copy of the sample workspace repo (tests may write into it)."""
    dest = tmp_path / "sample-workspace"
    shutil.copytree(SAMPLE_WORKSPACE, dest)
    return dest


@pytest.fixture(autouse=True)
def _isolate_azure_identity(monkeypatch, tmp_path):
    """Keep every test off the network, the developer's az login, fabdep profile and env.

    apply_tenant_pin writes os.environ["AZURE_CONFIG_DIR"] and the module-level
    pin/credential caches directly; recording them here restores both at teardown.
    """
    from fabric_deploy import identity

    # An unset AZURE_CONFIG_DIR means ~/.azure: point az at an empty one instead.
    (tmp_path / "azure-config").mkdir()
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path / "azure-config"))

    connect = socket.socket.connect

    def _loopback_only(sock: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else None
        if sock.family != socket.AF_UNIX and host not in ("127.0.0.1", "::1", "localhost"):
            pytest.fail(f"test tried to reach the network: {address!r}")
        connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", _loopback_only)
    for var in ("FABRIC_BEARER_TOKEN", "POWERBI_BEARER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(identity, "_ACTIVE_PIN", None)
    monkeypatch.setattr(identity, "_CLI_CREDENTIAL", None)
    monkeypatch.setattr(identity, "_list_accounts", lambda: [])
    monkeypatch.setattr(
        "fabric_deploy.profile.FABDEP_PROFILE_PATH", tmp_path / "fabdep-profile.json"
    )
