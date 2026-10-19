#!/bin/bash
# Run CodePilot-Repair test targets inside the WSL2 trusted environment.
# usage: wsl-run-tests.sh <label> <pytest args...>
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
REPO="${CP_WORK:-$HOME}/cp-run/CodePilot-Repair"
EVID="${CP_EVID:-/mnt/c/Users/lenovo/Desktop/CodePilot-Repair/artifacts/phase11-wsl}"
LABEL="${1:-run}"; shift || true
mkdir -p "$EVID"
LOG="$EVID/${LABEL}.log"
cd "$REPO" || exit 9
{
  echo "=== $LABEL start $(date -Is) ==="
  echo "branch: $(git rev-parse --abbrev-ref HEAD) head: $(git rev-parse HEAD)"
  echo "git: $(git --version) rg: $(command -v rg)"
  echo "docker host: ${DOCKER_HOST:-unset}"
  echo "argv: $*"
} >"$LOG"
uv run pytest "$@" >>"$LOG" 2>&1
RC=$?
echo "EXIT=$RC" >>"$LOG"
echo "=== $LABEL end $(date -Is) rc=$RC ===" >>"$LOG"
exit $RC
