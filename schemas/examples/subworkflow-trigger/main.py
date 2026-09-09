#!/usr/bin/env python3
"""
Sub-Workflow Trigger Node - Reference Implementation

IMPORTANT: This is a CONCEPTUAL reference for in_process nodes.
In production, this node runs as a built-in Temporal activity within
the orchestrator process, NOT in an isolated container.

This reference demonstrates:
1. The input contract (parent context + ingress payload)
2. The output contract (StandardOutputWrapper with child workflow result)
3. How recursion depth is tracked and bounded

The actual Temporal activity implementation is in the orchestrator's
`dynamic_workflow.py`, where it calls Temporal's child workflow API.
"""

import json
import os
import sys
import time
from typing import Any, Dict


def load_inputs() -> Dict[str, Any]:
    """Load node inputs from JSON file or stdin."""
    input_path = os.environ.get("NODE_INPUT_PATH", "/tmp/node_input.json")

    if os.path.exists(input_path):
        with open(input_path, "r") as f:
            return json.load(f)

    return json.load(sys.stdin)


def invoke_child_workflow(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Conceptual child workflow invocation.

    In production, this is replaced by Temporal's built-in child workflow API:

    from temporalio import workflow

    child_result = await workflow.execute_child_workflow(
        workflow="child_workflow_name",
        id=inputs["workflow_id"],
        args=[inputs["ingress_payload"]],
        parent_close_policy=ParentClosePolicy.ABANDON,
        execution_timeout=inputs["timeout_seconds"]
    )
    """
    workflow_id = inputs["workflow_id"]
    workflow_name = inputs["workflow_name"]
    ingress_payload = inputs["ingress_payload"]
    parent_execution_id = inputs.get("parent_execution_id", "exec_root")
    max_depth = inputs.get("max_depth", 10)
    timeout_seconds = inputs.get("timeout_seconds", 3600)
    propagate_context = inputs.get("propagate_context", True)

    # Extract current depth from parent execution ID
    # Format: exec_parent_20260909_abc123_depth2
    current_depth = 1
    if "depth" in parent_execution_id:
        depth_part = parent_execution_id.split("_depth")[-1]
        try:
            current_depth = int(depth_part) + 1
        except ValueError:
            current_depth = 1

    # Check recursion depth limit
    if current_depth > max_depth:
        return {
            "Result": None,
            "StatusCode": 1,
            "StatusMessage": "Maximum recursion depth exceeded",
            "ErrorMessage": f"Depth {current_depth} exceeds max_depth {max_depth}. Preventing unbounded recursion."
        }

    # Generate child execution ID with depth tracking
    child_execution_id = f"exec_child_{workflow_name}_{int(time.time())}_depth{current_depth}"

    # In production, this is a Temporal child workflow invocation
    # For this reference, we simulate a successful child workflow completion
    start_time = time.time()

    try:
        # PRODUCTION CODE WOULD BE:
        # from temporalio import workflow
        # child_result = await workflow.execute_child_workflow(...)

        # Simulated child workflow result
        child_result = {
            "status": "success",
            "processed_items": len(ingress_payload) if isinstance(ingress_payload, dict) else 0,
            "ingress_payload": ingress_payload
        }

        elapsed_seconds = int(time.time() - start_time)

        # Return StandardOutputWrapper with child workflow result
        return {
            "Result": {
                "child_execution_id": child_execution_id,
                "child_workflow_name": workflow_name,
                "status": "COMPLETED",
                "result": child_result,
                "elapsed_seconds": elapsed_seconds,
                "invocation_depth": current_depth,
                "parent_execution_id": parent_execution_id
            },
            "StatusCode": 0,
            "StatusMessage": f"Child workflow '{workflow_name}' completed successfully at depth {current_depth}",
            "ErrorMessage": ""
        }

    except Exception as e:
        return {
            "Result": None,
            "StatusCode": 1,
            "StatusMessage": "Child workflow invocation failed",
            "ErrorMessage": f"{type(e).__name__}: {str(e)}"
        }


def main():
    """
    Node execution entrypoint.

    NOTE: For in_process nodes, this entrypoint is CONCEPTUAL.
    Production execution happens via Temporal activities in dynamic_workflow.py.
    """
    try:
        inputs = load_inputs()
        result = invoke_child_workflow(inputs)

        # Write StandardOutputWrapper to stdout
        print(json.dumps(result, indent=2))
        sys.exit(result["StatusCode"])

    except Exception as e:
        # Fatal error during input parsing
        error_output = {
            "Result": None,
            "StatusCode": 255,
            "StatusMessage": "Node execution failed",
            "ErrorMessage": f"Fatal error: {type(e).__name__}: {str(e)}"
        }
        print(json.dumps(error_output, indent=2))
        sys.exit(255)


if __name__ == "__main__":
    # This script is for reference and testing only.
    # Production in_process nodes are executed directly by Temporal activities.
    main()
