"""
Logging and formatting utilities for fabric_cicd.

This module provides custom logging formatters, error message formatting,
and Azure account information display utilities.
"""

import logging
import re
from collections.abc import Sequence
from typing import ClassVar

from fabric_cicd._common._color import Fore, Style
from fabric_cicd._common._logging import print_header

from .auth import AzureAccount

__all__ = [
    "get_logger",
    "print_header",
    "print_error",
    "format_auth_error_message",
    "format_account_list",
    "is_auth_error",
    "is_connection_error",
    "is_transient_error",
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
        SEP,
        "Authentication/Authorization Error Detected",
        SEP,
    ]

    if current_account:
        lines.extend(
            [
                "",
                f"Current: {current_account['name']} ({current_account['user']['name']})",
                "",
                "(This may not be the correct account for this repository!)",
            ]
        )

    if all_accounts and len(all_accounts) > 1:
        lines.extend(["", "Available accounts:"])
        lines.append(format_account_list(all_accounts))

        lines.extend(
            [
                "",
                "To switch accounts: fabdep auth switch <subscription-id>",
            ]
        )

    lines.append(f"\n{SEP}\n")

    return "\n".join(lines)


def is_connection_error(error_msg: str) -> bool:
    """
    Check if error message indicates a connection reference issue.

    This is specific to Fabric items (DataPipelines, etc.) that reference
    connections which don't exist in the target workspace.

    Args:
        error_msg: Error message string to check

    Returns:
        True if error is connection-related, False otherwise
    """
    error_lower = error_msg.lower()

    # Connection-specific error patterns
    connection_patterns = [
        "does not have access to the connection used in",
        "connection used in the pipeline",
        "connection used in the dataflow",
        "verify the user's permissions on the connection",
    ]

    return any(pattern in error_lower for pattern in connection_patterns)


def is_transient_error(error_msg: str) -> bool:
    """
    Check if error message indicates a transient/retryable API error.

    These are typically intermittent Fabric API errors that may succeed on retry.

    Args:
        error_msg: Error message string to check

    Returns:
        True if error appears transient and retryable, False otherwise
    """
    error_lower = error_msg.lower()

    # Transient error patterns - API glitches, temporary failures
    transient_patterns = [
        "unhandled error occurred",
        "an error occured",  # Note: API typo is intentional
        "an error occurred",
        "internal server error",
        "service unavailable",
        "gateway timeout",
        "bad gateway",
        "connection reset",
        "connection timed out",
        "temporarily unavailable",
    ]

    # Don't retry if it's clearly an auth or connection config error
    if is_auth_error(error_msg) or is_connection_error(error_msg):
        return False

    return any(pattern in error_lower for pattern in transient_patterns)


def is_auth_error(error_msg: str) -> bool:
    """
    Check if error message indicates an authentication/authorization issue.

    Args:
        error_msg: Error message string to check

    Returns:
        True if error is auth-related, False otherwise
    """
    error_lower = error_msg.lower()

    # Common authentication/authorization error patterns
    auth_patterns = [
        "401",  # Unauthorized HTTP status
        "403",  # Forbidden HTTP status
        "404",  # Not Found (can indicate auth issues with workspace access)
        "unauthorized",
        "forbidden",
        "not found",
        "authentication",
        "credential",
        "token",
        "login",
        "aadsts",  # Azure AD error codes (e.g., AADSTS50173 for expired tokens)
        "expired",  # Expired tokens or grants
        "revoked",  # Revoked tokens or grants
        "defaultazurecredential failed",
        "no response from the imds endpoint",
        "shared token cache credential",
        "azure cli credential",
        "managed identity credential",
    ]

    return any(pattern in error_lower for pattern in auth_patterns)
