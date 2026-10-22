"""`fabdep auth status`, the --auth flag, and the auth guidance in handle_command_error."""

from __future__ import annotations

import sys

import pytest

from fabric_deploy.cli import handle_command_error, main, parse_args
from fabric_deploy.identity import AuthError

from .conftest import APP_ID, CONTOSO_TENANT, FABRIKAM_TENANT, make_jwt


def _run(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["fabdep", *argv])
    main()


class TestAuthFlag:
    @pytest.mark.parametrize(
        "argv",
        [
            ["--auth", "env"],
            ["deploy", "--auth", "env"],
            ["-t", "Notebook", "--auth", "env"],
            ["refresh", "--auth", "env"],
            ["rls", "pull", "--auth", "env"],
            ["auth", "status", "--auth", "env"],
        ],
    )
    def test_accepted_by_commands_that_call_fabric(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", ["fabdep", *argv])
        assert parse_args().auth == "env"

    def test_rejects_an_unknown_mode(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["fabdep", "deploy", "--auth", "azcli"])
        with pytest.raises(SystemExit):
            _ = parse_args()


class TestAuthStatus:
    def test_user(self, tmp_path, monkeypatch, capsys, credential):
        (tmp_path / "fabdep.yml").write_text(f"tenant: {CONTOSO_TENANT}\n")
        _run(monkeypatch, "auth", "status", str(tmp_path))
        out = capsys.readouterr().out
        assert "cli (default)" in out
        assert "user@contoso.com" in out
        assert "user (delegated)" in out
        assert f"{CONTOSO_TENANT} (pinned in fabdep.yml)" in out

    def test_service_principal_via_flag(self, tmp_path, monkeypatch, capsys, credential):
        credential.claims = {"tid": CONTOSO_TENANT, "idtyp": "app", "appid": APP_ID}
        _run(monkeypatch, "auth", "status", str(tmp_path), "--auth", "managed")
        out = capsys.readouterr().out
        assert "managed (--auth)" in out
        assert f"app {APP_ID}" in out
        assert "app-only" in out
        assert f"{CONTOSO_TENANT} (fabdep.yml pins no tenant)" in out

    def test_relayed_token(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(
            "FABRIC_BEARER_TOKEN", make_jwt(tid=CONTOSO_TENANT, upn="ci@contoso.com")
        )
        _run(monkeypatch, "auth", "status", str(tmp_path))
        assert "token relayed in FABRIC_BEARER_TOKEN" in capsys.readouterr().out

    def test_refused_identity_exits_nonzero(self, tmp_path, monkeypatch, capsys, credential):
        credential.claims["tid"] = FABRIKAM_TENANT
        (tmp_path / "fabdep.yml").write_text(f"tenant: {CONTOSO_TENANT}\n")
        with pytest.raises(SystemExit) as exc:
            _run(monkeypatch, "auth", "status", str(tmp_path))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert f"pins tenant {CONTOSO_TENANT}" in captured.out + captured.err


class TestHandleCommandError:
    @pytest.fixture(autouse=True)
    def _no_subprocess(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("ran a subprocess"))

    def _output(self, capsys, error: Exception) -> str:
        with pytest.raises(SystemExit) as exc:
            handle_command_error(error, "Deployment")
        assert exc.value.code == 1
        captured = capsys.readouterr()
        return captured.out + captured.err

    def test_refused_request_explains_access(self, capsys):
        out = self._output(capsys, RuntimeError("Failed to list workspaces (HTTP 403)."))
        assert "fabdep auth status" in out
        assert "Contributor" in out
        assert "az login --tenant" in out

    def test_refused_relayed_token_points_at_the_relay(self, capsys, monkeypatch):
        monkeypatch.setenv("FABRIC_BEARER_TOKEN", make_jwt())
        out = self._output(capsys, RuntimeError("Failed to list workspaces (HTTP 401)."))
        assert "token relayed in FABRIC_BEARER_TOKEN" in out
        assert "az login" not in out

    def test_sign_in_error_explains_itself(self, capsys):
        out = self._output(capsys, AuthError("The cli credential could not get a token."))
        assert "could not get a token" in out
        assert "Contributor" not in out

    def test_expired_secret_says_to_issue_a_new_one(self, capsys):
        error = RuntimeError("AADSTS7000222: The provided client secret keys are expired.")
        assert "new client secret" in self._output(capsys, error)

    def test_other_failures_get_no_auth_guidance(self, capsys):
        out = self._output(capsys, RuntimeError("Item 'Token Login' not found"))
        assert "fabdep auth status" not in out
