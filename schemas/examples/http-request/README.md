# HTTP Request Node

**Category:** `action`  
**Execution Type:** `container`  
**Workload Classification:** `action`

Non-blocking HTTP/HTTPS API orchestrator with credential injection, response parsing, and automatic retry logic. Supports REST, GraphQL, and webhook integrations.

## Package Layout

```
http-request/
├── manifest.yaml          # [SDK]   Sole declarative authoring source (YAML, human-friendly)
├── test_register.py       # [SDK]   validate → compile → register in node_types
├── test_postgres_registry.py  # [SDK]   validate → compile → persist (JSONB) → advertise over a FastAPI registry API
├── main.py                # Reference container entrypoint (stdin → stdout) — stand-in
├── adapter.py             # Executor result → StandardOutputWrapper — stand-in
├── test_integration.py    # Runs the real executor image, asserts the mapped envelope
└── README.md              # This file
```

### Ownership boundary — what the SDK actually ships

This prototype straddles a handoff boundary, and the files are labeled accordingly:

- **Node SDK deliverables (what this repo owns).** `manifest.yaml` is the
  single authoring source of truth; `test_register.py` is the SDK conformance test that
  proves the pipeline **validate → compile → register**, and `test_postgres_registry.py`
  extends that pipeline end to end — **validate → compile → persist → advertise** —
  standing up a FastAPI registry that stores the compiled descriptor in a PostgreSQL
  JSONB column and serves the palette summary and full descriptor the React Flow visual
  builder consumes. There is no standalone
  `http_request.schema.json`; the manifest compiles to `node-definition.json` (a
  gitignored build artifact) and that compiled descriptor — never the source manifest —
  is what the registry persists and the canvas consumes.
- **execution-plane conformance stand-ins (NOT SDK deliverables).**
  `main.py`, `adapter.py`, and `test_integration.py` model code the **execution plane**
  owns: the node's imperative execution, the raw-result→envelope mapping, and the
  boundary integration test. They live here only to *prove compatibility* — that the
  executor's native output maps cleanly onto the SDK's envelope and that the registered
  `image_ref` resolves to a real, working image. The SDK does not ship or store them.

## Execution: shared executor image + adapter

This node runs on the execution plane's **shared, hardened HTTP executor image**:

```
image_ref: quay.io/ahetheri/http-executor:dev
```

That image reads JSON Lines commands on stdin and emits its own native result per
line — `{ok, status_code, headers, body, elapsed}` on a completed request,
`{ok: false, error_type, message}` on an execution failure. It is **not** the
`StandardOutputWrapper`. The orchestrator applies [`adapter.py`](adapter.py)
(`map_result`) to translate the native result into the platform envelope so
downstream template expressions (`${http_call.Result.status_code}`) stay stable.

**Mapping policy** (documented and unit-tested in `test_integration.py`):

- A result carrying a `status_code` means the HTTP exchange **completed** — even a
  4xx/5xx — so it is a task **success** (`StatusCode: 0`); the HTTP status lives in
  `Result.status_code`. Workflows branch on that, and 429/5xx retries are driven by
  the manifest's `retry_policy.retryable_status_codes`, not by `StatusCode`.
- A result with no `status_code` (transport / validation / SSRF / timeout) maps to a
  non-zero `StatusCode` with `Result: null` and a populated `ErrorMessage`.
- Pass `completed_non_2xx_is_success=False` to instead mirror the executor and report
  non-2xx as a failure.

[`main.py`](main.py) remains the standalone, polyglot **reference** entrypoint: it
performs the request itself and emits the `StandardOutputWrapper` directly, mirroring
the same stdin/SSRF/bounded-execution contract. Use it when building a self-contained
node image; use the shared executor image + `adapter.py` for the production path.

## Execution Contract (Language-Agnostic)

This node demonstrates the platform's **polyglot execution model**. While this reference implementation uses Python 3.12, nodes can be written in **any language** that satisfies the execution contract.

### Alignment with the execution-plane HTTP executor harness

`main.py` mirrors the container execution harness used by the 1803 execution plane's
hardened HTTP executor so that this node runs unchanged on that plane:

- **stdin transport — JSON Lines.** One JSON command per stdin line; exactly one
  JSON document written per line to stdout. This is the executor's wire format.
- **`httpx` with `trust_env=False`.** Proxy and credential environment variables are
  never inherited into an outbound request.
- **SSRF guard.** The destination is resolved and rejected if it points at a
  loopback, private, link-local, or otherwise non-public address, or at an in-cluster
  hostname suffix (`.local`, `.svc`, `.cluster.local`). Cloud metadata endpoints
  (e.g. `169.254.169.254`) are blocked.
- **Bounded execution.** URL length, header count, request/response size, and timeout
  are all capped (`HTTP_EXECUTOR_MAX_TIMEOUT_SECONDS`, `HTTP_EXECUTOR_MAX_RESPONSE_BYTES`).

The one deliberate difference: the hardened executor emits its own compact
`{ok, status_code, ...}` result, whereas a **node** must speak the platform's immutable
`StandardOutputWrapper`. `main.py` is the thin adapter that maps execution semantics
onto that envelope, keeping downstream template expressions
(`${http_call.Result.body.id}`) stable across node versions.

### Input Contract
- Platform injects inputs as JSON via:
  - stdin as **JSON Lines** (one command object per line) — the executor-harness path
  - OR a single JSON object file at the path in the `NODE_INPUT_PATH` env var — the
    single-invocation platform path (emits one pretty-printed envelope)

Example input:
```json
{
  "url": "https://api.github.com/repos/syntara/platform/issues",
  "method": "POST",
  "headers": {
    "Content-Type": "application/json",
    "Accept": "application/vnd.github.v3+json"
  },
  "body": {
    "title": "Automated issue from workflow",
    "labels": ["automation"]
  }
}
```

### Output Contract
- Node MUST write `StandardOutputWrapper` JSON to stdout (one document per input line)
- Process exit code echoes the last document's `StatusCode`

`StatusCode` reflects **execution/network success, not the HTTP status**: a completed
request — including a `4xx`/`5xx` response — is a task success (`StatusCode: 0`) and the
HTTP status lives in `Result.status_code`. Only transport, validation, SSRF, timeout, or
response-size failures set a non-zero `StatusCode` with `Result: null` and a populated
`ErrorMessage`.

Required output shape:
```json
{
  "Result": {
    "status_code": 201,
    "headers": {"content-type": "application/json"},
    "body": {"id": 123, "state": "open"},
    "elapsed_ms": 342,
    "url": "https://api.github.com/repos/syntara/platform/issues"
  },
  "StatusCode": 0,
  "StatusMessage": "HTTP POST completed with status 201",
  "ErrorMessage": ""
}
```

### Credential Injection
- Platform resolves `credential_id` UUID at runtime
- Injects via:
  - **`env`** (default): Available as `CREDENTIAL_VALUE` environment variable
  - **`file`**: Mounted at `/run/secrets/credential` (tmpfs, RAM-backed)

The reference implementation auto-injects Bearer tokens:
```python
credential_value = os.environ.get("CREDENTIAL_VALUE")
if credential_value:
    headers["Authorization"] = f"Bearer {credential_value}"
```

## Polyglot Implementation Examples

### Python (this reference)
```bash
python3 main.py < input.json
```

### Bash + curl + jq
```bash
#!/bin/bash
INPUT=$(cat)
URL=$(echo "$INPUT" | jq -r '.url')
METHOD=$(echo "$INPUT" | jq -r '.method')
curl -X "$METHOD" "$URL" -H "Authorization: Bearer $CREDENTIAL_VALUE" | jq '{
  Result: {status_code: .status, body: .},
  StatusCode: 0,
  StatusMessage: "Request completed",
  ErrorMessage: ""
}'
```

### Go
```go
package main

import (
    "encoding/json"
    "net/http"
    "os"
)

type Input struct {
    URL    string            `json:"url"`
    Method string            `json:"method"`
    Headers map[string]string `json:"headers"`
}

type Output struct {
    Result        map[string]interface{} `json:"Result"`
    StatusCode    int                    `json:"StatusCode"`
    StatusMessage string                 `json:"StatusMessage"`
    ErrorMessage  string                 `json:"ErrorMessage"`
}

func main() {
    var input Input
    json.NewDecoder(os.Stdin).Decode(&input)
    
    req, _ := http.NewRequest(input.Method, input.URL, nil)
    if cred := os.Getenv("CREDENTIAL_VALUE"); cred != "" {
        req.Header.Set("Authorization", "Bearer " + cred)
    }
    
    resp, err := http.DefaultClient.Do(req)
    if err != nil {
        json.NewEncoder(os.Stdout).Encode(Output{
            StatusCode: 1,
            StatusMessage: "Request failed",
            ErrorMessage: err.Error(),
        })
        os.Exit(1)
    }
    
    json.NewEncoder(os.Stdout).Encode(Output{
        Result: map[string]interface{}{
            "status_code": resp.StatusCode,
            "url": resp.Request.URL.String(),
        },
        StatusCode: 0,
        StatusMessage: "Request completed",
    })
}
```

## Network Requirements

Declared in `manifest.yaml`:
```yaml
scheduling_controls:
  connectivity_requirements:
    # Platform auto-extracts hostname from 'url' input
    # Only add additional endpoints if multi-hop requests are needed
```

The platform generates a NetworkPolicy that allows egress only to declared hostnames.

## Retry Policy

Automatic retry on transient failures:
- **HTTP 429** (rate limit)
- **HTTP 500-504** (server errors)
- **Exponential backoff:** 5s → 10s → 20s

## Testing Locally

### 1. Build container image
```bash
# Example Containerfile (not included)
FROM python:3.12-slim
RUN pip install httpx
COPY main.py /app/
ENTRYPOINT ["python3", "/app/main.py"]
```

### 2. Run with test input (JSON Lines over stdin)
```bash
# One command per line → one StandardOutputWrapper per line (compact)
printf '%s\n' '{"url":"https://httpbin.org/get","method":"GET"}' \
  | podman run -i --rm my-http-node
```

Expected output (single compact line, pretty-printed here for readability):
```json
{
  "Result": {
    "status_code": 200,
    "body": {...},
    "elapsed_ms": 234
  },
  "StatusCode": 0,
  "StatusMessage": "HTTP GET completed with status 200",
  "ErrorMessage": ""
}
```

Single-invocation path (file input, pretty-printed envelope):
```bash
echo '{"url":"https://httpbin.org/get","method":"GET"}' > /tmp/node_input.json
NODE_INPUT_PATH=/tmp/node_input.json python3 main.py
```

### 3. End-to-end registration test

`test_register.py` exercises the full SDK pipeline for this node — it validates
`manifest.yaml` against `common-definitions.json`, compiles it to
`node-definition.json`, and registers a `NodeTypeDescriptor` row, asserting the
`node_types` CHECK constraint (container ⇒ `image_ref` required; `in_process` ⇒
`image_ref` forbidden).

```bash
# Standalone (uv resolves deps from the script's PEP 723 header)
uv run --python 3.12 schemas/examples/http-request/test_register.py

# Or under pytest
uv run --python 3.12 --with pytest --with pyyaml --with jsonschema --with sqlmodel \
  pytest schemas/examples/http-request/test_register.py -o addopts=""
```

By default this runs against an in-memory SQLite engine, which uses the same
model but a generic `JSON` descriptor column. To exercise the **real JSONB
column and the DDL CHECK constraints on PostgreSQL** — the DB-configuration
verification path — set `SYNTARA_TEST_DATABASE_URL`; `test_register_and_constraints_on_postgres`
un-skips and runs against it:

```bash
# One-off throwaway Postgres:
#   podman run -d --name syntara-pg-test -e POSTGRES_PASSWORD=test \
#     -e POSTGRES_DB=syntara_test -p 55432:5432 postgres:15
SYNTARA_TEST_DATABASE_URL=postgresql+psycopg://postgres:test@localhost:55432/syntara_test \
  uv run --python 3.12 --with pytest --with pyyaml --with jsonschema --with sqlmodel \
    --with "psycopg[binary]" \
    pytest schemas/examples/http-request/test_register.py -o addopts=""
# Tear down:  podman rm -f syntara-pg-test
```

On Postgres the compiled DDL is `descriptor JSONB NOT NULL` plus
`CONSTRAINT node_types_image_ref_by_execution_type CHECK ((execution_type = 'container'
AND image_ref IS NOT NULL) OR (execution_type = 'in_process' AND image_ref IS NULL))`,
matching the `node_types` table in the architecture doc.

### 4. Real-image integration test

`test_integration.py` runs the **actual** execution-plane image
(`quay.io/ahetheri/http-executor:dev`), pipes JSON Lines commands through the
container, applies `adapter.map_result`, and asserts the resulting
`StandardOutputWrapper`. It also unit-tests the adapter mapping policy directly.
Container/network-dependent cases skip gracefully (no runtime, un-pullable image,
or blocked egress); the SSRF-block case needs no external network and always runs.

```bash
cd schemas/examples/http-request
uv run --python 3.12 test_integration.py           # standalone
# or: uv run --python 3.12 --with pytest pytest test_integration.py -o addopts=""
```

## Platform Integration

### From manifest to canvas

```
manifest.yaml ──build──▶ node-definition.json ──publish──▶ node_types.descriptor (JSONB)
     (author)               (compiled artifact)                (registry row)
                                                                     │
                                                                     ▼
                                                     React Flow canvas renders the
                                                     input form in <500ms from the
                                                     compiled descriptor (never the
                                                     source manifest)
```

The registry stores the compiled descriptor in `node_types.descriptor` alongside
`name`, `category`, `execution_type = container`, and `image_ref`. A table CHECK
constraint enforces that `container` nodes carry a non-null `image_ref`. Serving the
already-compiled JSON — rather than parsing YAML at request time — is what keeps canvas
form rendering under 500ms. See `test_register.py` for an executable walkthrough.

### Compilation
```bash
# Interim compiler (until the ao-sdk CLI ships)
./build-manifest.sh examples/http-request/manifest.yaml
# Produces: examples/http-request/node-definition.json  (gitignored build artifact)

# Future SDK CLI
ao-sdk build   # Produces: node-definition.json
```

### Registry Publication
```bash
ao-sdk publish \
  --image registry.syntara.io/nodes/http-request:1.0.0 \
  --definition node-definition.json
```

### Canvas Usage
1. Drag `HTTP Request` node onto workflow canvas
2. Configure inputs (URL, method, headers)
3. Link credential via UUID picker
4. Template expressions available: `${http_call.Result.body.data}`

## Security Notes

- **No raw secrets in manifest** — only `credential_id` UUIDs
- **Network egress enforced** via NetworkPolicy
- **Unprivileged container** — runs as non-root user
- **Resource limits** enforced by Kubernetes (500m CPU, 256Mi RAM)
