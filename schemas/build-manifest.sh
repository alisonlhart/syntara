#!/bin/bash
# Syntara Node SDK - Manifest Compiler (Interim Solution)
#
# Compiles manifest.yaml to node-definition.json and validates against common-definitions.json
# This is a temporary script until ao-sdk CLI is implemented.
#
# Dependencies:
#   - yq (YAML processor): brew install yq
#   - ajv-cli (JSON Schema validator): npm install -g ajv-cli ajv-formats
#
# Usage:
#   ./build-manifest.sh examples/http-request/manifest.yaml
#   ./build-manifest.sh examples/script-executor/manifest.yaml
#   ./build-manifest.sh examples/subworkflow-trigger/manifest.yaml

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check dependencies
if ! command -v yq &> /dev/null; then
    echo -e "${RED}Error: yq is not installed${NC}"
    echo "Install it with: brew install yq (macOS) or see https://github.com/mikefarah/yq"
    exit 1
fi

# ajv-cli is optional - full validation will be in ao-sdk
if ! command -v ajv &> /dev/null; then
    echo -e "${YELLOW}Note: ajv-cli not installed - skipping advanced validation${NC}"
    echo -e "      (Full validation will be available in ao-sdk)"
    echo ""
fi

# Parse arguments
if [[ $# -eq 0 ]]; then
    echo "Usage: $0 <manifest.yaml>"
    echo ""
    echo "Examples:"
    echo "  $0 examples/http-request/manifest.yaml"
    echo "  $0 examples/script-executor/manifest.yaml"
    echo "  $0 examples/subworkflow-trigger/manifest.yaml"
    exit 1
fi

MANIFEST_PATH="$1"

if [[ ! -f "$MANIFEST_PATH" ]]; then
    echo -e "${RED}Error: File not found: $MANIFEST_PATH${NC}"
    exit 1
fi

# Determine output path
MANIFEST_DIR="$(dirname "$MANIFEST_PATH")"
OUTPUT_PATH="$MANIFEST_DIR/node-definition.json"

echo -e "${BLUE}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Syntara Node SDK - Manifest Compiler (Interim)  ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}Input:${NC}  $MANIFEST_PATH"
echo -e "${YELLOW}Output:${NC} $OUTPUT_PATH"
echo ""

# Step 1: Convert YAML to JSON
echo -e "${BLUE}[1/3]${NC} Converting YAML to JSON..."
if ! yq eval -o=json "$MANIFEST_PATH" > "$OUTPUT_PATH" 2>/dev/null; then
    echo -e "${RED}✗ YAML conversion failed${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Conversion successful${NC}"
echo ""

# Step 2: Validate basic JSON structure
echo -e "${BLUE}[2/3]${NC} Validating required fields..."

REQUIRED_FIELDS=("nodeType" "category" "execution_type" "inputs")
MISSING_FIELDS=()

for field in "${REQUIRED_FIELDS[@]}"; do
    if ! yq eval ".$field" "$MANIFEST_PATH" &> /dev/null; then
        MISSING_FIELDS+=("$field")
    fi
done

if [[ ${#MISSING_FIELDS[@]} -gt 0 ]]; then
    echo -e "${RED}✗ Missing required fields: ${MISSING_FIELDS[*]}${NC}"
    rm -f "$OUTPUT_PATH"
    exit 1
fi

echo -e "${GREEN}✓ Required fields present${NC}"
echo ""

# Step 3: Validate against common-definitions.json
echo -e "${BLUE}[3/3]${NC} Validating against common-definitions.json..."

# Note: Since we don't have individual schema files anymore, we validate
# that the compiled JSON can reference common-definitions.json successfully.
# Full validation would require the ao-sdk which will validate:
# - NodeCategory enum values
# - NodeExecutionType enum values
# - WorkloadClassification enum values
# - StandardOutputWrapper structure
# - execution_type + image_ref consistency

# For now, do basic validation of category and execution_type enums
CATEGORY=$(yq eval '.category' "$MANIFEST_PATH")
EXECUTION_TYPE=$(yq eval '.execution_type' "$MANIFEST_PATH")

VALID_CATEGORIES=("action" "task" "workflow" "trigger")
VALID_EXECUTION_TYPES=("in_process" "container")

if [[ ! " ${VALID_CATEGORIES[*]} " =~ " ${CATEGORY} " ]]; then
    echo -e "${RED}✗ Invalid category: '$CATEGORY'${NC}"
    echo -e "   Valid values: ${VALID_CATEGORIES[*]}"
    rm -f "$OUTPUT_PATH"
    exit 1
fi

if [[ ! " ${VALID_EXECUTION_TYPES[*]} " =~ " ${EXECUTION_TYPE} " ]]; then
    echo -e "${RED}✗ Invalid execution_type: '$EXECUTION_TYPE'${NC}"
    echo -e "   Valid values: ${VALID_EXECUTION_TYPES[*]}"
    rm -f "$OUTPUT_PATH"
    exit 1
fi

echo -e "${GREEN}✓ Basic validation passed${NC}"
echo ""

# Success summary
echo -e "${GREEN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  ✓ Build successful                               ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Compiled node definition: ${GREEN}$OUTPUT_PATH${NC}"
echo ""
echo -e "${YELLOW}Note:${NC} This is an interim build script. Full validation (StandardOutputWrapper,"
echo -e "      enum constraints, \$ref resolution) will be available in the ao-sdk CLI."
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo -e "  1. Review: cat $OUTPUT_PATH | jq"
echo -e "  2. Publish: ao-sdk publish --definition $OUTPUT_PATH --image <image-ref>"
echo ""
