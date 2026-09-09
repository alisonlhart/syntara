# Syntara Node SDK - JSON Schemas

Official JSON Schema (Draft-07) specifications for the Syntara Automation Orchestrator Node SDK. These schemas define the API contract for all task execution nodes.

## Quick Start

### Node Authoring Workflow

Developers author nodes using **YAML manifests** (`manifest.yaml`), which are validated against JSON Schemas and compiled to JSON for registry storage.

**Authoring Pipeline:**
```
manifest.yaml → ao-sdk validate → ao-sdk build → manifest.json → Registry
```

**Standard Package Layout:**
```
my-custom-node/
├── manifest.yaml         # Primary authoring manifest (YAML)
├── main.py              # Imperative execution code
├── requirements.txt     # Python dependencies (optional)
└── README.md
```

### Install Dependencies

```bash
# YAML processor (required for manifest compilation)
brew install yq  # macOS
# or see https://github.com/mikefarah/yq for other platforms

# JSON Schema validator (optional, for advanced validation)
npm install -g ajv-cli ajv-formats
```

**Note:** The `ao-sdk` CLI is a future tool. Current workflow uses `build-manifest.sh` as an interim solution.

### Compile Manifests (Current Workflow)

```bash
cd syntara/schemas

# Compile manifest.yaml → node-definition.json
./build-manifest.sh examples/http-request/manifest.yaml
./build-manifest.sh examples/script-executor/manifest.yaml
./build-manifest.sh examples/subworkflow-trigger/manifest.yaml
```

**Expected output:**
```
╔═══════════════════════════════════════════════════╗
║  Syntara Node SDK - Manifest Compiler (Interim)  ║
╚═══════════════════════════════════════════════════╝

Input:  examples/http-request/manifest.yaml
Output: examples/http-request/node-definition.json

[1/3] Converting YAML to JSON...
✓ Conversion successful

[2/3] Validating required fields...
✓ Required fields present

[3/3] Validating against common-definitions.json...
✓ Basic validation passed

╔═══════════════════════════════════════════════════╗
║  ✓ Build successful                               ║
╚═══════════════════════════════════════════════════╝

Compiled node definition: examples/http-request/node-definition.json
```

### Create Your First Node

**Create `manifest.yaml`:**
```yaml
# Simple Python script node
nodeType: script_executor
category: task
execution_type: container  # Required field

displayName: Hello World Script
description: Simple greeting script

inputs:
  properties:
    script:
      type: string
      description: Python script code
      
    language:
      type: string
      enum: [python3]
      default: python3
      
  required: [script, language]

outputs:
  # Reference StandardOutputWrapper from platform meta-schema
  $ref: "../common-definitions.json#/definitions/StandardOutputWrapper"

metadata:
  workload_classification: action
  execution_timeout: 60
  resource_requirements:
    limits: {cpu: 500m, memory: 256Mi}
    requests: {cpu: 100m, memory: 128Mi}
```

**Compile it:**
```bash
# Current workflow (interim)
./build-manifest.sh my-node/manifest.yaml
# Produces: my-node/node-definition.json

# Future SDK CLI (not yet implemented):
# ao-sdk build my-node/manifest.yaml
```

## Schema Files

### Platform Meta-Schema

The Syntara Node SDK maintains a **single platform meta-schema** that defines shared types, enums, and validation rules:

#### [`common-definitions.json`](common-definitions.json)
**SOLE platform meta-schema** (JSON Schema Draft-07) defining:

- **`NodeCategory`** - Four-category taxonomy: `"action"`, `"task"`, `"workflow"`, `"trigger"`
- **`NodeExecutionType`** - Execution plane fork: `"in_process"` (built-in Temporal activity) | `"container"` (isolated worker pod)
- **`WorkloadClassification`** - Credential access control: `"action"` | `"agentic"`
- **`StandardOutputWrapper`** - Immutable output contract:
  ```json
  {
    "Result": <any>,           // Primary task payload
    "StatusCode": 0,           // 0 = success
    "StatusMessage": "...",    // Human-readable summary
    "ErrorMessage": ""         // Error details (if any)
  }
  ```
- **`CredentialReference`** - UUID-based resource injection (no raw secrets):
  ```json
  {
    "credential_id": "550e8400-...",
    "credential_mount_type": "env"  // or "file"
  }
  ```
- **`ResourceRequirements`** - Kubernetes CPU/memory limits
- **`SchedulingControls`** - Network egress allowlist + node affinity
- **`DependencyDeclaration`** - Platform version + capability requirements

### Node Definitions

Individual node schemas are **not** maintained as separate `.schema.json` files. Instead:

1. **Developers author** `manifest.yaml` files (human-friendly YAML with comments)
2. **SDK validates** manifests against `common-definitions.json` via JSON Schema Draft-07
3. **SDK compiles** manifests into `node-definition.json` build artifacts via `ao-sdk build`
4. **Registry persists** compiled artifacts in the `node_types.descriptor` JSONB column
5. **Orchestrator and canvas consume** the compiled artifacts (not the source manifests)

**Reference implementations:** See [examples/](#example-node-packages) for complete node packages with manifest.yaml, execution code, and documentation.

## Key Concepts

### Node Taxonomy (4 Categories)

All nodes are classified into one of four categories:

| Category | Purpose | Examples |
|----------|---------|----------|
| **action** | Domain and API integrations | HTTP Request, REST API, AAP Job Templates |
| **workflow** | In-memory control-plane logic | Loop, Condition, Switch, Sub-workflow |
| **task** | Atomic compute and script executors | Python 3.12, Bash 5.2 |
| **trigger** | Event entry points | Webhook, Schedule, Manual |

**Example:**
```json
{
  "nodeType": "script_executor",
  "category": "task",
  "inputs": {...}
}
```

The `category` field is **required** and used for:
- UI organization and filtering
- Routing decisions
- Capability validation
- Architectural enforcement

**Note:** `WorkloadClassification` (`"deterministic"` | `"agentic"`) is separate and used specifically for credential access control (preventing LLM-driven nodes from accessing infrastructure credentials).

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

**Note:** Examples below show YAML authoring format (what developers write). These are validated and compiled to JSON for registry storage.

### Script with Credentials

```yaml
nodeType: script_executor
category: task

inputs:
  properties:
    script:
      type: string
      # Access credential via os.environ
      default: |
        import os
        api_key = os.environ['API_KEY']
        print(f'Key: {api_key[:8]}...')
    
    language:
      type: string
      default: python3
      
  required: [script, language]

secrets:
  credential_id: 550e8400-e29b-41d4-a716-446655440000
  credential_mount_type: env  # Mount as environment variable
```

### HTTP Request with Retry

```yaml
nodeType: http_request
category: action

inputs:
  properties:
    url:
      type: string
      default: https://api.example.com/data
    
    method:
      type: string
      default: GET
    
    timeout_seconds:
      type: integer
      default: 30
      
  required: [url, method]

metadata:
  retry_policy:
    max_attempts: 3
    initial_interval_seconds: 5
    backoff_coefficient: 2.0  # Exponential backoff: 5s, 10s, 20s
    retryable_status_codes: [429, 500, 502, 503, 504]
```

### Resource-Intensive Task

```yaml
nodeType: script_executor
category: task

inputs:
  properties:
    script: {type: string}
    language: {type: string, default: python3}
  required: [script, language]

metadata:
  resource_requirements:
    limits:
      cpu: "4"        # 4 CPU cores max
      memory: 8Gi     # 8 GiB max
    requests:
      cpu: "4"        # 4 CPU cores guaranteed (no overcommit)
      memory: 8Gi
  
  execution_timeout: 3600  # 1 hour max
  
  scheduling_controls:
    affinity_labels:
      - workload=high-memory  # Target high-memory worker pool
```

## Validation & Compilation

### Current Workflow (Interim)

Use the `build-manifest.sh` script to compile and validate manifests:

```bash
# Compile and validate a manifest
./build-manifest.sh examples/http-request/manifest.yaml

# The script:
# 1. Converts manifest.yaml → node-definition.json (using yq)
# 2. Validates required fields (nodeType, category, execution_type, inputs)
# 3. Validates enum values (NodeCategory, NodeExecutionType)
# 4. Produces compiled node-definition.json artifact
```

**What's validated:**
- ✓ YAML → JSON conversion
- ✓ Required fields present
- ✓ Valid `category` enum (`action`, `task`, `workflow`, `trigger`)
- ✓ Valid `execution_type` enum (`in_process`, `container`)

**What's NOT yet validated** (requires future `ao-sdk`):
- ✗ `StandardOutputWrapper` structure compliance
- ✗ `$ref` resolution to `common-definitions.json`
- ✗ `execution_type` + `image_ref` consistency checks
- ✗ Full JSON Schema Draft-07 validation

### Future SDK Workflow

When `ao-sdk` CLI is implemented:

```bash
# Full validation against common-definitions.json
ao-sdk validate manifest.yaml

# Compile with complete JSON Schema validation
ao-sdk build manifest.yaml

# Publish to registry
ao-sdk publish \
  --definition node-definition.json \
  --image registry.syntara.io/nodes/my-node:1.0.0
```

The SDK will validate:
- JSON Schema Draft-07 compliance
- Required fields (nodeType, category, execution_type, inputs, outputs)
- `$ref` resolution to `common-definitions.json`
- Enum constraints (NodeCategory, NodeExecutionType, WorkloadClassification)
- `execution_type` + `image_ref` consistency (container nodes require image_ref)
- StandardOutputWrapper immutability

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

## Example Node Packages

Complete reference implementations organized as encapsulated node packages under `examples/`. Each package contains:
- **`manifest.yaml`** - Sole declarative authoring source (human-friendly YAML)
- **`main.py`** - Imperative execution entrypoint (demonstrates language-agnostic contract)
- **`README.md`** - Node documentation, usage examples, and integration guide

### Available Examples

| Package | Category | Execution Type | Description |
|---------|----------|----------------|-------------|
| [http-request/](examples/http-request/) | `action` | `container` | REST/GraphQL/webhook orchestrator with credential injection and retry logic |
| [script-executor/](examples/script-executor/) | `task` | `container` | Python 3.12 / Bash 5.2 script executor with sandboxing and network controls |
| [subworkflow-trigger/](examples/subworkflow-trigger/) | `trigger` | `in_process` | Sub-workflow invocation with recursion depth tracking and context propagation |

### Package Structure

```
examples/
├── http-request/              # Action Node (container)
│   ├── manifest.yaml         # Category: action, execution_type: container
│   ├── main.py              # Python reference implementation
│   └── README.md            # Polyglot examples (Python, Bash+curl, Go)
│
├── script-executor/          # Task Node (container)
│   ├── manifest.yaml         # Category: task, execution_type: container
│   ├── main.py              # Script wrapper runtime
│   └── README.md            # Python/Bash execution patterns
│
└── subworkflow-trigger/      # Trigger Node (in_process)
    ├── manifest.yaml         # Category: trigger, execution_type: in_process
    ├── main.py              # Conceptual reference (production runs in orchestrator)
    └── README.md            # Temporal child workflow integration

```

### Language-Agnostic Execution Contract

While reference implementations use **Python 3.12**, the platform is completely **polyglot**. Nodes can be written in any language that satisfies the execution contract:

**Input:** JSON payload (stdin or file)  
**Output:** `StandardOutputWrapper` JSON (stdout)  
**Exit code:** Matches `StatusCode` field

Examples in package READMEs demonstrate implementations in:
- Python 3.12 (all packages)
- Bash + curl + jq (http-request)
- Go (http-request, script-executor)

---

**Schema Version:** 1.0.0  
**Platform Compatibility:** Syntara >=3.0.0  
**Last Updated:** 2026-09-08
