"""Phase33 T04: build docs/notes/phase33-runtime-results.json from evidence only.

Why this script exists (D3): scripts/phase32_runtime_results.py hardcoded
phase32_harness=5e18317 (the JSON said 03ebbbf), real_requests=0,
real_cost_cny=0, page changed_files=0, docker wall_s=310.36 and used
run.get("failed", 0) which is always 0. This generator reads EVERY value from
evidence:

- commits            -> git rev-parse / git log (never hardcoded)
- regression rows    -> <scratch>/test-gate*.json (all parts merged) + JSON record lines
                          rescued from <scratch>/runs/t08-gate*.log + each run's JUnit XML
                        (failed = failure + error element counts, parsed)
- first_round_failures -> <scratch>/evidence/first-attempt/fa-<group>-run<n>.{log,xml,meta.json}
- real_requests/cost -> docs/notes/phase33-provider-matrix.json
- page changed_files -> git diff --name-only <base>..HEAD -- web/src
- docker minimal     -> runs/docker-minimal-<n>.log (summary line parsed)
- index profile      -> evidence/t05/profile-after.json (+ baseline)
- static checks      -> <scratch>/static-checks.json
- archive verify     -> <scratch>/verify-v6.json (T09)

A missing input is recorded as {"status": "missing-input", "expected": ...},
never fabricated. --check rebuilds the payload and compares with the written
file (generated_at normalised); any mismatch exits 1. Only --write writes.

Usage:
  .venv/Scripts/python.exe scripts/phase33_runtime_results.py --check
  .venv/Scripts/python.exe scripts/phase33_runtime_results.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "b4825911b8e2880adfe6177b9aca6f4c35bf15b1"
DEFAULT_SCRATCH = ROOT / ".codex-pytest-tmp" / "phase33"
DEFAULT_OUT = ROOT / "docs" / "notes" / "phase33-runtime-results.json"

PYTEST_TIME = re.compile(r"in (\d+(?:\.\d+)?)s")


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed rc={done.returncode}: {done.stderr.strip()}"
        )
    return (done.stdout or "").strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def missing(*paths: Path) -> dict[str, Any]:
    return {"status": "missing-input", "expected": [rel(p) for p in paths]}


def junit_stats(path: Path) -> dict[str, Any]:
    """failed/error/skipped/passed counts + failed node ids + first error text."""
    if not path.is_file():
        return {"available": False, "expected": rel(path)}
    tree = ET.parse(path)
    failed_nodes: list[str] = []
    error_text: str | None = None
    counts = {"passed": 0, "failure": 0, "error": 0, "skipped": 0}
    for case in tree.iter("testcase"):
        outcome = "passed"
        text = None
        for child in case:
            if child.tag in {"failure", "error", "skipped"}:
                outcome = child.tag
                text = (child.get("message") or child.text or "").strip()
                break
        counts[outcome] += 1
        if outcome in {"failure", "error"}:
            node = f"{case.get('classname', '').replace('.', '/')}::{case.get('name', '')}"
            failed_nodes.append(node)
            if error_text is None and text:
                error_text = text[:400]
    return {
        "available": True,
        "failed": counts["failure"] + counts["error"],
        "counts": counts,
        "failed_nodes": failed_nodes,
        "first_error_text": error_text,
    }


def regression_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in records:
        # gate record log/junit are display paths (relative to ROOT when inside repo)
        log_path = ROOT / run["log"] if not Path(run["log"]).is_absolute() else Path(run["log"])
        junit_path = (
            ROOT / run["junit"]
            if not run.get("junit") or not Path(run["junit"]).is_absolute()
            else Path(run["junit"])
        )
        stats = junit_stats(junit_path)
        rows.append(
            {
                "group": run["suite"],
                "run": run["run"],
                "command": run.get("command"),
                "exit_code": run["exit_code"],
                "summary_line": run.get("summary_line"),
                "wall_s": run.get("wall_s"),
                "failed": stats.get("failed"),
                "junit_counts": stats.get("counts"),
                "log": rel(log_path),
                "log_sha256": sha256(log_path),
                "junit": rel(junit_path),
                "junit_sha256": sha256(junit_path),
                "memory_before": run.get("memory_before"),
                "memory_after": run.get("memory_after"),
                "basetemp": run.get("basetemp"),
            }
        )
    return rows


def merge_gate_records(scratch: Path) -> dict[str, Any]:
    """Merge every <scratch>/test-gate*.json summary plus JSON record lines rescued
    from gate logs. A gate invocation killed before writing its summary still prints
    one record line per finished group (part1's graph run lives only in
    runs/t08-gate.log). Dedupe key: (suite, run, started_at)."""
    sources: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for path in sorted(scratch.glob("test-gate*.json")):
        gate = load_json(path)
        if not isinstance(gate, dict):
            continue
        summaries.append(gate)
        sources.append({"path": rel(path), "sha256": sha256(path), "kind": "gate-summary"})
        for run in gate.get("runs", []):
            key = (run.get("suite"), run.get("run"), run.get("started_at"))
            if key not in seen:
                seen.add(key)
                records.append(run)
    runs_dir = scratch / "runs"
    if runs_dir.is_dir():
        for log_path in sorted(runs_dir.glob("t08-gate*.log")):
            rescued: list[dict[str, Any]] = []
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith('{"suite"'):
                    continue
                try:
                    run = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(run, dict):
                    continue
                key = (run.get("suite"), run.get("run"), run.get("started_at"))
                if key in seen:
                    continue
                seen.add(key)
                rescued.append(run)
            if rescued:
                sources.append(
                    {
                        "path": rel(log_path),
                        "sha256": sha256(log_path),
                        "kind": "record-lines-rescued-from-gate-log",
                    }
                )
                records.extend(rescued)
    records.sort(key=lambda run: str(run.get("started_at", "")))
    latest: dict[str, Any] | None = None
    for gate in summaries:
        stamp = str(gate.get("generated_at", ""))
        if latest is None or stamp >= str(latest.get("generated_at", "")):
            latest = gate
    return {"sources": sources, "summaries": summaries, "records": records, "latest": latest}


def unit_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Sum the disjoint unit-* batches back into the phase32 single-'unit' figure."""
    unit_rows = [row for row in rows if str(row.get("group", "")).startswith("unit-")]
    if len(unit_rows) < 2:
        return None
    totals: dict[str, int] = {}
    wall = 0.0
    for row in unit_rows:
        for key, value in (row.get("junit_counts") or {}).items():
            totals[key] = totals.get(key, 0) + int(value or 0)
        wall += float(row.get("wall_s") or 0.0)
    return {
        "batches": sorted({str(row.get("group")) for row in unit_rows}),
        "junit_counts": totals,
        "failed": totals.get("failure", 0) + totals.get("error", 0),
        "wall_s": round(wall, 2),
    }


def first_round_failures(scratch: Path) -> dict[str, Any]:
    fa = scratch / "evidence" / "first-attempt"
    entries: list[dict[str, Any]] = []
    if fa.is_dir():
        for meta_path in sorted(fa.glob("fa-*-run*.meta.json")):
            stem = meta_path.name[: -len(".meta.json")]
            meta = load_json(meta_path) or {}
            log_path = fa / f"{stem}.log"
            xml_path = fa / f"{stem}.xml"
            stats = junit_stats(xml_path)
            group, _, run_s = stem.removeprefix("fa-").partition("-run")
            entries.append(
                {
                    "group": group,
                    "run": int(run_s) if run_s.isdigit() else None,
                    "summary_line": meta.get("summary_line"),
                    "exit_code": meta.get("exit_code"),
                    "free_mb_after": (
                        meta.get("memory_after") or meta.get("memory_after_graph") or {}
                    ).get("avail_phys_mb"),
                    "log": rel(log_path),
                    "log_sha256": sha256(log_path),
                    "junit": rel(xml_path),
                    "junit_sha256": sha256(xml_path),
                    "nodes": stats.get("failed_nodes"),
                    "error_text": stats.get("first_error_text"),
                }
            )
    return {
        "count": len(entries),
        "entries": entries,
        "note": "empty means no group failed on its first final-regression attempt",
    }


def docker_section(scratch: Path) -> dict[str, Any]:
    done_path = scratch / "runs" / "docker-minimal.done"
    logs = sorted(scratch.glob("runs/docker-minimal-*.log"))
    if not done_path.is_file() or not logs:
        return missing(done_path, scratch / "runs" / "docker-minimal-N.log")
    final_log = logs[-1]
    text = final_log.read_text(encoding="utf-8", errors="replace")
    # pytest summary is NOT necessarily the last line (tee appends EXIT=n), so
    # scan from the end for the first timing match.
    m = None
    for line in reversed(text.splitlines()):
        if line.strip():
            m = PYTEST_TIME.search(line)
            if m:
                break
    if m is None:
        # fallback: done marker records wall time, e.g. "e2e passed 1/1 EXIT=0 71.26s"
        dm = re.search(r"(\d+(?:\.\d+)?)s", done_path.read_text(encoding="utf-8"))
        if dm:
            m = dm
    passed = bool(re.search(r"\b1 passed\b", text))
    attempts = []
    for lg in logs:
        t = lg.read_text(encoding="utf-8", errors="replace")
        tail = [x for x in t.splitlines() if x.strip()][-1:]
        attempts.append(
            {"log": rel(lg), "log_sha256": sha256(lg), "last_line": tail[0] if tail else ""}
        )
    return {
        "official_probe": {
            "status": "not-run-in-phase33",
            "reason": "phase33 禁止官方 Harness 全量；phase32 probe 证据保留于 docs/notes/phase32-runtime-results.json",
        },
        "minimal_e2e": {
            "test": "tests/graph/test_minimal_e2e.py::test_fake_llm_with_real_docker_executes_controlled_fixture",
            "passed": passed,
            "wall_s": float(m.group(1)) if m else None,
            "final_log": rel(final_log),
            "final_log_sha256": sha256(final_log),
            "done_marker": done_path.read_text(encoding="utf-8").strip(),
            "attempts": attempts,
        },
    }


def provider_section() -> dict[str, Any]:
    path = ROOT / "docs" / "notes" / "phase33-provider-matrix.json"
    data = load_json(path)
    if data is None:
        return missing(path)
    real = data.get("real_provider", {})
    return {
        "source": rel(path),
        "source_sha256": sha256(path),
        "real_mode": data.get("real_mode", False),
        "real_requests": real.get("real_requests"),
        "real_cost_cny": real.get("real_cost_cny"),
        "cost_known": real.get("cost_known"),
        "budget": real.get("budget"),
        "rows": [
            {
                "row": r.get("row"),
                "status": r.get("status"),
                "missing": r.get("missing"),
                "real_requests": r.get("real_requests"),
                "cost_known": r.get("cost_known"),
            }
            for r in real.get("rows", [])
        ],
        "fake_totals": data.get("totals_from_junit"),
    }


def index_section(scratch: Path) -> dict[str, Any]:
    after = scratch / "evidence" / "t05" / "profile-after.json"
    baseline = scratch / "evidence" / "t05" / "profile-baseline.json"
    data = load_json(after)
    if data is None:
        return missing(after)
    base = load_json(baseline)

    def slim(runs: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if runs is None:
            return None
        keys = (
            "scenario",
            "association_mode",
            "fast_mode",
            "wall_index_s",
            "wall_commit_s",
            "scan_files",
            "symbol_rows",
            "edge_rows",
            "parsed",
            "content_reused",
            "parse_errors",
            "db_bytes",
            "peak_working_set_bytes",
            "child_total_wall_s",
        )
        return [{k: r.get(k) for k in keys if k in r} for r in runs]

    out: dict[str, Any] = {
        "source": rel(after),
        "source_sha256": sha256(after),
        "runs_after": slim(data.get("runs")),
    }
    for extra, key in (
        ("verdict", "verdict"),
        ("limits", "limits"),
        ("export", "export"),
        ("probe_file", "probe_file"),
    ):
        if extra in data:
            out[key] = data[extra]
    if base is not None:
        out["runs_baseline_phase32_repro"] = slim(base.get("runs"))
        out["baseline_source"] = rel(baseline)
    return out


def static_section(scratch: Path) -> dict[str, Any]:
    path = scratch / "static-checks.json"
    data = load_json(path)
    if data is None:
        return missing(path)
    return {
        "source": rel(path),
        "source_sha256": sha256(path),
        "file_count": data.get("file_count"),
        "files": data.get("files"),
        "failed": data.get("failed"),
        "checks": [
            {"name": c.get("name"), "exit_code": c.get("exit_code")} for c in data.get("checks", [])
        ],
        "docs_copy": data.get("docs_copy"),
        "docs_copy_sha256": sha256(ROOT / data["docs_copy"]) if data.get("docs_copy") else None,
    }


def archive_section(scratch: Path) -> dict[str, Any]:
    path = scratch / "verify-v6.json"
    data = load_json(path)
    if data is None:
        return missing(path)
    keep = (
        "versions",
        "new",
        "old",
        "new_failures",
        "unexplained",
        "historical_files_modified",
        "witness",
        "historical_integrity",
        "receipt",
        "expect_receipt",
    )
    out = {k: data.get(k) for k in keep if k in data}
    out["source"] = rel(path)
    out["source_sha256"] = sha256(path)
    return out


def candidates_section() -> dict[str, Any]:
    out: dict[str, Any] = {}
    v6_dir = ROOT / "configs" / "harness" / "candidates" / "phase33-candidate-v6"
    v6 = load_json(v6_dir / "candidate.json")
    if v6 is None:
        out["v6"] = missing(v6_dir / "candidate.json")
    else:
        files = {}
        for name in ("candidate.json", "candidate.diff", "snapshot.json", "sources.json"):
            files[name] = sha256(v6_dir / name)
        out["v6"] = {
            "version": v6.get("version"),
            "status": v6.get("status"),
            "parent": v6.get("parent"),
            "schema_version": v6.get("schema_version"),
            "source_commit": v6.get("source_commit"),
            "diff_base_commit": v6.get("diff_base_commit"),
            "file_sha256": files,
        }
    v5_dir = ROOT / "configs" / "harness" / "candidates" / "phase31-candidate-v5"
    v5 = load_json(v5_dir / "candidate.json")
    out["v5_unchanged"] = {
        "status": (v5 or {}).get("status"),
        "candidate_json_sha256": sha256(v5_dir / "candidate.json"),
    }
    active = ROOT / "configs" / "harness" / "candidates" / "active.json"
    out["active_pointer_exists"] = active.is_file()
    return out


def build_payload(base: str, scratch: Path) -> dict[str, Any]:
    head = git("rev-parse", "HEAD")
    log_out = git("log", "--format=%H%x09%s", f"{base}..HEAD")
    commits = []
    for line in log_out.splitlines():
        if line.strip():
            h, _, s = line.partition("\t")
            commits.append({"hash": h, "short": h[:7], "subject": s})
    web_diff = git("diff", "--name-only", f"{base}..HEAD", "--", "web/src")
    web_files = [x for x in web_diff.splitlines() if x.strip()]

    merged = merge_gate_records(scratch)
    if not merged["records"]:
        final_regression: dict[str, Any] = missing(scratch / "test-gate*.json")
    else:
        latest = merged["latest"] or {}
        records = merged["records"]
        failed = [
            {
                "group": run.get("suite"),
                "run": run.get("run"),
                "exit_code": run.get("exit_code"),
                "summary_line": run.get("summary_line"),
            }
            for run in records
            if run.get("exit_code") not in (0, None)
        ]
        blocked: list[Any] = []
        for src in merged["summaries"]:
            blocked.extend(src.get("blocked_runs", []))
        rows = regression_rows(records)
        final_regression = {
            "sources": merged["sources"],
            "rg_path": latest.get("rg_path"),
            "rg_version": latest.get("rg_version"),
            "preconditions": latest.get("preconditions"),
            "blocked_runs": blocked,
            "failed_runs": failed,
            "groups": rows,
            "unit_batches_note": (
                "unit runs as disjoint batches unit-a/b/c/d: an external process killed the single "
                "34-min unit run at ~91% (evidence/first-attempt/fa-unit-run1.meta.json); the first "
                "split missed tests/unit/tools, covered by the supplemental unit-d batch (part3). "
                "Batches are disjoint, so unit_aggregate legitimately sums them back into the "
                "phase32 single-'unit' figure"
            ),
            "overlap_note": (
                "archive 11-path group (10 files + tests/unit/test_phase31_receipt.py) overlaps unit-c; "
                "archive scope reports 131+27=158; never sum overlapping groups"
            ),
        }
        agg = unit_aggregate(rows)
        if agg is not None:
            final_regression["unit_aggregate"] = agg

    return {
        "phase": 33,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generator": "scripts/phase33_runtime_results.py",
        "base_commit": git("rev-parse", base),
        "head_commit": head,
        "code_commits": commits,
        "final_regression": final_regression,
        "first_round_failures": first_round_failures(scratch),
        "static_checks": static_section(scratch),
        "provider_matrix": provider_section(),
        "docker": docker_section(scratch),
        "index_profile": index_section(scratch),
        "candidates": candidates_section(),
        "archive_verify_v6": archive_section(scratch),
        "page_e2e": {
            "command": f"git diff --name-only {base[:7]}..HEAD -- web/src",
            "changed_files": len(web_files),
            "files": web_files,
            "result": (
                "conditionally skipped: web/src has zero diff in this phase"
                if not web_files
                else "frontend checks required (see static_checks)"
            ),
        },
        "real_model_usage": {
            "policy": "default: no real model requests, no new cost",
            "source": "docs/notes/phase33-provider-matrix.json real_provider block",
        },
    }


def normalise(payload: dict[str, Any], generated_at: str | None) -> str:
    data = dict(payload)
    if generated_at is not None:
        data["generated_at"] = generated_at
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default=DEFAULT_BASE, help="phase33 baseline commit (verified via git)"
    )
    parser.add_argument("--scratch", default=str(DEFAULT_SCRATCH))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="rebuild from evidence and compare; exit 1 on mismatch"
    )
    mode.add_argument("--write", action="store_true", help="write the rebuilt payload to --out")
    args = parser.parse_args(argv)

    scratch = Path(args.scratch)
    if not scratch.is_absolute():
        scratch = ROOT / scratch
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    git("rev-parse", "--verify", args.base)  # fail fast if base unknown
    payload = build_payload(args.base, scratch)

    if args.write:
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        out.write_text(text, encoding="utf-8", newline="\n")
        print(json.dumps({"written": rel(out), "sha256": sha256(out)}, indent=2))
        return 0

    if not out.is_file():
        print(
            json.dumps(
                {"check": "fail", "reason": "written file missing", "expected": rel(out)}, indent=2
            )
        )
        return 1
    written = json.loads(out.read_text(encoding="utf-8"))
    a = normalise(payload, written.get("generated_at"))
    b = normalise(written, written.get("generated_at"))
    if a == b:
        print(json.dumps({"check": "pass", "file": rel(out), "sha256": sha256(out)}, indent=2))
        return 0
    # report first differing top-level section for triage
    pa, pb = json.loads(a), json.loads(b)
    diffs = [k for k in sorted(set(pa) | set(pb)) if pa.get(k) != pb.get(k)]
    print(json.dumps({"check": "fail", "sections": diffs}, indent=2, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
