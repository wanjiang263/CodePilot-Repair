# Open-source release readiness

Audit date: 2026-11-06. Target: research / engineering preview.

The detailed results and evidence boundaries are in [the audit report](open-source-audit.md).

## Completed locally

- [x] Apache-2.0 LICENSE, jiaoenqi attribution, NOTICE and package metadata.
- [x] Owner confirmed personal ownership with no company/team restrictions.
- [x] SECURITY, CONTRIBUTING and CODE_OF_CONDUCT; owner-confirmed private contact email.
- [x] Remote branch/tag inventory and reachability checks; independent non-shallow clone of the project branch. The local pvlib main is excluded.
- [x] Current tree / project history / 176 local troubleshooting files scanned; 33 precise historical false positives reviewed. No detected real credentials requiring rotation.
- [x] Third-party source/evidence notice inventory and upstream license retrieval; frontend production dependency notices.
- [x] Universal Python lockfile and frontend dependency vulnerability checks, compatible security fixes and optional dependency version updates; both SBOMs.
- [x] Exact-path source archive and wheel resources; fresh installation, installed offline demo and relevant regression checks.
- [x] Frontend and package CI jobs, read-only workflow permissions, scheduled dependency audit and Dependabot configuration prepared.
- [x] GitHub vulnerability alerts and automated security fixes enabled and verified.
- [x] Default branch protection forbids force pushes and deletion, including for administrators; ordinary solo-maintainer pushes remain possible.

## Publication actions remaining

- [ ] Review and commit the candidate; push only the selected project branch, not local main or all refs.
- [ ] Update remote main normally and obtain actual results from the new CI jobs.
- [ ] Add required status checks after the new CI jobs actually execute; basic branch protection is already enabled.
- [ ] Confirm public visibility and preview release publication. Repository remains private in this audit.
- [ ] Optionally enable GitHub private vulnerability reporting after publication; confirmed email is already available.

## Explicitly unverified

No real provider request, model download, official Harness run, browser E2E, new full Docker regression or 24h soak was performed in this audit. Optional dependency upgrades have passed resolution and interface/offline checks, not real model or official Harness runtime verification. Preserve these limitations in a preview release.

Do not zip the whole working directory. Use the audited source archive/allowlist so ignored local data and nested worktrees cannot be included accidentally.
