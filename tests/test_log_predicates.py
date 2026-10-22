"""log.py: error classification and console output.

Auth checks read HTTP statuses structurally: a status attribute
anywhere in the exception chain, or an `HTTP nnn` marker in the message.
Everything else is a plain message predicate.
"""

from __future__ import annotations

import io
import logging
import re
import sys
import urllib.error

import pytest
from azure.core.exceptions import ClientAuthenticationError

from fabric_deploy.identity import AuthError
from fabric_deploy.log import (
    error_hint,
    get_logger,
    http_status,
    is_auth_error,
    is_connection_error,
    is_expired_credential_error,
    patch_fabric_cicd_logger,
    print_error,
    use_color,
)


class _StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__("request failed")
        self.status_code = status


def _raised_from(outer: str, cause: BaseException) -> RuntimeError:
    """A RuntimeError(outer) whose __cause__ is `cause`, as `raise ... from` leaves it."""
    try:
        try:
            raise cause
        except BaseException as e:
            raise RuntimeError(outer) from e
    except RuntimeError as e:
        return e


# --- http_status ---------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("Failed to list workspaces (HTTP 403).", 403),
        (_StatusError(429), 429),
        (urllib.error.HTTPError("https://x", 401, "Unauthorized", None, None), 401),  # type: ignore[arg-type]
        (_raised_from("deploy failed", _StatusError(503)), 503),
        ("Plan 401k rollout failed", None),
        (RuntimeError("no status here"), None),
    ],
)
def test_http_status(error, status):
    assert http_status(error) == status


# --- is_auth_error -------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        "Failed to list workspaces (HTTP 401).",
        'Failed to create workspace (HTTP 403).\n{"errorCode": "InsufficientPrivileges"}',
        "The executing identity is not authorized to call GET on 'https://x/v1/workspaces'.",
        "Failed to acquire Microsoft Entra token. Please run 'az login' to set up an account",
        "AADSTS7000222: The provided client secret keys for app 'x' are expired.",
        _StatusError(403),
        ClientAuthenticationError("no token"),
        AuthError("token is for another tenant"),
        _raised_from("deploy failed", ClientAuthenticationError("no token")),
    ],
)
def test_is_auth_error_true(error):
    assert is_auth_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        "Workspace 'Token Sales [DEV]' not found",  # words, not statuses
        "Login page item failed to publish: credential connection missing",
        "Failed to get item (HTTP 404).",
        "Refresh of model 'Plan 401k' failed",
        "Internal server error",
        _StatusError(404),
        "",
    ],
)
def test_is_auth_error_false(error):
    assert is_auth_error(error) is False


# --- is_expired_credential_error ------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        # As `az` prints it.
        (
            "ERROR: AADSTS7000222: The provided client secret keys for app "
            "'bbbbbbbb-...' are expired. Visit the Azure portal to create new keys."
        ),
        "AADSTS700082: The refresh token has expired due to inactivity.",
        "AADSTS70043: The refresh token has expired or is invalid.",
        "AADSTS50173: The provided grant has expired due to it being revoked.",
    ],
)
def test_is_expired_credential_error_true(msg: str):
    assert is_expired_credential_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "401 Unauthorized",  # auth, but not definitively an expired credential
        "Token has expired",  # no AADSTS code — could be a stale relay token
        "AADSTS700016: Application not found in the directory",
        "Internal server error",
        "",
    ],
)
def test_is_expired_credential_error_false(msg: str):
    assert is_expired_credential_error(msg) is False


# --- is_connection_error -------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "User does not have access to the connection used in pipeline X",
        "Connection used in the pipeline activity is invalid",
        "Connection used in the dataflow is invalid",
        "Verify the user's permissions on the connection",
    ],
)
def test_is_connection_error_true(msg: str):
    assert is_connection_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "401 Unauthorized",  # auth, not connection
        "Internal server error",
        "Pipeline failed",
    ],
)
def test_is_connection_error_false(msg: str):
    assert is_connection_error(msg) is False


# --- error_hint ----------------------------------------------------------


@pytest.mark.parametrize(
    ("msg", "needle"),
    [
        ("Deployment failed: RequiredOptionsMissing", "dataType:"),
        ("requiredoptionsmissing on update", "Bisect"),
        (
            "Workload_FailedToParseFile while publishing SemanticModel",
            "compatibilityLevel",
        ),
        ("error: FailedToParseFile", "1604"),
        # Verbatim service messages.
        (
            "The database compatibility level of 1601 is below the minimal "
            "compatability level of 1604 needed for [model Model].[table "
            "date_view_dim].[partition date_view_dim]::[Mode]. "
            "(Workload_FailedToParseFile)",
            "database.tmdl",
        ),
        (
            "Dataset_Import_FailedToImportDataset: You cannot use Direct Lake on "
            "SQL mode together with other storage modes in the same model. "
            "Composite model does not support Direct Lake on SQL mode. Please "
            "remove the unsupported tables or switch them to Direct Lake on "
            "OneLake mode.",
            "directLake",
        ),
        (
            "Dataset_Import_FailedToImportDataset: You cannot change the storage "
            "mode of partition 'date_view_dim'. Converting existing tables or "
            "partitions from Import or DirectQuery mode to Direct Lake is not "
            "supported.",
            "Delete the semantic model item",
        ),
    ],
)
def test_error_hint_matches_known(msg: str, needle: str):
    hint = error_hint(msg)
    assert hint is not None
    assert needle in hint


def test_error_hint_distinguishes_shared_error_code():
    """Both hybrid-mode and in-place-change share Dataset_Import_FailedToImportDataset
    but must map to different hints (matched on message, not code)."""
    hybrid = error_hint(
        "Dataset_Import_FailedToImportDataset: Composite model does not support "
        "Direct Lake on SQL mode."
    )
    inplace = error_hint(
        "Dataset_Import_FailedToImportDataset: You cannot change the storage mode of partition 'x'."
    )
    assert hybrid != inplace
    assert "directLake" in hybrid
    assert "Delete the semantic model item" in inplace


@pytest.mark.parametrize(
    "msg",
    [
        "401 Unauthorized",
        "User does not have access to the connection used in pipeline X",
        "Some unrelated failure",
        "",
    ],
)
def test_error_hint_none_for_unknown(msg: str):
    assert error_hint(msg) is None


# --- console output ------------------------------------------------------


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _closed() -> io.StringIO:
    stream = io.StringIO()
    stream.close()
    return stream


@pytest.mark.parametrize(
    ("stream", "no_color", "expected"),
    [
        (_Tty(), None, True),
        (_Tty(), "1", False),
        (_Tty(), "", True),  # an empty NO_COLOR doesn't count (no-color.org)
        (io.StringIO(), None, False),
        (_closed(), None, False),
    ],
)
def test_use_color(monkeypatch, stream, no_color, expected):
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color is not None:
        monkeypatch.setenv("NO_COLOR", no_color)
    assert use_color(stream) is expected


@pytest.mark.parametrize(("stream", "colored"), [(_Tty(), True), (io.StringIO(), False)])
def test_log_lines_are_colored_only_on_a_terminal(monkeypatch, stream, colored):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stderr", stream)
    log = get_logger(f"fabdep_colortest_{colored}.cli")
    log.info("plain")
    log.warning("careful")
    out = stream.getvalue()
    assert out.startswith("plain\n")
    assert "[warn]   careful" in out
    assert ("\x1b[" in out) is colored


def test_log_lines_follow_stderr(monkeypatch):
    """Lines go to the current sys.stderr, so redirecting or capturing it catches them."""
    log = get_logger("fabdep_followtest.cli")
    for _ in range(2):
        stream = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stream)
        log.info("hello")
        assert stream.getvalue() == "hello\n"


@pytest.mark.parametrize(("stream", "colored"), [(_Tty(), True), (io.StringIO(), False)])
def test_print_error_colors_only_a_terminal(monkeypatch, stream, colored):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stderr", stream)
    print_error("boom")
    assert "boom" in stream.getvalue()
    assert ("\x1b[" in stream.getvalue()) is colored


@pytest.mark.parametrize(("stream", "colored"), [(_Tty(), True), (io.StringIO(), False)])
def test_fabric_cicd_lines_follow_the_color_rule(monkeypatch, stream, colored):
    import fabric_cicd  # noqa: F401 -- sets up its console handler on import

    monkeypatch.delenv("NO_COLOR", raising=False)
    handler = next(
        h for h in logging.getLogger("fabric_cicd").handlers if isinstance(h, logging.StreamHandler)
    )
    monkeypatch.setattr(handler, "formatter", handler.formatter)  # restored after the test
    monkeypatch.setattr(handler, "stream", stream)
    patch_fabric_cicd_logger()
    patch_fabric_cicd_logger()

    logging.getLogger("fabric_cicd.publish").warning("Publishing Notebook")
    out = stream.getvalue()
    assert ("\x1b[" in out) is colored
    # fabric-cicd's own line format is kept: level tag, timestamp, message.
    assert re.search(r"\[warn\]\s+\d\d:\d\d:\d\d - Publishing Notebook", out)


def test_get_logger_child_does_not_double_print(monkeypatch):
    """A child record must print once even when an ancestor fabdep logger
    exists (identity creates 'fabric_deploy'; modules create children)."""
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    get_logger("fabric_deploy_dbltest")
    get_logger("fabric_deploy_dbltest.detect").warning("only-once")
    assert stream.getvalue().count("only-once") == 1
