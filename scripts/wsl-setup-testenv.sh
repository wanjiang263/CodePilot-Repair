#!/bin/bash
# WSL2 可信测试环境：原生 Linux git + 独立 venv，用于跑 CodePilot 的离线质量门禁。
# 只写 $HOME（ext4）与 Windows 侧证据目录；不改动 Windows 仓库内容。
# 可覆盖：CP_WIN_REPO / CP_WORK / CP_BRANCH / CP_EVID
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
SRC_WIN="${CP_WIN_REPO:-/mnt/c/Users/lenovo/Desktop/CodePilot-Repair}"
REPO="${CP_WORK:-$HOME}/cp-run/CodePilot-Repair"
BRANCH="${CP_BRANCH:-codex/phase11-runtime-acceptance}"
EVID="${CP_EVID:-$SRC_WIN/artifacts/phase11-wsl}"
LOG="$EVID/setup.log"
mkdir -p "$EVID" "$(dirname "$REPO")"
exec > >(tee -a "$LOG") 2>&1
echo "=== setup start $(date -Is) host=$(hostname) user=$(id -un) ==="

echo "--- [1/6] toolchain ---"
command -v uv >/dev/null || {
  curl -LsSf --max-time 60 https://astral.sh/uv/install.sh -o /tmp/uv-install.sh &&
    sh /tmp/uv-install.sh
}
export PATH="$HOME/.local/bin:$PATH"
uv --version || echo "UV_INSTALL_FAILED"
python3 -V

echo "--- [2/6] apt packages (ripgrep, git) ---"
if ! command -v rg >/dev/null; then
  apt-get -qq update >/dev/null 2>&1
  apt-get -qq install -y ripgrep >/dev/null 2>&1
fi
echo "rg: $(command -v rg) $(rg --version 2>/dev/null | head -1)"
git --version

echo "--- [3/6] clone repo (native linux git) ---"
if [ ! -d "$REPO/.git" ]; then
  git clone --branch "$BRANCH" "$SRC_WIN" "$REPO" 2>&1 | tail -5
fi
cd "$REPO" || exit 3
git log -1 --format='HEAD=%H %s'
git rev-parse --abbrev-ref HEAD
git status --porcelain | head -5

echo "--- [4/6] copy gitignored runtime inputs ---"
if [ -d "$SRC_WIN/cache/swebench" ]; then
  mkdir -p "$REPO/cache"
  cp -a "$SRC_WIN/cache/swebench" "$REPO/cache/"
  du -sh "$REPO/cache/swebench"
  ls "$REPO/cache/swebench" | head -5
else
  echo "NO_WINDOWS_SNAPSHOT"
fi
echo "note: 不复制 .env —— CI 的离线门禁在没有凭据文件的环境里定义。"

echo "--- [5/6] uv sync (dev + faiss, mirrors CI) ---"
export UV_HTTP_TIMEOUT=120
uv sync --frozen --extra dev --extra faiss --python 3.12 2>&1 | tail -15
echo "sync rc=$?"

echo "--- [6/6] versions ---"
uv run python - <<'PY'
import platform, sqlite3, ssl
from importlib.metadata import version

print("platform:", platform.platform())
print("python:", platform.python_version())
for p in ("tree-sitter", "tree-sitter-python", "pytest", "numpy", "tiktoken", "langgraph"):
    try:
        print(p, version(p))
    except Exception as e:
        print(p, "MISSING", type(e).__name__)
print("sqlite:", sqlite3.sqlite_version, "openssl:", ssl.OPENSSL_VERSION)
PY
echo "=== setup done $(date -Is) ==="
