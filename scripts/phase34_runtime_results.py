"""Phase34 runtime evidence: lifecycle-checked JSON for the v10 closeout.

Same evidence-head semantics as the Phase33 generator: the JSON describes the
tree at ``recorded_through``, not whatever HEAD is now. ``phase_commits`` covers
``base..recorded_through``; ``code_commits`` is path-filtered. ``--check`` reads
the recorded commits, allows only ``docs/`` changes after them, and rebuilds the
payload at the recorded tree. ``recorded_head`` is the evidence head;
``actual_head_at_write`` records the HEAD at generation time explicitly.

Evidence is self-contained: the default scratch is the in-repo pack at
``docs/notes/phase34-evidence`` and ``--check`` verifies its ``SHA256SUMS``
before rebuilding. A missing or tampered file is reported as
``evidence_missing``/``evidence_manifest_mismatch``, never as a payload
mismatch. Absolute paths recorded by the original gate runs are rebound to the
pack by basename, so a clean clone never reads the generation-time scratch.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.phase33_runtime_results import (  # noqa: E402
    DOC_PATH_PREFIX,
    commit_resolves,
    commit_touches_code,
    git,
    is_ancestor,
    load_json,
    merge_gate_records,
    missing,
    normalise,
    regression_rows,
    rel,
    sha256,
    static_section,
    unit_aggregate,
)
from scripts.phase34_evidence_manifest import MANIFEST_NAME, verify_manifest  # noqa: E402

DEFAULT_SCRATCH = ROOT / "docs" / "notes" / "phase34-evidence"
DEFAULT_OUT = ROOT / "docs" / "notes" / "phase34-runtime-results.json"
CANDIDATE_FILES = ("candidate.json", "candidate.diff", "snapshot.json", "sources.json")
CONSUMED_EVIDENCE = (
    "static-checks.json",
    "provider-matrix.json",
    "pre-fix-check.log",
)

UNVERIFIED = [
    "real provider streaming (non-streaming matrix only)",
    "real provider usage/billing (0 requests, 0 cost; rows blocked)",
    "full Docker suite (not run in Phase34)",
    "large repository (>=20k files)",
    "24h stability",
    "frontend checks (web/src zero diff; conditionally skipped)",
    "frontend browser E2E (no suite in the repository)",
    "src/codepilot/sandbox/swebench_env.py:423 build-context potential misuse (P2, audited)",
]

EXTERNAL_EVIDENCE = {
    "docker": "docker-summary.json",
    "large_repo": "large-repo-summary.json",
    "soak": "soak-summary.json",
    "frontend": "frontend-summary.json",
}


def commits_section(
    base: str, recorded_through: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    log_out = git("log", "--format=%H%x09%s", f"{base}..{recorded_through}")
    phase: list[dict[str, Any]] = []
    for line in log_out.splitlines():
        if line.strip():
            commit_hash, _, subject = line.partition("\t")
            phase.append({"hash": commit_hash, "short": commit_hash[:7], "subject": subject})
    code = [commit for commit in phase if commit_touches_code(commit["hash"])]
    return phase, code


def _scrub(value: str, scratch: Path) -> str:
    text = str(value)
    for actual, label in ((scratch, "<evidence>"), (ROOT, "<repo>")):
        for variant in {str(actual), str(actual).replace("\\", "/")}:
            if variant:
                text = text.replace(variant, label)
    return text


def _rebind(value: Any, scratch: Path) -> Any:
    """Map a generation-time absolute path to its file in the evidence pack."""
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute():
        return rel(scratch / (windows.name if windows.is_absolute() else path.name))
    return value.replace("\\", "/")


def evidence_records(scratch: Path) -> dict[str, Any]:
    """Merge gate summaries, rebinding every external path into the pack."""
    merged = merge_gate_records(scratch)
    for run in merged["records"]:
        for key in ("log", "junit"):
            run[key] = _rebind(run.get(key), scratch)
        basetemp = run.get("basetemp")
        if isinstance(basetemp, str) and basetemp:
            run["basetemp"] = PureWindowsPath(basetemp).name
        command = run.get("command")
        if isinstance(command, str) and command:
            run["command"] = _scrub(command, scratch)
    return merged


def _evidence_problem(scratch: Path) -> dict[str, Any] | None:
    report = verify_manifest(scratch)
    if not report.get("ok"):
        problem: dict[str, Any] = {
            "reason": str(report.get("reason") or "evidence_manifest_invalid"),
            "evidence_dir": rel(scratch),
        }
        for key in ("missing", "mismatched", "errors"):
            value = report.get(key)
            if value:
                problem[key] = value
        return problem
    merged = evidence_records(scratch)
    gate_files = sorted(path.name for path in scratch.glob("test-gate*.json"))
    if not gate_files:
        return {
            "reason": "evidence_missing",
            "evidence_dir": rel(scratch),
            "missing": ["test-gate-*.json"],
        }
    required = set(CONSUMED_EVIDENCE)
    if not _verify_reports(scratch):
        return {
            "reason": "evidence_missing",
            "evidence_dir": rel(scratch),
            "missing": ["verify-v*.json"],
        }
    for run in merged["records"]:
        for key in ("log", "junit"):
            value = run.get(key)
            if isinstance(value, str) and value:
                required.add(Path(value).name)
    missing_files = sorted(name for name in required if not (scratch / name).is_file())
    if missing_files:
        return {
            "reason": "evidence_missing",
            "evidence_dir": rel(scratch),
            "missing": missing_files,
        }
    listed = set(report.get("paths") or [])
    unlisted = sorted(set(gate_files) - listed)
    if unlisted:
        return {
            "reason": "evidence_unlisted",
            "evidence_dir": rel(scratch),
            "unlisted": unlisted,
        }
    return None


def selection_note(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    selected = 0
    for row in rows:
        counts = row.get("junit_counts") or {}
        total = sum(int(counts.get(key) or 0) for key in ("passed", "failure", "error", "skipped"))
        if total:
            parts.append(f"{row.get('group')} {total}")
            selected += total
    return (
        "full repository selection: "
        + " + ".join(parts)
        + f" = {selected}; tests/docker 35 deselected"
    )


def regression_section(scratch: Path) -> dict[str, Any]:
    merged = evidence_records(scratch)
    if not merged["records"]:
        return missing(scratch / "test-gate-phase34.json")
    rows = regression_rows(merged["records"])
    # The raw gate summary keeps the command; the payload omits it because the
    # command embeds generation-machine absolute paths and would differ in a clone.
    for row in rows:
        row.pop("command", None)
    latest = merged["latest"] or {}
    failed = [
        {
            "group": run.get("suite"),
            "run": run.get("run"),
            "exit_code": run.get("exit_code"),
            "summary_line": run.get("summary_line"),
        }
        for run in merged["records"]
        if run.get("exit_code") not in (0, None)
    ]
    blocked: list[Any] = []
    for gate in merged["summaries"]:
        blocked.extend(gate.get("blocked_runs", []))
    out: dict[str, Any] = {
        "sources": merged["sources"],
        "rg_path": latest.get("rg_path"),
        "rg_version": latest.get("rg_version"),
        "preconditions": latest.get("preconditions"),
        "blocked_runs": blocked,
        "failed_runs": failed,
        "groups": rows,
        "selected_tests_note": selection_note(rows),
    }
    aggregate = unit_aggregate(rows)
    if aggregate is not None:
        out["unit_aggregate"] = aggregate
    return out


def candidate_section(version: str) -> dict[str, Any]:
    folder = ROOT / "configs" / "harness" / "candidates" / version
    record = load_json(folder / "candidate.json")
    if not isinstance(record, dict):
        return missing(folder / "candidate.json")
    return {
        "version": record.get("version"),
        "status": record.get("status"),
        "parent": record.get("parent"),
        "schema_version": record.get("schema_version"),
        "source_commit": record.get("source_commit"),
        "diff_base_commit": record.get("diff_base_commit"),
        "normalization_policy": record.get("normalization_policy"),
        "file_sha256": {name: sha256(folder / name) for name in CANDIDATE_FILES},
    }


def _version_number(name: str) -> int:
    match = re.search(r"-v(\d+)", name)
    return int(match.group(1)) if match else -1


def _verify_reports(scratch: Path) -> list[Path]:
    return sorted(scratch.glob("verify-v*.json"), key=lambda path: _version_number(path.name))


def candidates_section() -> dict[str, Any]:
    folder = ROOT / "configs" / "harness" / "candidates"
    versions = sorted(
        (path for path in folder.glob("phase34-candidate-v*") if path.is_dir()),
        key=lambda path: _version_number(path.name),
    )
    out: dict[str, Any] = {
        f"v{_version_number(path.name)}": candidate_section(path.name) for path in versions
    }
    active = ROOT / "configs" / "harness"
    out["active_pointer_exists"] = (active / "active.json").is_file() or (
        active / "active.next.json"
    ).is_file()
    return out


def archive_section(scratch: Path) -> dict[str, Any]:
    reports = _verify_reports(scratch)
    path = reports[-1] if reports else scratch / "verify-v11.json"
    data = load_json(path)
    if not isinstance(data, dict):
        return missing(path)
    keep = (
        "exit_code",
        "witness",
        "historical_integrity",
        "new_archive_status",
        "totals",
        "receipt",
        "historical_baseline",
    )
    out: dict[str, Any] = {key: data.get(key) for key in keep if key in data}
    baseline = out.get("historical_baseline")
    if isinstance(baseline, dict):
        baseline = dict(baseline)
        baseline["manifest_path"] = "configs/harness/candidates/historical-baseline.json"
        out["historical_baseline"] = baseline
    versions = data.get("versions")
    if isinstance(versions, list):
        out["versions_summary"] = [
            {
                "version": entry.get("version"),
                "ok": entry.get("ok"),
                "normalization_note_stale": entry.get("normalization_note_stale"),
                "failures": len(entry.get("failures", [])),
            }
            for entry in versions
        ]
    out["source"] = rel(path)
    out["source_sha256"] = sha256(path)
    return out


def provider_section(scratch: Path) -> dict[str, Any]:
    path = scratch / "provider-matrix.json"
    data = load_json(path)
    if not isinstance(data, dict):
        return missing(path)
    real = data.get("real_provider", {})
    return {
        "source": rel(path),
        "source_sha256": sha256(path),
        "real_mode": data.get("real_mode"),
        "real_requests": real.get("real_requests"),
        "real_cost_cny": real.get("real_cost_cny"),
        "cost_known": real.get("cost_known"),
        "rows": [
            {
                "row": row.get("row"),
                "status": row.get("status"),
                "real_requests": row.get("real_requests"),
                "missing": row.get("missing"),
            }
            for row in real.get("rows", [])
        ],
        "fake_totals": data.get("totals_from_junit"),
    }


def lifecycle_section(scratch: Path) -> dict[str, Any]:
    pre = scratch / "pre-fix-check.log"
    return {
        "before_fix": {
            "commit": "dba045dabd70f0e6eb7779287136944b589f2734",
            "command": "scripts/phase33_runtime_results.py --check",
            "exit_code": 1,
            "sections": ["code_commits", "head_commit"],
            "log": rel(pre),
            "log_sha256": sha256(pre),
        },
        "rule": (
            "recorded_through/recorded_head describe the evidence tree; "
            "phase_commits=base..recorded_through; code_commits is path-filtered; "
            "changes after recorded_through must be docs-only"
        ),
    }


def external_validation_section(scratch: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, name in EXTERNAL_EVIDENCE.items():
        path = scratch / name
        data = load_json(path)
        if not isinstance(data, dict):
            out[key] = missing(path)
            continue
        out[key] = {
            "status": data.get("status"),
            "source": rel(path),
            "source_sha256": sha256(path),
            "summary": data.get("summary"),
        }
    return out


def unverified_list(external: dict[str, Any], provider: dict[str, Any]) -> list[str]:
    items = list(UNVERIFIED)

    def drop(prefix: str) -> None:
        items[:] = [item for item in items if not item.startswith(prefix)]

    if str(external.get("docker", {}).get("status")) in {"passed", "verified"}:
        drop("full Docker suite")
    if str(external.get("large_repo", {}).get("status")) in {"passed", "verified"}:
        drop("large repository")
    if str(external.get("soak", {}).get("status")) in {"passed", "verified"}:
        drop("24h stability")
    if str(external.get("frontend", {}).get("status")) in {"passed", "verified"}:
        drop("frontend checks")
    rows = provider.get("rows") or []
    if provider.get("real_requests", 0) and any(
        row.get("status") == "real-verified" for row in rows
    ):
        drop("real provider usage/billing")
    return items


def build_payload(
    base: str,
    scratch: Path,
    *,
    recorded_through: str,
    recorded_head: str,
    actual_head: str,
) -> dict[str, Any]:
    resolved_base = git("rev-parse", base)
    phase, code = commits_section(resolved_base, recorded_through)
    web_diff = git("diff", "--name-only", f"{resolved_base}..{recorded_through}", "--", "web/src")
    web_files = [path for path in web_diff.splitlines() if path.strip()]
    provider = provider_section(scratch)
    external = external_validation_section(scratch)
    return {
        "phase": 34,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generator": "scripts/phase34_runtime_results.py",
        "base_commit": resolved_base,
        "recorded_through": recorded_through,
        "recorded_head": recorded_head,
        "actual_head_at_write": actual_head,
        "evidence_dir": rel(scratch),
        "evidence_manifest_sha256": sha256(scratch / MANIFEST_NAME),
        "phase_commits": phase,
        "code_commits": code,
        "final_regression": regression_section(scratch),
        "static_checks": static_section(scratch),
        "provider_matrix": provider,
        "external_validation": external,
        "candidates": candidates_section(),
        "archive_verify": archive_section(scratch),
        "runtime_lifecycle": lifecycle_section(scratch),
        "page_e2e": {
            "command": f"git diff --name-only {resolved_base}..{recorded_through} -- web/src",
            "changed_files": len(web_files),
            "files": web_files,
            "result": (
                "conditionally skipped: web/src has zero diff in this phase"
                if not web_files
                else "frontend checks required"
            ),
        },
        "unverified": unverified_list(external, provider),
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _check(written_path: Path, scratch: Path, base_override: str | None) -> int:
    def fail(reason: str, **extra: Any) -> int:
        _emit({"check": "fail", "reason": reason, **extra})
        return 1

    if not written_path.is_file():
        return fail("written_file_missing", expected=rel(written_path))
    try:
        written = json.loads(written_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail("written_file_unreadable", detail=str(exc))
    if not isinstance(written, dict):
        return fail("written_file_not_object")

    recorded = written.get("recorded_through")
    recorded_head = written.get("recorded_head")
    actual_head = written.get("actual_head_at_write")
    if not isinstance(recorded, str) or not recorded:
        return fail("recorded_through_missing")
    if not isinstance(recorded_head, str) or not recorded_head:
        return fail("recorded_head_missing")
    if not isinstance(actual_head, str) or not actual_head:
        return fail("actual_head_at_write_missing")
    if not commit_resolves(recorded):
        return fail("recorded_through_unknown", recorded_through=recorded)
    if not commit_resolves(recorded_head):
        return fail("recorded_head_unknown", recorded_head=recorded_head)
    if recorded_head != recorded:
        return fail(
            "recorded_head_mismatch",
            recorded_through=recorded,
            recorded_head=recorded_head,
        )
    if not is_ancestor(recorded, "HEAD"):
        return fail("recorded_through_not_ancestor", recorded_through=recorded)
    changed = [p for p in git("diff", "--name-only", f"{recorded}..HEAD").splitlines() if p.strip()]
    offending = [path for path in changed if not path.startswith(DOC_PATH_PREFIX)]
    if offending:
        return fail(
            "code_changed_after_evidence",
            recorded_through=recorded,
            offending=offending[:20],
            changed=changed[:50],
        )

    base = written.get("base_commit")
    if not isinstance(base, str) or not base:
        return fail("base_commit_missing")
    if not commit_resolves(base):
        return fail("base_commit_unknown", base_commit=base)
    if base_override is not None:
        if not commit_resolves(base_override):
            return fail("base_override_unknown", base_override=base_override)
        if git("rev-parse", base_override) != git("rev-parse", base):
            return fail(
                "base_override_mismatch",
                written_base=git("rev-parse", base),
                base_override=git("rev-parse", base_override),
            )
    if not is_ancestor(base, recorded):
        return fail(
            "base_not_ancestor_of_recorded_through",
            base_commit=base,
            recorded_through=recorded,
        )

    problem = _evidence_problem(scratch)
    if problem is not None:
        reason = str(problem.pop("reason"))
        return fail(reason, **problem)

    payload = build_payload(
        base,
        scratch,
        recorded_through=recorded,
        recorded_head=recorded_head,
        actual_head=actual_head,
    )
    rebuilt = normalise(payload, written.get("generated_at"))
    current = normalise(written, written.get("generated_at"))
    if rebuilt == current:
        _emit({"check": "pass", "file": rel(written_path), "sha256": sha256(written_path)})
        return 0
    pa, pb = json.loads(rebuilt), json.loads(current)
    sections = [key for key in sorted(set(pa) | set(pb)) if pa.get(key) != pb.get(key)]
    return fail("payload_mismatch", sections=sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=None,
        help="phase34 baseline commit; --write requires it unless --out records base_commit",
    )
    parser.add_argument("--scratch", default=str(DEFAULT_SCRATCH))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify the written file against recorded_through; exit 1 on mismatch",
    )
    mode.add_argument("--write", action="store_true", help="write the rebuilt payload to --out")
    args = parser.parse_args(argv)

    scratch = Path(args.scratch)
    if not scratch.is_absolute():
        scratch = ROOT / scratch
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    if args.check:
        return _check(out, scratch, args.base)

    problem = _evidence_problem(scratch)
    if problem is not None:
        reason = str(problem.pop("reason"))
        _emit({"write": "fail", "reason": reason, **problem})
        return 2

    base = args.base
    if base is None and out.is_file():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and isinstance(existing.get("base_commit"), str):
            base = existing["base_commit"]
    if not isinstance(base, str) or not base:
        _emit({"write": "fail", "reason": "base_commit_required", "hint": "pass --base <sha>"})
        return 2
    try:
        git("rev-parse", "--verify", args.base or base)
    except RuntimeError as exc:
        _emit({"write": "fail", "reason": "base_commit_unknown", "detail": str(exc)})
        return 2

    recorded = git("rev-parse", "HEAD")
    payload = build_payload(
        base,
        scratch,
        recorded_through=recorded,
        recorded_head=recorded,
        actual_head=recorded,
    )
    if out.is_file():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, dict)
            and isinstance(existing.get("generated_at"), str)
            and normalise(payload, existing["generated_at"])
            == normalise(existing, existing["generated_at"])
        ):
            payload["generated_at"] = existing["generated_at"]
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    _emit({"written": rel(out), "sha256": sha256(out)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
