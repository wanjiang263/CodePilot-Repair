# Phase37 — browser acceptance, provider SSE and build-context guard

Date: 2026-11-07. Base: `d86360af4b1dd7ce8ec6087c2c2ac982d45a2027`.

This phase implements the three remaining items requested before publication.
The Phase36/v14 report remains historical evidence; it is not rewritten to
claim that its older source already contained these changes.

## Browser acceptance

`web/e2e/workbench.spec.ts` runs Chromium against the production frontend,
the real authenticated local API and a separate offline worker. No task,
settings, patch or report responses are fabricated. A network abort is used
only for the error/retry scenario. Every run starts with its own temporary
database/settings/artifact directory and refuses to reuse an existing server.
Live models are disabled explicitly, independently of local credentials.

Five tests pass:

1. Invalid key rejection, valid authentication, session-only key storage and reload.
2. Settings saved through the API and retained after reload.
3. Offline task creation and completion, the actual added patch line and report.
4. A visible network failure followed by successful retry against the same task.
5. A missing task displays the API's 404 error.

The suite is available with `cd web && npm run test:e2e`; install Chromium first
with `npx playwright install chromium`. CI installs its browser dependencies
and executes the suite in a separate job. Failed runs retain screenshots and
traces under ignored `output/playwright/`; fixture keys are not provider keys.
This is Chromium desktop coverage, not a claim about every browser/device.

## Provider streaming

Previously the HTTP matrix dripped ordinary JSON response bodies; that did
not validate Chat Completions SSE deltas. The new incremental SSE parser
assembles content, reasoning and indexed tool-call argument fragments and
requires a finish reason, `[DONE]` and acknowledged usage. Truncation,
malformed events or missing usage fail instead of becoming a zero-token success.

`OpenAICompatibleProvider.complete_stream(request, on_delta=...)` is opt-in
and retains the final `LLMResponse` contract. The existing `complete` and
usage wrapper also support models configured with:

```yaml
extra_body:
  stream: true
  stream_options:
    include_usage: true
```

Default models remain non-streaming. This does not introduce browser token
rendering or native Anthropic/Bedrock streaming. An observer runs on the
request's owned I/O thread and must not block it.

The stream uses the existing joined, cancellable HTTP lifecycle and dispatch
ledger. Streaming has no automatic retries, even when the general retry policy
allows retries. A dispatched cancellation, timeout or incomplete stream keeps
its reservation and freezes that ledger. One request's cancellation does not
poison concurrent or later calls using the same provider.

The loopback tests send real TCP fragments, including split UTF-8 characters,
and verify usage settlement, malformed/truncated streams, cancellation,
deadline enforcement, isolation, thread cleanup and wrapper accounting.

### Real provider evidence

The final implementation was exercised against BigModel `glm-5.3-flash`:

| Case | Result |
|---|---|
| Complete stream | 39 content deltas, acknowledged 29 input / 41 output tokens, known cost 0.000138 CNY |
| Cancel after content | Cancelled after one content delta, one dispatch, `unknown_frozen`, no owned I/O thread left |
| Total deadline | `ReadTimeout`, one dispatch, `unknown_frozen`, no owned I/O thread left |

The real timeout occurred before the first content delta. Loopback tests cover
deadlines during an ongoing SSE body; these are distinct pieces of evidence.
Only the OpenAI-compatible GLM endpoint was tested live. Spark/native protocol
streaming is not claimed as verified.

Three initial live probes preceded the per-line cancellation refinement;
three intermediate probes verified the transport afterward, and three accepted
probes revalidated the final parser error guards. All nine requests are
retained, without retries or credential/output-text recording. Across all three
runs the known settled cost is **0.000442 CNY**; six interrupted requests have
unknown actual cost and retain **4.8086016 CNY** of reservations. Their sum,
**4.8090436 CNY accounted or reserved**, is not an actual billed total.
Each scenario had a 1 CNY ledger ceiling and each three-request run a 3 CNY
aggregate ceiling, within the previously authorized 20 CNY envelope.

The probe stores source hashes. The SSE/HTTP/provider files and probe harness
match the `phase37-stream-accepted` live evidence and the current source tree;
all four recorded hashes were recomputed and matched. The directory named
`phase37-stream-final` is a retained intermediate run against an older parser
and probe harness; its name does not make it the accepted evidence. The
intermediate harness is retained as `live/intermediate-probe-used.py.txt`
(SHA-256 `dcf730a3…`) to reproduce that earlier run. The accepted harness hash
is `4e41868a…`.

## P2 build-context guard

Official SDK builds were already deferred and had no production caller.
`DockerSdkBackend.build` now rejects directory-context builds with
`UNSUPPORTED` before creating an SDK client, reading the directory or uploading
anything. Thus an accidental caller cannot send `.env`, caches or nested
worktrees to a daemon. The supported pull path and the generic sandbox's
existing minimal-context build are unchanged. This does not claim to implement
the deferred official image-build feature.

Tests cover ordinary, traversal-shaped and Dockerfile-text arguments and prove
that no SDK interaction or daemon connection occurs.

## Validation and release boundary

- Browser E2E: 5 passed, rerun against the final script.
- Targeted SSE/guard tests: 16 passed; a broader production-path set of 178 tests (SSE, guard, HTTP matrix, GLM provider and SWE-bench environment) also passed in `targeted-final.xml`.
- Protocol/environment regression before the last two extra SSE tests: 589 passed, 1 Docker test deselected; counts overlap and must not be summed.
- Frontend Vitest: 14 files / 50 passed; typecheck, lint and build pass.
- Ruff and format check pass; mypy passes on 218 production files and both new tools (220 files combined).
- Official npm registry audit: 0 known vulnerabilities; SBOM refreshed.
- Full offline regression and Docker revalidation were completed after the
  20:20 reconciliation; see the final evidence addendum below. Their numbers
  are separate current-source runs, not one uninterrupted zero-failure pass.

No provider retry, public push, PR, publication or candidate activation is
performed by these probes. v1–v14 archive bytes are preserved. Since production
source changed, the old Phase34 runtime check must not be presented as current
source acceptance; a new release candidate/evidence snapshot is required.

## Evidence reconciliation at 2026-11-07 20:20 +08:00

The browser suite was rerun against the exact current `web/e2e/workbench.spec.ts`
after that file changed following the first 18:17 run. The final run passed all
five tests (`browser-e2e-final.log`, `browser-results-final.json`, and
`browser-final-last-run.json`); its source hash is recorded in
`source-hashes-final.json`. The 18:17 evidence remains an earlier run.

`format-final.log` (566 files), `ruff-final.log` and `mypy-final.log` (218
production source files) are the current-tree static checks. The earlier
`mypy.log` also covered the two new tools, for 220 files total.
`source-hashes-final.json` binds all 19 changed and added source/configuration
files to this evidence; live SSE hashes are retained separately in each probe
summary.

Two full offline regression attempts were interrupted before completion:
`full-interrupted.log` (collected 4199 selected tests) and the later
`full-regression.log` attempt that was still running at this reconciliation.
The 589-test protocol/environment result and the 16 targeted tests are separate,
completed runs. Docker's authoritative Phase36 result is 50/50 on the prior
merged source; the Phase37 source changed the SDK directory-build method, HTTP
lifecycle and provider streaming. A Phase37 full Docker rerun encountered an
external `python -m pip install pytest` failure while preparing a test image;
it is retained as an incomplete environment-dependent attempt, not converted
into a source-test pass. A current-source full offline result and a current-source
Docker result remain outstanding before a stable release can be claimed.

No further paid provider requests were made during this reconciliation. The
earlier nine GLM requests, their known costs and unknown-cost reservations are
unchanged.

## Final evidence addendum at 2026-11-07 21:25 +08:00

The full offline run that was still active at 20:20 completed on the same
source hashes recorded in `full-regression.status.json`: `full-regression.xml`
has 4202 selected cases with **4199 passed, 1 failed, 2 skipped, 50 deselected**
and 88.04% coverage. The sole failure is the Windows process-tree timing test
`test_timeout_returns_within_the_timeout_plus_a_small_grace`; it took 3.75 s
against a 3.5 s assertion during the long loaded run. `gitcmd-timeout-rerun.xml`
re-ran the complete `tests/unit/repo/test_gitcmd.py` scope in isolation and all
21 cases passed, including that case. This is an isolated environment-dependent
rerun and is retained alongside the one-failure whole-run result; the two are
not merged into a fabricated zero-failure full pass. The two skips are the
opt-in local embedding model and a POSIX-only special-file transfer test.

`docker-current.xml` is a completed current-source Docker run: **50 passed,
0 failed, 0 skipped, 4202 deselected in 704.49 s**, including the real Linux
container commands. The incomplete earlier Docker attempt remains a separate
environment-dependent attempt and is not a source-test result.

`delivery-receipt.json` reconciles these runs with the 178 targeted tests,
5 Chromium browser tests, 14 frontend Vitest files / 50 tests, static checks,
secret scan and the accepted three-request GLM probe. `source-hashes-candidate.json`
binds the final 78 candidate files; its only difference from the 20:20 snapshot
is the README status wording. The earlier README hash remains in
`source-hashes-final.json` as provenance for the earlier snapshot.

A final exact-candidate Gitleaks scan copied all 79 changed/untracked files to
an isolated directory. Its two redacted `generic-api-key` matches are recorded
and reviewed in `gitleaks-precommit-final.json` and `secret-review-final.json`:
one is an evidence filename plus SHA-256 digest, the other is a Python path
attribute expression. Neither is a real credential; no scanner allowlist was
added. The earlier 50-file scan remains a separate intermediate result.

The candidate changed production source, so the historical runtime check is
not current-source acceptance. The candidate commit's GitHub CI, including the
new Chromium job, is still a separate pending result. No public visibility,
main update, PR or release activation has been performed.
