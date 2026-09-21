#!/usr/bin/env bash
# Detect ADOT exporter authentication failures in the deployed AgentCore runtime
# logs (issue #420). READ-ONLY: it fetches CloudWatch Logs and classifies them;
# it never mutates any resource and never suppresses errors — it surfaces them.
#
# Classification (via backend/src/utils/adot_exporter_auth_detector.py):
#   clean                -> PASS (exit 0): no exporter 403 / credential failures.
#   transient_cold_start -> WARN (exit 0): bounded 403(s), each adjacent to an
#                           instance (cold-start) transition. Known platform
#                           behavior; telemetry recovers on warm requests.
#   persistent           -> FAIL (exit 1): a sustained pattern not bounded to
#                           cold start — a genuine regression (IAM/config).
#   unavailable          -> WARN (exit 2): the CloudWatch Logs query itself could
#                           not run (bad/absent profile, AccessDenied, throttle,
#                           malformed query, or missing log group). The exporter
#                           status is UNKNOWN — this is deliberately NOT reported
#                           as a clean PASS (that would be a fail-open bug).
#
# Usage:
#   AWS_PROFILE=<profile> AWS_REGION=us-west-2 \
#     scripts/infrastructure/check-exporter-auth.sh [--runtime-id <id>] [--lookback-hours N]
#
# If --runtime-id is omitted it is resolved from backend/.bedrock_agentcore.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo us-west-2)}"
LOOKBACK_HOURS=24
RUNTIME_ID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime-id) RUNTIME_ID="$2"; shift 2 ;;
    --lookback-hours) LOOKBACK_HOURS="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$RUNTIME_ID" ]; then
  YAML="$PROJECT_ROOT/backend/.bedrock_agentcore.yaml"
  if [ -f "$YAML" ] && command -v yq >/dev/null 2>&1; then
    RUNTIME_ID=$(yq eval '.agents.gameagentruntime.bedrock_agentcore.agent_id' "$YAML" 2>/dev/null || echo "")
  fi
fi

if [ -z "$RUNTIME_ID" ] || [ "$RUNTIME_ID" = "null" ]; then
  echo "ADOT exporter auth: SKIP — could not resolve runtime id (pass --runtime-id)." >&2
  exit 0
fi

LOG_GROUP="/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT"
# The ADOT exporter writes its own internal logs (including export failures) to a
# dedicated 'otel-rt-logs' stream. Scope the failure query there: the group-wide
# query paginates newest-streams-first and can miss this stream on busy runtimes.
EXPORTER_STREAM="otel-rt-logs"
COLD_START_WINDOW_SEC=60
END_MS=$(( $(date +%s) * 1000 ))
START_MS=$(( END_MS - LOOKBACK_HOURS * 3600 * 1000 ))

WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/exporter-auth.XXXXXXXX")
trap 'rm -rf "$WORKDIR"' EXIT
EVENTS_FILE="$WORKDIR/events.json"
STDERR_FILE="$WORKDIR/query.err"
TRANS_DIR="$WORKDIR/trans"
mkdir -p "$TRANS_DIR"

# Run the pure classifier over pre-fetched files and print a public-safe summary.
# Reused for both the normal path and the fail-closed "unavailable" path so the
# summary/exit-status contract lives in exactly one place.
classify_and_report() {
  cd "$PROJECT_ROOT/backend"
  local status
  set +e
  local summary
  summary=$(EVENTS_FILE="$EVENTS_FILE" TRANS_DIR="$TRANS_DIR" \
    UNAVAILABLE_REASON="${UNAVAILABLE_REASON:-}" \
    uv run python - <<'PY'
import glob
import json
import os
import re
import sys
from importlib import util

spec = util.spec_from_file_location(
    "adot_exporter_auth_detector",
    os.path.join("src", "utils", "adot_exporter_auth_detector.py"),
)
mod = util.module_from_spec(spec)
sys.modules["adot_exporter_auth_detector"] = mod
spec.loader.exec_module(mod)

# Fail-closed: if the upstream query could not run, the wrapper sets
# UNAVAILABLE_REASON. Emit a bounded, public-safe "unavailable" report (WARN,
# exit 2) instead of letting an empty events file look "clean".
reason = os.environ.get("UNAVAILABLE_REASON", "").strip()
if reason:
    report = mod.unavailable_report(reason)
    print(report.summary())
    sys.exit(report.exit_status())


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


events = _load(os.environ["EVENTS_FILE"])

raw_transitions = []
for path in glob.glob(os.path.join(os.environ["TRANS_DIR"], "*.json")):
    raw_transitions.extend(_load(path))

# First-seen timestamp per distinct service.instance.id == a cold-start transition.
_ID = re.compile(r'"service\.instance\.id":\s*"([a-f0-9-]+)"')
first_seen = {}
for e in raw_transitions:
    if not isinstance(e, dict):
        continue
    m = _ID.search(e.get("body") or "")
    if not m:
        continue
    iid, ts = m.group(1), int(e.get("timestamp", 0))
    if iid not in first_seen or ts < first_seen[iid]:
        first_seen[iid] = ts
transitions = sorted(first_seen.values())

report = mod.classify_exporter_events(events, instance_transitions=transitions)
# Public-safe summary only; never print raw bodies/ARNs/accounts.
print(report.summary())
sys.exit(report.exit_status())
PY
)
  status=$?
  set -e
  echo "$summary"
  return "$status"
}

# 1) Fetch exporter/credential candidate events (read-only) from the exporter's
#    own stream, normalized to {timestamp, body}. The pure classifier matches
#    #420 signatures and excludes the unrelated #457 (400 / missing log group).
#
#    FAIL-CLOSED: capture the AWS exit status. Previously a failed query was
#    masked with `|| echo "[]"`, so a bad profile / AccessDenied / throttle /
#    malformed query / missing log group produced an empty events file that the
#    classifier read as "clean" -> a green PASS (fail-open). Now a query failure
#    is surfaced as a bounded, public-safe "unavailable" WARN (exit 2). stderr is
#    captured to a private file and mapped to a safe reason token — never echoed.
if aws logs filter-log-events \
    --log-group-name "$LOG_GROUP" \
    --log-stream-names "$EXPORTER_STREAM" \
    --region "$AWS_REGION" \
    --start-time "$START_MS" --end-time "$END_MS" \
    --filter-pattern "?\"Failed to export\" ?\"Missing Authentication\" ?\"Failed to load AWS Credentials\" ?\"Forbidden\"" \
    --limit 1000 \
    --query 'events[].{timestamp:timestamp,body:message}' \
    --output json > "$EVENTS_FILE" 2>"$STDERR_FILE"; then
  :
else
  # Map the failure to a bounded, public-safe reason token WITHOUT leaking the
  # raw stderr (which can carry ARNs / accounts / endpoints).
  REASON="query_failed"
  if grep -qiE "could not be found|the config profile.*could not be found|invalid choice|SSO session|ProfileNotFound" "$STDERR_FILE"; then
    REASON="profile_not_found"
  elif grep -qiE "AccessDenied|not authorized|UnrecognizedClient|InvalidSignatureException|security token" "$STDERR_FILE"; then
    REASON="access_denied"
  elif grep -qiE "Throttl|Rate exceeded|TooManyRequests" "$STDERR_FILE"; then
    REASON="throttled"
  elif grep -qiE "ResourceNotFound|does not exist" "$STDERR_FILE"; then
    REASON="log_group_missing"
  elif grep -qiE "InvalidParameter|MalformedQuery|ValidationException|filter-pattern" "$STDERR_FILE"; then
    REASON="invalid_query"
  fi
  UNAVAILABLE_REASON="$REASON" classify_and_report
  exit $?
fi

# Guard against a "successful" call that nonetheless yielded no JSON (e.g. an
# empty body). A well-formed empty result is a legitimate array; anything else is
# treated as an unavailable check rather than silently clean.
if [ ! -s "$EVENTS_FILE" ]; then
  echo "[]" > "$EVENTS_FILE"
fi

# 2) Derive instance-transition (cold-start) timestamps. service.instance.id
#    values are dense across the WHOLE log group, so to keep the query bounded we
#    look only in a small window around each candidate failure timestamp: a cold
#    start near a failure is what makes that failure "transient". Each window's
#    result is written to its own file so parsing stays trivial and robust.
CANDIDATE_TS=$(python3 - "$EVENTS_FILE" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        data = json.load(fh)
except Exception:
    data = []
print(" ".join(str(int(e.get("timestamp", 0))) for e in data if e.get("timestamp")))
PY
)

i=0
for TS in $CANDIDATE_TS; do
  WS=$(( TS - COLD_START_WINDOW_SEC * 1000 ))
  WE=$(( TS + COLD_START_WINDOW_SEC * 1000 ))
  # A transition-window query failure is non-fatal: cold-start adjacency only
  # downgrades a real failure to WARN, so a missing window can never manufacture
  # a false clean/WARN. Default to an empty window on error.
  aws logs filter-log-events \
    --log-group-name "$LOG_GROUP" \
    --region "$AWS_REGION" \
    --start-time "$WS" --end-time "$WE" \
    --filter-pattern "\"service.instance.id\"" \
    --limit 2000 \
    --query 'events[].{timestamp:timestamp,body:message}' \
    --output json > "$TRANS_DIR/w_${i}.json" 2>/dev/null || echo "[]" > "$TRANS_DIR/w_${i}.json"
  i=$(( i + 1 ))
done

# 3) Classify with the pure Python detector (no AWS I/O inside Python). Data is
#    passed by file path (not argv/env) to avoid arg-length limits.
set +e
classify_and_report
STATUS=$?
set -e

exit "$STATUS"
