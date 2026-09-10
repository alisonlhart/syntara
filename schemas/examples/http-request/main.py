#!/usr/bin/env python3
"""HTTP Request Node - Reference Container Entrypoint.

    ┌───────────────────────────────────────────────────────────────────┐
    │ EXECUTION-PLANE CONFORMANCE STAND-IN — NOT a Node SDK deliverable.  │
    │ Models a node's imperative execution code, which the execution      │
    │ plane (not the SDK) owns. Present only to prove the stdin/envelope  │
    │ contract round-trips. Not shipped by the SDK; not stored in the     │
    │ registry. SDK deliverables: manifest.yaml, common-definitions.json, │
    │ the compile step, and node_types registration (test_register.py).   │
    └───────────────────────────────────────────────────────────────────┘

This is the language-agnostic execution contract for the ``http_request``
action node. It is a *reference* implementation: any runtime that reads JSON
from stdin, performs the request, and writes a ``StandardOutputWrapper`` JSON
document to stdout satisfies the same contract (see README for Bash/Go stubs).

Alignment with the 1803 execution-plane HTTP executor harness
-------------------------------------------------------------
The production execution plane runs a hardened, stdin-driven HTTP executor
(``python -m syntara.http_executor``). This entrypoint deliberately mirrors the
parts of that harness that make up the *contract*:

* **stdin transport** - one JSON command per line (JSON Lines). Each line is
  parsed, executed, and answered with exactly one JSON document on stdout.
* **httpx** as the client, with ``trust_env=False`` so proxy/credential
  environment variables never leak into an outbound request.
* **SSRF guard** - the destination is resolved and rejected if it points at a
  loopback, private, link-local, or otherwise non-public address, or at an
  in-cluster hostname suffix (``.local``, ``.svc``, ``.cluster.local``).
* **bounded execution** - URL length, header count, and response size are
  capped; the request timeout is clamped to a maximum.

The one intentional difference: the hardened executor emits its own compact
``{ok, status_code, ...}`` result, whereas a *node* must speak the platform's
immutable ``StandardOutputWrapper`` envelope
(``{Result, StatusCode, StatusMessage, ErrorMessage}``). This entrypoint is the
thin adapter that maps execution semantics onto that envelope so downstream
workflow template expressions (e.g. ``${http_call.Result.body.id}``) stay
stable across node versions.

Where this reference is broader than the hardened executor (it honours the
``verify_ssl`` and ``follow_redirects`` inputs declared in ``manifest.yaml``,
and accepts ``HEAD``/``OPTIONS``) the production plane may narrow the behaviour
for security; the envelope contract is unaffected.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx

# --- Bounds (mirroring the hardened executor's defaults) --------------------
ALLOWED_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".svc", ".cluster.local")
MAX_URL_LENGTH = 8_192
MAX_HEADERS = 100
MAX_INPUT_LINE_BYTES = 1_048_576


def _max_timeout_seconds() -> int:
    return _positive_env_int("HTTP_EXECUTOR_MAX_TIMEOUT_SECONDS", 600)


def _max_response_bytes() -> int:
    return _positive_env_int("HTTP_EXECUTOR_MAX_RESPONSE_BYTES", 1_048_576)


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


class RequestError(Exception):
    """A safe, envelope-mappable failure with a POSIX-style status code."""

    def __init__(self, status_code: int, status_message: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.status_message = status_message
        self.detail = detail


# --- Envelope helpers -------------------------------------------------------
def _success_envelope(result: dict[str, Any], status_message: str) -> dict[str, Any]:
    return {
        "Result": result,
        "StatusCode": 0,
        "StatusMessage": status_message,
        "ErrorMessage": "",
    }


def _failure_envelope(error: RequestError) -> dict[str, Any]:
    return {
        "Result": None,
        "StatusCode": error.status_code,
        "StatusMessage": error.status_message,
        "ErrorMessage": error.detail,
    }


# --- Validation (SSRF guard + shape checks) ---------------------------------
def _validate_public_destination(url: str) -> None:
    """Reject non-public destinations before httpx opens a connection."""
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise RequestError(1, "Invalid URL", "url must include a hostname")
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(BLOCKED_HOST_SUFFIXES):
        raise RequestError(1, "Blocked destination", "url targets a disallowed hostname")
    try:
        addresses = socket.getaddrinfo(
            hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError) as exc:
        raise RequestError(
            2, "DNS resolution failed", f"url hostname could not be resolved: {exc}"
        ) from exc
    for info in addresses:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise RequestError(
                1, "Blocked destination", "url resolves to a non-public address"
            )


def _string_mapping(value: object, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RequestError(1, "Invalid input", f"{field} must be an object")
    if len(value) > MAX_HEADERS:
        raise RequestError(1, "Invalid input", f"{field} exceeds {MAX_HEADERS} entries")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RequestError(
                1, "Invalid input", f"{field} keys and values must be strings"
            )
        result[key] = item
    return result


def _apply_authentication(headers: dict[str, str], value: object) -> None:
    """Map an ``authentication`` block onto request headers (mirrors executor)."""
    if value is None:
        return
    if not isinstance(value, dict):
        raise RequestError(1, "Invalid input", "authentication must be an object")
    auth_type = value.get("type")
    credentials = value.get("credentials")
    if not isinstance(auth_type, str) or not isinstance(credentials, str) or not credentials:
        raise RequestError(
            1, "Invalid input", "authentication requires type and credentials strings"
        )
    if auth_type in {"bearer", "oauth2"}:
        headers["Authorization"] = f"Bearer {credentials}"
    elif auth_type == "basic":
        import base64

        encoded = base64.b64encode(credentials.encode()).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    elif auth_type == "api_key":
        headers["X-API-Key"] = credentials
    else:
        raise RequestError(
            1, "Invalid input", "authentication type must be basic, bearer, api_key, or oauth2"
        )


def _parse_inputs(payload: object) -> dict[str, Any]:
    """Validate an untrusted input document into a normalized request spec."""
    if not isinstance(payload, dict):
        raise RequestError(1, "Invalid input", "input must be a JSON object")

    url = payload.get("url")
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise RequestError(1, "Invalid input", "url must be a non-empty HTTP(S) URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise RequestError(
            1, "Invalid input", "url must be HTTP or HTTPS without embedded userinfo"
        )

    method = payload.get("method", "GET")
    if not isinstance(method, str) or method.upper() not in ALLOWED_METHODS:
        raise RequestError(
            1, "Invalid input", f"method must be one of {sorted(ALLOWED_METHODS)}"
        )

    timeout = payload.get("timeout_seconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise RequestError(1, "Invalid input", "timeout_seconds must be a number")
    timeout = max(1, min(int(timeout), _max_timeout_seconds()))

    headers = _string_mapping(payload.get("headers"), "headers")
    _apply_authentication(headers, payload.get("authentication"))

    # Env-injected credential (zero-trust: resolved by the orchestrator at runtime).
    credential_value = os.environ.get("CREDENTIAL_VALUE")
    if credential_value and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {credential_value}"

    # Accept both the manifest field name and the executor's shorthand.
    query_params = _string_mapping(
        payload.get("query_parameters", payload.get("query_params")), "query_parameters"
    )

    body = payload.get("body")
    if body is not None and not isinstance(body, (dict, list, str)):
        raise RequestError(1, "Invalid input", "body must be an object, array, string, or null")

    return {
        "url": url,
        "method": method.upper(),
        "headers": headers,
        "query_params": query_params,
        "body": body,
        "timeout": float(timeout),
        "verify_ssl": bool(payload.get("verify_ssl", True)),
        "follow_redirects": bool(payload.get("follow_redirects", True)),
    }


# --- Execution --------------------------------------------------------------
def execute_http_request(payload: object) -> dict[str, Any]:
    """Execute one request and return a ``StandardOutputWrapper`` document.

    A completed request (including HTTP 4xx/5xx) is a task *success*
    (``StatusCode == 0``); the HTTP status lives in ``Result.status_code``.
    Only transport/validation/timeout failures set a non-zero ``StatusCode``.
    """
    try:
        spec = _parse_inputs(payload)
        _validate_public_destination(spec["url"])
    except RequestError as error:
        return _failure_envelope(error)

    started = monotonic()
    max_response_bytes = _max_response_bytes()
    try:
        with httpx.Client(
            verify=spec["verify_ssl"],
            follow_redirects=spec["follow_redirects"],
            trust_env=False,  # never inherit proxy/credential env vars
            timeout=spec["timeout"],
        ) as client:
            with client.stream(
                spec["method"],
                spec["url"],
                headers=spec["headers"],
                params=spec["query_params"],
                json=spec["body"] if isinstance(spec["body"], (dict, list)) else None,
                content=spec["body"] if isinstance(spec["body"], str) else None,
            ) as response:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_response_bytes:
                        return _failure_envelope(
                            RequestError(
                                1,
                                "Response too large",
                                f"response exceeded {max_response_bytes} bytes",
                            )
                        )
                    chunks.append(chunk)
    except httpx.TimeoutException as exc:
        return _failure_envelope(
            RequestError(2, "Request timed out", f"request timed out: {exc}")
        )
    except httpx.HTTPError as exc:
        return _failure_envelope(
            RequestError(
                2, "HTTP request failed", f"{type(exc).__name__}: {exc}"
            )
        )

    content = b"".join(chunks)
    try:
        parsed_body: Any = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed_body = content.decode(response.encoding or "utf-8", errors="replace")

    elapsed_ms = int((monotonic() - started) * 1000)
    result = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": parsed_body,
        "elapsed_ms": elapsed_ms,
        "url": str(response.url),
    }
    return _success_envelope(
        result,
        f"HTTP {spec['method']} completed with status {response.status_code}",
    )


# --- stdin/stdout loop (JSON Lines, aligned with the executor harness) -------
def _emit(document: dict[str, Any], *, pretty: bool) -> None:
    if pretty:
        sys.stdout.write(json.dumps(document, indent=2) + "\n")
    else:
        sys.stdout.write(
            json.dumps(document, separators=(",", ":"), allow_nan=False) + "\n"
        )
    sys.stdout.flush()


def main() -> None:
    """Entry point.

    * If ``NODE_INPUT_PATH`` points at a JSON file, execute that single command
      and emit one pretty-printed envelope (single-invocation platform path).
    * Otherwise consume stdin as JSON Lines: one command per line, one compact
      envelope per line (executor-harness path).

    The process exit code echoes the ``StatusCode`` of the last document, so a
    single-command invocation surfaces failure to the pod supervisor.
    """
    input_path = os.environ.get("NODE_INPUT_PATH")
    last_status = 0

    if input_path and os.path.exists(input_path):
        with open(input_path) as handle:
            payload = json.load(handle)
        document = execute_http_request(payload)
        _emit(document, pretty=True)
        sys.exit(document["StatusCode"] & 0xFF)

    for raw_line in sys.stdin.buffer:
        if not raw_line.strip():
            continue
        if len(raw_line) > MAX_INPUT_LINE_BYTES:
            document = _failure_envelope(
                RequestError(1, "Invalid input", "command exceeds the 1 MiB input limit")
            )
        else:
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                document = _failure_envelope(
                    RequestError(1, "Invalid input", "command must be valid JSON")
                )
            else:
                document = execute_http_request(payload)
        _emit(document, pretty=False)
        last_status = document["StatusCode"]

    sys.exit(last_status & 0xFF)


if __name__ == "__main__":
    main()
