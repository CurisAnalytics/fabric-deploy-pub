"""Console output and the error classification behind the CLI's hints.

fabdep writes its log lines to stderr: INFO bare, other levels behind a
`[level]` tag. Colour is used only on a terminal, never when NO_COLOR is set
(https://no-color.org), and fabric-cicd's console lines follow the same rule.
"""

import logging
import os
import re
import sys
from collections.abc import Iterator
from typing import TextIO

from azure.core.exceptions import ClientAuthenticationError

from .identity import AuthError

__all__ = [
    "GREEN",
    "RED",
    "error_hint",
    "get_logger",
    "http_status",
    "is_auth_error",
    "is_connection_error",
    "is_expired_credential_error",
    "patch_fabric_cicd_logger",
    "print_error",
    "use_color",
]

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


# ANSI colours, written only where use_color() allows.
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"
_ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Tag and colour per level; INFO lines print bare.
_LEVELS: dict[int, tuple[str, str]] = {
    logging.DEBUG: ("[debug]", ""),
    logging.WARNING: ("[warn]", YELLOW),
    logging.ERROR: ("[error]", RED),
    logging.CRITICAL: ("[crit]", RED),
}


def use_color(stream: TextIO) -> bool:
    """Whether to colour `stream`: only a terminal, and only while NO_COLOR is unset or empty."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return stream.isatty()
    except ValueError:  # closed
        return False


class _Formatter(logging.Formatter):
    """A level tag (none for INFO), coloured by level or by `extra={"color": ...}`."""

    def format(self, record: logging.LogRecord) -> str:  # pyright: ignore[reportImplicitOverride]
        tag, color = _LEVELS.get(record.levelno, ("", ""))
        message = f"{tag:<8} {record.getMessage()}" if tag else record.getMessage()
        color = getattr(record, "color", color)
        return f"{color}{message}{RESET}" if color else message


class _Console(logging.Handler):
    """Writes to sys.stderr as it is at each record, so redirecting it takes effect."""

    def emit(self, record: logging.LogRecord) -> None:  # pyright: ignore[reportImplicitOverride]
        stream = sys.stderr
        try:
            text = self.format(record)
            _ = stream.write((text if use_color(stream) else _ANSI.sub("", text)) + "\n")
            _ = stream.flush()
        except Exception:
            self.handleError(record)


class _Uncolored(logging.Formatter):
    """Another formatter's output without its ANSI colour."""

    def __init__(self, inner: logging.Formatter) -> None:
        super().__init__()
        self.inner: logging.Formatter = inner

    def format(self, record: logging.LogRecord) -> str:  # pyright: ignore[reportImplicitOverride]
        return _ANSI.sub("", self.inner.format(record))


def patch_fabric_cicd_logger() -> None:
    """Hold fabric-cicd's console lines to the colour rule, keeping their format.

    fabric-cicd colours its console output unconditionally. Call after importing
    fabric_cicd, which sets up its logger on import.
    """
    for handler in logging.getLogger("fabric_cicd").handlers:
        stream: TextIO | None = getattr(handler, "stream", None)
        formatter = handler.formatter
        if (
            stream is not None
            and formatter
            and not isinstance(formatter, _Uncolored)
            and not use_color(stream)
        ):
            handler.setFormatter(_Uncolored(formatter))


def get_logger(name: str) -> logging.Logger:
    """Return logger `name`, giving its top-level logger fabdep's console handler.

    The handler lives on the top-level logger of the requested name
    ("fabric_deploy" for "fabric_deploy.detect"), never on descendants: with
    one handler per tree a record prints exactly once no matter which logger
    in the chain emits it, and records still propagate to the root logger
    (pytest caplog; fabric_cicd's file handler filters them out by name).
    """
    top = logging.getLogger(name.split(".", 1)[0])
    if not top.handlers:
        handler = _Console()
        handler.setFormatter(_Formatter())
        top.addHandler(handler)
        top.setLevel(logging.INFO)
    return logging.getLogger(name)


def print_error(message: str) -> None:
    """Print an error message to stderr, without a log prefix; red on a terminal."""
    color = use_color(sys.stderr)
    print(f"{RED}{message}{RESET}" if color else message, file=sys.stderr)


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
