# Sub-Workflow Trigger Node

**Category:** `trigger`  
**Execution Type:** `in_process`  
**Workload Classification:** N/A (not containerized)

Invokes a child workflow as a composable, reusable unit. Passes parent context and ingress payload, awaits terminal output, and returns result to parent workflow. Platform tracks invocation depth to prevent unbounded recursion.

## Execution Contract (In-Process Nodes)

**Critical distinction:** Unlike `container` nodes (e.g., `http_request`, `script_executor`), `in_process` nodes run as **built-in Temporal activities** within the orchestrator process itself.

### Production Execution Path

In production, this node type is **NOT** containerized. The orchestrator's `dynamic_workflow.py` implements the actual execution:

```python
from temporalio import workflow

@workflow.defn
class DynamicWorkflow:
    async def execute_node(self, node_def: dict):
        if node_def["execution_type"] == "in_process":
            # Built-in activity - runs in orchestrator process
            if node_def["nodeType"] == "subworkflow_trigger":
                result = await workflow.execute_child_workflow(
                    workflow="child_workflow_name",
                    args=[node_def["inputs"]["ingress_payload"]],
                    execution_timeout=node_def["inputs"]["timeout_seconds"]
                )
                return StandardOutputWrapper(Result=result, StatusCode=0, ...)
        elif node_def["execution_type"] == "container":
            # Dispatch to isolated worker pod
            return await self.execute_container_node(node_def)
```

The `main.py` in this package is a **conceptual reference** for testing and understanding the contract, not the production implementation.

## Input Contract

```json
{
  "workflow_id": "550e8400-e29b-41d4-a716-446655440000",
  "workflow_name": "process_order",
  "ingress_payload": {
    "order_id": "ORD-12345",
    "user_id": "USR-98765",
    "priority": "high"
  },
  "parent_execution_id": "exec_parent_20260909_abc123",
  "max_depth": 10,
  "timeout_seconds": 3600,
  "propagate_context": true
}
```

### Fields Explained

- **`workflow_id`** — UUID of the child workflow definition in the registry
- **`workflow_name`** — Human-readable name for tracing (e.g., `process_order`)
- **`ingress_payload`** — Data passed to child workflow (must conform to child's input schema)
- **`parent_execution_id`** — Parent workflow execution ID for lineage tracking
- **`max_depth`** — Recursion depth limit (default: 10, max: 50)
- **`timeout_seconds`** — Child workflow timeout (0 = no timeout, max: 24 hours)
- **`propagate_context`** — Inherit parent's credentials and execution tags

## Output Contract

Returns `StandardOutputWrapper` with child workflow result:

```json
{
  "Result": {
    "child_execution_id": "exec_child_process_order_1725897600_depth2",
    "child_workflow_name": "process_order",
    "status": "COMPLETED",
    "result": {
      "order_status": "processed",
      "items_count": 5
    },
    "elapsed_seconds": 42,
    "invocation_depth": 2,
    "parent_execution_id": "exec_parent_20260909_abc123"
  },
  "StatusCode": 0,
  "StatusMessage": "Child workflow 'process_order' completed successfully at depth 2",
  "ErrorMessage": ""
}
```

### Recursion Depth Tracking

The platform prevents unbounded recursion by tracking invocation depth:

```
Parent Workflow (depth 0)
  └─> Sub-Workflow A (depth 1)
        └─> Sub-Workflow B (depth 2)
              └─> Sub-Workflow C (depth 3)
                    └─> ... (up to max_depth)
```

If `max_depth` is exceeded, the node returns:

```json
{
  "Result": null,
  "StatusCode": 1,
  "StatusMessage": "Maximum recursion depth exceeded",
  "ErrorMessage": "Depth 11 exceeds max_depth 10. Preventing unbounded recursion."
}
```

## Workflow Composition Example

### Parent Workflow
```yaml
workflow_name: e2e_order_processing
steps:
  - id: validate_order
    type: http_request
    inputs:
      url: "https://api.example.com/orders/validate"
      method: POST
      body:
        order_id: "${trigger.order_id}"

  - id: process_payment
    type: subworkflow_trigger  # Reusable sub-workflow
    inputs:
      workflow_id: "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
      workflow_name: payment_processing
      ingress_payload:
        order_id: "${trigger.order_id}"
        amount: "${validate_order.Result.body.total_amount}"
      timeout_seconds: 600

  - id: send_confirmation
    type: http_request
    inputs:
      url: "https://api.example.com/notifications"
      method: POST
      body:
        message: "Payment processed: ${process_payment.Result.result.transaction_id}"
```

### Child Workflow (Reusable Payment Processing)
```yaml
workflow_name: payment_processing
trigger: subworkflow_trigger

steps:
  - id: charge_card
    type: http_request
    inputs:
      url: "https://payments.stripe.com/charges"
      method: POST
      body:
        amount: "${trigger.amount}"

  - id: update_ledger
    type: script_executor
    inputs:
      script: "def main(txn_id): ..."
      arguments:
        txn_id: "${charge_card.Result.body.id}"

outputs:
  transaction_id: "${charge_card.Result.body.id}"
  ledger_entry_id: "${update_ledger.Result.return_value.entry_id}"
```

## Template Expressions

Parent workflows reference child results:

```yaml
# After sub-workflow completes, reference its outputs:
${process_payment.Result.result.transaction_id}
${process_payment.Result.elapsed_seconds}
${process_payment.Result.invocation_depth}
```

## Comparison: `in_process` vs `container` Nodes

| Aspect | `in_process` (this node) | `container` |
|--------|-------------------------|-------------|
| **Execution** | Built-in Temporal activity | Isolated worker pod |
| **Provisioning** | None (already running) | Pod creation overhead |
| **Security** | Trusted platform code | Zero-trust isolation |
| **Resource limits** | Shared orchestrator limits | Per-pod CPU/memory quotas |
| **Network** | Orchestrator network | Restricted NetworkPolicy |
| **Credentials** | Full platform access | Injected via tmpfs/env |
| **Use cases** | Control-plane logic, workflow composition | User code, external integrations |
| **Examples** | Loop, Condition, Switch, Converge, `subworkflow_trigger` | `http_request`, `script_executor`, AAP job templates |

## Why In-Process for Sub-Workflows?

Sub-workflow invocation is a **first-class Temporal primitive**:
- No network overhead (same process)
- Guaranteed ACID semantics (workflow state machine)
- No credential handoff needed (parent and child share orchestrator context)
- Faster execution (no pod provisioning delay)

Containerizing workflow composition would add latency and complexity without security benefit, as the child workflow itself may spawn container nodes that require isolation.

## Testing Locally (Conceptual)

Since `in_process` nodes don't run in containers, this script is for **understanding the contract**, not production use:

```bash
cat > input.json <<'EOF'
{
  "workflow_id": "550e8400-e29b-41d4-a716-446655440000",
  "workflow_name": "test_child",
  "ingress_payload": {"key": "value"},
  "parent_execution_id": "exec_test_parent"
}
EOF

python3 main.py < input.json
```

Expected output (simulated):
```json
{
  "Result": {
    "child_execution_id": "exec_child_test_child_1725897600_depth1",
    "status": "COMPLETED",
    "result": {
      "status": "success",
      "processed_items": 1
    }
  },
  "StatusCode": 0,
  "StatusMessage": "Child workflow 'test_child' completed successfully at depth 1",
  "ErrorMessage": ""
}
```

## Platform Integration

### Compilation
```bash
ao-sdk build
# Validates manifest.yaml
# Produces: node-definition.json
```

### Registry Publication
```bash
ao-sdk publish \
  --definition node-definition.json \
  # NO --image flag for in_process nodes
```

**Critical:** The `node_types` table enforces this constraint:
```sql
CHECK (
  (execution_type = 'container'  AND image_ref IS NOT NULL) OR
  (execution_type = 'in_process' AND image_ref IS NULL)
)
```

Attempting to publish an `in_process` node with an `image_ref` will fail.

## Security Notes

- **Recursion depth bounded** — prevents fork bombs and infinite loops
- **Timeout enforced** — child workflows cannot run indefinitely
- **Context propagation controlled** — parent can isolate or share credentials
- **Lineage tracked** — full parent → child execution tree auditable

## Error Handling

### Child Workflow Failure
```json
{
  "Result": null,
  "StatusCode": 1,
  "StatusMessage": "Child workflow invocation failed",
  "ErrorMessage": "WorkflowExecutionError: Child workflow timed out after 3600s"
}
```

### Invalid Ingress Payload
```json
{
  "Result": null,
  "StatusCode": 1,
  "StatusMessage": "Child workflow invocation failed",
  "ErrorMessage": "ValidationError: ingress_payload does not match child workflow schema"
}
```

### Workflow Not Found
```json
{
  "Result": null,
  "StatusCode": 1,
  "StatusMessage": "Child workflow invocation failed",
  "ErrorMessage": "WorkflowNotFoundError: workflow_id '550e8400-...' not found in registry"
}
```

## Real-World Use Cases

### 1. Multi-Tenant Processing
Parent workflow fans out sub-workflows per tenant:
```yaml
- id: process_all_tenants
  type: loop
  inputs:
    collection: "${trigger.tenant_ids}"
    body:
      - id: tenant_workflow
        type: subworkflow_trigger
        inputs:
          workflow_name: tenant_data_sync
          ingress_payload:
            tenant_id: "${item}"
```

### 2. Approval Gates with Timeout
Child workflow waits for approval:
```yaml
- id: await_approval
  type: subworkflow_trigger
  inputs:
    workflow_name: approval_gate
    timeout_seconds: 86400  # 24 hours
    ingress_payload:
      approver_email: "manager@example.com"
      request_details: "${trigger.request}"
```

### 3. Recursive Data Processing
Process nested data structures:
```yaml
- id: process_node
  type: subworkflow_trigger
  inputs:
    workflow_name: tree_traversal
    max_depth: 20
    ingress_payload:
      node_id: "${trigger.root_node_id}"
```

The platform prevents infinite recursion even if the data structure contains cycles.
