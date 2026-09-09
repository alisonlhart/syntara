# Script Executor Node

**Category:** `task`  
**Execution Type:** `container`  
**Workload Classification:** `action`

Executes Python 3.12 or Bash 5.2 scripts in isolated, unprivileged containers with credential injection, network egress controls, and custom resource limits.

## Execution Contract (Language-Agnostic)

While this reference implementation uses Python 3.12 as the node runtime, it demonstrates how **user scripts in any language** can be executed within the platform's execution contract.

### Input Contract

Platform injects inputs as JSON:

```json
{
  "script": "def main(name, count):\n    return [f'Hello {name}' for i in range(count)]",
  "language": "python3",
  "arguments": {
    "name": "Alice",
    "count": 3
  },
  "environment_variables": {
    "LOG_LEVEL": "INFO"
  },
  "working_directory": "/workspace"
}
```

### Output Contract

Node returns `StandardOutputWrapper` with script results:

```json
{
  "Result": {
    "return_value": ["Hello Alice", "Hello Alice", "Hello Alice"],
    "stdout": "",
    "stderr": "",
    "exit_code": 0
  },
  "StatusCode": 0,
  "StatusMessage": "Script execution completed",
  "ErrorMessage": ""
}
```

## Python Script Execution

Python scripts must define a `main()` function that accepts keyword arguments:

```python
def main(api_url, max_retries=3):
    """
    Fetch data from API with retry logic.
    
    Arguments are injected from the 'arguments' input field.
    """
    import requests
    
    for attempt in range(max_retries):
        try:
            resp = requests.get(api_url, timeout=10)
            resp.raise_for_status()
            return {
                "status": "success",
                "data": resp.json(),
                "attempts": attempt + 1
            }
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            continue
    
    return {"status": "failed"}
```

Canvas input:
```yaml
arguments:
  api_url: "https://api.example.com/data"
  max_retries: 5
```

## Bash Script Execution

Bash scripts receive positional arguments:

```bash
#!/bin/bash
# Process files with checksums

INPUT_DIR=$1
OUTPUT_DIR=$2

mkdir -p "$OUTPUT_DIR"

for file in "$INPUT_DIR"/*.txt; do
    filename=$(basename "$file")
    sha256sum "$file" > "$OUTPUT_DIR/$filename.sha256"
done

echo "Processed $(ls "$INPUT_DIR"/*.txt | wc -l) files"
```

Canvas input:
```yaml
language: bash
arguments:
  - "/data/input"
  - "/data/output"
```

## Credential Injection

Scripts can access platform-managed credentials:

### Environment Variable Mount (API Keys)

```python
import os

def main(repo_owner, repo_name):
    """Create GitHub issue using platform-injected token."""
    import requests
    
    github_token = os.environ.get("CREDENTIAL_VALUE")
    if not github_token:
        raise ValueError("GitHub credential not injected")
    
    resp = requests.post(
        f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues",
        headers={"Authorization": f"Bearer {github_token}"},
        json={"title": "Automated issue", "body": "Created by workflow"}
    )
    
    return {"issue_number": resp.json()["number"]}
```

### File Mount (SSH Keys, Certificates)

```bash
#!/bin/bash
# Clone private repo using platform-injected SSH key

SSH_KEY="/run/secrets/credential"
REPO_URL=$1

# Use credential from tmpfs mount
ssh-agent bash -c "
    ssh-add $SSH_KEY
    git clone $REPO_URL /workspace/repo
"

echo "Cloned repository to /workspace/repo"
```

Canvas configuration:
```yaml
secrets:
  credential_id: "550e8400-e29b-41d4-a716-446655440000"
  credential_mount_type: file  # Mounts to /run/secrets/credential
```

## Network Egress Control

Declared endpoints are enforced via Kubernetes NetworkPolicy:

```yaml
scheduling_controls:
  connectivity_requirements:
    - pypi.org           # Allow pip install
    - api.github.com     # Allow GitHub API
```

**Undeclared endpoints will timeout.** This prevents:
- Data exfiltration
- Unaudited external API calls
- Malicious network activity

## Resource Limits

```yaml
resource_requirements:
  limits:
    cpu: "1"        # 1 CPU core max
    memory: 512Mi   # OOMKilled if exceeded
  requests:
    cpu: 250m       # 0.25 CPU guaranteed
    memory: 256Mi
```

Hard limits enforced by Kubernetes. Scripts that exceed memory will be terminated with `exit code 137`.

## Testing Locally

### Python Script Test

```bash
# Create test input
cat > input.json <<'EOF'
{
  "script": "def main(x, y): return x + y",
  "language": "python3",
  "arguments": {"x": 10, "y": 32}
}
EOF

# Run node
python3 main.py < input.json
```

Expected output:
```json
{
  "Result": {
    "return_value": 42,
    "exit_code": 0
  },
  "StatusCode": 0,
  "StatusMessage": "Script execution completed",
  "ErrorMessage": ""
}
```

### Bash Script Test

```bash
cat > input.json <<'EOF'
{
  "script": "echo \"Args: $1, $2\"\ndate\nhostname",
  "language": "bash",
  "arguments": ["value1", "value2"]
}
EOF

python3 main.py < input.json
```

## Security Isolation

- **Unprivileged container** — no root access
- **Read-only root filesystem** — only `/tmp` and `/workspace` are writable
- **No host network** — isolated network namespace
- **Resource quotas** — CPU/memory hard limits
- **Network policies** — egress allowlist only
- **Credential isolation** — tmpfs mounts (RAM-backed, never persisted)

## Template Expressions in Workflows

Downstream nodes can reference script results:

```yaml
# Workflow step 1: Run Python script
- id: data_processor
  type: script_executor
  inputs:
    script: "def main(threshold): return {'valid_count': 42}"
    arguments:
      threshold: 100

# Workflow step 2: Use script output
- id: send_notification
  type: http_request
  inputs:
    url: "https://api.slack.com/webhooks/..."
    body:
      text: "Found ${data_processor.Result.return_value.valid_count} valid records"
```

## Platform Integration

### Compilation
```bash
ao-sdk build
# Validates manifest.yaml against common-definitions.json
# Produces: node-definition.json
```

### Registry Publication
```bash
ao-sdk publish \
  --image registry.syntara.io/nodes/script-executor:1.0.0 \
  --definition node-definition.json
```

### Containerfile Example
```dockerfile
FROM python:3.12-slim

# Install Bash (for bash language support)
RUN apt-get update && apt-get install -y bash && rm -rf /var/lib/apt/lists/*

# Copy node runtime
COPY main.py /app/main.py

# Create workspace directory
RUN mkdir -p /workspace && chmod 777 /workspace

# Run as non-root user
USER 1000:1000

ENTRYPOINT ["python3", "/app/main.py"]
```

## Error Handling

The node returns structured errors in the `StandardOutputWrapper`:

### Script Timeout
```json
{
  "Result": null,
  "StatusCode": 124,
  "StatusMessage": "Script execution timeout",
  "ErrorMessage": "Script exceeded 300 second timeout"
}
```

### Python Exception
```json
{
  "Result": null,
  "StatusCode": 1,
  "StatusMessage": "Script execution failed",
  "ErrorMessage": "ValueError: Invalid input format"
}
```

### Missing main() Function
```json
{
  "Result": null,
  "StatusCode": 1,
  "StatusMessage": "Script execution failed",
  "ErrorMessage": "No main() function found in script"
}
```

## Polyglot Runtime Alternative

The script executor node itself is a **wrapper runtime**. You could also implement this node in Go, Rust, or any other language:

### Go Implementation (Alternative)
```go
package main

import (
    "encoding/json"
    "os"
    "os/exec"
)

type Input struct {
    Script    string            `json:"script"`
    Language  string            `json:"language"`
    Arguments []string          `json:"arguments"`
}

func main() {
    var input Input
    json.NewDecoder(os.Stdin).Decode(&input)
    
    cmd := exec.Command(input.Language, "-c", input.Script)
    cmd.Args = append(cmd.Args, input.Arguments...)
    
    output, err := cmd.CombinedOutput()
    
    result := map[string]interface{}{
        "Result": map[string]interface{}{
            "stdout": string(output),
        },
        "StatusCode": 0,
        "StatusMessage": "Script completed",
        "ErrorMessage": "",
    }
    
    if err != nil {
        result["StatusCode"] = 1
        result["ErrorMessage"] = err.Error()
    }
    
    json.NewEncoder(os.Stdout).Encode(result)
}
```

The key is **honoring the execution contract** (JSON in → JSON out, StandardOutputWrapper), not the implementation language.
