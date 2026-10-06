#!/usr/bin/env bash
# Guard against the unbootable long-path AgentCore runtime (issue #517).
#
# `agentcore launch` packages the backend venv's console scripts (54 of them,
# including the `opentelemetry-instrument` entrypoint) into
# `backend/.bedrock_agentcore/gameagentruntime/dependencies.zip`. When the venv
# interpreter path exceeds the kernel shebang limit of 127 characters, uv writes
# a `/bin/sh` exec trampoline that embeds the absolute LOCAL interpreter path
# instead of a portable `#!/usr/bin/env python3` shebang. The starter toolkit's
# shebang rewriter only normalizes single-line `#!` shebangs, so the trampoline
# ships unchanged and the runtime dies at exec:
#
#   /var/task/bin/opentelemetry-instrument: line 2:
#     /Users/.../backend/.venv/bin/python3: No such file or directory
#
# The failure is silent: deploy exits 0 and the control-plane status reaches
# READY, so validate-deployment.sh passes while every invocation fails. Catch it
# before CodeBuild runs by refusing an over-limit interpreter path.
#
# This file is meant to be sourced; it defines functions and does not run work
# at load time.

# The kernel `#!` shebang length limit. uv emits a portable `#!` shebang at or
# below this length and the /bin/sh trampoline above it.
RUNTIME_SHEBANG_LIMIT=127

# venv_python_path_is_portable PATH
#   Return 0 (success) when PATH is short enough for a portable shebang, 1 when
#   it would trigger uv's unbootable trampoline.
venv_python_path_is_portable() {
  local candidate="$1"
  [ "${#candidate}" -le "$RUNTIME_SHEBANG_LIMIT" ]
}

# assert_portable_venv_python PATH
#   Fail-fast guard. Prints an actionable remediation and returns non-zero when
#   PATH exceeds the shebang limit, so a `set -e` caller aborts before launch.
assert_portable_venv_python() {
  local venv_python="$1"
  local length="${#venv_python}"

  if venv_python_path_is_portable "$venv_python"; then
    return 0
  fi

  {
    echo "❌ Backend venv interpreter path length ${length} exceeds the ${RUNTIME_SHEBANG_LIMIT}-char shebang limit."
    echo "   Path: ${venv_python}"
    echo ""
    echo "   uv emits a /bin/sh trampoline embedding this absolute local path"
    echo "   instead of a portable shebang, which produces an AgentCore runtime"
    echo "   that cannot start (issue #517). Deploy from a checkout with a"
    echo "   shorter path so that backend/.venv/bin/python3 is <= ${RUNTIME_SHEBANG_LIMIT}"
    echo "   characters, then re-run the deployment."
  } >&2
  return 1
}
