"""Decide whether one internal M2 attempt satisfies the l2cal10e closure.

This reads facts already written by the graph; it never re-runs tests and never
looks at the official verdict. A rejected attempt must not reach the official
harness.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def _run_report(root: Path, *, instance: str, label: str) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for path in sorted((root / "runs").glob("*/report.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("task_id") != instance:
            continue
        run_id = str(payload.get("run_id") or "")
        root_payload = _root_report(root, run_id)
        run_label = root_payload.get("run_label")
        if run_label is not None and run_label != label:
            continue
        if run_label is None and f"-{label}-" not in run_id:
            continue
        candidates.append((path.stat().st_mtime_ns, path, payload))
    if not candidates:
        return None
    _, path, payload = max(candidates, key=lambda item: item[0])
    return path, payload


def _root_report(root: Path, run_id: str) -> dict[str, Any]:
    for path in sorted((root / "reports").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("run_id") == run_id:
            return payload
    return {}


def evaluate(root: Path, *, instance: str, label: str) -> dict[str, Any]:
    found = _run_report(root, instance=instance, label=label)
    if found is None:
        raise ValueError(f"no internal run report for {instance} label={label}")
    report_path, run = found
    root_report = _root_report(root, str(run.get("run_id") or ""))
    closure = run.get("closure") or {}
    inference = run.get("inference") or root_report.get("inference") or {}
    reproduction = run.get("reproduction") or root_report.get("reproduction") or {}
    review = run.get("review") or {}
    attempts = run.get("attempts") or []
    last_attempt = attempts[-1] if attempts else {}
    validation = last_attempt.get("validation") or {}

    candidate = closure.get("candidate_patch_sha256")
    final = inference.get("final_patch_sha256")
    reviewed = review.get("reviewed_patch_hash")
    target_source = inference.get("target_test_source") or last_attempt.get("target_test_source")
    target_tests = inference.get("target_tests") or last_attempt.get("target_tests") or []
    repro_status = str(reproduction.get("status") or "")
    checks: dict[str, bool] = {
        "real_run": bool(
            inference.get("simulated_llm") is False and inference.get("sandbox_backend") == "docker"
        ),
        "patch_present": bool(closure.get("patch_present") and final and validation.get("passed")),
        "patch_validator": bool(
            closure.get("validator_executed") is True
            and closure.get("validator_passed") is True
            and validation.get("passed") is True
        ),
        "reproduce_recorded": bool(
            repro_status
            and (
                repro_status == "reproduced"
                or bool(reproduction.get("fail_reason"))
                or bool(reproduction.get("kind"))
            )
        ),
        "target_test_executed": bool(closure.get("target_tests_executed") is True),
        "target_test_passed": bool(closure.get("target_tests_status") == "PASS"),
        "target_test_present": bool(target_tests),
        "target_test_source_visible": bool(target_source and target_source != "none"),
        "regression_clean": bool(
            closure.get("regression_executed") is True
            and closure.get("regression_complete") is True
            and closure.get("regression_new_failures") == 0
        ),
        "review_executed": bool(closure.get("review_executed") is True),
        "review_no_high_issues": bool(closure.get("review_high_issues") == 0),
        "review_bound_to_candidate": bool(
            closure.get("review_bound_to_candidate") is True
            and reviewed is not None
            and reviewed == candidate
        ),
    }
    accepted = all(checks.values())
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "instance_id": instance,
        "label": label,
        "run_id": run.get("run_id"),
        "run_report": str(report_path),
        "accepted": accepted,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "checks": checks,
        "evidence": {
            "candidate_patch_sha256": candidate,
            "final_patch_sha256": final,
            "candidate_equals_delivered_patch": candidate == final,
            "reviewed_patch_hash": reviewed,
            "review_approved": review.get("approved"),
            "target_test_source": target_source,
            "target_tests": target_tests,
            "reproduction_status": repro_status,
            "regression_new_failures": closure.get("regression_new_failures"),
            "review_high_issues": closure.get("review_high_issues"),
            "official_evaluation": "DEFERRED",
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = evaluate(args.root, instance=args.instance, label=args.label)
    except (OSError, ValueError) as exc:
        print(f"ERROR {exc}")
        return 2
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
