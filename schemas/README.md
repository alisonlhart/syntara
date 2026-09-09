# Syntara Node SDK - JSON Schemas

Official JSON Schema (Draft-07) specifications for the Syntara Automation Orchestrator Node SDK. These schemas define the API contract for all task execution nodes.

## Quick Start

### Install Validator

```bash
npm install -g ajv-cli ajv-formats
```

### Validate Schemas

```bash
cd syntara/schemas
./validate.sh
```

**Expected output:**
```
✓ PASS script-python-example
✓ PASS script-bash-example
✓ PASS http-github-api-example
✓ PASS http-webhook-example

All validations passed! ✓
```

### Create Your First Node

**Python script example:**
```json
{
  "nodeType": "script_executor",
  "inputs": {
    "script": "def main(name):\n    return f'Hello {name}!'\n",
    "language": "python3",
    "arguments": {"name": "Alice"}
  },
  "outputs": {
    "Result": "Hello Alice!",
    "StatusCode": 0,
    "StatusMessage": "Execution completed",
    "ErrorMessage": ""
  },
  "metadata": {
    "execution_timeout": 60,
    "resource_requirements": {
      "limits": {"cpu": "500m", "memory": "256Mi"},
      "requests": {"cpu": "100m", "memory": "128Mi"}
    }
  }
}
```

**Validate it:**
```bash
ajv validate \
  -s script.schema.json \
  -r common-definitions.json \
  -d my-node.json
```

## Schema Files

### [`common-definitions.json`](common-definitions.json)
Shared definitions referenced across all node types.

**Key Definitions:**

- **StandardOutputWrapper** - Immutable output contract
  ```json
  {
    "Result": <any>,           // Primary task payload
    "StatusCode": 0,           // 0 = success
    "StatusMessage": "...",    // Human-readable summary
    "ErrorMessage": ""         // Error details (if any)
  }
  ```

- **CredentialReference** - UUID-based resource injection (no raw secrets)
  ```json
  {
    "credential_id": "550e8400-...",
    "credential_mount_type": "env"  // or "file"
  }
  ```

- **ResourceRequirements** - Kubernetes CPU/memory limits
- **SchedulingControls** - Network egress allowlist + node affinity
- **DependencyDeclaration** - Platform version + capability requirements
- **ExecutionTimeout** - Max duration (1-86400 seconds)
- **WorkloadClassification** - Security classification (`"action"` | `"agentic"`)

### [`script.schema.json`](script.schema.json)
Containerized Python 3.12 / Bash 5.2 executor.

**Node Type:** `script_executor`

**Required Inputs:**
- `script` - Raw source code (max 1MB)
- `language` - `"python3"` or `"bash"`

**Optional:**
- `arguments` - Named (object) or positional (array)
- `environment_variables` - Additional env vars
- `working_directory` - Container working path (default: `/workspace`)

**Examples:** [script-python-example.json](examples/script-python-example.json), [script-bash-example.json](examples/script-bash-example.json)

### [`http_request.schema.json`](http_request.schema.json)
HTTP/REST API orchestrator.

**Node Type:** `http_request`

**Required Inputs:**
- `url` - HTTP/HTTPS endpoint (must start with `https?://`)
- `method` - `GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`

**Optional:**
- `headers` - HTTP headers
- `query_parameters` - URL query params
- `body` - Request payload (object for JSON, string for raw)
- `timeout_seconds` - Request timeout (default: 30s)
- `response_format` - `"json"`, `"text"`, `"auto"`
- `follow_redirects` - Boolean (default: true)
- `verify_ssl` - Boolean (default: true)

**Examples:** [http-github-api-example.json](examples/http-github-api-example.json), [http-webhook-example.json](examples/http-webhook-example.json)

## Key Concepts

### Backwards Compatibility

The `StandardOutputWrapper` structure is **immutable**. All nodes must return:
```json
{
  "Result": <any>,
  "StatusCode": <integer>,
  "StatusMessage": <string>,
  "ErrorMessage": <string>
}
```

This enables stable workflow template expressions:
```python
${script_task.Result.stdout}
${http_request.Result.body.id}
```

### Resource Injection (UUID Pattern)

**Never put raw secrets in workflows:**
```json
// ❌ NEVER
{"api_key": "sk_live_abc123..."}

// ✓ ALWAYS
{
  "credential_id": "550e8400-e29b-41d4-a716-446655440000",
  "credential_mount_type": "env"
}
```

**How it works:**
1. Workflow stores UUID reference
2. Platform validates UUID exists at deployment
3. Scheduler resolves at runtime (decrypts from vault)
4. Injects as K8s Secret (env vars or tmpfs mount)
5. Worker accesses via `os.environ['API_KEY']` or `/run/secrets/ssh-key`
6. Secret deleted on pod exit

**Same pattern for all resources:** credentials, inventory, projects, file sources.

### Network Egress Controls

Declare required endpoints in `scheduling_controls.connectivity_requirements`:

```json
{
  "metadata": {
    "scheduling_controls": {
      "connectivity_requirements": ["api.github.com", "pypi.org"]
    },
    "dependencies": {
      "capabilities": ["network-egress"]
    }
  }
}
```

Scheduler compiles OpenShift NetworkPolicy allowing egress **only** to declared endpoints. Undeclared connections timeout.

## Common Patterns

### Script with Credentials

```json
{
  "nodeType": "script_executor",
  "inputs": {
    "script": "import os\napi_key = os.environ['API_KEY']\nprint(f'Key: {api_key[:8]}...')\n",
    "language": "python3"
  },
  "secrets": {
    "credential_id": "550e8400-e29b-41d4-a716-446655440000",
    "credential_mount_type": "env"
  }
}
```

### HTTP Request with Retry

```json
{
  "nodeType": "http_request",
  "inputs": {
    "url": "https://api.example.com/data",
    "method": "GET",
    "timeout_seconds": 30
  },
  "metadata": {
    "retry_policy": {
      "max_attempts": 3,
      "initial_interval_seconds": 5,
      "backoff_coefficient": 2.0,
      "retryable_status_codes": [429, 500, 502, 503, 504]
    }
  }
}
```

### Resource-Intensive Task

```json
{
  "nodeType": "script_executor",
  "inputs": {...},
  "metadata": {
    "resource_requirements": {
      "limits": {"cpu": "4", "memory": "8Gi"},
      "requests": {"cpu": "4", "memory": "8Gi"}
    },
    "execution_timeout": 3600,
    "scheduling_controls": {
      "affinity_labels": ["workload=high-memory"]
    }
  }
}
```

## Validation

### Automated Testing

```bash
./validate.sh              # All examples
./validate.sh script       # Only script nodes
./validate.sh http         # Only HTTP nodes
```

### Manual Validation

```bash
ajv validate \
  -s script.schema.json \
  -r common-definitions.json \
  -d my-node.json \
  --strict=false
```

### Test Invalid Data

```bash
cat > test-invalid.json <<'EOF'
{
  "nodeType": "script_executor",
  "inputs": {
    "script": "print('hello')",
    "language": "ruby"
  }
}
EOF

ajv validate -s script.schema.json -r common-definitions.json -d test-invalid.json
# Expected: validation error (ruby not in enum)
```

## Troubleshooting

### Error: `data should have required property 'StatusCode'`

Missing required output fields. All nodes must return complete `StandardOutputWrapper`:

```json
"outputs": {
  "Result": <your-data>,
  "StatusCode": 0,
  "StatusMessage": "...",
  "ErrorMessage": ""
}
```

### Error: `data.inputs.url should match pattern`

URL must include protocol:

```json
// ❌ Wrong
"url": "api.github.com/repos"

// ✓ Correct
"url": "https://api.github.com/repos"
```

### Error: `data.inputs.body should NOT be valid`

GET/HEAD/DELETE methods cannot have a request body:

```json
// ❌ Wrong
{"method": "GET", "body": {...}}

// ✓ Correct
{"method": "POST", "body": {...}}
```

## Architecture

For deep dive on schema design, resource injection, and execution flow, see:
- **[node-sdk-architecture.md](node-sdk-architecture.md)** - Architecture principles, diagrams, flows
- **[VERIFICATION.md](VERIFICATION.md)** - Testing guide (6 verification methods)

## Examples

| Example | Type | Use Case |
|---------|------|----------|
| [script-python-example.json](examples/script-python-example.json) | Python 3.12 | Data processing with credential injection |
| [script-bash-example.json](examples/script-bash-example.json) | Bash 5.2 | Log analysis with threshold validation |
| [http-github-api-example.json](examples/http-github-api-example.json) | HTTP POST | GitHub API integration with auth |
| [http-webhook-example.json](examples/http-webhook-example.json) | HTTP GET | Simple webhook with query parameters |

---

**Schema Version:** 1.0.0  
**Platform Compatibility:** Syntara >=3.0.0  
**Last Updated:** 2026-09-08
