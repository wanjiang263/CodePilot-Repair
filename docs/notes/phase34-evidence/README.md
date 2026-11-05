# Phase34 evidence pack

This directory is the self-contained evidence input for
`scripts/phase34_runtime_results.py`. It exists so `--check` works from a clean
clone without the generation-time scratch directory (`D:/cp34/final-v10` or
`D:/cp34/final-v11`).

## Final archive

The final candidate is `phase34-candidate-v12` (`pending`, parent
`phase34-candidate-v11`, source commit `254b0a81248b8455c236696f9915e2a237b8c05e`,
diff base `bc2160d471cf3ddf937f214935c0f57408715588`). It resolves the v9/v10
source drift: the 133-file curated source manifest (787 effective SOURCE_SCOPE
files; the candidate covers the inherited list plus every scope change) includes
the evidence-tool scripts, the `configs/harness` byte pin, the
`test_phase26_final.py` startup-wait fix and the LF normalization.

`verify-v12.json` is the 18-version archive report:
`new=11 old=7 new_failures=0 unexplained=0 witness=verified
historical_integrity=ok`, historical baseline 72 files / 0 mismatches. The v9,
v10 and v11 reports are retained as history; the runtime generator reads the
newest `verify-v*.json`.

## Regression evidence

- Eight groups (`unit-a`, `unit-b`, `unit-d`, `eval`, `sandbox`, `integration`,
  `api`, `graph`) are the C32b full run at code C30.
- `unit-c` is a re-run at C35b after the test-only changes: the spawn child's
  `codepilot.graph.builder` import takes 10–13s on this machine, so the test's
  startup wait was raised to 60s (assertions unchanged), and the file was
  normalized from CRLF to LF to satisfy the static EOL gate.
- Final totals: **4181 passed / 0 failed / 3 skipped**, selected **4184**.
  `resumable.log` is the C32b nine-group log (it records the pre-fix unit-c
  timeout); `run-unit-c.log` and `test-gate-unit-c.json` are the green C35b
  re-run. The runtime generator reads the per-group summaries, all exit 0.

## Entry points

| File | Purpose |
|---|---|
| `test-gate-<group>.json` | Phase32 test-gate summary for one regression group |
| `gate-<group>-run1.log` / `.xml` | Raw gate log and JUnit XML; the runtime JSON records their sha256 and parses the counts |
| `run-<group>.log`, `run-resumable-gate.sh`, `resumable.log` | Runner logs and the exact nine-group runner |
| `static-checks.json` | `scripts/phase32_static_checks.py` result at C33 |
| `provider-matrix.json`, `provider-matrix/` | Fake-provider matrix summary plus its JUnit/log; real requests 0, cost 0 |
| `verify-v12.json`, `verify-v12.log` | `scripts/phase28_archive_verify.py --all` report for 18 archive versions |
| `external-witness-v12.txt` | External witness value for the v12 receipt |
| `pre-fix-check.log`, `verify-v9.json`, `verify-v10.json`, `verify-v11.json` | Historical evidence retained for provenance |
| `SHA256SUMS` | Integrity manifest for every file in this directory except itself |

## Commands

```bash
# Normal delivery check: no --scratch argument, reads this directory.
.venv/Scripts/python.exe scripts/phase34_runtime_results.py --check

# After changing evidence: rewrite SHA256SUMS, then the runtime JSON.
.venv/Scripts/python.exe scripts/phase34_evidence_manifest.py --write
.venv/Scripts/python.exe scripts/phase34_runtime_results.py --write --base dba045dabd70f0e6eb7779287136944b589f2734

# Verify only the manifest.
.venv/Scripts/python.exe scripts/phase34_evidence_manifest.py --check

# Clean-clone reproducibility regression.
bash scripts/phase34_selfcontained_check.sh
```

`--scratch` can still point at another directory, but that directory must be a
copy of this pack with its own `SHA256SUMS`; a bare generation-time scratch
directory is rejected as `evidence_missing`.

## History and byte stability

The current repository is shallow (`shallow` contains `04a523f`). The boundary
commit is not reachable from any `origin` branch, so `git fetch --unshallow` is
a no-op; the clean-clone result covers the existing 318-commit history only.
Counts at the C38 closeout: 318 commits; 787 effective SOURCE_SCOPE files;
v12's 133-file source set is curated; the main checkout has 172 `git status`
untracked entries / 176 files (`logs/` collapses 5→1). The clean-clone script
prints `SOURCE_SHALLOW` and `CLONE_SHALLOW`; `CLONE_SHALLOW=false` is a
`--local` marker artifact, not full-history proof. A full-history clone is not
verified.

Evidence, candidate, receipt and generated note files are pinned with
`-text` in `docs/notes/.gitattributes` and `configs/harness/.gitattributes`, so
a clone with a different `core.autocrlf` setting checks out the recorded bytes.
The first clean-clone attempts are preserved under
`docs/notes/phase34-selfcontained-check.first-attempt.log` and
`...pre-json-pin.log`.

The unverified scope is unchanged: real provider streaming/usage/billing, full
Docker suite, large repository (>=20k files), 24h stability, frontend checks,
and the `swebench_runner.py:423` P2 build-context risk were not run in Phase34.
