#!/usr/bin/env bash
# Resumable final regression for the v9 closeout. Short scratch avoids MAX_PATH;
# GIT_CONFIG_GLOBAL is deliberately not overridden (it broke workspace fetch tests).
set -u

ROOT="D:/Projects/Desktop/CodePilot-Repair/.claude/worktrees/phase34-v7-archive-closure-942bb1"
V="D:/Projects/Desktop/CodePilot-Repair/.venv/Scripts/python.exe"
export PYTHONPATH="$ROOT/src"
export PATH="/c/Users/lenovo/AppData/Local/OpenAI/Codex/bin:$PATH"
cd "$ROOT" || exit 1

run_group() {
    name="$1"
    paths="$2"
    summary="D:/cp34/final-v11/test-gate-$name.json"
    if [ -f "$summary" ]; then
        ok=$("$V" -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(1 if d.get('runs') and all(r.get('exit_code')==0 for r in d['runs']) else 0)" "$summary")
        if [ "$ok" = "1" ]; then
            echo "SKIP $name (green)"
            return 0
        fi
    fi
    echo "RUN $name"
    "$V" scripts/phase32_test_gate.py \
        --target "$name=$paths" \
        --repeat 1 \
        --pytest-arg=-m \
        --pytest-arg="not docker and not llm" \
        --pytest-arg=-p \
        --pytest-arg=no:cacheprovider \
        --scratch D:/cp34/final-v11 \
        --summary "$summary" \
        --min-free-mb 400 \
        --min-headroom-mb 1000 \
        --wait-seconds 300 \
        > "D:/cp34/final-v11/run-$name.log" 2>&1
    echo "$name exit=$?"
    return 0
}

run_group unit-a "tests/unit/agents,tests/unit/api,tests/unit/context,tests/unit/eval,tests/unit/failure,tests/unit/graph,tests/unit/llm"
run_group unit-b "tests/unit/observability,tests/unit/patch,tests/unit/repo,tests/unit/retrieval,tests/unit/sandbox,tests/unit/schemas,tests/unit/settings,tests/unit/storage"
UNIT_C=$("$V" -c "from pathlib import Path; print(','.join(p.as_posix() for p in sorted(Path('tests/unit').glob('test_*.py'))))")
run_group unit-c "$UNIT_C"
run_group unit-d "tests/unit/tools"
run_group eval "tests/eval"
run_group sandbox "tests/sandbox"
run_group integration "tests/integration"
run_group api "tests/api"
run_group graph "tests/graph"
echo "ALL_GROUPS_DONE"
