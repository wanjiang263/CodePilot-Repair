#!/bin/bash
# 真实官方 Harness 控制运行（WSL2 + Docker Desktop guest socket）。
# 用法: bash /tmp/ctl.sh gold|wrong|garbage
set -uo pipefail
cd /root/cp-run/CodePilot-Repair || exit 9
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock
export CP_CONTROL="${1:-gold}"
EVID=/mnt/c/Users/lenovo/Desktop/CodePilot-Repair/artifacts/phase11-wsl
LOG="$EVID/official-control-${CP_CONTROL}.log"
INSTANCE=marshmallow-code__marshmallow-1343
{
  echo "=== control $CP_CONTROL start $(date -Is) ==="
  echo "DOCKER_HOST=$DOCKER_HOST"
  docker version --format 'server={{.Server.Version}} os={{.Server.Os}}' 2>&1 | head -2
  df -h / | tail -1
} >"$LOG"
uv run python scripts/swebench_official_control.py \
  --control "$CP_CONTROL" \
  --instance-id "$INSTANCE" \
  --split dev \
  --harness-python /root/swebench-venv/bin/python \
  --artifact-root /root/cp-run/artifacts/official-control \
  --cache-level instance \
  --max-workers 1 \
  --timeout-seconds 1800 >>"$LOG" 2>&1
echo "EXIT=$?" >>"$LOG"
echo "=== control $CP_CONTROL end $(date -Is) ===" >>"$LOG"
cp -a /root/cp-run/artifacts/official-control "$EVID/" 2>/dev/null
docker images --format '{{.Repository}}:{{.Tag}}|{{.Size}}' 2>/dev/null | grep -i sweb >>"$LOG"
docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null | grep -i sweb >>"$LOG"
