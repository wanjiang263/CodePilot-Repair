# Phase 36 — merged open-source preview validation

## Context

Phase34/35 closed at C49 (`2813c65`) on `claude/phase34-v7-archive-closure-942bb1`
with a v13 candidate and a passing evidence chain. The open-source preview
branch `codex/open-source-preview` had diverged from the same C38 ancestor and
carried 10 commits (`af109cf`..`c6500d4`) touching CLI, eval, Docker fixtures,
web locales, CI, SBOM and licensing. At the user's direction the Phase34/35
line was merged into the preview branch as `65b5221` (parents `c6500d4` and
`2813c65`, no conflicts; 74 files changed relative to C49, 154 relative to the
v13 diff base).

The merge changed SOURCE_SCOPE paths, so the v13 evidence no longer described
HEAD: the default `scripts/phase34_runtime_results.py --check` correctly
returned `code_changed_after_evidence` instead of being weakened. The merged
tree was therefore re-validated in full and archived as v14. No history was
rewritten, no commit deleted and no report retro-edited.

## Archive chain (C50–C52)

| item | value |
|---|---|
| candidate | `phase34-candidate-v14`, `pending`, parent `phase34-candidate-v13`, schema 5 |
| source commit | `65b52216a64a770ce67d2ed3883827603056673d` (the merge commit) |
| diff base | `7d5e75dd1e57243ce923f35ecc1ab2c0d9757c0d` (v13 source) |
| resolved source files | 150 |
| candidate commit | `d01b500f23f3a13fb63a296e4e104c7b7c2272bf` (C50) |
| baseline manifest | 80 files, `cc94d57a77706bf73b64189e616fbcaf0fd3148b` (C51) |
| receipt | `407d8f9bb57f7b0bad5d1aacaed20c1f1b6503fcc1268401b559c594fda7b0f2`, bound in C52 |
| external witness | `407d8f9b…b0f2`, verified |
| archive verify | exit 0: 20 versions, 13 new, 7 old, 0 new failures, 0 unexplained, historical integrity ok, baseline 80 files / 0 mismatches, v14 32/32 |

`verify-v14.json` is in the evidence pack. The candidate remains `pending`;
there is no `active.json` or `active.next.json` in `configs/harness`.

## Merged-tree regression (C53)

Nine groups via `scripts/phase32_test_gate.py`, Docker and LLM markers excluded,
scratch `D:/cp35/merged-final`:

| group | result |
|---|---|
| unit-a | 1535 passed |
| unit-b | 1138 passed, 2 skipped |
| unit-c | 427 passed (includes the v14 activation dry-run test) |
| unit-d | 25 passed |
| eval | 483 passed |
| sandbox | 66 passed |
| integration | 126 passed |
| api | 118 passed |
| graph | 266 passed |

Total **4184 passed / 0 failed / 2 skipped**, selected 4186; every group exited
0. Raw summaries, logs and JUnits are in the evidence pack.

## Static checks (merged tree)

`static-checks.json`: 19 changed files. AST parse, ruff check, ruff format,
changed-line mypy, locale JSON parse, LF EOL and the added-line credential scan
all exit 0. The `web/src` scope (typecheck, lint, vitest, build) exits 0 — this
covers the two merged locale files. Both `git diff --check` variants exit 2 and
report only generated archive/evidence artifacts (v13/v14 candidate `.diff`
snapshots, the generated baseline JSON and retained JUnit XML); the
informational full-tree mypy debt check exits 1 and is not gated. No source
file is flagged.

## Docker (merged tree)

The merged suite has 50 tests (the preview adds 15). First attempt: 49/50 —
`apt-get update` returned HTTP 502 through the local proxy `127.0.0.1:7899`.
After a direct image pull the full suite passed **50/50 in 306.72s** under
Bash/Git Bash. The authoritative JUnit is `docker-junit-merged.xml`; the
earlier 35/35 v13 run and the PowerShell history JUnits are retained. The 502
was an external network condition, not a code failure, and is recorded as such
in `docker-summary.json`.

## Carried-over external evidence and provenance

| evidence | source tree | why it still applies to the merged tree |
|---|---|---|
| 24h soak (1440 cycles, 0 errors, RSS slope -98,110 B/cycle) | v13 | `git diff 7d5e75d..65b5221` touches no `src/codepilot/{llm,graph,observability}` file (0 files each) |
| real provider (GLM + Spark, 2 requests) | v13 | same: no `src/codepilot/llm` change |
| 20k large repo (20,201 files) | v13 | no `src/codepilot/{index,repo,retrieval}` change (0 files each) |
| frontend standalone (14 files / 50 vitest) | v13 | merged `web/src` changes are two locale JSON files; the merged static run re-ran typecheck, lint, vitest and build successfully |

The soak and provider evidence remains v13-source evidence by provenance; the
merged tree does not contain different code for those paths.

## Clean clone (C55)

`scripts/phase34_selfcontained_check.sh` at C54 (`f6a1813`) exits 0:
`SOURCE_SHALLOW=true`, `CLONE_SHALLOW=false`, runtime `--check` pass on runtime
JSON sha256 `b149088ccf067ad6a68bd9c867f56c300476be5e5ec064b4824ebd9cb20932b6`,
`SELF_CONTAINED_CHECK=PASS`. The log is
`docs/notes/phase36-selfcontained-check.log`. The clone covers the existing
shallow history only; a full-history clone remains unverified because the
shallow boundary is not on origin.

## Runtime results (C54)

`docs/notes/phase34-runtime-results.json` is recorded at C53
(`8744dd4`), `recorded_through = recorded_head = actual_head_at_write`; the
default `--check` passes. It records 66 phase commits and 17 path-filtered code
commits from the base `dba045d`, all nine gate groups green, Docker 50/50,
archive v14, both provider rows `real-verified`, and the three remaining
unverified items below.

## Remaining gaps

- Real-provider streaming was not exercised; the matrix is non-streaming.
- Browser E2E is absent; only typecheck/lint/vitest/build exist.
- Full-history clone is not verified (shallow boundary not on origin).
- P2 `images.build` context misuse is audited, not guarded.
- Spark cost is unknown (`cost_known=false`; 0.0 is a lower bound).

## Constraints honored

No push, PR, publish or activation. Candidates v1–v14 remain `pending`; no
active pointer was created. Historical candidate bytes are unchanged (archive
verify historical integrity ok). Two real model requests total, within the
authorized 20 CNY gate. No test was skipped, deleted or weakened to hide a
failure. The Spark key is not in the repository or the evidence pack.

## Commit chain

| commit | content |
|---|---|
| `65b5221` | merge of the Phase34/35 line into `codex/open-source-preview` |
| `d01b500` (C50) | v14 candidate files |
| `cc94d57` (C51) | historical baseline manifest (80 files) |
| `c00ab84` (C52) | v14 receipt binding + external witness |
| `8744dd4` (C53) | merged gate/static/Docker evidence, README, SHA256SUMS, verify-v14 |
| `f6a1813` (C54) | runtime results recorded at C53 |
| `df697c6` (C55) | clean-clone check log |
| this commit (C56) | this report and the Phase36 delivery receipt |
