"""Shared test fixtures for fabric-deploy."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from azure.core.credentials import AccessToken

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


CONTOSO_TENANT = "00000000-0000-0000-0000-0000000000e1"
FABRIKAM_TENANT = "00000000-0000-0000-0000-0000000000e2"
APP_ID = "00000000-0000-0000-0000-0000000000a5"


def make_jwt(**claims: object) -> str:
    """An unsigned JWT carrying `claims` (fabdep reads token claims, never verifies them)."""
    claims = {"exp": int(time.time()) + 3600, **claims}

    def part(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class FakeCredential:
    """An azure-identity credential stand-in that mints JWTs carrying `claims`."""

    def __init__(self, **claims: object) -> None:
        self.claims = claims
        self.lifetime = 3600
        self.calls: list[tuple[str, ...]] = []
        self.built: list[tuple[str, str | None]] = []  # (mode, tenant) per credential built

    def get_token(self, *scopes: str, **_: object) -> AccessToken:
        self.calls.append(scopes)
        exp = int(time.time()) + self.lifetime
        return AccessToken(make_jwt(**{**self.claims, "exp": exp}), exp)


@pytest.fixture
def credential(monkeypatch) -> FakeCredential:
    """Serve every credential mode from one FakeCredential: a user in the Contoso tenant."""
    from fabric_deploy import identity

    fake = FakeCredential(
        tid=CONTOSO_TENANT, idtyp="user", upn="user@contoso.com", scp="user_impersonation"
    )

    def _new_credential(mode: str, tenant_id: str | None) -> FakeCredential:
        fake.built.append((mode, tenant_id))
        return fake

    monkeypatch.setattr(identity, "_new_credential", _new_credential)
    return fake


@pytest.fixture(autouse=True)
def _isolate_azure_identity(monkeypatch, tmp_path):
    """Keep every test off the network, the developer's sign-in and auth settings.

    identity keeps the adopted fabdep.yml pin, the --auth override and the shared
    credential in module globals; resetting them here restores them at teardown.
    A test that needs tokens uses the `credential` fixture.
    """
    from fabric_deploy import identity

    for var in ("FABRIC_BEARER_TOKEN", "POWERBI_BEARER_TOKEN", "FABDEP_AUTH"):
        monkeypatch.delenv(var, raising=False)
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
    monkeypatch.setattr(identity, "_pin", None)
    monkeypatch.setattr(identity, "_mode_flag", None)
    monkeypatch.setattr(identity, "_credential", None)

    def _no_real_credential(mode: str, tenant_id: str | None) -> None:
        raise AssertionError(f"test tried to sign in for real ({mode} credential)")

    monkeypatch.setattr(identity, "_new_credential", _no_real_credential)
