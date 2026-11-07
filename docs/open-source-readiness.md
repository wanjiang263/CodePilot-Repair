# Open-source release readiness

Audit date: 2026-11-06; Phase 37 addendum: 2026-11-07. Target: research / engineering preview.

The detailed results and evidence boundaries are in [the audit report](open-source-audit.md).

## Completed locally

- [x] Apache-2.0 LICENSE, jiaoenqi attribution, NOTICE and package metadata.
- [x] Owner confirmed personal ownership with no company/team restrictions.
- [x] SECURITY, CONTRIBUTING and CODE_OF_CONDUCT; owner-confirmed private contact email.
- [x] Remote branch/tag inventory and reachability checks; independent non-shallow clone of the project branch in the 2026-11-06 audit. The local pvlib main is excluded; this is historical evidence, not a new clone of the Phase 37 candidate.
- [x] Current tree / project history / 176 local troubleshooting files scanned; 33 precise historical false positives reviewed. No detected real credentials requiring rotation.
- [x] Third-party source/evidence notice inventory and upstream license retrieval; frontend production dependency notices.
- [x] Universal Python lockfile and frontend dependency vulnerability checks, compatible security fixes and optional dependency version updates; both SBOMs.
- [x] Exact-path source archive and wheel resources; fresh installation, installed offline demo and relevant regression checks.
- [x] Frontend and package CI jobs, read-only workflow permissions, scheduled dependency audit and Dependabot configuration prepared.
- [x] GitHub vulnerability alerts and automated security fixes enabled and verified.
- [x] Default branch protection forbids force pushes and deletion, including for administrators; ordinary solo-maintainer pushes remain possible.

## Publication actions remaining

- [x] Commit and push the candidate to `codex/open-source-preview` only; local main and other refs were excluded.
- [x] Phase 37 adds Chromium browser acceptance, bounded real GLM SSE streaming,
  interrupt/timeout/usage/offline tests, and a fail-closed guard for the
  deferred SDK directory-build path. See [Phase 37 validation](notes/phase37-validation.md).
- [x] Confirm the candidate's six CI jobs and separate dependency audit are
  green: [CI run 42378433945](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/42378433945)
  and [audit run 42378433962](https://github.com/wanjiang263/CodePilot-Repair/actions/runs/42378433962)
  on `cddeeb2`. Local Windows full offline, Docker, browser, targeted and
  real GLM evidence is recorded in the
  [Phase 37 addendum](notes/phase37-validation.md#final-evidence-addendum-at-2026-11-07-2125-0800);
  the one Windows full-run timeout failure and its isolated passing rerun
  remain separate evidence. A later documentation-only commit must have its
  own green CI result before publication; its source code is unchanged.
- [ ] Update remote main normally after candidate CI passes.
- [ ] Add required status checks after the new CI jobs actually execute; basic branch protection is already enabled.
- [ ] Confirm public visibility and preview release publication. Repository remains private in this audit.
- [ ] Optionally enable GitHub private vulnerability reporting after publication; confirmed email is already available.

## Explicitly unverified

The original 2026-11-06 audit did not perform a real provider request, model
download, official Harness run, browser E2E or 24h soak. Those items were
addressed by later phase-specific evidence: see the 24h soak and provider
results in the Phase 34/35 evidence pack, and the newer browser and SSE
streaming results in the Phase 37 report. These are separate runs with distinct
source provenance and are not one CI acceptance pass.

Phase 37 changed production HTTP/SSE, provider and sandbox code. Targeted
offline tests, browser acceptance, bounded real GLM streaming, full offline
regression and full Docker were rerun on that source, and candidate CI passed
on Linux. The local Windows full offline run has
one Windows process-tree timing failure whose complete test module passed in
an isolated rerun; these are distinct current-source results and must not be
described as one zero-failure pass. Optional dependency upgrades
have passed resolution and interface/offline checks, not real model or official
Harness runtime verification. Full-history clone remains unverified because
the local shallow boundary is not on origin; the earlier audit's clean
non-shallow clone covers the project branch at that time, while a fresh clone
of the final candidate is still recommended before publication. Preserve
these limitations in a preview release.

Do not zip the whole working directory. Use the audited source archive/allowlist so ignored local data and nested worktrees cannot be included accidentally.
