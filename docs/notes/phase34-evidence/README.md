# Phase34 evidence pack

This directory is the self-contained evidence input for
`scripts/phase34_runtime_results.py`. It exists so `--check` works from a clean
clone without the generation-time scratch directory (`D:/cp34/final-v9`).

## Entry points

| File | Purpose |
|---|---|
| `test-gate-<group>.json` | Phase32 test-gate summary for one regression group (nine groups: unit-a/b/c/d, eval, sandbox, integration, api, graph) |
| `gate-<group>-run1.log` / `.xml` | Raw gate log and JUnit XML; the runtime JSON records their sha256 and parses the counts |
| `run-<group>.log`, `run-resumable-gate.sh`, `resumable.log` | Runner log and the exact resumable runner used for the nine groups |
| `static-checks.json` | `scripts/phase32_static_checks.py` result at the evidence code commit |
| `provider-matrix.json`, `provider-matrix/` | Fake-provider matrix summary plus its JUnit/log; real requests 0, cost 0 |
| `verify-v9.json`, `verify-v9.log` | `scripts/phase28_archive_verify.py --all` report for 15 archive versions |
| `external-witness-v9.txt` | External witness value for the v9 receipt |
| `pre-fix-check.log` | Phase33 `--check` failure at the Phase34 base commit (before the lifecycle fix) |
| `SHA256SUMS` | Integrity manifest for every file in this directory |

## Commands

```bash
# Normal delivery check: no --scratch argument, reads this directory.
.venv/Scripts/python.exe scripts/phase34_runtime_results.py --check

# After changing evidence: rewrite SHA256SUMS, then the runtime JSON.
.venv/Scripts/python.exe scripts/phase34_evidence_manifest.py --write
.venv/Scripts/python.exe scripts/phase34_runtime_results.py --write --base dba045dabd70f0e6eb7779287136944b589f2734

# Verify only the manifest.
.venv/Scripts/python.exe scripts/phase34_evidence_manifest.py --check

# Clean-clone reproducibility regression (clones this repository, no external scratch).
bash scripts/phase34_selfcontained_check.sh
```

`--scratch` can still point at another directory, but that directory must be a
copy of this pack with its own `SHA256SUMS`; a bare generation-time scratch
directory is rejected as `evidence_missing`.

## Path provenance

Raw logs and JSON summaries may contain absolute paths from the machine that
generated them. They are historical provenance only. The runtime generator
rebinds gate `log`/`junit` paths to the matching file name in this directory,
reduces `basetemp` to its directory name, and strips repository/evidence
prefixes from recorded commands before writing
`docs/notes/phase34-runtime-results.json`. The delivered runtime JSON therefore
contains no `D:/cp34` path and passes `--check` in a clone.

The full nine-group regression was run at code commit
`cc0dd9a9e0b9d61d9d897b0584ee4c54f6c89af2` (C15); this pack preserves those
gate summaries, logs and JUnit files byte-for-byte. The v9 candidate and its
receipt/witness live under `configs/harness/candidates/phase34-candidate-v9/`
and `configs/harness/receipts/phase34-candidate-v9.json`.

The unverified scope is unchanged: real provider streaming/usage/billing, full
Docker suite, large repository (>=20k files), 24h stability, frontend checks,
and the `swebench_runner.py:423` P2 build-context risk were not run in Phase34.
