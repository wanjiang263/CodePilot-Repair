#!/usr/bin/env python3
"""Read-only final verification for the Phase 18E closure."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/mnt/c/Users/lenovo/Desktop/CodePilot-Repair")
L2E = ROOT / "artifacts/phase18/l2-cal10e"
REPORTS = ROOT / "artifacts/sync-reports"
DOCS = [
    "docs/notes/phase18-m2-environment-and-l2-cal10.md",
    "docs/project-summary.md",
    "docs/04-分阶段开发任务清单.md",
    "docs/05-精细化待办清单.md",
    "docs/notes/phase15-m3-execution-plan.md",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    registry = json.loads((L2E / "attempts.json").read_text(encoding="utf-8"))
    attempts = registry["attempts"]
    pytest_attempts = [a for a in attempts if a["instance_id"] == "pytest-dev__pytest-11148"]
    sklearn_attempts = [a for a in attempts if a["instance_id"] == "scikit-learn__scikit-learn-12471"]

    ledger_rows = []
    ledger_ok = 0
    for a in attempts:
        p = ROOT / a["ledger_path"]
        present = p.is_file()
        actual = sha256(p) if present else None
        match = present and actual == a["ledger_sha256"]
        ledger_ok += 1 if match else 0
        ledger_rows.append({
            "attempt_label": a["attempt_label"],
            "instance_id": a["instance_id"],
            "ledger_path": a["ledger_path"],
            "ledger_present": present,
            "expected_sha256": a["ledger_sha256"],
            "actual_sha256": actual,
            "match": match,
        })

    acc_path = L2E / "pytest-dev__pytest-11148-a21/acceptance-l2cal10e-a21.json"
    acc = json.loads(acc_path.read_text(encoding="utf-8"))
    report_path = L2E / "pytest-dev__pytest-11148-a21/reports/swebench-smoke-l2cal10e-a21-pytest-dev__pytest-11148-s0-6f621582.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    pytest_official_files = sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in L2E.glob("pytest-dev__pytest-11148*/**/*")
        if p.is_file() and "official" in p.name.lower()
    )

    protected_path = L2E / "preflight/protected-verification.json"
    protected = json.loads(protected_path.read_text(encoding="utf-8"))
    protected_rows = []
    protected_ok = 0
    for rel, info in protected["files"].items():
        p = ROOT / rel
        actual = sha256(p) if p.is_file() else None
        match = actual == info["expected_sha256"]
        protected_ok += 1 if match else 0
        protected_rows.append({"path": rel, "present": p.is_file(), "expected_sha256": info["expected_sha256"],
                               "actual_sha256": actual, "match": match})

    sklearn_recheck = json.loads((L2E / "scikit-learn__scikit-learn-12471-a7/acceptance-l2cal10e-a7-recheck.json").read_text(encoding="utf-8"))
    sklearn_official = json.loads((L2E / "scikit-learn__scikit-learn-12471-a7/official-evaluation-l2cal10e-a7.json").read_text(encoding="utf-8"))

    old_files = {}
    for name in ["cost-ledger-reconciliation-l2cal10e.json", "official-evaluation-summary-l2cal10e.json", "phase18e-summary.json"]:
        j = json.loads((L2E / name).read_text(encoding="utf-8"))
        old_files[name] = {"generated_at": j.get("generated_at"), "sha256": sha256(L2E / name)}

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "registry": {
            "total": len(attempts),
            "pytest": len(pytest_attempts),
            "sklearn": len(sklearn_attempts),
            "pytest_labels": [a["attempt_label"] for a in pytest_attempts],
            "sklearn_labels": [a["attempt_label"] for a in sklearn_attempts],
            "ledger_present_and_matching": ledger_ok,
            "ledger_rows": ledger_rows,
        },
        "a21": {
            "verdict": acc.get("verdict"),
            "accepted": acc.get("accepted"),
            "target_test_passed": acc.get("checks", {}).get("target_test_passed"),
            "regression_clean": acc.get("checks", {}).get("regression_clean"),
            "review_executed": acc.get("checks", {}).get("review_executed"),
            "review_bound_to_candidate": acc.get("checks", {}).get("review_bound_to_candidate"),
            "run_status": report.get("status"),
            "stop_reason": report.get("inference", {}).get("stop_reason"),
            "model_run_status": report.get("model_run_status"),
            "harness_decision": report.get("harness", {}).get("decision"),
            "harness_verdict": report.get("harness", {}).get("verdicts", [{}])[0].get("verdict"),
            "harness_report_path": report.get("harness", {}).get("report_path"),
            "counts_toward_resolved_rate": report.get("harness", {}).get("counts_toward_resolved_rate"),
        },
        "pytest_official_harness_files": pytest_official_files,
        "sklearn": {
            "recheck_verdict": sklearn_recheck.get("verdict"),
            "recheck_accepted": sklearn_recheck.get("accepted"),
            "official_verdict": sklearn_official.get("official_verdict"),
        },
        "protected": {"matched": protected_ok, "total": len(protected_rows), "all_match": protected.get("all_match"), "rows": protected_rows},
        "old_top_level_summaries": old_files,
    }
    (REPORTS / "final-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    synced = [line.strip() for line in (REPORTS / "synced-files.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
    final_paths = sorted(set(synced + DOCS))
    with (REPORTS / "final-sync-sha256.txt").open("w", encoding="utf-8", newline="\n") as fh:
        for rel in final_paths:
            p = ROOT / rel
            fh.write(f"{sha256(p)}  {rel}\n")

    print(json.dumps({
        "registry": {k: result["registry"][k] for k in ("total", "pytest", "sklearn", "ledger_present_and_matching")},
        "a21": result["a21"],
        "pytest_official_harness_files": pytest_official_files,
        "sklearn": result["sklearn"],
        "protected": {"matched": protected_ok, "total": len(protected_rows), "all_match": protected.get("all_match")},
        "final_sync_sha256_lines": len(final_paths),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
