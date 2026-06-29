"""HTTP transport for the Fabric / Power BI REST APIs.

A thin request layer: authenticated calls, status + header exposure for
long-running operations, and the Fabric LRO poller. No knowledge of specific
endpoints (that's the `client` module) and no auth logic (that's `identity`).
"""

import json
import time
import urllib.error
import urllib.request

from .identity import FABRIC_SCOPE, get_access_token

__all__ = [
    "FABRIC_HOST",
    "POWERBI_HOST",
    "http_request",
    "fabric_api_request",
    "poll_fabric_lro",
]

# pyright: reportMissingTypeArgument=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownVariableType=false

FABRIC_HOST = "https://api.fabric.microsoft.com"
POWERBI_HOST = "https://api.powerbi.com"


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
    `Retry-After`). An empty response body parses to `{}`.

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

    try:
        req = urllib.request.Request(
            url, data=request_data, headers=headers, method=method
        )
        with urllib.request.urlopen(req) as response:
            body = response.read().decode()
            parsed = json.loads(body) if body.strip() else {}
            return response.status, dict(response.headers), parsed
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(
            f"Failed to {error_context} (HTTP {e.code}).\n{error_body}\n\n"
            "Check your permissions and network connectivity."
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to connect to the API.\n{e.reason}\n\n"
            "Check your network connectivity."
        ) from e


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
        f"{FABRIC_HOST}{endpoint}",
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

    Returns:
        The operation result body (`{}` if the operation has no result).
    """
    location = headers.get("Location") or headers.get("location")
    op_id = headers.get("x-ms-operation-id") or headers.get("x-ms-operationid")
    # Clamp to >= 1: a Retry-After of 0 would never advance `waited` -> spin.
    retry_after = max(1, int(headers.get("Retry-After") or 5))
    if not location:
        # 202 with nothing to poll — don't report a no-op as success.
        raise RuntimeError(
            f"{error_context}: 202 Accepted but no Location header to poll."
        )

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
                        f"{FABRIC_HOST}/v1/operations/{op_id}/result",
                        scope=FABRIC_SCOPE,
                        error_context=f"fetch {error_context} result",
                    )
                    return result
                except RuntimeError:
                    return body  # result endpoint optional for some ops
            return body
        if op_status in ("Failed", "Cancelled"):
            raise RuntimeError(f"{error_context} {op_status}: {body.get('error')}")
        retry_after = max(1, int(hdrs.get("Retry-After") or retry_after))
        if waited >= timeout_s:
            raise RuntimeError(
                f"{error_context} did not finish within {timeout_s}s "
                f"(last status: {op_status})."
            )
