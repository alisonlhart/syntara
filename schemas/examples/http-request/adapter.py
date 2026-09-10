#!/usr/bin/env python3
"""Adapter: execution-plane HTTP executor result -> StandardOutputWrapper.

    ┌───────────────────────────────────────────────────────────────────┐
    │ EXECUTION-PLANE CONFORMANCE STAND-IN — NOT a Node SDK deliverable.  │
    │ This raw-result -> envelope mapping lives on the EXECUTION PLANE    │
    │ side of the handoff boundary: per the architecture, the plane is    │
    │ what RETURNS StandardOutputWrapper. It is reproduced here only so   │
    │ the prototype can assert the executor's native output maps cleanly  │
    │ onto the SDK's envelope. In production the plane (or a thin shim at  │
    │ the container boundary) owns this, including the 4xx/5xx policy.     │
    └───────────────────────────────────────────────────────────────────┘

The production execution plane runs a hardened, shared HTTP executor image
(``quay.io/ahetheri/http-executor:dev``) that emits its own compact native
result, one JSON object per stdout line:

* success / completed (incl. non-2xx)::

      {"ok": true,  "status_code": 200, "headers": {...}, "body": ..., "elapsed": 0.09}
      {"ok": false, "status_code": 404, "headers": {...}, "body": ..., "elapsed": 0.01,
       "error_type": "HTTPError", "message": "HTTP 404 Not Found"}

* execution failure (transport / validation / SSRF / timeout)::

      {"ok": false, "error_type": "SSRFValidationError", "message": "..."}   # no status_code

A *node* must instead speak the platform's immutable ``StandardOutputWrapper``
(``{Result, StatusCode, StatusMessage, ErrorMessage}``). This module is the thin
translation the orchestrator applies when a node's ``image_ref`` points at that
shared executor image, so downstream template expressions
(``${http_call.Result.status_code}``) stay stable.

Mapping policy (see ``map_result``):

* A raw result carrying a ``status_code`` means the HTTP exchange *completed* —
  even a 4xx/5xx. That is a task **success** (``StatusCode == 0``); the HTTP
  status lives in ``Result.status_code``. This is deliberate: workflows branch
  on the HTTP status as data, and 429/5xx retries are driven by the manifest's
  ``retry_policy.retryable_status_codes`` (which reads ``Result.status_code``),
  not by ``StatusCode``.
* A raw result with no ``status_code`` is an execution failure and maps to a
  non-zero ``StatusCode`` with ``Result: null`` and a populated ``ErrorMessage``.

To instead mirror the executor's own success/failure opinion (non-2xx ->
non-zero ``StatusCode``), set ``completed_non_2xx_is_success=False``.
"""

from __future__ import annotations

from typing import Any

# Non-zero StatusCodes for execution failures, keyed by the executor's
# error_type. Anything unrecognized falls back to 255 (generic failure).
_ERROR_STATUS_CODES = {
    "ValidationError": 1,
    "SSRFValidationError": 1,
    "ResponseTooLarge": 1,
    "DNSResolutionError": 2,
    "TimeoutError": 2,
    "ConnectError": 2,
    "HTTPError": 2,
}


def map_result(
    raw: dict[str, Any],
    *,
    request_url: str | None = None,
    completed_non_2xx_is_success: bool = True,
) -> dict[str, Any]:
    """Translate one executor result into a ``StandardOutputWrapper`` document.

    Args:
        raw: One parsed JSON result line from the executor image.
        request_url: The URL the command targeted. The executor result does not
            echo a final URL (it never follows redirects), so this is used to
            populate ``Result.url`` when available.
        completed_non_2xx_is_success: When True (default), a completed request
            with a non-2xx status is a task success. When False, the node
            mirrors the executor and reports non-2xx as a failure.
    """
    status_code = raw.get("status_code")
    completed = status_code is not None

    if not completed:
        # Transport / validation / SSRF / timeout: no HTTP exchange occurred.
        error_type = str(raw.get("error_type", "ExecutionError"))
        message = str(raw.get("message", "HTTP execution failed"))
        return {
            "Result": None,
            "StatusCode": _ERROR_STATUS_CODES.get(error_type, 255),
            "StatusMessage": error_type,
            "ErrorMessage": message,
        }

    result = {
        "status_code": status_code,
        "headers": raw.get("headers", {}),
        "body": raw.get("body"),
        "elapsed_ms": int(round(float(raw.get("elapsed", 0.0)) * 1000)),
        "url": request_url or "",
    }

    is_2xx = 200 <= int(status_code) < 300
    if is_2xx or completed_non_2xx_is_success:
        return {
            "Result": result,
            "StatusCode": 0,
            "StatusMessage": f"HTTP request completed with status {status_code}",
            "ErrorMessage": "" if is_2xx else str(raw.get("message", "")),
        }

    # completed_non_2xx_is_success=False: report the non-2xx as a failure but
    # still surface the response detail in Result for debugging.
    return {
        "Result": result,
        "StatusCode": _ERROR_STATUS_CODES["HTTPError"],
        "StatusMessage": f"HTTP request failed with status {status_code}",
        "ErrorMessage": str(raw.get("message", f"HTTP {status_code}")),
    }
