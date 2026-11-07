# Phase34/35 evidence pack

This directory is the self-contained evidence input for
`scripts/phase34_runtime_results.py`. It exists so `--check` works from a clean
clone without any generation-time scratch directory.

## Final archive

The final candidate is `phase34-candidate-v14` (`pending`, parent
`phase34-candidate-v13`, source commit
`65b52216a64a770ce67d2ed3883827603056673d`, diff base
`7d5e75dd1e57243ce923f35ecc1ab2c0d9757c0d`). That source commit is the merge
that brings the open-source preview work into the Phase34/35 line; v14 adds
the preview commits (15 Docker tests plus CLI, eval and web/locale changes) on
top of the v13 source tree. Its 150-file curated source manifest covers the
inherited list plus every SOURCE_SCOPE change.

`verify-v14.json` is the 20-version archive report:
`new=13 old=7 new_failures=0 unexplained=0 witness=verified
historical_integrity=ok`, historical baseline 80 files / 0 mismatches, v14
32/32. v13 and earlier verify reports are retained as history; the runtime
generator reads the newest `verify-v*.json`.

## Final regression (merged tree at 65b5221)

| group | result |
|---|---|
| unit-a | 1535 passed |
| unit-b | 1138 passed, 2 skipped |
| unit-c | 427 passed |
| unit-d | 25 passed |
| eval | 483 passed |
| sandbox | 66 passed |
| integration | 126 passed |
| api | 118 passed |
| graph | 266 passed |

Total: **4184 passed / 0 failed / 2 skipped**, selected **4186**. The scratch
root is `D:/cp35/merged-final`. The merged tree adds 15 Docker tests and the
preview's code changes; `docker` and `llm` markers are excluded from this gate
and covered by the Docker and provider sections below. unit-c includes
`tests/unit/test_phase35_activation_dryrun.py`, which resolved v14 as the
latest pending candidate.

`static-checks.json` covers the same merged tree: 19 changed files; AST parse,
ruff check, ruff format, changed-line mypy, locale JSON, EOL and credential
scan all pass, and the `web/src` scope (typecheck, lint, vitest, build) passes.
Both `git diff --check` variants report only generated archive/evidence
artifacts (candidate `.diff` snapshots, baseline JSON, retained JUnit XML);
the informational full-tree mypy debt check remains non-gated.

## External validation

- **Frontend**: `npm ci`, typecheck, lint, vitest **14 files / 50 tests** and
  build all passed in the v13 standalone run; the merged tree re-ran the
  `web/src` scope successfully inside `static-checks.json`. The repository has
  no Playwright/Cypress suite; browser E2E remains a known gap.
- **20k large repo**: `phase32_index_profile.py` on 20,201 files: cold 208.4s,
  no-change 77.5s, single-file 73.5s, fast 72.6s, 0 parse errors, DB 24.5 MB,
  peak working set 332–463 MB. `large-repo-profile.json` is the raw output.
- **Docker suite**: **PASS** on the merged tree — **50/50** under the
  Bash/Git Bash environment (306.72s). The first attempt was 49/50 because
  `apt-get update` returned HTTP 502 through the local proxy; after a direct
  image pull the full suite passed 50/50. The authoritative JUnit is
  `docker-junit-merged.xml`; the earlier 35/35 v13 Bash run
  (`docker-junit-bash.xml`) and the PowerShell history JUnits are retained.
- **24h soak**: **PASS** — 1440 cycles over 24h (2026-11-05 18:54 → 2026-11-06
  18:54 UTC), 0 errors, 0 lock failures, 0 checkpoint failures. RSS first
  140.9 MB / last 145.5 MB / max 695.4 MB (sawtooth); the regression slope is
  **-98,110 bytes/cycle**, i.e. no upward trend. The run used the v13 source
  tree; `git diff 7d5e75d..65b5221 -- src/codepilot/llm src/codepilot/graph
  src/codepilot/observability` is empty, so the merge does not touch the code
  under test. `soak-summary.json` and the full `soak-heartbeat.jsonl` are in
  the pack. Earlier attempts under full-regression / large-repo / Docker load
  failed with `WorkspaceError: base_commit not in cache`; they and the two
  15-cycle idle smokes are retained as history (`soak-attempt*.jsonl`,
  `soak-idle-smoke*.json`).
- **Real provider**: **PASS for both models** (OpenAI-compatible protocol,
  budget gate 20 CNY, one request per model, no retries). `glm-5.3-flash`
  returned `real-verified`: 17 in / 16 out tokens, cost 0.0000584 CNY
  (`cost_known=true`). `spark-x2.6` returned `real-verified`: 25 in / 16 out
  tokens; no price is configured for it, so `cost_known=false` and the cost
  stays unknown (0.0 is a lower bound, not a claim). The merge does not touch
  `src/codepilot/llm`, so the same model client code is covered. The merged
  matrix has 2 real requests, 42 in / 32 out tokens, `cost_known=false`. Two
  earlier Spark attempts failed through the local proxy `127.0.0.1:7899`; the
  direct `NO_PROXY='*'` retry succeeded, and those uncertain attempts are kept
  under `provider-real/spark/`. Raw runs, JUnit and logs are in
  `provider-real/glm` and `provider-real/spark`; no key is stored in the pack.

## Counts (merged HEAD)

- 343 commits at C52, the v14 evidence-chain head (318 at the Phase34
  closeout; the merge and the archive/evidence chain add the rest).
- 788 effective SOURCE_SCOPE files (including `configs/harness/`, excluding
  candidates/receipts); 787 at the Phase34 closeout, plus one preview file.
- v14's 150-file source set is the curated inherited+changed list.
- Main checkout: 172 `git status` untracked entries / 176 files (`logs/`
  collapses 5→1), measured at the Phase34 closeout.

## Entry points and commands

| File | Purpose |
|---|---|
| `test-gate-<group>.json` | Phase32 gate summary for one merged-tree group |
| `gate-<group>-run1.log/.xml` | raw merged-tree gate log + JUnit |
| `static-checks.json` | merged-tree static result (19 files, web scope) |
| `provider-matrix.json` | merged fake 166 tests + two real-verified rows |
| `provider-real/` | raw GLM/Spark real runs, JUnit, logs, uncertain attempts |
| `verify-v14.json`, `external-witness-v14.txt` | 20-version archive report + witness |
| `frontend-summary.json`, `frontend-run.log` | frontend build/vitest evidence |
| `large-repo-summary.json`, `large-repo-profile.json` | 20k index timings |
| `docker-summary.json`, `docker-junit-merged.xml` | merged Docker suite evidence |
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
