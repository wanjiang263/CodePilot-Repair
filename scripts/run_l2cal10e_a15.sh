#!/usr/bin/env bash
# l2cal10e a15: pytest only, reusing the prepared a9 workspace/index/cache.
# a14 exhausted its token budget retrying forbidden test-file edits; this is a
# fresh coder run with the reviewer correction retry still enabled.
set -u
REPO=${CODEPILOT_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$REPO" || exit 9
set -a
. ./.env
set +a
export DOCKER_HOST=${DOCKER_HOST:-unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock}
export CODEPILOT_TESTDEPS_DIR=${CODEPILOT_TESTDEPS_DIR:-$REPO/artifacts/phase18/l2-cal10e/testdeps}
PY=${CODEPILOT_PYTHON:-$REPO/.venv/bin/python}

BASE=artifacts/phase18/l2-cal10e
IID=pytest-dev__pytest-11148
LABEL=l2cal10e-a15
ATTEMPT=l2cal10e-a15
A15="$BASE/$IID-a15"
RUN_ID=swebench-smoke-l2cal10e-a15-pytest-dev__pytest-11148-s0-5fed8ca4
RUN_DIR="$A15/runs/$RUN_ID"
WORKSPACE="$RUN_DIR/workspace"
INDEX="$RUN_DIR/index.sqlite3"
LEDGER_NAME="dispatch-ledger-$ATTEMPT.json"
LEDGER="$A15/$LEDGER_NAME"
MANIFEST="$BASE/recovery-manifest-$ATTEMPT.json"
REGISTRY="$BASE/attempts.json"
STATUS="$BASE/batch-status-$ATTEMPT.log"
GRAPH_BUDGET=14.625118
PER_INSTANCE_OPENING=5.37488128
BATCH_CAP=100.0
PER_INSTANCE=20.0
REVISION=6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2
BASE_COMMIT=2f7415cfbc4b6ca62f9013f1abd27136f46b9653

mkdir -p "$BASE"
echo "BATCH_START $ATTEMPT $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"

for path in "$WORKSPACE" "$INDEX" "$A15/repo-cache" "$A15/env-cache"; do
  if [ ! -e "$path" ]; then
    echo "STOP_REUSE_SCENE_MISSING $path" >> "$STATUS"
    exit 9
  fi
done
if [ ! -x "$WORKSPACE/.git" ] && [ ! -d "$WORKSPACE/.git" ] && [ ! -f "$WORKSPACE/.git" ]; then
  echo "STOP_REUSE_SCENE_NOT_GIT $WORKSPACE" >> "$STATUS"
  exit 9
fi
head=$("$PY" - "$WORKSPACE" <<'PY'
import subprocess
import sys
print(subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=sys.argv[1], capture_output=True, text=True, check=True
).stdout.strip())
PY
)
if [ "$head" != "$BASE_COMMIT" ]; then
  echo "STOP_REUSE_WORKSPACE_HEAD $head" >> "$STATUS"
  exit 9
fi
dirty=$("$PY" - "$WORKSPACE" <<'PY'
import subprocess
import sys
print(subprocess.run(
    ["git", "status", "--porcelain"], cwd=sys.argv[1], capture_output=True, text=True, check=True
).stdout.strip())
PY
)
if [ -n "$dirty" ]; then
  echo "STOP_REUSE_WORKSPACE_DIRTY $dirty" >> "$STATUS"
  exit 9
fi
repo_id=$("$PY" - <<'PY'
from codepilot.repo.workspace import normalize_source, repository_id
print(repository_id(normalize_source("https://github.com/pytest-dev/pytest"), "2f7415cfbc4b6ca62f9013f1abd27136f46b9653"))
PY
)
index_repo=$("$PY" - "$INDEX" <<'PY'
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
row = connection.execute("select repository_id from files limit 1").fetchone()
print(row[0] if row else "")
PY
)
if [ "$repo_id" != "$index_repo" ]; then
  echo "STOP_REUSE_INDEX_MISMATCH repo=$repo_id index=$index_repo" >> "$STATUS"
  exit 9
fi
for path in "$WORKSPACE/testdeps" "$WORKSPACE/src/_pytest/_version.py"; do
  if [ ! -e "$path" ]; then
    echo "STOP_REUSE_SCENE_INCOMPLETE $path" >> "$STATUS"
    exit 9
  fi
done
echo "REUSE_SCENE_OK run_dir=$RUN_DIR repo_id=$repo_id" >> "$STATUS"

computed_run_id=$("$PY" - "$A15" "$IID" "$REVISION" "$GRAPH_BUDGET" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "scripts")
import swebench_smoke as smoke

artifact_root, instance_id, revision, budget = sys.argv[1:5]
argv = [
    "--instance-id", instance_id,
    "--split", "test",
    "--revision", revision,
    "--provider", "bigmodel",
    "--model", "glm-5.3-flash-main",
    "--budget", budget,
    "--live-llm",
    "--record-llm",
    "--artifact-root", artifact_root,
    "--snapshot-root", str(Path("cache/swebench")),
    "--spec-snapshot", str(Path("configs/swebench/env-specs-4.1.0.json")),
    "--harness-python", "/root/swebench-venv/bin/python",
    "--run-label", "l2cal10e-a15",
    "--timeout", "1800",
    "--image-overrides", "configs/swebench/instance-image-overrides.json",
]
config = smoke._config_from_args(smoke._parse_args(argv))
print(smoke.SmokePaths.build(config).run_id)
PY
)
if [ "$computed_run_id" != "$RUN_ID" ]; then
  echo "STOP_RUN_ID_MISMATCH computed=$computed_run_id expected=$RUN_ID" >> "$STATUS"
  exit 9
fi
echo "RUN_IDENTITY_OK $RUN_ID" >> "$STATUS"

if [ -e "$MANIFEST" ] || [ -e "$LEDGER" ]; then
  echo "STOP_ATTEMPT_EXISTS manifest=$MANIFEST ledger=$LEDGER" >> "$STATUS"
  exit 9
fi

"$PY" scripts/recovery_admin.py manifest \
  --output "$MANIFEST" \
  --run-id "$ATTEMPT" \
  --reason "pytest a15: fresh coder after a14 exhausted tokens on forbidden test edits" \
  --repo-root "$REPO" \
  --source "artifacts/phase18/l2-cal10/pydata__xarray-4493/dispatch-ledger-l2cal10c.json=pydata__xarray-4493" \
  --source "artifacts/phase18/l2-cal10-resume/pylint-dev__pylint-6506/dispatch-ledger-l2cal10d.json=pylint-dev__pylint-6506" \
  --source "artifacts/phase18/l2-cal10e/pytest-dev__pytest-11148-a9/dispatch-ledger-l2cal10e-a9-resume.json=pytest-dev__pytest-11148" \
  --settled "artifacts/phase18/l2-cal10/astropy__astropy-12907/dispatch-ledger-l2cal10c.json=astropy__astropy-12907" \
  --settled "artifacts/phase18/l2-cal10/django__django-11910/dispatch-ledger-l2cal10c.json=django__django-11910" \
  --settled "artifacts/phase18/l2-cal10/matplotlib__matplotlib-23314/dispatch-ledger-l2cal10c.json=matplotlib__matplotlib-23314" \
  --settled "artifacts/phase18/l2-cal10/mwaskom__seaborn-3010/dispatch-ledger-l2cal10c.json=mwaskom__seaborn-3010" \
  --settled "artifacts/phase18/l2-cal10/pallets__flask-4045/dispatch-ledger-l2cal10c.json=pallets__flask-4045" \
  --settled "artifacts/phase18/l2-cal10/psf__requests-2148/dispatch-ledger-l2cal10c.json=psf__requests-2148" \
  --include-registry --registry "$REGISTRY" > "$BASE/preflight/manifest-$ATTEMPT.json" 2>&1
if [ $? -ne 0 ]; then
  echo "STOP_MANIFEST_FAIL" >> "$STATUS"
  exit 9
fi
opening=$("$PY" - "$MANIFEST" <<'PY'
import json
import sys
print(f"{json.load(open(sys.argv[1], encoding='utf-8'))['total_upper_bound_cny']:.6f}")
PY
)
echo "OPENING $opening" >> "$STATUS"
if ! awk -v o="$opening" 'BEGIN {exit !(o >= 8.808569 && o <= 8.808572)}'; then
  echo "STOP_OPENING_MISMATCH $opening" >> "$STATUS"
  exit 9
fi

budget_out=$("$PY" scripts/batch_budget.py \
  --base "$BASE" \
  --ledger "$LEDGER_NAME" \
  --batch-cap "$BATCH_CAP" \
  --per-instance "$PER_INSTANCE" \
  --opening-reconciliation "$MANIFEST" \
  --instance "$IID" \
  --attempt-registry "$REGISTRY" \
  --repo-root "$REPO")
budget_code=$?
echo "BUDGET $budget_out" >> "$STATUS"
if [ "$budget_code" -ne 0 ]; then
  echo "STOP_BUDGET code=$budget_code" >> "$STATUS"
  exit 9
fi
allowed=$(printf '%s\n' "$budget_out" | sed -n 's/.*allowed=\([0-9.]*\).*/\1/p')
if ! awk -v a="$allowed" 'BEGIN {exit !(a >= 14.625118 && a <= 14.625120)}'; then
  echo "STOP_BUDGET_UNEXPECTED allowed=$allowed" >> "$STATUS"
  exit 9
fi
if ! awk -v a="$allowed" -v g="$GRAPH_BUDGET" 'BEGIN {exit !(g <= a + 0.000000001)}'; then
  echo "STOP_GRAPH_BUDGET_EXCEEDS_ALLOWED graph=$GRAPH_BUDGET allowed=$allowed" >> "$STATUS"
  exit 9
fi

MANIFEST_SHA=$(sha256sum "$MANIFEST" | awk '{print $1}')
OPENING_SOURCE=$(printf '{"kind":"per_instance","recovery_manifest":"%s","recovery_manifest_sha256":"%s"}' \
  "$MANIFEST" "$MANIFEST_SHA")
export CODEPILOT_MAX_TEST_RUNS=18
export CODEPILOT_DISPATCH_BUDGET_CNY="$PER_INSTANCE"
export CODEPILOT_DISPATCH_OPENING_CNY="$PER_INSTANCE_OPENING"
export CODEPILOT_DISPATCH_OPENING_SOURCE="$OPENING_SOURCE"
export CODEPILOT_DISPATCH_LEDGER="$LEDGER"
export CODEPILOT_REUSE_WORKSPACE="$WORKSPACE"

echo "START $IID allowed=$allowed graph_budget=$GRAPH_BUDGET $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"
"$PY" -u scripts/l2cal10e_internal.py \
  --instance-id "$IID" \
  --split test \
  --revision "$REVISION" \
  --provider bigmodel \
  --model glm-5.3-flash-main \
  --budget "$GRAPH_BUDGET" \
  --live-llm \
  --record-llm \
  --artifact-root "$A15" \
  --snapshot-root "$REPO/cache/swebench" \
  --spec-snapshot "$REPO/configs/swebench/env-specs-4.1.0.json" \
  --harness-python /root/swebench-venv/bin/python \
  --run-label "$LABEL" \
  --timeout 1800 \
  --image-overrides configs/swebench/instance-image-overrides.json \
  > "$A15/smoke-$ATTEMPT.log" 2>&1
run_code=$?
unset CODEPILOT_DISPATCH_BUDGET_CNY CODEPILOT_DISPATCH_OPENING_CNY
unset CODEPILOT_DISPATCH_OPENING_SOURCE CODEPILOT_DISPATCH_LEDGER
unset CODEPILOT_REUSE_WORKSPACE

ledger_state=missing
if [ -f "$LEDGER" ]; then
  ledger_state=$("$PY" - "$LEDGER" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("frozen"):
    print("frozen")
elif any(item.get("status") != "settled" for item in payload.get("requests") or []):
    print("unsettled")
else:
    print("settled")
PY
)
fi
case "$ledger_state" in
  frozen|unsettled) attempt_status=stopped ;;
  missing) attempt_status=not_dispatched ;;
  *) if [ "$run_code" -eq 0 ]; then attempt_status=completed; else attempt_status=invalid; fi ;;
esac
"$PY" scripts/recovery_admin.py record \
  --registry "$REGISTRY" \
  --attempt-label "$ATTEMPT" \
  --instance "$IID" \
  --artifact-root "$A15" \
  --ledger "$LEDGER" \
  --status "$attempt_status" \
  --repo-root "$REPO" > "$A15/attempt-$ATTEMPT.json" 2>&1
echo "END $IID exit=$run_code ledger=$ledger_state attempt=$attempt_status $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"

if [ "$attempt_status" = "stopped" ]; then
  echo "STOP_LEDGER state=$ledger_state" >> "$STATUS"
  exit 4
fi
if [ "$attempt_status" = "not_dispatched" ]; then
  echo "STOP_PRE_DISPATCH" >> "$STATUS"
  exit 5
fi

"$PY" scripts/l2cal10e_acceptance.py \
  --root "$A15" \
  --instance "$IID" \
  --label "$LABEL" \
  --output "$A15/acceptance-$ATTEMPT.json" > "$A15/acceptance-$ATTEMPT.log" 2>&1
accept_code=$?
echo "ACCEPTANCE code=$accept_code" >> "$STATUS"
if [ "$accept_code" -eq 0 ]; then
  official_mode=$("$PY" - "$A15" "$LABEL" <<'PY'
import glob
import json
import sys
root, label = sys.argv[1], sys.argv[2]
for path in sorted(glob.glob(root + "/reports/*.json")):
    payload = json.load(open(path, encoding="utf-8"))
    if payload.get("run_label") == label:
        print((payload.get("m2_vs_official") or {}).get("official") or "")
        break
else:
    print("")
PY
)
  if [ -n "$official_mode" ] && [ "$official_mode" != "NOT_EVALUATED" ] \
     && [ "$official_mode" != "OFFICIAL_NOT_EVALUATED" ]; then
    "$PY" - "$A15" "$ATTEMPT" "$IID" "$LABEL" <<'PY'
import glob
import json
import sys
from datetime import UTC, datetime
root, attempt, instance_id, label = sys.argv[1:5]
report = {}
for path in sorted(glob.glob(root + "/reports/*.json")):
    payload = json.load(open(path, encoding="utf-8"))
    if payload.get("run_label") == label:
        report = payload
        break
harness = report.get("harness") or {}
verdict = (report.get("m2_vs_official") or {}).get("official") or "NOT_EVALUATED"
summary = {
    "schema_version": 1,
    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    "instance_id": instance_id,
    "label": label,
    "run_id": report.get("run_id"),
    "official_verdict": verdict,
    "official": harness,
    "source": "runner_report",
}
with open(f"{root}/official-evaluation-{attempt}.json", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
print(verdict)
PY
    echo "OFFICIAL source=runner_report" >> "$STATUS"
  else
    "$PY" scripts/l2cal10e_official.py \
      --root "$A15" \
      --instance "$IID" \
      --label "$LABEL" \
      --snapshot-root "$REPO/cache/swebench" \
      --harness-python /root/swebench-venv/bin/python \
      --output "$A15/official-evaluation-$ATTEMPT.json" > "$A15/official-$ATTEMPT.log" 2>&1
    echo "OFFICIAL code=$?" >> "$STATUS"
  fi
fi

"$PY" scripts/batch_budget.py \
  --base "$BASE" \
  --ledger "$LEDGER_NAME" \
  --batch-cap "$BATCH_CAP" \
  --per-instance "$PER_INSTANCE" \
  --opening-reconciliation "$MANIFEST" \
  --instance "$IID" \
  --attempt-registry "$REGISTRY" \
  --repo-root "$REPO" >> "$STATUS" 2>&1 || true
"$PY" scripts/recovery_admin.py totals --registry "$REGISTRY" --repo-root "$REPO" >> "$STATUS" 2>&1 || true
echo "BATCH_END $ATTEMPT $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"
