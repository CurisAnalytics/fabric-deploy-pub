"""format_account_list: plain by default, ANSI colour only when asked."""

import re

from fabric_deploy.log import format_account_list


def test_account_list_colours_only_when_asked():
    accounts = [
        {"name": "Tenant A", "id": "sub-a", "isDefault": True, "user": {"name": "me@a.com", "type": "user"}},
        {"name": "Tenant B", "id": "sub-b", "isDefault": False, "user": {"name": "app-id", "type": "servicePrincipal"}},
    ]
    plain = format_account_list(accounts)
    assert "\033[" not in plain
    assert "  • Tenant A [CURRENT]\n    User: me@a.com\n    Subscription: sub-a" in plain
    assert "    SPN: app-id" in plain

    coloured = format_account_list(accounts, color=True)
    assert "\033[32m[CURRENT]" in coloured
    assert _strip_ansi(coloured) == plain


def _strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)
