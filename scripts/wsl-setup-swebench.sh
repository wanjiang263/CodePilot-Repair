#!/bin/bash
# Build the official SWE-bench 4.1.0 runtime in WSL2 (isolated venv, native Linux).
set -uo pipefail
LOG="${CP_EVID:-/mnt/c/Users/lenovo/Desktop/CodePilot-Repair/artifacts/phase11-wsl}/swebench-runtime.log"
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
export PATH="$HOME/.local/bin:$PATH"
echo "=== swebench runtime start $(date -Is) ==="
echo "--- python/git ---"
python3 -V; git --version; uname -m; lsb_release -d 2>/dev/null

echo "--- [1/4] create isolated venv ---"
rm -rf "$HOME/swebench-venv"
uv venv --python 3.12 "$HOME/swebench-venv" 2>&1 | tail -3

echo "--- [2/4] install swebench==4.1.0 ---"
VIRTUAL_ENV="$HOME/swebench-venv" uv pip install --python "$HOME/swebench-venv/bin/python" \
  "swebench==4.1.0" "datasets>=2.19,<4" "huggingface-hub>=0.23,<1" "pyarrow>=15" 2>&1 | tail -25
echo "install rc=$?"

echo "--- [3/4] identity ---"
"$HOME/swebench-venv/bin/python" - <<'PY'
import importlib.metadata as md, swebench, sys, pathlib
print("python:", sys.version.replace("\n", " "))
print("swebench version:", md.version("swebench"))
print("swebench package at:", pathlib.Path(swebench.__file__).resolve())
import swebench.harness.constants as C
names = [n for n in dir(C) if n.isupper()]
for n in sorted(names):
    v = getattr(C, n)
    if isinstance(v, (str, int, float)):
        print(f"C.{n} = {v!r}")
print("--- constants file:", pathlib.Path(C.__file__).resolve())
PY

echo "--- [4/4] official CLI ---"
"$HOME/swebench-venv/bin/python" -m swebench.harness.run_evaluation --help 2>&1
echo "help rc=$?"
ls "$HOME/swebench-venv/bin" | head -20
echo "=== swebench runtime done $(date -Is) ==="
