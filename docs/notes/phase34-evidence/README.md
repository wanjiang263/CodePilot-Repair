# Phase34/35 evidence pack

This directory is the self-contained evidence input for
`scripts/phase34_runtime_results.py`. It exists so `--check` works from a clean
clone without any generation-time scratch directory.

## Final archive

The final candidate is `phase34-candidate-v13` (`pending`, parent
`phase34-candidate-v12`, source commit
`7d5e75dd1e57243ce923f35ecc1ab2c0d9757c0d`, diff base
`254b0a81248b8455c236696f9915e2a237b8c05e`). It adds the activation dry-run
test and the runtime external-validation section to the v12 source tree; the
134-file curated source manifest covers the inherited list plus every
SOURCE_SCOPE change.

`verify-v13.json` is the 19-version archive report:
`new=12 old=7 new_failures=0 unexplained=0 witness=verified
historical_integrity=ok`, historical baseline 76 files / 0 mismatches, v13
32/32. v12 and earlier verify reports are retained as history; the runtime
generator reads the newest `verify-v*.json`.

## Final regression (C43, code C40)

| group | result |
|---|---|
| unit-a | 1535 passed |
| unit-b | 1138 passed, 2 skipped |
| unit-c | 427 passed |
| unit-d | 25 passed |
| eval | 482 passed, 1 skipped (Docker unavailable) |
| sandbox | 66 passed |
| integration | 126 passed |
| api | 117 passed, 1 skipped |
| graph | 266 passed |

Total: **4182 passed / 0 failed / 4 skipped**, selected **4186**. The scratch
root is `D:/cp35/final13`; a previous run under `D:/cp35/regression` exposed a
path-name-sensitive issue analyzer rule ("regression" in the tmp path), so the
final run uses a neutral scratch name.

## External validation

- **Frontend**: `npm ci`, typecheck, lint, vitest **14 files / 50 tests** and
  build all passed. The repository has no Playwright/Cypress suite; browser E2E
  remains a known gap.
- **20k large repo**: `phase32_index_profile.py` on 20,201 files: cold 208.4s,
  no-change 77.5s, single-file 73.5s, fast 72.6s, 0 parse errors, DB 24.5 MB,
  peak working set 332–463 MB. `large-repo-profile.json` is the raw output.
- **Docker suite**: **26 passed / 9 failed** of 35. The 9 failures are all in
  `tests/docker/test_synthetic_suite.py`; the evaluator's workspace cache cannot
  resolve the fixture `metadata.base_commit` (`WorkspaceError`). Reproduced in a
  focused recheck and in an idle full rerun, so it is deterministic and not a
  v13 regression. The 26 non-synthetic Docker tests pass. A fix changes
  `tests/` or `src/` and requires a new candidate (v14).
- **24h soak**: running in a clean, Docker-stopped environment; the summary is
  `summary-final.json` (expected 2026-11-07 02:45 +08:00). Earlier attempts under
  the full-regression / large-repo / Docker load failed with the same
  `WorkspaceError: base_commit not in cache`; the failed attempts are retained as
  `soak-attempt1-heartbeat.jsonl`, `soak-attempt2-failed.json` /
  `soak-attempt2-heartbeat.jsonl`, and `soak-attempt3-heartbeat.jsonl`. The idle
  smokes (`soak-idle-smoke1.json`, `soak-idle-smoke2.json`) passed with 15
  cycles each and 0 errors/lock/checkpoint failures. `soak-summary.json` is added
  when the 24h run completes.
- **Real provider**: blocked. `.env` with `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
  `PHASE32_REAL_BUDGET_CNY=20` was not present, so no real request was made
  (`real_requests=0`, `cost=0`). The provider matrix remains the fake 166-test
  run. `provider-matrix.json` will be replaced by the merged two-model
  evidence if the credentials are provided later.

## Counts (C38/C43)

- 318 commits at the Phase34 closeout (grows with later docs commits).
- 787 effective SOURCE_SCOPE files (including `configs/harness/`, excluding
  candidates/receipts).
- v13's 134-file source set is the curated inherited+changed list.
- Main checkout: 172 `git status` untracked entries / 176 files (`logs/`
  collapses 5→1).

## Entry points and commands

| File | Purpose |
|---|---|
| `test-gate-<group>.json` | Phase32 gate summary for one group |
| `gate-<group>-run1.log/.xml` | raw gate log + JUnit |
| `static-checks.json` | C40 static result (17 files) |
| `provider-matrix.json` | fake provider matrix (166 tests; real blocked) |
| `verify-v13.json`, `external-witness-v13.txt` | 19-version archive report + witness |
| `frontend-summary.json`, `frontend-run.log` | frontend build/vitest evidence |
| `large-repo-summary.json`, `large-repo-profile.json` | 20k index timings |
| `docker-summary.json`, `docker-junit*.xml` | Docker suite evidence |
| `soak-summary.json` | 24h soak summary (added when complete) |
| `SHA256SUMS` | integrity manifest for every evidence file except itself |

```bash
.venv/Scripts/python.exe scripts/phase34_runtime_results.py --check
.venv/Scripts/python.exe scripts/phase34_evidence_manifest.py --check
bash scripts/phase34_selfcontained_check.sh
```

## History and byte stability

The repository is shallow (`shallow` contains `04a523f`; that boundary is not on
any `origin` branch, so `git fetch --unshallow` is a no-op). The clean-clone
result covers the existing history only; a full-history clone is not verified.
Evidence, candidate, receipt and generated note files are pinned with `-text`
in `docs/notes/.gitattributes` and `configs/harness/.gitattributes`.

The P2 `images.build(path=...)` concern was audited at
`src/codepilot/sandbox/swebench_env.py:423`: the official path does not call it,
and the generic base build uses a `fileobj` micro-context
(`src/codepilot/sandbox/docker.py:354`). No `src/` change was made; it remains a
potential future-misuse guard item.
