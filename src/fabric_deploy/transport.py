"""HTTP transport for the Fabric / Power BI REST APIs.

A thin request layer: authenticated calls with a timeout and bounded retries
on throttling / server errors, status + header exposure for long-running
operations, and the Fabric LRO poller. No knowledge of specific endpoints
(that's the `client` module) and no auth logic (that's `identity`).
"""

import email.utils
import json
import logging
import time
import urllib.error
import urllib.request

from fabric_cicd import constants

from .identity import FABRIC_SCOPE, get_access_token

__all__ = [
    "fabric_url",
    "powerbi_url",
    "http_request",
    "fabric_api_request",
    "poll_fabric_lro",
]

# pyright: reportMissingTypeArgument=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownVariableType=false

REQUEST_TIMEOUT_S = 60
# Attempts per request when the service answers 429 or 5xx. Each wait follows
# the response's Retry-After, else doubles from BACKOFF_BASE_S, and is capped
# at MAX_RETRY_WAIT_S so a long throttling window fails instead of hanging.
MAX_ATTEMPTS = 5
BACKOFF_BASE_S = 2
MAX_RETRY_WAIT_S = 60

_logger = logging.getLogger(__name__)


# fabric-cicd's API roots, read per call: its `FABRIC_API_ROOT_URL` and
# `DEFAULT_API_ROOT_URL` overrides then send fabdep's own calls to the same
# hosts as the deployment.
def fabric_url(path: str) -> str:
    """Absolute Fabric REST URL for `path` (e.g. "/v1/workspaces")."""
    return f"{constants.FABRIC_API_ROOT_URL}{path}"


def powerbi_url(path: str) -> str:
    """Absolute Power BI REST URL for `path` (e.g. "/v1.0/myorg/groups")."""
    return f"{constants.DEFAULT_API_ROOT_URL}{path}"


def http_request(
    url: str,
    method: str = "GET",
    data: dict | None = None,
    scope: str = FABRIC_SCOPE,
    error_context: str = "API request",
) -> tuple[int, dict[str, str], dict]:
    """Make an authenticated HTTP request and return (status, headers, body).

    Unlike `fabric_api_request`, this exposes the status code and response
    headers — needed to follow long-running operations (202 + `Location` /
    `Retry-After`). Header names are lower-cased (HTTP header names are
    case-insensitive). An empty response body parses to `{}`. Throttled
    (429) and server-error (5xx) responses are retried up to `MAX_ATTEMPTS`
    times.

    Args:
        url: Absolute request URL.
        method: HTTP method.
        data: Optional JSON request payload. For bodyless POST/PATCH/PUT an
            empty body is sent so a `Content-Length: 0` header is present
            (some Fabric endpoints require it).
        scope: OAuth scope for the token (Fabric vs Power BI).
        error_context: Context string for error messages.

    Returns:
        (status_code, headers_dict, parsed_body)

    Raises:
        RuntimeError: If the API call fails.
    """
    token = get_access_token(scope)
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        request_data = json.dumps(data).encode()
    elif method in ("POST", "PATCH", "PUT"):
        request_data = b""  # forces Content-Length: 0
    else:
        request_data = None

    req = urllib.request.Request(url, data=request_data, headers=headers, method=method)
    status, resp_headers, body = _send(req, error_context)
    if not body.strip():
        return status, resp_headers, {}
    try:
        return status, resp_headers, json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to {error_context}: the response (HTTP {status}) is not JSON:\n{body[:500]}"
        ) from e


def _send(req: urllib.request.Request, error_context: str) -> tuple[int, dict[str, str], str]:
    """Open `req` and return (status, lower-cased headers, body text), retrying 429/5xx."""
    attempt = 1
    while True:
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as response:
                headers = {name.lower(): value for name, value in response.headers.items()}
                return response.status, headers, response.read().decode()
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or e.code >= 500
            if retryable and attempt < MAX_ATTEMPTS:
                delay = _retry_after_s(e.headers.get("Retry-After"))
                if delay is None:
                    delay = BACKOFF_BASE_S * 2 ** (attempt - 1)
                delay = min(delay, MAX_RETRY_WAIT_S)
                attempt += 1
                _logger.warning(
                    f"{error_context}: HTTP {e.code}, retrying in {delay:.0f}s "
                    f"(attempt {attempt} of {MAX_ATTEMPTS})"
                )
                time.sleep(delay)
                continue
            error_body = e.read().decode() if e.fp else ""
            hint = (
                f"Still failing after {attempt} attempts."
                if retryable
                else "Check your permissions and network connectivity."
            )
            raise RuntimeError(
                f"Failed to {error_context} (HTTP {e.code}).\n{error_body}\n\n{hint}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Failed to connect to the API.\n{e.reason}\n\nCheck your network connectivity."
            ) from e
        except TimeoutError as e:
            raise RuntimeError(
                f"Failed to {error_context}: no response within {REQUEST_TIMEOUT_S}s."
            ) from e


def _retry_after_s(value: str | None) -> float | None:
    """Seconds a `Retry-After` header asks for (delay-seconds or HTTP-date), or None."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _poll_interval(retry_after: str | None, default: float) -> float:
    """Seconds until the next LRO poll; at least 1 so a zero never spins."""
    seconds = _retry_after_s(retry_after)
    return max(1.0, default if seconds is None else seconds)


def fabric_api_request(
    endpoint: str,
    method: str = "GET",
    data: dict | None = None,
    error_context: str = "API request",
) -> dict:
    """
    Generic Fabric API request handler with error handling.

    Args:
        endpoint: API endpoint path (e.g., "/v1/workspaces")
        method: HTTP method (GET or POST)
        data: Optional request payload for POST requests
        error_context: Context string for error messages

    Returns:
        Parsed JSON response as dictionary

    Raises:
        RuntimeError: If API call fails
    """
    _status, _headers, body = http_request(
        fabric_url(endpoint),
        method=method,
        data=data,
        scope=FABRIC_SCOPE,
        error_context=error_context,
    )
    return body


def poll_fabric_lro(
    headers: dict[str, str],
    error_context: str,
    timeout_s: int = 600,
) -> dict:
    """Follow a Fabric 202 long-running operation to completion.

    Polls the `Location` URL on the cadence the service asks for
    (`Retry-After`), then fetches the operation result. Raises on a Failed /
    Cancelled operation, a 202 with no `Location` to poll, or a timeout.

    Args:
        headers: The 202 response's headers, as `http_request` returns them.
        error_context: Operation name for error messages.
        timeout_s: Give up after this many seconds.

    Returns:
        The operation result body (`{}` if the operation has no result).
    """
    location = headers.get("location")
    op_id = headers.get("x-ms-operation-id")
    retry_after = _poll_interval(headers.get("retry-after"), default=5)
    if not location:
        # 202 with nothing to poll — don't report a no-op as success.
        raise RuntimeError(f"{error_context}: 202 Accepted but no Location header to poll.")

    waited = 0
    while True:
        sleep_for = min(retry_after, max(1, timeout_s - waited))
        time.sleep(sleep_for)
        waited += sleep_for
        _status, hdrs, body = http_request(
            location, scope=FABRIC_SCOPE, error_context=f"poll {error_context}"
        )
        op_status = body.get("status")
        if op_status in ("Succeeded", "Completed"):
            if op_id:
                try:
                    _s, _h, result = http_request(
                        fabric_url(f"/v1/operations/{op_id}/result"),
                        scope=FABRIC_SCOPE,
                        error_context=f"fetch {error_context} result",
                    )
                    return result
                except RuntimeError:
                    return body  # result endpoint optional for some ops
            return body
        if op_status in ("Failed", "Cancelled"):
            raise RuntimeError(f"{error_context} {op_status}: {body.get('error')}")
        retry_after = _poll_interval(hdrs.get("retry-after"), default=retry_after)
        if waited >= timeout_s:
            raise RuntimeError(
                f"{error_context} did not finish within {timeout_s}s (last status: {op_status})."
            )
