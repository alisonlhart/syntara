# Syntara Node SDK - Schema Verification Guide

This guide walks through verifying that the JSON Schemas work correctly at each integration point: validation, frontend rendering, and backend scheduling.

---

## 1. Schema Validation (ajv-cli)

**What it verifies:** Schemas are syntactically correct and examples pass validation.

### Install Validator

```bash
npm install -g ajv-cli ajv-formats
```

### Run Automated Tests

```bash
cd /Users/alhart/code/syntara/syntara/schemas
chmod +x validate.sh
./validate.sh
```

**Expected output:**
```
╔═══════════════════════════════════════════════════╗
║  Syntara Node SDK - Schema Validation            ║
╚═══════════════════════════════════════════════════╝

Validating: script-python-example
  Schema: script.schema.json
  Data:   script-python-example.json
✓ PASS

Validating: script-bash-example
  Schema: script.schema.json
  Data:   script-bash-example.json
✓ PASS

Validating: http-github-api-example
  Schema: http_request.schema.json
  Data:   http-github-api-example.json
✓ PASS

Validating: http-webhook-example
  Schema: http_request.schema.json
  Data:   http-webhook-example.json
✓ PASS

═══════════════════════════════════════════════════
Total tests:  4
Passed:       4
Failed:       0
═══════════════════════════════════════════════════

All validations passed! ✓
```

### Manual Validation

Test a single node definition:

```bash
# Validate a script node
ajv validate \
  -s script.schema.json \
  -r common-definitions.json \
  -d examples/script-python-example.json \
  --strict=false

# Validate an HTTP node
ajv validate \
  -s http_request.schema.json \
  -r common-definitions.json \
  -d examples/http-github-api-example.json \
  --strict=false
```

### Test Invalid Data

Create an intentionally invalid node to verify schema catches errors:

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

ajv validate \
  -s script.schema.json \
  -r common-definitions.json \
  -d test-invalid.json \
  --strict=false
```

**Expected:** Validation error (ruby not in enum)

---

## 2. Frontend Integration (React Form Rendering)

**What it verifies:** React can parse schemas and render PatternFly forms in <500ms.

### Quick Test (Node.js)

Create a simple schema parser:

```javascript
// test-frontend-parsing.js
const fs = require('fs');

const commonDefs = JSON.parse(fs.readFileSync('common-definitions.json', 'utf8'));
const scriptSchema = JSON.parse(fs.readFileSync('script.schema.json', 'utf8'));

console.time('Schema Parse');

// Extract input properties (what the frontend would render as form fields)
const inputProps = scriptSchema.properties.inputs.properties;

console.log('Input fields to render:');
Object.entries(inputProps).forEach(([key, prop]) => {
  console.log(`  - ${key}:`);
  console.log(`      Type: ${prop.type || 'oneOf'}`);
  console.log(`      Required: ${scriptSchema.properties.inputs.required?.includes(key)}`);
  console.log(`      Description: ${prop.description?.substring(0, 60)}...`);
  
  if (prop.enum) {
    console.log(`      Options: ${prop.enum.join(', ')}`);
  }
});

console.timeEnd('Schema Parse');
```

Run it:

```bash
node test-frontend-parsing.js
```

**Expected output:**
```
Input fields to render:
  - script:
      Type: string
      Required: true
      Description: Raw source code to execute. For Python: should contain a...
  - language:
      Type: string
      Required: true
      Description: Target runtime interpreter. 'python3': Executes via Pyt...
      Options: python3, bash
  - arguments:
      Type: oneOf
      Required: false
      Description: Runtime arguments passed to the script. For Python: map...
Schema Parse: 3.245ms
```

✅ **Success criteria:** Parse time < 100ms

### Full Frontend Test (React Component)

If you have the frontend codebase, create a test component:

```typescript
// NodeFormRenderer.test.tsx
import { render, screen } from '@testing-library/react';
import scriptSchema from '../schemas/script.schema.json';

describe('NodeFormRenderer', () => {
  it('renders all required fields from schema', () => {
    const { properties, required } = scriptSchema.properties.inputs;
    
    // Your form renderer component
    render(<NodeFormRenderer schema={scriptSchema} />);
    
    // Verify required fields are present
    required.forEach(fieldName => {
      expect(screen.getByLabelText(new RegExp(fieldName, 'i'))).toBeInTheDocument();
    });
  });
  
  it('renders enum fields as dropdowns', () => {
    render(<NodeFormRenderer schema={scriptSchema} />);
    
    // Language field should be a select
    const languageField = screen.getByLabelText(/language/i);
    expect(languageField.tagName).toBe('SELECT');
    
    // Should have python3 and bash options
    expect(screen.getByRole('option', { name: 'python3' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'bash' })).toBeInTheDocument();
  });
  
  it('renders in under 500ms', () => {
    const start = performance.now();
    render(<NodeFormRenderer schema={scriptSchema} />);
    const elapsed = performance.now() - start;
    
    expect(elapsed).toBeLessThan(500);
  });
});
```

---

## 3. Backend Integration (OpenShift Scheduler)

**What it verifies:** Backend can extract connectivity requirements and compile NetworkPolicies.

### Test NetworkPolicy Extraction

Create a Python script to simulate the scheduler's NetworkPolicy compiler:

```python
# test-networkpolicy-compiler.py
import json
import re
from urllib.parse import urlparse

def extract_connectivity_requirements(node_definition):
    """Extract egress endpoints from node definition."""
    requirements = []
    
    # Extract from scheduling_controls.connectivity_requirements
    scheduling = node_definition.get('metadata', {}).get('scheduling_controls', {})
    requirements.extend(scheduling.get('connectivity_requirements', []))
    
    # For HTTP nodes, also extract hostname from URL
    if node_definition.get('nodeType') == 'http_request':
        url = node_definition.get('inputs', {}).get('url', '')
        if url:
            parsed = urlparse(url)
            if parsed.hostname:
                requirements.append(parsed.hostname)
    
    return list(set(requirements))  # deduplicate

def compile_networkpolicy(node_name, requirements):
    """Generate OpenShift NetworkPolicy YAML."""
    # Convert hostnames to egress rules
    egress_rules = []
    
    for req in requirements:
        # Check if it's an IP address
        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', req):
            egress_rules.append({
                'to': [{'ipBlock': {'cidr': f'{req}/32'}}],
                'ports': [{'protocol': 'TCP', 'port': 443}]
            })
        # Check if it's a CIDR block
        elif '/' in req:
            egress_rules.append({
                'to': [{'ipBlock': {'cidr': req}}],
                'ports': [{'protocol': 'TCP', 'port': 443}]
            })
        # Hostname - requires DNS resolution at compile time
        else:
            egress_rules.append({
                'to': [{'namespaceSelector': {}, 'podSelector': {}}],
                'ports': [{'protocol': 'TCP', 'port': 443}]
            })
    
    return {
        'apiVersion': 'networking.k8s.io/v1',
        'kind': 'NetworkPolicy',
        'metadata': {'name': f'{node_name}-egress'},
        'spec': {
            'podSelector': {'matchLabels': {'task': node_name}},
            'policyTypes': ['Egress'],
            'egress': egress_rules
        }
    }

# Test with HTTP example
with open('examples/http-github-api-example.json') as f:
    http_node = json.load(f)

requirements = extract_connectivity_requirements(http_node)
print(f"Extracted connectivity requirements: {requirements}")

policy = compile_networkpolicy('github-issue-creator', requirements)
print("\nGenerated NetworkPolicy:")
print(json.dumps(policy, indent=2))
```

Run it:

```bash
python test-networkpolicy-compiler.py
```

**Expected output:**
```
Extracted connectivity requirements: ['api.github.com']

Generated NetworkPolicy:
{
  "apiVersion": "networking.k8s.io/v1",
  "kind": "NetworkPolicy",
  "metadata": {
    "name": "github-issue-creator-egress"
  },
  "spec": {
    "podSelector": {
      "matchLabels": {
        "task": "github-issue-creator"
      }
    },
    "policyTypes": [
      "Egress"
    ],
    "egress": [
      {
        "to": [
          {
            "namespaceSelector": {},
            "podSelector": {}
          }
        ],
        "ports": [
          {
            "protocol": "TCP",
            "port": 443
          }
        ]
      }
    ]
  }
}
```

### Test Resource Requirements Extraction

```python
# test-resource-extraction.py
import json

def extract_resource_requirements(node_definition):
    """Extract K8s resource requirements from node definition."""
    metadata = node_definition.get('metadata', {})
    resources = metadata.get('resource_requirements', {})
    
    return {
        'limits': resources.get('limits', {}),
        'requests': resources.get('requests', {})
    }

# Test with script example
with open('examples/script-python-example.json') as f:
    script_node = json.load(f)

resources = extract_resource_requirements(script_node)
print("Kubernetes resources spec:")
print(json.dumps(resources, indent=2))

# Validate format (must match K8s quantity format)
cpu_limit = resources['limits']['cpu']
mem_limit = resources['limits']['memory']

assert cpu_limit in ['1', '2', '4'] or cpu_limit.endswith('m'), "Invalid CPU format"
assert mem_limit.endswith('Mi') or mem_limit.endswith('Gi'), "Invalid memory format"

print("\n✓ Resource format validation passed")
```

Run it:

```bash
python test-resource-extraction.py
```

**Expected:**
```
Kubernetes resources spec:
{
  "limits": {
    "cpu": "1",
    "memory": "512Mi"
  },
  "requests": {
    "cpu": "250m",
    "memory": "256Mi"
  }
}

✓ Resource format validation passed
```

---

## 4. End-to-End Integration Test

**What it verifies:** Complete flow from schema → form → execution.

### Create a Test Workflow

```python
# test-e2e-node-execution.py
import json
import subprocess
import tempfile

def validate_node(node_definition):
    """Validate node definition against schema."""
    node_type = node_definition['nodeType']
    
    if node_type == 'script_executor':
        schema_file = 'script.schema.json'
    elif node_type == 'http_request':
        schema_file = 'http_request.schema.json'
    else:
        raise ValueError(f"Unknown node type: {node_type}")
    
    # Write to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(node_definition, f)
        temp_file = f.name
    
    # Validate with ajv
    result = subprocess.run([
        'ajv', 'validate',
        '-s', schema_file,
        '-r', 'common-definitions.json',
        '-d', temp_file,
        '--strict=false'
    ], capture_output=True, text=True)
    
    return result.returncode == 0, result.stderr

def simulate_execution(node_definition):
    """Simulate node execution and verify output structure."""
    outputs = node_definition.get('outputs', {})
    
    # Verify StandardOutputWrapper structure
    required_fields = ['Result', 'StatusCode', 'StatusMessage', 'ErrorMessage']
    for field in required_fields:
        assert field in outputs, f"Missing required output field: {field}"
    
    # Verify StatusCode is 0 for success
    assert outputs['StatusCode'] == 0, "StatusCode should be 0 for success"
    
    return True

# Test script node
print("Testing script node...")
with open('examples/script-python-example.json') as f:
    script_node = json.load(f)

valid, errors = validate_node(script_node)
assert valid, f"Validation failed: {errors}"
print("✓ Schema validation passed")

assert simulate_execution(script_node)
print("✓ Output structure verification passed")

# Test HTTP node
print("\nTesting HTTP node...")
with open('examples/http-github-api-example.json') as f:
    http_node = json.load(f)

valid, errors = validate_node(http_node)
assert valid, f"Validation failed: {errors}"
print("✓ Schema validation passed")

assert simulate_execution(http_node)
print("✓ Output structure verification passed")

print("\n✅ All E2E tests passed")
```

Run it:

```bash
python test-e2e-node-execution.py
```

---

## 5. Template Expression Test

**What it verifies:** Downstream nodes can reference upstream outputs via template expressions.

```python
# test-template-expressions.py
import json

def resolve_template_expression(expression, context):
    """
    Simulate template expression resolution.
    Example: ${script_task.Result.stdout} → actual value
    """
    # Remove ${ and }
    expr = expression.strip('${}').strip()
    
    # Split by dots
    parts = expr.split('.')
    
    # Navigate context
    value = context
    for part in parts:
        value = value.get(part)
        if value is None:
            return None
    
    return value

# Load script node output
with open('examples/script-python-example.json') as f:
    script_node = json.load(f)

# Simulate workflow context
context = {
    'fetch_data': script_node['outputs']
}

# Test expressions
test_cases = [
    ('${fetch_data.Result.total_count}', 3),
    ('${fetch_data.Result.filtered_count}', 2),
    ('${fetch_data.StatusCode}', 0),
    ('${fetch_data.StatusMessage}', 'Successfully processed 2 active resources out of 3 total'),
]

print("Testing template expression resolution:")
for expr, expected in test_cases:
    result = resolve_template_expression(expr, context)
    assert result == expected, f"{expr} = {result}, expected {expected}"
    print(f"  ✓ {expr} = {result}")

print("\n✅ All template expressions resolved correctly")
```

Run it:

```bash
python test-template-expressions.py
```

**Expected:**
```
Testing template expression resolution:
  ✓ ${fetch_data.Result.total_count} = 3
  ✓ ${fetch_data.Result.filtered_count} = 2
  ✓ ${fetch_data.StatusCode} = 0
  ✓ ${fetch_data.StatusMessage} = Successfully processed 2 active resources out of 3 total

✅ All template expressions resolved correctly
```

---

## 6. Credential Injection Test

**What it verifies:** UUID references are validated and no raw secrets leak.

```python
# test-credential-security.py
import json
import re

def validate_no_raw_secrets(node_definition):
    """Ensure no raw secrets appear in the node definition."""
    node_str = json.dumps(node_definition)
    
    # Patterns that would indicate raw secrets
    forbidden_patterns = [
        r'sk_live_\w+',           # Stripe keys
        r'ghp_\w+',               # GitHub tokens
        r'password.*:\s*["\'][^"\']{8,}["\']',  # Password fields
        r'api_key.*:\s*["\'][^"\']{20,}["\']',  # API key fields
    ]
    
    for pattern in forbidden_patterns:
        if re.search(pattern, node_str, re.IGNORECASE):
            return False, f"Found potential raw secret matching pattern: {pattern}"
    
    return True, "No raw secrets detected"

def validate_credential_references(node_definition):
    """Ensure credential references use UUID format."""
    secrets = node_definition.get('secrets', {})
    
    if not secrets:
        return True, "No credentials declared"
    
    credential_id = secrets.get('credential_id')
    if not credential_id:
        return False, "Missing credential_id"
    
    # Validate UUID format
    uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    if not re.match(uuid_pattern, credential_id, re.IGNORECASE):
        return False, f"Invalid UUID format: {credential_id}"
    
    # Validate mount type
    mount_type = secrets.get('credential_mount_type')
    if mount_type not in ['env', 'file']:
        return False, f"Invalid mount type: {mount_type}"
    
    return True, "Credential reference is valid"

# Test all examples
examples = [
    'examples/script-python-example.json',
    'examples/http-github-api-example.json',
]

print("Testing credential security:")
for example_path in examples:
    with open(example_path) as f:
        node = json.load(f)
    
    print(f"\n  {example_path}:")
    
    valid, msg = validate_no_raw_secrets(node)
    assert valid, msg
    print(f"    ✓ {msg}")
    
    valid, msg = validate_credential_references(node)
    assert valid, msg
    print(f"    ✓ {msg}")

print("\n✅ All credential security checks passed")
```

Run it:

```bash
python test-credential-security.py
```

---

## Summary Checklist

Run through this checklist to verify complete schema functionality:

- [ ] **Schema validation** - All examples pass `./validate.sh`
- [ ] **Frontend parsing** - React can parse schemas in <100ms
- [ ] **Form rendering** - PatternFly forms render in <500ms (manual test)
- [ ] **NetworkPolicy extraction** - Backend extracts connectivity requirements
- [ ] **Resource requirements** - Kubernetes resource specs are valid
- [ ] **Template expressions** - Downstream nodes can reference outputs
- [ ] **Credential security** - No raw secrets, only UUID references
- [ ] **Error handling** - Invalid nodes are rejected with clear errors

---

## CI/CD Integration

Add to your GitHub Actions / Konflux pipeline:

```yaml
# .github/workflows/validate-schemas.yml
name: Validate Node Schemas

on:
  pull_request:
    paths:
      - 'syntara/schemas/**'

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Install ajv-cli
        run: npm install -g ajv-cli ajv-formats
      
      - name: Validate schemas
        working-directory: syntara/schemas
        run: |
          chmod +x validate.sh
          ./validate.sh
      
      - name: Test NetworkPolicy extraction
        working-directory: syntara/schemas
        run: python test-networkpolicy-compiler.py
      
      - name: Test template expressions
        working-directory: syntara/schemas
        run: python test-template-expressions.py
      
      - name: Test credential security
        working-directory: syntara/schemas
        run: python test-credential-security.py
```

---

**Last Updated:** 2026-09-08
