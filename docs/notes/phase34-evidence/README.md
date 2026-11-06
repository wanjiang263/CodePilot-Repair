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
- **Docker suite**: **PASS** — 35/35 under the Bash/Git Bash environment
  (316.44s). The 9 synthetic-suite failures occurred only in PowerShell Terminal
  runs; the same tests pass under Bash, so this was a Terminal git/workspace
  environment artifact, not a v13 code or fixture defect. The failed PowerShell
  JUnits are retained as history; the authoritative JUnit is
  `docker-junit-bash.xml`.
- **24h soak**: **PASS** — 1440 cycles over 24h (2026-11-05 18:54 → 2026-11-06
  18:54 UTC), 0 errors, 0 lock failures, 0 checkpoint failures. RSS first
  140.9 MB / last 145.5 MB / max 695.4 MB (sawtooth); the regression slope is
  **-98,110 bytes/cycle**, i.e. no upward trend. `soak-summary.json` and the full
  `soak-heartbeat.jsonl` are in the pack. Earlier attempts under full-regression
  / large-repo / Docker load failed with `WorkspaceError: base_commit not in
  cache`; they and the two 15-cycle idle smokes are retained as history
  (`soak-attempt*.jsonl`, `soak-idle-smoke*.json`).
- **Real provider**: **PASS for both models** (OpenAI-compatible protocol,
  budget gate 20 CNY, one request per model, no retries). `glm-5.3-flash`
  returned `real-verified`: 17 in / 16 out tokens, cost 0.0000584 CNY
  (`cost_known=true`). `spark-x2.6` returned `real-verified`: 25 in / 16 out
  tokens; no price is configured for it, so `cost_known=false` and the cost
  stays unknown (0.0 is a lower bound, not a claim). The merged matrix has 2
  real requests, 42 in / 32 out tokens, `cost_known=false`. Two earlier Spark
  attempts failed through the local proxy `127.0.0.1:7899`; the direct
  `NO_PROXY='*'` retry succeeded, and those uncertain attempts are kept under
  `provider-real/spark/`. Raw runs, JUnit and logs are in `provider-real/glm`
  and `provider-real/spark`; no key is stored in the pack.

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
| `provider-matrix.json` | merged fake 166 tests + two real-verified rows |
| `provider-real/` | raw GLM/Spark real runs, JUnit, logs, uncertain attempts |
| `verify-v13.json`, `external-witness-v13.txt` | 19-version archive report + witness |
| `frontend-summary.json`, `frontend-run.log` | frontend build/vitest evidence |
| `large-repo-summary.json`, `large-repo-profile.json` | 20k index timings |
| `docker-summary.json`, `docker-junit*.xml` | Docker suite evidence |
| `soak-summary.json`, `soak-heartbeat.jsonl` | 24h soak summary + full heartbeat |
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
