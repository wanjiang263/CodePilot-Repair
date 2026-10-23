#!/usr/bin/env bash
# Phase 18 P4: resume the three Cal10 instances that never dispatched.
# Real provider + real sandbox + official harness, serial, one fresh ledger per instance.
set -u
REPO=${CODEPILOT_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
cd "$REPO" || exit 9
set -a
. ./.env
set +a
export DOCKER_HOST=${DOCKER_HOST:-unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock}
PY=${CODEPILOT_PYTHON:-$REPO/.venv/bin/python}
BASE=artifacts/phase18/l2-cal10-resume
LABEL=l2cal10d
LEDGER_NAME=dispatch-ledger-l2cal10d.json
STATUS="$BASE/batch-status-$LABEL.log"
RECONCILIATION="$BASE/recovery-manifest-l2cal10c.json"
MANIFEST_SHA=0a1119db38d578f17ff4f45695a5412b8e2a78459650461e9cdd538aa187a135
SCOPE_SHA=86069d6c7d75e92d0788d5cf21e2abffddf9f84c420b8734b61327d291badd26
IDS=(
  pylint-dev__pylint-6506
  pytest-dev__pytest-11148
  scikit-learn__scikit-learn-12471
)
CLASSIFY=/mnt/c/Users/lenovo/Desktop/CodePilot-Repair/artifacts/phase18/l2-cal10/classify_run.py

mkdir -p "$BASE"
echo "BATCH_START $LABEL $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"

manifest_sha=$(sha256sum configs/subsets/swebench-lite-cal10.json | awk '{print $1}')
scope_sha=$(printf '%s\n' "${IDS[@]}" | sha256sum | awk '{print $1}')
if [ "$manifest_sha" != "$MANIFEST_SHA" ]; then
  echo "PREFLIGHT_FAIL manifest_sha=$manifest_sha" >> "$STATUS"; exit 3
fi
if [ "$scope_sha" != "$SCOPE_SHA" ]; then
  echo "PREFLIGHT_FAIL scope_sha=$scope_sha" >> "$STATUS"; exit 3
fi
if ! docker info >/dev/null 2>&1; then
  echo "PREFLIGHT_FAIL docker_daemon" >> "$STATUS"; exit 3
fi
if [ ! -s "$REPO/artifacts/phase18/settings/settings.sqlite3" ]; then
  echo "PREFLIGHT_FAIL settings_db" >> "$STATUS"; exit 3
fi
if [ -z "${GLM_API_KEY:-}" ]; then
  echo "PREFLIGHT_FAIL glm_api_key" >> "$STATUS"; exit 3
fi
if [ ! -s "$RECONCILIATION" ]; then
  echo "PREFLIGHT_FAIL reconciliation_manifest" >> "$STATUS"; exit 3
fi
if [ "$(sha256sum artifacts/phase18/l2-cal10/pydata__xarray-4493/dispatch-ledger-l2cal10c.json | awk '{print $1}')" \
   != "291026a82dcd006f26c6ed15cb0885cd7bd36db7ba7e450b5088cd42e9696a2a" ]; then
  echo "PREFLIGHT_FAIL xarray_ledger_hash" >> "$STATUS"; exit 3
fi
for id in "${IDS[@]}"; do
  image="swebench/sweb.eval.x86_64.${id/__/_1776_}:latest"
  image_id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
  case "$id" in
    pylint-dev__pylint-6506) expected=sha256:f2304d2a6461bc098bc872efc4474b34b54eec72f5a87bacc0509a88ba52fb2d ;;
    pytest-dev__pytest-11148) expected=sha256:b8eef9af40c714067a5455e8d88620439b536a5455fed96c5646b4ed37028442 ;;
    scikit-learn__scikit-learn-12471) expected=sha256:57ac1dbdfb6e37c3ab6f8ec5d4a5374369f0b32dc4ffe895e49141e0a7901f5c ;;
    *) expected="" ;;
  esac
  if [ -z "$image_id" ] || [ "$image_id" != "$expected" ]; then
    echo "PREFLIGHT_FAIL image_identity=$id actual=$image_id expected=$expected" >> "$STATUS"; exit 3
  fi
  case "$id" in
    pylint-dev__pylint-6506) base_commit=0a4204fd7555cfedd43f43017c94d24ef48244a5 ;;
    pytest-dev__pytest-11148) base_commit=2f7415cfbc4b6ca62f9013f1abd27136f46b9653 ;;
    scikit-learn__scikit-learn-12471) base_commit=02dc9ed680e7f53f1b0d410dcdd37341c7958eb1 ;;
    *) base_commit="" ;;
  esac
  cache=$(find "$BASE/$id/repo-cache" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -n 1)
  cache_head=$(git -C "$cache" rev-parse HEAD 2>/dev/null || true)
  cache_commits=$(git -C "$cache" rev-list --count HEAD 2>/dev/null || true)
  if [ -z "$cache" ] || [ "$cache_head" != "$base_commit" ] || [ "$cache_commits" != "1" ]; then
    echo "PREFLIGHT_FAIL cache_identity=$id cache=$cache head=$cache_head commits=$cache_commits" >> "$STATUS"; exit 3
  fi
done

for id in "${IDS[@]}"; do
  root="$BASE/$id"
  ledger="$root/$LEDGER_NAME"
  if [ -e "$ledger" ] || [ -e "$root/.done-$LABEL" ]; then
    echo "STOP_LEDGER_EXISTS $id $ledger" >> "$STATUS"
    break
  fi
  mkdir -p "$root"

  budget_out=$("$PY" scripts/batch_budget.py \
    --base "$BASE" \
    --ledger "$LEDGER_NAME" \
    --batch-cap 30.0 \
    --per-instance 3.0 \
    --opening-reconciliation "$RECONCILIATION")
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
  "$PY" -u scripts/swebench_smoke.py \
    --instance-id "$id" \
    --split test \
    --revision 6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2 \
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
  classification=$("$PY" "$CLASSIFY" \
    --root "$root" \
    --exit-code "$run_code" \
    --ledger "$LEDGER_NAME" \
    --log "smoke-$LABEL.log" \
    --allowed "$allowed")
  printf '%s\n' "$classification" > "$root/run-classification-$LABEL.json"
  echo "END $id exit=$run_code $(date -u +%Y-%m-%dT%H:%M:%SZ) $classification" >> "$STATUS"

  if printf '%s' "$classification" | grep -q '"decision": "stop"'; then
    touch "$root/.stopped-$LABEL"
    echo "STOP_SHARED_FAILURE $id" >> "$STATUS"
    break
  fi
  if [ "$run_code" -eq 0 ]; then
    touch "$root/.done-$LABEL"
  else
    touch "$root/.invalid-$LABEL"
  fi
done

echo "BATCH_END $LABEL $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS"
"$PY" scripts/batch_budget.py \
  --base "$BASE" \
  --ledger "$LEDGER_NAME" \
  --batch-cap 30.0 \
  --per-instance 3.0 \
  --opening-reconciliation "$RECONCILIATION" >> "$STATUS" 2>&1 || true
