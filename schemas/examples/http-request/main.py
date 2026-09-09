#!/usr/bin/env python3
"""
HTTP Request Node - Reference Implementation

This is a language-agnostic execution contract example. The platform injects
inputs as JSON and expects StandardOutputWrapper JSON on stdout.

Any language that can:
- Read JSON from stdin or environment variables
- Make HTTP requests
- Write JSON to stdout
...can implement this contract (Python, Go, Rust, Bash+curl+jq, etc.)
"""

import json
import os
import sys
import time
from typing import Any, Dict, Optional

import httpx  # Modern async HTTP client (pip install httpx)


def load_inputs() -> Dict[str, Any]:
    """Load node inputs from JSON file or stdin."""
    input_path = os.environ.get("NODE_INPUT_PATH", "/tmp/node_input.json")

    if os.path.exists(input_path):
        with open(input_path, "r") as f:
            return json.load(f)

    # Fallback: read from stdin
    return json.load(sys.stdin)


def execute_http_request(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute HTTP request and return StandardOutputWrapper envelope.

    Returns:
        {
            "Result": {"status_code": 200, "headers": {...}, "body": {...}, ...},
            "StatusCode": 0,
            "StatusMessage": "Request completed successfully",
            "ErrorMessage": ""
        }
    """
    url = inputs["url"]
    method = inputs.get("method", "GET").upper()
    headers = inputs.get("headers", {})
    query_params = inputs.get("query_parameters", {})
    body = inputs.get("body")
    timeout = inputs.get("timeout_seconds", 30)
    response_format = inputs.get("response_format", "auto")
    follow_redirects = inputs.get("follow_redirects", True)
    verify_ssl = inputs.get("verify_ssl", True)

    # Inject credentials if provided
    credential_value = os.environ.get("CREDENTIAL_VALUE")
    if credential_value:
        headers["Authorization"] = f"Bearer {credential_value}"

    start_time = time.time()

    try:
        with httpx.Client(
            verify=verify_ssl,
            follow_redirects=follow_redirects,
            timeout=timeout
        ) as client:
            response = client.request(
                method=method,
                url=url,
                headers=headers,
                params=query_params,
                json=body if isinstance(body, dict) else None,
                content=body if isinstance(body, str) else None
            )

        elapsed_ms = int((time.time() - start_time) * 1000)

        # Parse response body based on format
        if response_format == "json" or (
            response_format == "auto" and "application/json" in response.headers.get("content-type", "")
        ):
            try:
                parsed_body = response.json()
            except json.JSONDecodeError:
                parsed_body = response.text
        else:
            parsed_body = response.text

        # Success - return StandardOutputWrapper
        return {
            "Result": {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": parsed_body,
                "elapsed_ms": elapsed_ms,
                "url": str(response.url)
            },
            "StatusCode": 0,
            "StatusMessage": f"HTTP {method} completed with status {response.status_code}",
            "ErrorMessage": ""
        }

    except httpx.TimeoutException as e:
        return {
            "Result": None,
            "StatusCode": 1,
            "StatusMessage": "Request timeout",
            "ErrorMessage": f"Request timed out after {timeout}s: {str(e)}"
        }

    except httpx.ConnectError as e:
        return {
            "Result": None,
            "StatusCode": 2,
            "StatusMessage": "Connection failed",
            "ErrorMessage": f"Could not connect to {url}: {str(e)}"
        }

    except Exception as e:
        return {
            "Result": None,
            "StatusCode": 255,
            "StatusMessage": "HTTP request failed",
            "ErrorMessage": f"Unexpected error: {type(e).__name__}: {str(e)}"
        }


def main():
    """Node execution entrypoint."""
    try:
        inputs = load_inputs()
        result = execute_http_request(inputs)

        # Write StandardOutputWrapper to stdout
        print(json.dumps(result, indent=2))
        sys.exit(result["StatusCode"])

    except Exception as e:
        # Fatal error during input parsing or setup
        error_output = {
            "Result": None,
            "StatusCode": 255,
            "StatusMessage": "Node execution failed",
            "ErrorMessage": f"Fatal error: {type(e).__name__}: {str(e)}"
        }
        print(json.dumps(error_output, indent=2))
        sys.exit(255)


if __name__ == "__main__":
    main()
