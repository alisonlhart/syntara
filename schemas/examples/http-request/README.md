# HTTP Request Node

**Category:** `action`  
**Execution Type:** `container`  
**Workload Classification:** `action`

Non-blocking HTTP/HTTPS API orchestrator with credential injection, response parsing, and automatic retry logic. Supports REST, GraphQL, and webhook integrations.

## Execution Contract (Language-Agnostic)

This node demonstrates the platform's **polyglot execution model**. While this reference implementation uses Python 3.12, nodes can be written in **any language** that satisfies the execution contract:

### Input Contract
- Platform injects inputs as JSON via:
  - File: `/tmp/node_input.json` (path set in `NODE_INPUT_PATH` env var)
  - OR stdin (fallback)

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
- Node MUST write `StandardOutputWrapper` JSON to stdout
- Exit with `StatusCode` (0 = success, non-zero = failure)

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

### 2. Run with test input
```bash
echo '{
  "url": "https://httpbin.org/get",
  "method": "GET"
}' | podman run -i --rm my-http-node
```

Expected output:
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

## Platform Integration

### Compilation
```bash
ao-sdk build
# Produces: node-definition.json
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
