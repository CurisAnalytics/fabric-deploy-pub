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
    ],
)
def test_error_hint_matches_known(msg: str, needle: str):
    hint = error_hint(msg)
    assert hint is not None
    assert needle in hint


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
