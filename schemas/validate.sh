#!/bin/bash
# Syntara Node SDK - Schema Validation Script
#
# Validates COMPILED JSON node definitions against the official JSON Schemas.
# For YAML manifest validation, use: ao-sdk validate manifest.yaml
#
# This script is primarily for testing the compiled JSON format that gets
# stored in the registry after YAML manifests are built via ao-sdk build.
#
# Usage:
#   ./validate.sh                    # Validate all JSON examples
#   ./validate.sh script            # Validate only script examples
#   ./validate.sh http              # Validate only HTTP examples
#   ./validate.sh <example-file>    # Validate specific file

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMAS_DIR="$SCRIPT_DIR"
EXAMPLES_DIR="$SCRIPT_DIR/examples"

# Check if ajv-cli is installed
if ! command -v ajv &> /dev/null; then
    echo -e "${RED}Error: ajv-cli is not installed${NC}"
    echo "Install it with: npm install -g ajv-cli ajv-formats"
    exit 1
fi

echo -e "${BLUE}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Syntara Node SDK - Schema Validation            ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════╝${NC}"
echo

validate_file() {
    local data_file="$1"
    local schema_file="$2"
    local node_name="$3"

    echo -e "${YELLOW}Validating:${NC} $node_name"
    echo -e "  Schema: $(basename "$schema_file")"
    echo -e "  Data:   $(basename "$data_file")"

    if ajv validate \
        -s "$schema_file" \
        -r "$SCHEMAS_DIR/common-definitions.json" \
        -d "$data_file" \
        --strict=false \
        --all-errors 2>&1 | grep -q "valid"; then
        echo -e "${GREEN}✓ PASS${NC}\n"
        return 0
    else
        echo -e "${RED}✗ FAIL${NC}"
        ajv validate \
            -s "$schema_file" \
            -r "$SCHEMAS_DIR/common-definitions.json" \
            -d "$data_file" \
            --strict=false \
            --all-errors 2>&1 || true
        echo
        return 1
    fi
}

# Parse command line arguments
FILTER="${1:-all}"

TOTAL=0
PASSED=0
FAILED=0

# Validate script examples
if [[ "$FILTER" == "all" || "$FILTER" == "script" || -f "$FILTER" ]]; then
    for example in "$EXAMPLES_DIR"/script-*.json; do
        if [[ ! -f "$example" ]]; then
            continue
        fi

        if [[ "$FILTER" != "all" && "$FILTER" != "script" && "$FILTER" != "$example" ]]; then
            continue
        fi

        TOTAL=$((TOTAL + 1))
        if validate_file "$example" "$SCHEMAS_DIR/script.schema.json" "$(basename "$example" .json)"; then
            PASSED=$((PASSED + 1))
        else
            FAILED=$((FAILED + 1))
        fi
    done
fi

# Validate HTTP request examples
if [[ "$FILTER" == "all" || "$FILTER" == "http" || -f "$FILTER" ]]; then
    for example in "$EXAMPLES_DIR"/http-*.json; do
        if [[ ! -f "$example" ]]; then
            continue
        fi

        if [[ "$FILTER" != "all" && "$FILTER" != "http" && "$FILTER" != "$example" ]]; then
            continue
        fi

        TOTAL=$((TOTAL + 1))
        if validate_file "$example" "$SCHEMAS_DIR/http_request.schema.json" "$(basename "$example" .json)"; then
            PASSED=$((PASSED + 1))
        else
            FAILED=$((FAILED + 1))
        fi
    done
fi

# Summary
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo -e "Total tests:  $TOTAL"
echo -e "${GREEN}Passed:       $PASSED${NC}"
if [[ $FAILED -gt 0 ]]; then
    echo -e "${RED}Failed:       $FAILED${NC}"
else
    echo -e "Failed:       $FAILED"
fi
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"

if [[ $FAILED -gt 0 ]]; then
    exit 1
else
    echo -e "\n${GREEN}All validations passed! ✓${NC}\n"
    exit 0
fi
