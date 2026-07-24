"""Unit tests for log.py error-classification predicates.

These are pure string predicates with no external state, so each case is a
single line of expected behavior.
"""

from __future__ import annotations

import pytest

from fabric_deploy.log import (
    error_hint,
    is_auth_error,
    is_connection_error,
    is_expired_credential_error,
    is_transient_error,
)


# --- is_auth_error -------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "401 Unauthorized",
        "403 Forbidden",
        "404 Not Found",
        "Token has expired",
        "Token revoked",
        "AADSTS50173: token has expired",
        "DefaultAzureCredential failed to retrieve a token",
        "AzureCliCredential could not authenticate",
        "Login required",
        "No response from the IMDS endpoint",
    ],
)
def test_is_auth_error_true(msg: str):
    assert is_auth_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "Internal server error",
        "Pipeline failed at step 3",
        "Workspace foo does not exist",  # plain "does not exist" is not an auth pattern
        "",
    ],
)
def test_is_auth_error_false(msg: str):
    assert is_auth_error(msg) is False


# --- is_expired_credential_error ------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        # Verbatim shape of the live failure that motivated this predicate.
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


# --- is_transient_error --------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "An unhandled error occurred during deployment",
        "An error occured while processing",  # API typo
        "An error occurred while processing",
        "Internal server error",
        "Service unavailable",
        "Gateway timeout",
        "Bad gateway",
        "Connection reset by peer",
        "Connection timed out",
        "Connection aborted",
        "Remote end closed connection without response",
        "RemoteDisconnected: server closed the connection",
        "Resource temporarily unavailable",
    ],
)
def test_is_transient_error_true(msg: str):
    assert is_transient_error(msg) is True


def test_is_transient_error_does_not_retry_auth_error():
    """Even when the message contains 'internal server error', auth wins → no retry."""
    assert is_transient_error("401 Unauthorized: internal server error") is False


def test_is_transient_error_does_not_retry_connection_error():
    """Connection misconfig is permanent — don't retry."""
    msg = (
        "User does not have access to the connection used in pipeline. "
        "Internal server error"
    )
    assert is_transient_error(msg) is False


@pytest.mark.parametrize(
    "msg",
    [
        "Pipeline syntax invalid",
        "Item type not supported",
        "",
    ],
)
def test_is_transient_error_false(msg: str):
    assert is_transient_error(msg) is False


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
        "Dataset_Import_FailedToImportDataset: You cannot change the storage "
        "mode of partition 'x'."
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
