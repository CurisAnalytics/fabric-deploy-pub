"""Tests for switch_account name/alias resolution."""

from unittest.mock import patch

import pytest

from fabric_deploy.auth import switch_account


_ACCOUNTS = [
    {
        "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "name": "Personal B",
        "user": {"name": "user@example.com", "type": "user"},
        "isDefault": False,
    },
    {
        "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "name": "Personal C",
        "user": {"name": "other@example.com", "type": "user"},
        "isDefault": True,
    },
    {
        "id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "name": "Test SPN",
        "user": {"name": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "type": "servicePrincipal"},
        "isDefault": False,
    },
]


def _patched_switch(target: str):
    """Run switch_account against a stubbed list_accounts and capture the az call."""
    with (
        patch("fabric_deploy.auth.list_accounts", return_value=_ACCOUNTS),
        patch("fabric_deploy.auth.subprocess.run") as run,
    ):
        switch_account(target)
        run.assert_called_once()
        return run.call_args.args[0]


def test_switch_by_id_passes_id_through():
    cmd = _patched_switch("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    assert cmd == [
        "az",
        "account",
        "set",
        "--subscription",
        "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    ]


def test_switch_by_name_resolves_to_id():
    cmd = _patched_switch("Personal B")
    assert cmd[-1] == "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def test_switch_by_name_is_case_insensitive():
    cmd = _patched_switch("example personal")
    assert cmd[-1] == "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def test_switch_unknown_name_raises():
    with patch("fabric_deploy.auth.list_accounts", return_value=_ACCOUNTS):
        with pytest.raises(ValueError, match="not found"):
            switch_account("Nonexistent Account")


def test_switch_ambiguous_name_raises():
    accounts = _ACCOUNTS + [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "name": "Personal B",
            "user": {"name": "other@example.com", "type": "user"},
            "isDefault": False,
        }
    ]
    with patch("fabric_deploy.auth.list_accounts", return_value=accounts):
        with pytest.raises(ValueError, match="Multiple accounts match"):
            switch_account("Personal B")
