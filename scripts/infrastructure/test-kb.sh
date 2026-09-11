#!/bin/bash
set -e

# Post-deploy smoke test for the three per-domain Bedrock Knowledge Bases.
#
# For each domain (gamelift, eks, cost) this script:
#   1. Resolves KnowledgeBaseId from the per-domain CloudFormation stack
#      (game-agent-kb-<domain>, same pattern as seed-kb-<domain>.sh)
#   2. Runs one domain-appropriate retrieval query against the KB
#   3. Requires at least one retrieval result
#
# Exits non-zero if any stack is missing, any KB ID cannot be resolved, or
# any retrieval fails or returns no results, so it can gate deployments as
# a smoke check (see docs/DEPLOYMENT_GUIDE.md, "Verify Deployment Health").

REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="game-agent"

# Resolve AWS profile from environment or ui/.env.local (matches scripts/deploy.sh).
# An explicitly set AWS_PROFILE always wins; otherwise fall back to ui/.env.local.
# (Inherited automatically when invoked by deploy.sh; needed for standalone runs.)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ -z "${AWS_PROFILE:-}" ] && [ -f "$PROJECT_ROOT/ui/.env.local" ]; then
    _profile=$(grep '^AWS_PROFILE=' "$PROJECT_ROOT/ui/.env.local" | cut -d= -f2 | tr -d '[:space:]')
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi

DOMAINS=(gamelift eks cost)

# Domain-appropriate retrieval queries. Keep these free of double quotes and
# backslashes: they are embedded verbatim into the JSON --retrieval-query
# payload below (scripts/test/test_kb_script_checks.py enforces this).
query_for_domain() {
    case "$1" in
        gamelift) echo "What are GameLift fleet auto-scaling best practices?" ;;
        eks)      echo "How do I troubleshoot failing pods in an EKS cluster?" ;;
        cost)     echo "How can I analyze and reduce AWS spending?" ;;
    esac
}

echo "=================================================="
echo "🧪 Testing Knowledge Bases (per-domain stacks)"
echo "=================================================="

ERR_FILE=$(mktemp)
trap 'rm -f "$ERR_FILE"' EXIT

FAILURES=0

for domain in "${DOMAINS[@]}"; do
    STACK_NAME="${PROJECT_NAME}-kb-${domain}"
    QUERY="$(query_for_domain "$domain")"

    echo ""
    echo "▶ ${domain} (stack: ${STACK_NAME})"

    KB_ID=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query 'Stacks[0].Outputs[?OutputKey==`KnowledgeBaseId`].OutputValue' \
        --output text 2>/dev/null || echo "")

    if [ -z "$KB_ID" ] || [ "$KB_ID" = "None" ]; then
        echo "  ❌ Could not resolve KnowledgeBaseId from stack ${STACK_NAME}"
        echo "     Deploy the Knowledge Bases first: ./scripts/infrastructure/deploy-kb.sh"
        FAILURES=$((FAILURES + 1))
        continue
    fi

    echo "  Knowledge Base ID: $KB_ID"
    echo "  Query: $QUERY"

    if ! RETRIEVE_OUTPUT=$(aws bedrock-agent-runtime retrieve \
        --knowledge-base-id "$KB_ID" \
        --retrieval-query "{\"text\": \"$QUERY\"}" \
        --retrieval-configuration '{"vectorSearchConfiguration": {"numberOfResults": 3}}' \
        --region "$REGION" \
        --query '[length(retrievalResults), retrievalResults[0].score]' \
        --output text 2>"$ERR_FILE"); then
        echo "  ❌ Retrieval call failed:"
        sed 's/^/     /' "$ERR_FILE"
        FAILURES=$((FAILURES + 1))
        continue
    fi

    read -r RESULT_COUNT TOP_SCORE <<< "$RETRIEVE_OUTPUT"

    if [ "${RESULT_COUNT:-0}" = "0" ] || [ "${RESULT_COUNT:-None}" = "None" ]; then
        echo "  ❌ No retrieval results (KB may still be ingesting)"
        echo "     Seed it first: ./scripts/infrastructure/seed-kb-${domain}.sh"
        FAILURES=$((FAILURES + 1))
        continue
    fi

    echo "  ✅ ${RESULT_COUNT} result(s), top score: ${TOP_SCORE}"
done

echo ""
echo "=================================================="
if [ "$FAILURES" -gt 0 ]; then
    echo "❌ Knowledge Base test FAILED (${FAILURES} of ${#DOMAINS[@]} domains)"
    echo "=================================================="
    exit 1
fi
echo "✅ All ${#DOMAINS[@]} Knowledge Bases returned retrieval results"
echo "=================================================="
