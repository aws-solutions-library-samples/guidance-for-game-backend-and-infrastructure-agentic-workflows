#!/bin/bash
# watch-traces.sh - Real-time trace and log viewer for Game Agent
# Usage: ./scripts/dev/watch-traces.sh [options]
#
# Options:
#   --quiet       Hide OTEL noise (clean output for demos)
#   --errors      Show only errors
#   --frontend    Show frontend logs instead of backend
#   --since <time> Show logs since time (e.g., 10m, 1h, 2d)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"

# Default settings
REGION="${AWS_REGION:-us-west-2}"
QUIET_MODE=false
ERRORS_ONLY=false
FRONTEND=false
SINCE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --quiet)
      QUIET_MODE=true
      shift
      ;;
    --errors)
      ERRORS_ONLY=true
      shift
      ;;
    --frontend)
      FRONTEND=true
      shift
      ;;
    --since)
      SINCE="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: $0 [--quiet] [--errors] [--frontend] [--since <time>]"
      exit 1
      ;;
  esac
done

# Get runtime ID from .bedrock_agentcore.yaml
if [ -f "$BACKEND_DIR/.bedrock_agentcore.yaml" ]; then
  RUNTIME_ID=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' "$BACKEND_DIR/.bedrock_agentcore.yaml" 2>/dev/null)
else
  echo "❌ Error: .bedrock_agentcore.yaml not found"
  echo "   Run ./deploy-all.sh first to deploy the runtime"
  exit 1
fi

if [ -z "$RUNTIME_ID" ]; then
  echo "❌ Error: Could not extract runtime ID"
  exit 1
fi

# Determine log group and filter
if [ "$FRONTEND" = true ]; then
  # Find the ECS Express frontend log group
  FRONTEND_LOG_GROUP=$(aws logs describe-log-groups \
    --log-group-name-prefix "/ecs/game-agent-frontend" \
    --region "$REGION" \
    --query 'logGroups[0].logGroupName' \
    --output text 2>/dev/null)

  if [ -z "$FRONTEND_LOG_GROUP" ] || [ "$FRONTEND_LOG_GROUP" = "None" ]; then
    echo "❌ No ECS frontend log group found matching /ecs/game-agent-frontend"
    echo "   The frontend may not have started yet, or logs may not have been created."
    exit 1
  fi

  LOG_GROUP="$FRONTEND_LOG_GROUP"
  FILTER_PATTERN=""
else
  LOG_GROUP="/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"

  if [ "$SHOW_ALL" = true ]; then
    FILTER_PATTERN=""
  elif [ "$ERRORS_ONLY" = true ]; then
    FILTER_PATTERN="ERROR"
  else
    # Filter out OTEL noise
    FILTER_PATTERN='-"pycares" -"aiohttp-client failed"'
  fi
fi

# Build command
CMD="aws logs tail \"$LOG_GROUP\" --follow --region $REGION --format short"

if [ -n "$SINCE" ]; then
  CMD="$CMD --since $SINCE"
fi

if [ -n "$FILTER_PATTERN" ]; then
  CMD="$CMD --filter-pattern '$FILTER_PATTERN'"
fi

# Display header
echo "=========================================="
echo "🔍 Game Agent - Real-Time Traces"
echo "=========================================="
if [ "$FRONTEND" = true ]; then
  echo "📱 Source: Frontend (ECS Express)"
else
  echo "🤖 Source: AgentCore Runtime"
fi
echo "🆔 Runtime: $RUNTIME_ID"
echo "🌍 Region: $REGION"
if [ "$QUIET_MODE" = true ]; then
  echo "📊 Filter: Quiet mode (OTEL noise hidden)"
elif [ "$ERRORS_ONLY" = true ]; then
  echo "📊 Filter: Errors only"
else
  echo "📊 Filter: All logs (production mode)"
fi
echo ""
echo "Press Ctrl+C to stop"
echo "=========================================="
echo ""

# Execute with intelligent formatting
eval "$CMD" | while IFS= read -r line; do
  # Check if line contains JSON (starts with timestamp and has {)
  if [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})[[:space:]]+(\{.+\})$ ]]; then
    timestamp="${BASH_REMATCH[1]}"
    json="${BASH_REMATCH[2]}"

    # Classify and format the log entry
    formatted=$(echo "$json" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)

    # Determine log type and format accordingly
    event_name = data.get('attributes', {}).get('event.name', '')
    severity = data.get('severityText', '')
    body = data.get('body', {})

    # 1. User Query
    if 'input' in body and 'messages' in body.get('input', {}):
        messages = body['input']['messages']
        user_msg = None
        for msg in messages:
            if msg.get('role') == 'user':
                content = msg.get('content', {})
                if isinstance(content, dict):
                    text_parts = content.get('content', [])
                    if text_parts and len(text_parts) > 0:
                        user_msg = text_parts[0].get('text', '')
                elif isinstance(content, str):
                    user_msg = content
        if user_msg:
            print(f'👤 USER QUERY: {user_msg}')
        else:
            print('👤 USER QUERY: (processing...)')

    # 2. Agent Response
    elif 'output' in body and 'messages' in body.get('output', {}):
        messages = body['output']['messages']
        if messages and len(messages) > 0:
            msg_content = messages[0].get('content', {})
            if isinstance(msg_content, dict):
                response = msg_content.get('message', '')
                finish_reason = msg_content.get('finish_reason', '')

                # Truncate long responses
                if len(response) > 200:
                    preview = response[:200] + '...'
                else:
                    preview = response

                print(f'🤖 AGENT RESPONSE: {preview}')
                if finish_reason:
                    print(f'   └─ Status: {finish_reason}')

    # 3. Tool/Function Calls
    elif 'tool_use' in str(body) or 'function_call' in str(body):
        print('🔧 TOOL EXECUTION: Agent calling AWS/MCP tools')

    # 4. MCP Operations
    elif 'mcp' in str(body).lower() or 'eks-mcp' in str(body).lower():
        print('🔌 MCP SERVER: Executing MCP operation')

    # 5. Bedrock API Calls
    elif 'bedrock' in str(body).lower() or 'claude' in str(body).lower():
        print('🧠 BEDROCK API: Calling Claude model')

    # 6. Errors
    elif severity == 'ERROR':
        error_msg = data.get('attributes', {}).get('exception.message', 'Unknown error')
        error_type = data.get('attributes', {}).get('exception.type', '')
        body_str = str(data.get('body', ''))

        # In quiet mode, skip OTEL noise
        if '$QUIET_MODE' == 'true':
            if 'pycares' in error_msg or 'aiohttp' in error_msg:
                pass  # Skip OTEL instrumentation noise
            elif 'ResourceNotFoundException' in body_str or 'Failed to export batch' in body_str:
                pass  # Skip OTEL trace export errors
            elif error_type == '' and error_msg == 'Unknown error':
                pass  # Skip empty/unknown errors
            else:
                print(f'❌ ERROR: {error_type}: {error_msg[:100]}')
        else:
            # Default: show all errors (production standard)
            if 'pycares' in error_msg or 'aiohttp' in error_msg:
                print(f'⚠️  OTEL: {error_msg[:100]}')
            elif 'ResourceNotFoundException' in body_str or 'Failed to export batch' in body_str:
                print(f'⚠️  OTEL: Trace export failed - {body_str[:100]}')
            elif error_type == '' and error_msg == 'Unknown error':
                print(f'⚠️  WARN: Unknown error in logs')
            else:
                print(f'❌ ERROR: {error_type}: {error_msg[:100]}')

    # 7. Trace/Span Events
    elif event_name == 'strands.telemetry.tracer':
        print('📊 TRACE: Agent execution span')

    # 8. Memory Operations
    elif 'memory' in str(body).lower():
        print('🧠 MEMORY: Conversation context operation')

    # 9. Generic Info
    elif severity == 'INFO':
        if isinstance(body, str):
            print(f'ℹ️  INFO: {body[:100]}')
        else:
            print('ℹ️  INFO: System event')

    # 10. Unknown - show minimal JSON
    else:
        print('📝 LOG: ' + str(body)[:100] if body else 'System event')

except Exception as e:
    # Fallback: show raw
    print(f'📄 RAW: {sys.stdin.read()[:100]}')
" 2>/dev/null || echo "📄 $json")

    echo "[$timestamp] $formatted"
  else
    # Not JSON, print as-is with timestamp if it looks like a log
    if [[ "$line" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2} ]]; then
      echo "$line"
    else
      echo "[$(date +%H:%M:%S)] $line"
    fi
  fi
done
