#!/bin/bash
# Runs inside a Render sandbox. Serves exactly one Managed Agents session, then exits.
#
# The orchestrator sets these at sandbox creation (none of them are secrets):
#   ANTHROPIC_ENVIRONMENT_ID, ANTHROPIC_SESSION_ID, ANTHROPIC_WORK_ID
#   ANTHROPIC_SDK_VERSION, WORKER_MAX_IDLE, optionally ANTHROPIC_BASE_URL
# It uploads runner.py and the per-session secret to $STATE_DIR.
# The long-lived environment key never enters the sandbox.
set -euo pipefail

STATE_DIR=/run/claude-worker
WORKDIR=/workspace
VENV=/opt/claude-runner
SDK_VERSION="${ANTHROPIC_SDK_VERSION:-1.12.1}"

mkdir -p "$WORKDIR"

# The prepared snapshot already has the SDK. Without it, install it now (about 6 seconds;
# needs pypi.org and files.pythonhosted.org on the allow-list).
installed="$("$VENV/bin/python" -c 'import anthropic; print(anthropic.__version__)' 2>/dev/null || true)"
if [ "$installed" != "$SDK_VERSION" ]; then
  echo "installing anthropic ${SDK_VERSION}"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check "anthropic==${SDK_VERSION}"
fi

cd "$WORKDIR"
# PYTHONFAULTHANDLER prints a Python stack trace if the process crashes hard.
exec env PYTHONFAULTHANDLER=1 "$VENV/bin/python" "$STATE_DIR/runner.py"
