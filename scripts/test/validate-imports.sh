#!/bin/bash
# Validate that all test imports reference existing modules

set -e

echo "🔍 Validating test imports..."

BACKEND_DIR="backend"
ERRORS=0

# Check all test files for imports
while IFS= read -r test_file; do
    # Extract imports from test file
    imports=$(grep -E "^from (utils|agents|models|config)\." "$test_file" 2>/dev/null || true)

    if [ -n "$imports" ]; then
        while IFS= read -r import_line; do
            # Extract module path (e.g., "utils.aws_cache" from "from utils.aws_cache import ...")
            module=$(echo "$import_line" | sed -E 's/from ([^ ]+) import.*/\1/' | tr '.' '/')

            # Check if module file exists
            if [ ! -f "$BACKEND_DIR/src/${module}.py" ]; then
                echo "❌ Orphaned import in $test_file:"
                echo "   $import_line"
                echo "   Module not found: $BACKEND_DIR/src/${module}.py"
                ERRORS=$((ERRORS + 1))
            fi
        done <<< "$imports"
    fi
done < <(find "$BACKEND_DIR/tests" -name "test_*.py" -type f)

if [ $ERRORS -gt 0 ]; then
    echo ""
    echo "❌ Found $ERRORS orphaned import(s)"
    echo "💡 Remove the orphaned test files or fix the imports"
    exit 1
fi

echo "✅ All test imports are valid!"
