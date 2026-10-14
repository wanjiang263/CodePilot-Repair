# Controlled pytest failure corpus

These files were captured by real local pytest 9.1.1 executions on 2026-10-14,
not composed to resemble pytest output. They measure parser/rule correctness, not
real model diagnosis accuracy. `provenance.json` records each command, backend,
source and observed exit code. Source programs are retained under `sources/`.

Explicit regeneration (executes only the source literals embedded in the script):

```powershell
.venv\Scripts\python.exe scripts/generate_failure_fixtures.py
```

There are 17 scenarios: 16 JUnit documents containing 17 failed/error testcase
records, plus one real local pytest-timeout execution. On this Windows run the
timeout plugin's thread mode exited before writing JUnit. `timeout.execution.json`
records that outcome with backend `controlled_local_pytest`; it is neither a
fabricated testcase nor a DockerSandbox result. The separately marked Docker E2E
test supplies independent container evidence.

Sanitization replaces the controlled source root with `/fixture`, installation
paths with `[runtime]`, and removes XML hostname/timestamp/duration attributes.
Relative third-party stack paths are retained as untrusted display identities;
the parser never opens them. No private host root is retained. The volatile pair
raises real exceptions containing controlled addresses and temporary paths; these
values are explicit test inputs, not claims of allocated object addresses.

`expected.json` is independently maintained and never read by the classifier or
overwritten by the generator/collector. Expected types, complete messages and
deepest repository lines are asserted in `test_classifier_golden.py`.

For existing project artifacts, use the collection-only command:

```powershell
.venv\Scripts\python.exe scripts/collect_failures.py --input artifacts/phase7-fixtures --output artifacts/phase7-collected
```

It never runs tests, uses content-addressed sanitized samples, reports collected /
duplicate / error counts and sources, and rejects input/output paths outside this
repository. Golden expectations remain separate. Sample files should be reviewed
before committing: generic redaction cannot identify every domain-specific secret.
