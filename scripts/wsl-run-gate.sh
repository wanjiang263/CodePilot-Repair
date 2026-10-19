#!/bin/bash
# Full offline quality gate inside the WSL2 trusted environment.
# Mirrors .github/workflows/ci.yml: ruff + ruff format --check + mypy + pytest -m "not docker and not llm" --cov
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
REPO="${CP_WORK:-$HOME}/cp-run/CodePilot-Repair"
EVID="${CP_EVID:-/mnt/c/Users/lenovo/Desktop/CodePilot-Repair/artifacts/phase11-wsl}"
mkdir -p "$EVID"
LOG="$EVID/gate-full.log"
cd "$REPO" || exit 9
{
  echo "=== full gate start $(date -Is) ==="
  echo "branch=$(git rev-parse --abbrev-ref HEAD) head=$(git rev-parse HEAD)"
  echo "uname=$(uname -a)"
  echo "rg=$(command -v rg) $(rg --version | head -1)"
  echo "git=$(git --version)"
} >"$LOG"
run_step() {
  local name="$1"; shift
  echo "" >>"$LOG"
  echo "########## $name ##########" >>"$LOG"
  "$@" >>"$LOG" 2>&1
  echo "EXIT_${name}=$?" >>"$LOG"
}
run_step ruff-check uv run ruff check .
run_step ruff-format uv run ruff format --check .
run_step mypy uv run mypy src
run_step pytest uv run pytest -m "not docker and not llm" --cov --cov-report=term-missing --junitxml=pytest-results-linux.xml
echo "" >>"$LOG"
echo "=== full gate end $(date -Is) ===" >>"$LOG"
