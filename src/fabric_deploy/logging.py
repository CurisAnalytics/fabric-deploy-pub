"""
Logging and formatting utilities for fabric_cicd.

This module provides custom logging formatters, error message formatting,
and Azure account information display utilities.
"""

import logging
import re
from collections.abc import Sequence
from typing import ClassVar, override

from fabric_cicd._common._color import Fore, Style
from fabric_cicd._common._logging import print_header

from .auth import AzureAccount

__all__ = [
    "get_logger",
    "print_header",
    "print_error",
    "format_auth_error_message",
    "format_account_list",
]

SEP = "=" * 100


class CustomFormatter(logging.Formatter):
    LEVEL_COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": Fore.BLACK,
        "INFO": Fore.WHITE + Style.BRIGHT,
        "WARNING": Fore.YELLOW,
        "ERROR": Fore.RED,
        "CRITICAL": Style.BRIGHT + Fore.RED,
    }

    @override
    def format(self, record: logging.LogRecord) -> str:
        level_color = self.LEVEL_COLORS.get(record.levelname, "")
        level_name = {
            "WARNING": "warn",
            "DEBUG": "debug",
            "INFO": "info",
            "ERROR": "error",
            "CRITICAL": "crit",
        }.get(record.levelname, "unknown")
        level_name = f"{level_color}[{level_name}]"
        message = f"{record.getMessage()}{Style.RESET_ALL}"
        # Calculate visual length by removing ANSI escape codes
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        # Get visual length of level_name without ANSI codes
        visual_level_length = len(ansi_escape.sub("", level_name))
        # Pad to 8 visual characters
        padding = " " * max(0, 8 - visual_level_length)
        full_message = f"{level_name}{padding} {message}"

        return full_message


def get_logger(name: str) -> logging.Logger:
    """Configure and return logger with CustomFormatter."""
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(CustomFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    return log


def print_error(message: str) -> None:
    """Print error message without log prefix (useful for stack traces, etc)."""
    print(f"{Fore.RED}{message}{Style.RESET_ALL}")


def format_account_list(
    accounts: Sequence[AzureAccount], current_id: str | None = None
) -> str:
    """
    Format list of Azure accounts with current marker.
    Args:
        accounts: List of Azure account dictionaries
        current_id: Subscription ID of current account (for marking)
    Returns: Formatted string with account information
    """
    lines: list[str] = []
    for acct in accounts:
        is_current = acct.get("isDefault") or acct.get("id") == current_id
        marker = " [CURRENT]" if is_current else ""
        lines.append(f"  • {acct['name']}{marker}")
        lines.append(f"    User: {acct['user']['name']}")
        lines.append(f"    Subscription: {acct['id']}")
    return "\n".join(lines)


def format_auth_error_message(
    current_account: AzureAccount | None = None,
    all_accounts: Sequence[AzureAccount] | None = None,
) -> str:
    """
    Format helpful authentication error message with account info.
    Args:
        current_account: Current Azure account dict (optional)
        all_accounts: List of all available Azure accounts (optional)
    Returns:  Formatted error message string with troubleshooting tips
    """
    lines = [
        "\n" + SEP,
        "Authentication/Authorization Error Detected",
        SEP,
    ]

    if current_account:
        msg = f"Current: {current_account['name']} ({current_account['user']['name']})"
        lines.extend(["", msg])

    if all_accounts and len(all_accounts) > 1:
        lines.extend(["", "Available accounts:"])
        lines.append(format_account_list(all_accounts))

        lines.extend(
            [
                "",
                "To switch accounts:",
                "\tfabdep auth switch <subscription-id>",
                "",
                "To rename a subscription:",
                "\tfabdep auth rename <subscription-id> <new-name>",
            ]
        )

    lines.extend(
        [
            "",
            "Common causes:",
            "\t• Wrong tenant/subscription (workspace may be in different org)",
            "\t• Expired token (run: az login --allow-no-subscriptions)",
            "",
            "Helpful commands:",
            "\t• fabdep auth list   - View current and available accounts",
            "\t• fabdep auth switch - Switch to different account",
            "\t• fabdep auth rename - Rename subscription for clarity",
            "",
            SEP + "\n",
        ]
    )

    return "\n".join(lines)
