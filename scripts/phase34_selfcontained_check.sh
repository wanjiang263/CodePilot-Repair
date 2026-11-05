#!/usr/bin/env bash
# Phase34 evidence self-containment regression: clone the repository into a
# temporary directory (no hardlinks, no alternates), run the runtime --check
# with its default in-repo evidence pack, and assert that the delivered JSON
# contains no external scratch paths. The clone does not contain D:/cp34, so a
# pass proves the check is reproducible away from the generation machine.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
source_shallow=$(git -C "$root" rev-parse --is-shallow-repository)
work=$(mktemp -d "${TMPDIR:-/tmp}/phase34-selfcontained-XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM

branch=$(git -C "$root" rev-parse --abbrev-ref HEAD)
python=${PYTHON:-"$root/.venv/Scripts/python.exe"}
if [ ! -x "$python" ]; then
    python=$(command -v python3 || command -v python)
fi

git -c core.longpaths=true clone --quiet --no-hardlinks --local \
    --single-branch --branch "$branch" "$root" "$work/repo"

cd "$work/repo"
export PYTHONPATH="$work/repo/src${PYTHONPATH:+:$PYTHONPATH}"
clone_shallow=$(git rev-parse --is-shallow-repository)
echo "SOURCE_SHALLOW=$source_shallow"
echo "CLONE_SHALLOW=$clone_shallow"
if [ "$clone_shallow" = "true" ]; then
    echo "HISTORY_SCOPE=existing_shallow_history_only"
fi
if [ "${PHASE34_REQUIRE_FULL_HISTORY:-0}" = "1" ] && [ "$clone_shallow" = "true" ]; then
    echo "FULL_HISTORY_REQUIRED_BUT_SHALLOW" >&2
    exit 1
fi
"$python" scripts/phase34_runtime_results.py --check

"$python" - <<'PY'
import json
import re
from pathlib import Path

payload = json.loads(
    Path("docs/notes/phase34-runtime-results.json").read_text(encoding="utf-8")
)
assert payload["evidence_dir"] == "docs/notes/phase34-evidence", payload["evidence_dir"]
assert re.fullmatch(r"[0-9a-f]{64}", payload["evidence_manifest_sha256"]), payload[
    "evidence_manifest_sha256"
]
for group in payload["final_regression"]["groups"]:
    for key in ("log", "junit"):
        assert group[key].startswith("docs/notes/phase34-evidence/"), group[key]
    assert "/" not in group["basetemp"] and "\\" not in group["basetemp"], group["basetemp"]
text = Path("docs/notes/phase34-runtime-results.json").read_text(encoding="utf-8")
assert "D:/cp34" not in text and "D:\\\\cp34" not in text
print("SELF_CONTAINED_CHECK=PASS")
PY
