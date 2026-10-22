"""
Logging and formatting utilities for fabric_cicd.

This module provides custom logging formatters, error message formatting,
and error classification for the CLI's troubleshooting output.
"""

import logging
import re
from collections.abc import Iterator

from azure.core.exceptions import ClientAuthenticationError
from fabric_cicd._common._color import Fore, Style

from .identity import AuthError

__all__ = [
    "get_logger",
    "patch_fabric_cicd_logger",
    "print_error",
    "http_status",
    "is_auth_error",
    "is_expired_credential_error",
    "is_connection_error",
    "error_hint",
]

SEP = "=" * 100


# Known cryptic Fabric / fabric-cicd errors → a plain-language hint at the
# actual cause. Each entry is (substrings, hint); a hint fires when ANY of its
# substrings appears (case-insensitively) in the error text. Keep hints short
# and actionable — they print alongside the raw error, not instead of it.
# Append-only; add a row the first time an opaque error costs real bisecting.
_ERROR_HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("requiredoptionsmissing",),
        "RequiredOptionsMissing names neither the table nor the property. "
        "Usual causes (most common first):\n"
        "\t• a column missing its `dataType:` line\n"
        "\t• a `?updateMetadata=True` payload missing `.platform`\n"
        "\t• a bidirectional relationship cycle\n"
        "\t• a `sortByColumn:` pointing at a removed/calculated column\n"
        "\t• a `measure 'Name'` with no `= expr` body\n"
        "Bisect by moving half the table .tmdl files (and their `ref table` "
        "lines in model.tmdl) out, then redeploy to narrow it down.",
    ),
    (
        # Match the error CODE, not the message — Microsoft's own text misspells
        # it ("minimal compatability level of 1604").
        ("workload_failedtoparsefile", "failedtoparsefile"),
        "Workload_FailedToParseFile usually means the SemanticModel's "
        "compatibilityLevel is too low for a feature it uses (e.g. 1601 < 1604 "
        "for Direct Lake). Bump `compatibilityLevel` in database.tmdl to "
        ">= 1604, then redeploy.",
    ),
    (
        # Dataset_Import_FailedToImportDataset, hybrid-mode variant. Matched by
        # message substring because it shares its code with the next entry.
        (
            "composite model does not support direct lake on sql mode",
            "direct lake on sql mode together with other storage modes",
        ),
        "Pure Direct-Lake-on-SQL can't be mixed with import/DirectQuery or "
        "calculated/import tables in the same model. Convert every data table "
        "to a `mode: directLake` entity (or drop the unsupported ones).",
    ),
    (
        # Dataset_Import_FailedToImportDataset, in-place storage-mode-change variant.
        (
            "cannot change the storage mode of partition",
            "converting existing tables or partitions from import or directquery mode to direct lake is not supported",
        ),
        "fabric-cicd can't flip a partition's storage mode in place "
        "(Import/DirectQuery -> Direct Lake). Delete the semantic model item, "
        "then redeploy it fresh as Direct Lake.",
    ),
    (
        # Raised while resolving $items.UserDataFunction.<name>.$id, which the
        # report UDF rebind emits. That dynamic reads the TARGET workspace, so
        # it only resolves once the UDF itself has been published there.
        ("not found as a deployed userdatafunction",),
        "A report button is bound to a User Data Function that isn't deployed "
        "in the target workspace yet. UserDataFunction publishes before Report "
        "in a full deploy — so either drop the item-type filter for one run, or "
        "deploy the function first (`fabdep -t UserDataFunction`) and re-run.",
    ),
]


def error_hint(error_msg: str) -> str | None:
    """Map a known-cryptic Fabric error to a plain-language hint, or None.

    Fabric / fabric-cicd surface several opaque errors whose real cause is
    well understood but never stated in the message (RequiredOptionsMissing,
    Workload_FailedToParseFile, ...). This translates those signatures into a
    one-shot hint so the cause doesn't have to be re-bisected each time.

    Args:
        error_msg: Error message string to classify.

    Returns:
        The first matching hint, or None when the error isn't a known one.
    """
    error_lower = error_msg.lower()
    for substrings, hint in _ERROR_HINTS:
        if any(s in error_lower for s in substrings):
            return hint
    return None


# ANSI escape code pattern for calculating visual length.
_ANSI_RE: re.Pattern[str] = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": Fore.BLACK,
    "INFO": Fore.WHITE + Style.BRIGHT,
    "WARNING": Fore.YELLOW,
    "ERROR": Fore.RED,
    "CRITICAL": Style.BRIGHT + Fore.RED,
}

_LEVEL_NAMES: dict[str, str] = {
    "WARNING": "warn",
    "DEBUG": "debug",
    "INFO": "info",
    "ERROR": "error",
    "CRITICAL": "crit",
}


def _level_prefix(record: logging.LogRecord) -> str:
    """Colored, width-padded '[level] ' prefix; empty for INFO."""
    if record.levelname == "INFO":
        return ""
    color = _LEVEL_COLORS.get(record.levelname, "")
    level_name = f"{color}[{_LEVEL_NAMES.get(record.levelname, 'unknown')}]"
    # Pad based on visual width (after stripping ANSI escapes).
    padding = " " * max(0, 8 - len(_ANSI_RE.sub("", level_name)))
    return f"{level_name}{padding} "


class CustomFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # pyright: ignore[reportImplicitOverride]
        message = f"{record.getMessage()}{Style.RESET_ALL}"
        if record.levelname == "INFO":
            return message
        return f"{_level_prefix(record)}{message}"


class FabricCicdFormatter(logging.Formatter):
    """Formatter for the fabric_cicd logger that omits timestamps on header/blank lines.

    Header lines (containing '####') and blank separator lines are formatted
    without timestamps to reduce visual noise. All other lines keep timestamps
    for operational timing information.
    """

    def format(self, record: logging.LogRecord) -> str:  # pyright: ignore[reportImplicitOverride]
        raw_message = record.getMessage()
        message = f"{raw_message}{Style.RESET_ALL}"
        stripped = _ANSI_RE.sub("", raw_message).strip()

        prefix = _level_prefix(record)

        # Header lines (####) and blank separators get no timestamp.
        if "####" in stripped or stripped == "":
            return f"{prefix}{message}"

        timestamp = self.formatTime(record, self.datefmt)

        # fabric_cicd marks indented lines with "->".
        if "->" in raw_message:
            message = message.replace("->", "")

        return f"{prefix}{timestamp} - {message}"


def patch_fabric_cicd_logger() -> None:
    """Replace fabric_cicd's logger formatter to omit timestamps on header lines.

    Call this AFTER importing fabric_cicd (which runs configure_logger() at
    import time). Patches both the 'fabric_cicd' logger and 'console_only'
    (used for exception messages).
    """
    for name in ("fabric_cicd", "console_only"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setFormatter(FabricCicdFormatter(datefmt="%H:%M:%S"))
                break


def get_logger(name: str) -> logging.Logger:
    """Configure and return logger with CustomFormatter.

    The console handler lives on the top-level logger of the requested name
    ("fabric_deploy" for "fabric_deploy.detect"), never on descendants: with
    one handler per tree a record prints exactly once no matter which logger
    in the chain emits it, and records still propagate to the root logger
    (pytest caplog; fabric_cicd's file handler filters them out by name).
    """
    top = logging.getLogger(name.split(".", 1)[0])
    if not top.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(CustomFormatter())
        top.addHandler(handler)
        top.setLevel(logging.INFO)
    return logging.getLogger(name)


def print_error(message: str) -> None:
    """Print error message without log prefix (useful for stack traces, etc)."""
    print(f"{Fore.RED}{message}{Style.RESET_ALL}")


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


_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
# How Entra and fabric-cicd word auth failures, for errors that arrive as text
# only (fabric-cicd's 401 handler raises a bare Exception with no status).
_AUTH_MESSAGES = re.compile(
    r"\bAADSTS\d+|Microsoft Entra token|executing identity is not authorized", re.IGNORECASE
)


def _chain(error: BaseException) -> Iterator[BaseException]:
    """`error` and the exceptions behind it, as a traceback would print them."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        suppressed = current.__cause__ is None and current.__suppress_context__
        current = current.__cause__ or (None if suppressed else current.__context__)


def http_status(error: BaseException | str) -> int | None:
    """The HTTP status an error carries, or None.

    Reads a status attribute anywhere in the exception chain (`status_code`,
    `status`, or urllib's `code`), else an `HTTP nnn` marker in the message.
    """
    if isinstance(error, BaseException):
        for exc in _chain(error):
            for attr in ("status_code", "status", "code"):
                value = getattr(exc, attr, None)
                if isinstance(value, int) and 100 <= value <= 599:
                    return value
    match = _HTTP_STATUS.search(str(error))
    return int(match.group(1)) if match else None


def is_auth_error(error: BaseException | str) -> bool:
    """True when no token could be had, or the API refused the identity (401/403)."""
    if isinstance(error, BaseException) and any(
        isinstance(exc, ClientAuthenticationError | AuthError) for exc in _chain(error)
    ):
        return True
    return http_status(error) in (401, 403) or bool(_AUTH_MESSAGES.search(str(error)))


def is_expired_credential_error(error_msg: str) -> bool:
    """True when an Entra error code says the credential itself has expired.

    An expired client secret or certificate needs a new one issued; signing in
    again with the same one cannot help.
    """
    error_lower = error_msg.lower()

    expired_codes = [
        "aadsts7000222",  # client secret keys for the app are expired
        "aadsts700082",  # refresh token expired due to inactivity
        "aadsts70043",  # refresh token expired or revoked
        "aadsts50173",  # grant expired — fresh sign-in required
    ]

    return any(code in error_lower for code in expired_codes)
