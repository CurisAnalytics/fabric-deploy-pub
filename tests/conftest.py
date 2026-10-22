"""Shared test fixtures for fabric-deploy."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.core.credentials import AccessToken

FAKE_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
FAKE_CAPACITY_ID = "00000000-0000-0000-0000-0000000000c0"


@pytest.fixture
def mock_fabric_api(monkeypatch):
    """Stand in for the Fabric API calls a WorkspaceDeployer makes while it is built.

    The target workspace resolves to `workspace_id` (also what a created one
    gets), the default capacity is `capacity_id`, and the other visible
    workspaces are `workspaces`: by default the sample repo's DEV workspace,
    the one its content was exported from, holding no lakehouses. The
    returned handle lets a test change any of them.
    """

    class Handle:
        workspace_id: str = FAKE_WORKSPACE_ID
        capacity_id: str = FAKE_CAPACITY_ID
        workspaces: list[dict[str, str]] = [
            {"id": SAMPLE_DEV_WORKSPACE_ID, "displayName": "Fabdep Test [DEV]"}
        ]

    handle = Handle()
    for name, fake in {
        "get_workspace_id_by_name": lambda _name: handle.workspace_id,
        "get_default_capacity_id": lambda: handle.capacity_id,
        "create_workspace": lambda workspace_name, capacity_id: handle.workspace_id,
        "list_workspaces": lambda: handle.workspaces,
        "list_workspace_items": lambda _workspace_id, _item_type=None: [],
        "patch_fabric_cicd_logger": lambda: None,
    }.items():
        monkeypatch.setattr(f"fabric_deploy.workspace.{name}", fake)
    return handle


@pytest.fixture
def fabric_cicd(monkeypatch) -> SimpleNamespace:
    """fabric-cicd's deploy entry points, mocked. `workspace` is the FabricWorkspace class."""
    fake = SimpleNamespace(
        workspace=MagicMock(name="FabricWorkspace"),
        publish=MagicMock(name="publish_all_items"),
        unpublish=MagicMock(name="unpublish_all_orphan_items"),
        flags=[],
    )
    monkeypatch.setattr("fabric_deploy.workspace.FabricWorkspace", fake.workspace)
    monkeypatch.setattr("fabric_deploy.workspace.publish_all_items", fake.publish)
    monkeypatch.setattr("fabric_deploy.workspace.unpublish_all_orphan_items", fake.unpublish)
    monkeypatch.setattr("fabric_deploy.workspace.append_feature_flag", fake.flags.append)
    return fake


def make_item(
    repo: Path,
    name: str,
    item_type: str,
    *,
    folder: str | None = None,
    logical_id: str | None = None,
    files: Mapping[str, str | dict[str, object]] | None = None,
) -> Path:
    """Create a repo item: a directory with a `.platform` declaring `name` and `item_type`.

    Args:
        repo: Workspace repo directory.
        name: The item's displayName.
        item_type: The item's type.
        folder: Item directory relative to `repo`; default `<name>.<item_type>`.
        logical_id: The `.platform` config.logicalId, if any.
        files: Files to write in the item, by path within it; a dict is written as JSON.

    Returns:
        The item directory.
    """
    item = repo / (folder or f"{name}.{item_type}")
    item.mkdir(parents=True, exist_ok=True)
    platform: dict[str, object] = {"metadata": {"type": item_type, "displayName": name}}
    if logical_id:
        platform["config"] = {"version": "2.0", "logicalId": logical_id}
    (item / ".platform").write_text(json.dumps(platform), encoding="utf-8")
    for path, content in (files or {}).items():
        target = item / path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        target.write_text(text, encoding="utf-8")
    return item


SAMPLE_WORKSPACE = Path(__file__).parent / "fixtures" / "sample-workspace"
# Ids the sample notebook's legacy lakehouse metadata carries.
SAMPLE_DEV_WORKSPACE_ID = "00000000-0000-0000-0000-0000000000de"
SAMPLE_LAKEHOUSE_ID = "00000000-0000-0000-0000-0000000000a1"


# The live tests (marked `integration`) deploy to a real workspace, so they run
# only when FABDEP_INTEGRATION_REPO names a workspace repo of your own: a git
# checkout with a fabdep.yml whose workspaces for the MAIN branch and for the
# checked-out branch are throwaway ones (the deploys unpublish every item the
# repo doesn't have). Sign in first, then:
#   FABDEP_INTEGRATION_REPO=/path/to/repo pytest -m integration
INTEGRATION_REPO_ENV = "FABDEP_INTEGRATION_REPO"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get(INTEGRATION_REPO_ENV):
        return
    skip = pytest.mark.skip(reason=f"live deploy test; set {INTEGRATION_REPO_ENV} to run it")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


def _is_live(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("integration") is not None


@pytest.fixture
def sample_ws(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A private copy of the sample workspace repo (tests may write into it).

    A live test gets a copy of the FABDEP_INTEGRATION_REPO repo instead.
    """
    source = Path(os.environ[INTEGRATION_REPO_ENV]) if _is_live(request) else SAMPLE_WORKSPACE
    dest = tmp_path / "sample-workspace"
    shutil.copytree(source, dest, symlinks=True)
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
def _isolate_azure_identity(request: pytest.FixtureRequest, monkeypatch, tmp_path):
    """Keep every test off the network, the developer's sign-in and auth settings.

    identity keeps the adopted fabdep.yml auth settings, the --auth override and
    the shared credential in module globals; resetting them here restores them at
    teardown. A test that needs tokens uses the `credential` fixture. Live
    (`integration`) tests keep the network and the developer's sign-in.
    """
    from fabric_deploy import identity

    # Under a git hook or `rebase --exec`, GIT_DIR & co. would aim the tests' scratch
    # repos at the enclosing repository.
    for var in [v for v in os.environ if v.startswith("GIT_")]:
        monkeypatch.delenv(var)
    monkeypatch.setattr(identity, "_settings", None)
    monkeypatch.setattr(identity, "_mode_flag", None)
    monkeypatch.setattr(identity, "_credential", None)
    if _is_live(request):
        return

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

    def _no_real_credential(mode: str, tenant_id: str | None) -> None:
        raise AssertionError(f"test tried to sign in for real ({mode} credential)")

    monkeypatch.setattr(identity, "_new_credential", _no_real_credential)
