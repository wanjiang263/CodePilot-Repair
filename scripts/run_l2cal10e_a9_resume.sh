#!/usr/bin/env bash
# Resume pytest from the a9 graph checkpoint with a fresh per-instance ledger.
# The a9 config identity (including its 0.816691 graph budget) is kept so the
# deterministic run_id still finds the checkpoint; the new 20/100 guard applies
# to the HTTP ledger and the batch budget.
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
LABEL=l2cal10e-a9
ATTEMPT=l2cal10e-a9-resume
A9="$BASE/$IID-a9"
LEDGER_NAME="dispatch-ledger-$ATTEMPT.json"
LEDGER="$A9/$LEDGER_NAME"
MANIFEST="$BASE/recovery-manifest-$ATTEMPT.json"
REGISTRY="$BASE/attempts.json"
STATUS="$BASE/batch-status-$ATTEMPT.log"
BACKUP="$A9/failed-attempt-a9"
GRAPH_BUDGET=0.816691
PER_INSTANCE_OPENING=2.194482
BATCH_CAP=100.0
PER_INSTANCE=20.0
REVISION=6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2

mkdir -p "$BASE" "$BACKUP"
echo "RESUME_START $ATTEMPT $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"

RUN_DIR=$(find "$A9/runs" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -n 1)
if [ -z "$RUN_DIR" ] || [ ! -f "$RUN_DIR/checkpoints.sqlite" ] || [ ! -f "$RUN_DIR/index.sqlite3" ] \
   || [ ! -d "$RUN_DIR/workspace" ] || [ ! -d "$A9/repo-cache" ]; then
  echo "STOP_A9_CHECKPOINT_MISSING run_dir=$RUN_DIR" >> "$STATUS"
  exit 9
fi
checkpoint_base=$("$PY" - "$RUN_DIR/checkpoint_manifest.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("base_commit", ""))
PY
)
if [ "$checkpoint_base" != "2f7415cfbc4b6ca62f9013f1abd27136f46b9653" ]; then
  echo "STOP_A9_CHECKPOINT_INCOMPATIBLE base=$checkpoint_base" >> "$STATUS"
  exit 9
fi
echo "A9_CHECKPOINT_OK run_dir=$RUN_DIR" >> "$STATUS"

# Preserve the failed a9 evidence before the resume overwrites the report/state.
for name in "dispatch-ledger-l2cal10e-a9.json" "acceptance-l2cal10e-a9.json" \
            "attempt-l2cal10e-a9.json" "smoke-l2cal10e-a9.log"; do
  if [ -f "$A9/$name" ]; then cp -f "$A9/$name" "$BACKUP/$name"; fi
done
if [ -d "$A9/reports" ]; then cp -rf "$A9/reports" "$BACKUP/"; fi
if [ -d "$A9/predictions" ]; then cp -rf "$A9/predictions" "$BACKUP/"; fi
for name in report.json events.jsonl llm_calls.jsonl llm_requests.jsonl \
            state_final.json checkpoint_manifest.json checkpoints.sqlite patch_final.diff; do
  if [ -f "$RUN_DIR/$name" ]; then cp -f "$RUN_DIR/$name" "$BACKUP/$name"; fi
done
echo "A9_FAILED_EVIDENCE_BACKED_UP $BACKUP" >> "$STATUS"

if [ -e "$MANIFEST" ] || [ -e "$LEDGER" ]; then
  echo "STOP_ATTEMPT_EXISTS manifest=$MANIFEST ledger=$LEDGER" >> "$STATUS"
  exit 9
fi

"$PY" scripts/recovery_admin.py manifest \
  --output "$MANIFEST" \
  --run-id "$ATTEMPT" \
  --reason "pytest resume from a9 checkpoint with a fresh per-instance ledger" \
  --repo-root "$REPO" \
  --source "artifacts/phase18/l2-cal10/pydata__xarray-4493/dispatch-ledger-l2cal10c.json=pydata__xarray-4493" \
  --source "artifacts/phase18/l2-cal10-resume/pylint-dev__pylint-6506/dispatch-ledger-l2cal10d.json=pylint-dev__pylint-6506" \
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
if ! awk -v o="$opening" 'BEGIN {exit !(o >= 5.628170 && o <= 5.628172)}'; then
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
if [ -z "$allowed" ] || ! awk -v a="$allowed" 'BEGIN {exit !(a > 0 && a <= 20.0)}'; then
  echo "STOP_BUDGET invalid_allowed=$allowed" >> "$STATUS"
  exit 9
fi

MANIFEST_SHA=$(sha256sum "$MANIFEST" | awk '{print $1}')
OPENING_SOURCE=$(printf '{"kind":"per_instance","recovery_manifest":"%s","recovery_manifest_sha256":"%s"}' \
  "$MANIFEST" "$MANIFEST_SHA")
export CODEPILOT_DISPATCH_BUDGET_CNY="$PER_INSTANCE"
export CODEPILOT_DISPATCH_OPENING_CNY="$PER_INSTANCE_OPENING"
export CODEPILOT_DISPATCH_OPENING_SOURCE="$OPENING_SOURCE"
export CODEPILOT_DISPATCH_LEDGER="$LEDGER"

echo "START_RESUME allowed=$allowed graph_budget=$GRAPH_BUDGET $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"
"$PY" -u scripts/l2cal10e_internal.py \
  --instance-id "$IID" \
  --split test \
  --revision "$REVISION" \
  --provider bigmodel \
  --model glm-5.3-flash-main \
  --budget "$GRAPH_BUDGET" \
  --live-llm \
  --record-llm \
  --artifact-root "$A9" \
  --snapshot-root "$REPO/cache/swebench" \
  --spec-snapshot "$REPO/configs/swebench/env-specs-4.1.0.json" \
  --harness-python /root/swebench-venv/bin/python \
  --run-label "$LABEL" \
  --timeout 1800 \
  --image-overrides configs/swebench/instance-image-overrides.json \
  --resume graph \
  > "$A9/smoke-$ATTEMPT.log" 2>&1
run_code=$?
unset CODEPILOT_DISPATCH_BUDGET_CNY CODEPILOT_DISPATCH_OPENING_CNY
unset CODEPILOT_DISPATCH_OPENING_SOURCE CODEPILOT_DISPATCH_LEDGER

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
  --artifact-root "$A9" \
  --ledger "$LEDGER" \
  --status "$attempt_status" \
  --repo-root "$REPO" > "$A9/attempt-$ATTEMPT.json" 2>&1
echo "END_RESUME exit=$run_code ledger=$ledger_state attempt=$attempt_status $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"

if [ "$attempt_status" = "stopped" ]; then
  echo "STOP_LEDGER state=$ledger_state" >> "$STATUS"
  exit 4
fi
if [ "$attempt_status" = "not_dispatched" ]; then
  echo "STOP_PRE_DISPATCH" >> "$STATUS"
  exit 5
fi

"$PY" scripts/l2cal10e_acceptance.py \
  --root "$A9" \
  --instance "$IID" \
  --label "$LABEL" \
  --output "$A9/acceptance-$ATTEMPT.json" > "$A9/acceptance-$ATTEMPT.log" 2>&1
accept_code=$?
echo "ACCEPTANCE code=$accept_code" >> "$STATUS"
if [ "$accept_code" -eq 0 ]; then
  official_mode=$("$PY" - "$A9" "$LABEL" <<'PY'
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
    "$PY" - "$A9" "$ATTEMPT" "$IID" "$LABEL" <<'PY'
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
    "source": "resume_runner",
}
with open(f"{root}/official-evaluation-{attempt}.json", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
print(verdict)
PY
    echo "OFFICIAL source=resume_runner" >> "$STATUS"
  else
    "$PY" scripts/l2cal10e_official.py \
      --root "$A9" \
      --instance "$IID" \
      --label "$LABEL" \
      --snapshot-root "$REPO/cache/swebench" \
      --harness-python /root/swebench-venv/bin/python \
      --output "$A9/official-evaluation-$ATTEMPT.json" > "$A9/official-$ATTEMPT.log" 2>&1
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
echo "RESUME_END $ATTEMPT $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"
