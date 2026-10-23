#!/usr/bin/env bash
# l2cal10e: finish the two Cal10 instances that never dispatched.
#
# Serial, one fresh ledger per attempt. The internal M2 closure runs first and
# the official harness is deferred until the closure is accepted. A retry must
# use a new label (first argument) and a new artifact root suffix (second
# argument); the opening manifest then includes every earlier registry entry.
set -u
REPO=${CODEPILOT_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$REPO" || exit 9
set -a
. ./.env
set +a
export DOCKER_HOST=${DOCKER_HOST:-unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock}
PY=${CODEPILOT_PYTHON:-$REPO/.venv/bin/python}
export CODEPILOT_TESTDEPS_DIR=${CODEPILOT_TESTDEPS_DIR:-$REPO/artifacts/phase18/l2-cal10e/testdeps}

LABEL=${1:-l2cal10e}
ROOT_SUFFIX=${2:-}
BASE=artifacts/phase18/l2-cal10e
CACHE_SOURCE=artifacts/phase18/l2-cal10-resume
CATALOG=configs/swebench/l2cal10e-instances.json
REGISTRY="$BASE/attempts.json"
LEDGER_NAME="dispatch-ledger-$LABEL.json"
STATUS="$BASE/batch-status-$LABEL.log"
REVISION=6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2
if [ "$LABEL" = "l2cal10e" ]; then
  MANIFEST="$BASE/recovery-manifest-l2cal10c-l2cal10d.json"
  EXPECTED_OPENING=2.809654
else
  MANIFEST="$BASE/recovery-manifest-$LABEL.json"
  EXPECTED_OPENING=""
fi

mkdir -p "$BASE" "$BASE/preflight"
echo "BATCH_START $LABEL $(date -u +%Y-%m-%dT%H:%M:%SZ) suffix=$ROOT_SUFFIX" >> "$STATUS"

IDS=($("$PY" - "$CATALOG" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(" ".join(item["instance_id"] for item in payload["instances"]))
PY
))
if [ -n "${L2CAL10E_ONLY:-}" ]; then
  IDS=("$L2CAL10E_ONLY")
fi

# ---------------------------------------------------------------- preflight
preflight_out=$("$PY" scripts/l2cal10e_preflight.py \
  --repo-root "$REPO" \
  --base "$BASE" \
  --label "$LABEL" \
  --root-suffix="$ROOT_SUFFIX" \
  --cache-source "$CACHE_SOURCE" \
  --instances "$CATALOG" \
  --prepare-caches 2>&1)
preflight_code=$?
printf '%s\n' "$preflight_out" > "$BASE/preflight/preflight-$LABEL.json"
if [ "$preflight_code" -ne 0 ]; then
  echo "PREFLIGHT_FAIL code=$preflight_code" >> "$STATUS"
  echo "$preflight_out" | tail -20
  exit 3
fi
echo "PREFLIGHT_OK $LABEL" >> "$STATUS"

for id in "${IDS[@]}"; do
  dry_root="$BASE/preflight/dry-run-$LABEL-$id"
  mkdir -p "$dry_root"
  "$PY" -u scripts/swebench_smoke.py \
    --instance-id "$id" \
    --split test \
    --revision "$REVISION" \
    --provider bigmodel \
    --model glm-5.3-flash-main \
    --budget 3.00 \
    --dry-run \
    --artifact-root "$dry_root" \
    --snapshot-root "$REPO/cache/swebench" \
    --spec-snapshot "$REPO/configs/swebench/env-specs-4.1.0.json" \
    --harness-python /root/swebench-venv/bin/python \
    --run-label "${LABEL}Dry" \
    --image-overrides configs/swebench/instance-image-overrides.json \
    > "$dry_root/dry-run.log" 2>&1
  dry_code=$?
  dry_report=$(ls -t "$dry_root"/reports/*.json 2>/dev/null | head -n 1)
  if [ "$dry_code" -ne 0 ] || [ -z "$dry_report" ]; then
    echo "PREFLIGHT_FAIL dry_run=$id code=$dry_code" >> "$STATUS"
    exit 3
  fi
  if ! "$PY" - "$dry_report" <<'PY' >> "$STATUS" 2>&1
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
checks = (payload.get("dry_run_checks") or {}).get("checks") or {}
expected = ("llm_called", "docker_ensure_called", "harness_executed")
bad = {key: checks.get(key) for key in expected if checks.get(key) is not False}
print("DRY_RUN_CHECKS " + json.dumps(checks, ensure_ascii=False))
raise SystemExit(0 if not bad else 1)
PY
  then
    echo "PREFLIGHT_FAIL dry_run_checks=$id" >> "$STATUS"
    exit 3
  fi
done

# ------------------------------------------------------- opening and budget
if [ -e "$MANIFEST" ]; then
  if [ -z "$EXPECTED_OPENING" ]; then
    echo "STOP_MANIFEST_EXISTS $MANIFEST" >> "$STATUS"
    exit 9
  fi
  echo "MANIFEST_REUSED $MANIFEST" >> "$STATUS"
else
  manifest_args=(
    manifest
    --output "$MANIFEST"
    --run-id "$LABEL"
    --reason "l2cal10e opening: l2cal10c plus l2cal10d upper bounds"
    --repo-root "$REPO"
    --source "artifacts/phase18/l2-cal10/pydata__xarray-4493/dispatch-ledger-l2cal10c.json=pydata__xarray-4493"
    --source "artifacts/phase18/l2-cal10-resume/pylint-dev__pylint-6506/dispatch-ledger-l2cal10d.json=pylint-dev__pylint-6506"
    --settled "artifacts/phase18/l2-cal10/astropy__astropy-12907/dispatch-ledger-l2cal10c.json=astropy__astropy-12907"
    --settled "artifacts/phase18/l2-cal10/django__django-11910/dispatch-ledger-l2cal10c.json=django__django-11910"
    --settled "artifacts/phase18/l2-cal10/matplotlib__matplotlib-23314/dispatch-ledger-l2cal10c.json=matplotlib__matplotlib-23314"
    --settled "artifacts/phase18/l2-cal10/mwaskom__seaborn-3010/dispatch-ledger-l2cal10c.json=mwaskom__seaborn-3010"
    --settled "artifacts/phase18/l2-cal10/pallets__flask-4045/dispatch-ledger-l2cal10c.json=pallets__flask-4045"
    --settled "artifacts/phase18/l2-cal10/psf__requests-2148/dispatch-ledger-l2cal10c.json=psf__requests-2148"
  )
  if [ -f "$REGISTRY" ]; then
    manifest_args+=(--include-registry --registry "$REGISTRY")
  fi
  "$PY" scripts/recovery_admin.py "${manifest_args[@]}" > "$BASE/preflight/manifest-$LABEL.json" 2>&1
  manifest_code=$?
  if [ "$manifest_code" -ne 0 ]; then
    echo "STOP_MANIFEST_FAIL code=$manifest_code" >> "$STATUS"
    exit 9
  fi
fi
opening=$("$PY" - "$MANIFEST" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"{payload['total_upper_bound_cny']:.6f}")
PY
)
echo "OPENING $LABEL $opening manifest_sha=$(sha256sum "$MANIFEST" | awk '{print $1}')" >> "$STATUS"
if [ -n "$EXPECTED_OPENING" ] && [ "$opening" != "$EXPECTED_OPENING" ]; then
  echo "STOP_OPENING_MISMATCH expected=$EXPECTED_OPENING actual=$opening" >> "$STATUS"
  exit 9
fi

for id in "${IDS[@]}"; do
  root="$BASE/$id$ROOT_SUFFIX"
  ledger="$root/$LEDGER_NAME"
  if [ -e "$ledger" ] || [ -e "$root/.done-$LABEL" ]; then
    echo "STOP_LEDGER_EXISTS $id $ledger" >> "$STATUS"
    break
  fi
  mkdir -p "$root"

  budget_args=(
    --base "$BASE"
    --ledger "$LEDGER_NAME"
    --batch-cap 30.0
    --per-instance 3.0
    --opening-reconciliation "$MANIFEST"
    --instance "$id"
    --repo-root "$REPO"
  )
  if [ -f "$REGISTRY" ]; then
    budget_args+=(--attempt-registry "$REGISTRY")
  fi
  budget_out=$("$PY" scripts/batch_budget.py "${budget_args[@]}" 2>&1)
  budget_code=$?
  echo "BUDGET $id $budget_out" >> "$STATUS"
  if [ "$budget_code" -ne 0 ]; then
    echo "STOP_BUDGET $id code=$budget_code" >> "$STATUS"
    break
  fi
  allowed=$(printf '%s\n' "$budget_out" | sed -n 's/.*allowed=\([0-9.]*\).*/\1/p')
  if [ -z "$allowed" ] || ! awk -v a="$allowed" 'BEGIN {exit !(a > 0 && a <= 3.0)}'; then
    echo "STOP_BUDGET $id invalid_allowed=$allowed" >> "$STATUS"
    break
  fi

  export CODEPILOT_DISPATCH_BUDGET_CNY="$allowed"
  export CODEPILOT_DISPATCH_LEDGER="$ledger"
  echo "START $id allowed=$allowed $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"
  "$PY" -u scripts/l2cal10e_internal.py \
    --instance-id "$id" \
    --split test \
    --revision "$REVISION" \
    --provider bigmodel \
    --model glm-5.3-flash-main \
    --budget "$allowed" \
    --live-llm \
    --record-llm \
    --artifact-root "$root" \
    --snapshot-root "$REPO/cache/swebench" \
    --spec-snapshot "$REPO/configs/swebench/env-specs-4.1.0.json" \
    --harness-python /root/swebench-venv/bin/python \
    --run-label "$LABEL" \
    --timeout 1800 \
    --image-overrides configs/swebench/instance-image-overrides.json \
    > "$root/smoke-$LABEL.log" 2>&1
  run_code=$?
  unset CODEPILOT_DISPATCH_BUDGET_CNY CODEPILOT_DISPATCH_LEDGER

  ledger_state=missing
  if [ -f "$ledger" ]; then
    ledger_state=$("$PY" - "$ledger" <<'PY'
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
    --attempt-label "$LABEL" \
    --instance "$id" \
    --artifact-root "$root" \
    --ledger "$ledger" \
    --status "$attempt_status" \
    --repo-root "$REPO" > "$root/attempt-$LABEL.json" 2>&1
  echo "END $id exit=$run_code ledger=$ledger_state attempt=$attempt_status $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"

  if [ "$ledger_state" = "frozen" ] || [ "$ledger_state" = "unsettled" ]; then
    touch "$root/.stopped-$LABEL"
    echo "STOP_LEDGER $id state=$ledger_state" >> "$STATUS"
    break
  fi
  if [ "$attempt_status" = "not_dispatched" ]; then
    touch "$root/.invalid-$LABEL"
    echo "STOP_PRE_DISPATCH $id" >> "$STATUS"
    break
  fi

  "$PY" scripts/l2cal10e_acceptance.py \
    --root "$root" \
    --instance "$id" \
    --label "$LABEL" \
    --output "$root/acceptance-$LABEL.json" > "$root/acceptance-$LABEL.log" 2>&1
  accept_code=$?
  echo "ACCEPTANCE $id code=$accept_code" >> "$STATUS"
  if [ "$accept_code" -eq 0 ]; then
    "$PY" scripts/l2cal10e_official.py \
      --root "$root" \
      --instance "$id" \
      --label "$LABEL" \
      --snapshot-root "$REPO/cache/swebench" \
      --harness-python /root/swebench-venv/bin/python \
      --output "$root/official-evaluation-$LABEL.json" > "$root/official-$LABEL.log" 2>&1
    official_code=$?
    echo "OFFICIAL $id code=$official_code" >> "$STATUS"
    touch "$root/.done-$LABEL"
  else
    touch "$root/.rejected-$LABEL"
    if [ "$run_code" -ne 0 ] || grep -Eqi 'Docker daemon|Cannot connect to the Docker|WorkspaceError|SWEBenchEnvironmentError|镜像.*(失败|不存在)|image .*not found' "$root/smoke-$LABEL.log"; then
      echo "STOP_INFRA $id" >> "$STATUS"
      break
    fi
  fi
done

echo "BATCH_END $LABEL $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"
for id in "${IDS[@]}"; do
  final_args=(
    --base "$BASE"
    --ledger "$LEDGER_NAME"
    --batch-cap 30.0
    --per-instance 3.0
    --opening-reconciliation "$MANIFEST"
    --instance "$id"
    --repo-root "$REPO"
  )
  if [ -f "$REGISTRY" ]; then
    final_args+=(--attempt-registry "$REGISTRY")
  fi
  "$PY" scripts/batch_budget.py "${final_args[@]}" >> "$STATUS" 2>&1 || true
done
if [ -f "$REGISTRY" ]; then
  "$PY" scripts/recovery_admin.py totals --registry "$REGISTRY" --repo-root "$REPO" >> "$STATUS" 2>&1 || true
fi
