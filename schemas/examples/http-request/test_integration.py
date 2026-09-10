#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest>=8.0"]
# ///
"""Integration test: run the real execution-plane image, assert the envelope.

    ┌───────────────────────────────────────────────────────────────────┐
    │ EXECUTION-PLANE CONFORMANCE HARNESS — verifies BOUNDARY             │
    │ compatibility, not SDK internals. It exercises the execution-plane  │
    │ image + adapter stand-in. The SDK's own conformance test is         │
    │ test_register.py (validate -> compile -> register).                 │
    └───────────────────────────────────────────────────────────────────┘

Unlike ``test_register.py`` (which validates and registers the node definition),
this exercises the *execution* half of the prototype end-to-end against Aaron's
actual hardened HTTP executor image published on quay:

    quay.io/ahetheri/http-executor:dev

For each case it pipes a JSON Lines command into the container, reads the
native ``{ok, status_code, ...}`` result, applies ``adapter.map_result`` (the
orchestrator-side translation), and asserts the resulting
``StandardOutputWrapper``.

Tests skip gracefully when podman is unavailable, the image cannot be pulled,
or the network path to the target is blocked (e.g. Konflux worker egress), so
the suite is safe in CI. The SSRF-block case needs no external network and
always runs when a container runtime is present.

Run standalone:

    uv run --python 3.12 schemas/examples/http-request/test_integration.py

Or under pytest:

    uv run --python 3.12 --with pytest pytest \
      schemas/examples/http-request/test_integration.py -o addopts=""
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from adapter import map_result

IMAGE = "quay.io/ahetheri/http-executor:dev"
HERE = Path(__file__).resolve().parent


def _container_runtime() -> str | None:
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def _ensure_image(runtime: str) -> bool:
    """Return True if the image is present or can be pulled."""
    inspect = subprocess.run(
        [runtime, "image", "exists", IMAGE] if runtime == "podman" else [runtime, "image", "inspect", IMAGE],
        capture_output=True,
    )
    if inspect.returncode == 0:
        return True
    pull = subprocess.run([runtime, "pull", IMAGE], capture_output=True, timeout=300)
    return pull.returncode == 0


def _require_image() -> str:
    runtime = _container_runtime()
    if runtime is None:
        pytest.skip("no container runtime (podman/docker) available")
    if not _ensure_image(runtime):
        pytest.skip(f"could not pull {IMAGE}")
    return runtime


def _run(runtime: str, commands: list[dict]) -> list[dict]:
    """Pipe JSON Lines commands into the executor image; return raw results."""
    stdin = "".join(json.dumps(command) + "\n" for command in commands)
    proc = subprocess.run(
        [runtime, "run", "--rm", "-i", IMAGE],
        input=stdin.encode(),
        capture_output=True,
        timeout=120,
    )
    lines = [line for line in proc.stdout.decode().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _skip_if_unreachable(raw: dict) -> None:
    """Skip when the environment cannot reach the target (not a node defect)."""
    if raw.get("error_type") in {"TimeoutError", "ConnectError", "DNSResolutionError"}:
        pytest.skip(f"target unreachable in this environment: {raw.get('error_type')}")


# ---------------------------------------------------------------------------
# Adapter unit coverage (no container needed) - pins the mapping policy.
# ---------------------------------------------------------------------------
def test_adapter_maps_2xx_to_success() -> None:
    envelope = map_result(
        {"ok": True, "status_code": 200, "headers": {"content-type": "application/json"},
         "body": {"message": "success"}, "elapsed": 0.142},
        request_url="https://api.example.com/data",
    )
    assert envelope["StatusCode"] == 0
    assert envelope["ErrorMessage"] == ""
    assert envelope["Result"]["status_code"] == 200
    assert envelope["Result"]["elapsed_ms"] == 142
    assert envelope["Result"]["url"] == "https://api.example.com/data"
    assert set(envelope) == {"Result", "StatusCode", "StatusMessage", "ErrorMessage"}


def test_adapter_maps_completed_404_to_success_by_default() -> None:
    envelope = map_result(
        {"ok": False, "status_code": 404, "headers": {}, "body": "not found",
         "elapsed": 0.01, "error_type": "HTTPError", "message": "HTTP 404 Not Found"}
    )
    assert envelope["StatusCode"] == 0  # completed request is a task success
    assert envelope["Result"]["status_code"] == 404


def test_adapter_can_mirror_executor_for_non_2xx() -> None:
    envelope = map_result(
        {"ok": False, "status_code": 500, "headers": {}, "body": "",
         "elapsed": 0.01, "error_type": "HTTPError", "message": "HTTP 500"},
        completed_non_2xx_is_success=False,
    )
    assert envelope["StatusCode"] != 0
    assert envelope["Result"]["status_code"] == 500
    assert "500" in envelope["ErrorMessage"]


def test_adapter_maps_ssrf_error_to_failure() -> None:
    envelope = map_result(
        {"ok": False, "error_type": "SSRFValidationError",
         "message": "url resolves to a non-public address"}
    )
    assert envelope["StatusCode"] == 1
    assert envelope["Result"] is None
    assert "non-public" in envelope["ErrorMessage"]


# ---------------------------------------------------------------------------
# Real-image integration - runs Aaron's executor container.
# ---------------------------------------------------------------------------
def test_real_image_blocks_ssrf() -> None:
    """No external network needed: localhost resolves locally and is rejected."""
    runtime = _require_image()
    (raw,) = _run(runtime, [{"method": "GET", "url": "http://localhost/admin"}])
    envelope = map_result(raw, request_url="http://localhost/admin")
    assert raw["ok"] is False
    assert raw["error_type"] == "SSRFValidationError"
    assert envelope["StatusCode"] == 1
    assert envelope["Result"] is None


def test_real_image_get_maps_to_standard_output_wrapper() -> None:
    runtime = _require_image()
    url = "https://example.com"
    (raw,) = _run(runtime, [{"method": "GET", "url": url}])
    _skip_if_unreachable(raw)
    envelope = map_result(raw, request_url=url)
    assert envelope["StatusCode"] == 0
    assert envelope["Result"]["status_code"] == 200
    assert envelope["Result"]["elapsed_ms"] >= 0
    assert envelope["Result"]["url"] == url
    assert set(envelope) == {"Result", "StatusCode", "StatusMessage", "ErrorMessage"}


def test_real_image_404_is_task_success() -> None:
    runtime = _require_image()
    url = "https://example.com/definitely-not-here"
    (raw,) = _run(runtime, [{"method": "GET", "url": url}])
    _skip_if_unreachable(raw)
    envelope = map_result(raw, request_url=url)
    assert envelope["Result"]["status_code"] == 404
    assert envelope["StatusCode"] == 0  # completed request


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------
def _run_standalone() -> int:
    import types

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and isinstance(v, types.FunctionType)]
    passed = skipped = failed = 0
    for test in tests:
        try:
            test()
        except pytest.skip.Exception as exc:  # type: ignore[attr-defined]
            skipped += 1
            print(f"  - {test.__name__}: SKIP ({exc})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ✓ {test.__name__}")
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
