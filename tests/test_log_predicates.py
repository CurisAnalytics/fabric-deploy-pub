"""Unit tests for log.py error-classification predicates.

Auth checks read HTTP statuses structurally: a status attribute
anywhere in the exception chain, or an `HTTP nnn` marker in the message.
Everything else is a plain message predicate.
"""

from __future__ import annotations

import urllib.error

import pytest
from azure.core.exceptions import ClientAuthenticationError

from fabric_deploy.identity import AuthError
from fabric_deploy.log import (
    error_hint,
    http_status,
    is_auth_error,
    is_connection_error,
    is_expired_credential_error,
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
        # Verbatim strings captured from a live Northwind framing session.
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


# --- get_logger ----------------------------------------------------------


def test_get_logger_child_does_not_double_print(capsys):
    """A child record must print once even when an ancestor fabdep logger
    exists (identity creates 'fabric_deploy'; modules create children)."""
    import io
    import logging

    from fabric_deploy.log import get_logger

    parent = get_logger("fabric_deploy_dbltest")
    child = get_logger("fabric_deploy_dbltest.detect")

    streams = []
    for lg in (parent, child):
        stream = io.StringIO()
        for h in lg.handlers:
            if isinstance(h, logging.StreamHandler):
                h.setStream(stream)
        streams.append(stream)

    child.warning("only-once")
    total = "".join(s.getvalue() for s in streams)
    assert total.count("only-once") == 1
