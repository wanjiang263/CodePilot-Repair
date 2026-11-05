"""Phase34 runtime evidence: lifecycle-checked JSON for the v8 closeout.

Same evidence-head semantics as the Phase33 generator: the JSON describes the
tree at ``recorded_through``, not whatever HEAD is now. ``phase_commits`` covers
``base..recorded_through``; ``code_commits`` is path-filtered. ``--check`` reads
the recorded commits, allows only ``docs/`` changes after them, and rebuilds the
payload at the recorded tree. ``recorded_head`` is the evidence head;
``actual_head_at_write`` records the HEAD at generation time explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
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

DEFAULT_SCRATCH = ROOT / ".codex-pytest-tmp" / "phase34"
DEFAULT_OUT = ROOT / "docs" / "notes" / "phase34-runtime-results.json"
CANDIDATE_FILES = ("candidate.json", "candidate.diff", "snapshot.json", "sources.json")

UNVERIFIED = [
    "real provider streaming/usage/billing (0 requests, 0 cost; rows blocked)",
    "full Docker suite (not run in Phase34)",
    "large repository (>=20k files)",
    "24h stability",
    "frontend checks (web/src zero diff; conditionally skipped)",
    "src/codepilot/eval/swebench_runner.py:423 build-context risk (P2, untouched)",
]


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


def regression_section(scratch: Path) -> dict[str, Any]:
    merged = merge_gate_records(scratch)
    if not merged["records"]:
        return missing(scratch / "test-gate-phase34.json")
    rows = regression_rows(merged["records"])
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
        "selected_tests_note": (
            "full repository selection: unit 3120 (3118 passed + 2 skipped) + "
            "eval 483 + sandbox 66 + integration 126 + api 118 + graph 266 = 4179; "
            "tests/docker 35 deselected"
        ),
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


def archive_section(scratch: Path) -> dict[str, Any]:
    path = scratch / "verify-v9.json"
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
    active = ROOT / "configs" / "harness"
    return {
        "phase": 34,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generator": "scripts/phase34_runtime_results.py",
        "base_commit": resolved_base,
        "recorded_through": recorded_through,
        "recorded_head": recorded_head,
        "actual_head_at_write": actual_head,
        "phase_commits": phase,
        "code_commits": code,
        "final_regression": regression_section(scratch),
        "static_checks": static_section(scratch),
        "provider_matrix": provider_section(scratch),
        "candidates": {
            "v7": candidate_section("phase34-candidate-v7"),
            "v8": candidate_section("phase34-candidate-v8"),
            "v9": candidate_section("phase34-candidate-v9"),
            "active_pointer_exists": (active / "active.json").is_file()
            or (active / "active.next.json").is_file(),
        },
        "archive_verify_v8": archive_section(scratch),
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
        "unverified": UNVERIFIED,
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
