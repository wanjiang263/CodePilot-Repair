"""Decide whether one internal M2 attempt satisfies the l2cal10e closure.

This reads facts already written by the graph; it never re-runs tests and never
looks at the official verdict. A rejected attempt must not reach the official
harness. The attempt identity is explicit: mtime is not an identity, and two
matching reports are a hard error unless the caller supplies the run id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(ROOT / "src"))

from codepilot.graph.stop_rules import patch_hash  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def _run_report(
    root: Path,
    *,
    instance: str,
    label: str,
    run_id: str | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((root / "runs").glob("*/report.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("task_id") != instance:
            continue
        report_run_id = str(payload.get("run_id") or "")
        if not report_run_id:
            continue
        if run_id is not None and report_run_id != run_id:
            continue
        root_payload = _root_report(root, report_run_id)
        run_label = root_payload.get("run_label")
        if run_label is not None and run_label != label:
            continue
        if run_label is None and f"-{label}-" not in report_run_id:
            continue
        candidates.append((path, payload))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError(
            f"ambiguous internal run report for {instance} label={label}: "
            f"{len(candidates)} candidates; pass --run-id"
        )
    return candidates[0]


def _root_report(root: Path, run_id: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in sorted((root / "reports").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("run_id") == run_id:
            matches.append(payload)
    if len(matches) > 1:
        raise ValueError(f"duplicate root reports for run_id={run_id}")
    return matches[0] if matches else {}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evaluate(root: Path, *, instance: str, label: str, run_id: str | None = None) -> dict[str, Any]:
    found = _run_report(root, instance=instance, label=label, run_id=run_id)
    if found is None:
        raise ValueError(f"no internal run report for {instance} label={label}")
    report_path, run = found
    actual_run_id = str(run.get("run_id") or "")
    root_report = _root_report(root, actual_run_id)
    closure = run.get("closure") or {}
    inference = run.get("inference") or root_report.get("inference") or {}
    reproduction = run.get("reproduction") or root_report.get("reproduction") or {}
    review = run.get("review") or {}
    attempts = run.get("attempts") or []
    last_attempt = attempts[-1] if attempts else {}
    validation = last_attempt.get("validation") or {}

    candidate = closure.get("candidate_patch_sha256")
    final = inference.get("final_patch_sha256") or run.get("patch_hash")
    reviewed = review.get("reviewed_patch_hash")
    delivered = str(run.get("git_diff") or "")
    delivered_identity = patch_hash(delivered) if delivered else None
    delivered_raw = _sha256(delivered) if delivered else None
    target_source = (
        inference.get("target_test_source")
        or last_attempt.get("target_test_source")
        or closure.get("target_test_source")
    )
    target_tests = (
        inference.get("target_tests")
        or last_attempt.get("target_tests")
        or closure.get("target_tests")
        or []
    )
    repro_status = str(reproduction.get("status") or "")
    regression = run.get("regression") or {}
    regression_mode = closure.get("regression_mode") or regression.get("mode")
    unexecuted = closure.get("regression_unexecuted_tests")
    if unexecuted is None:
        unexecuted = len(regression.get("unexecuted_tests") or [])
    review_executed = bool(closure.get("review_executed") is True and review)
    review_high = closure.get("review_high_issues")
    review_known = closure.get("review_high_issues_known", review_executed)
    checks: dict[str, bool] = {
        "real_run": bool(
            inference.get("simulated_llm") is False and inference.get("sandbox_backend") == "docker"
        ),
        "patch_present": bool(closure.get("patch_present") and final and validation.get("passed")),
        # The delivered patch must be the exact candidate that was validated and
        # tested. ``candidate`` is the normalized diff hash; ``final`` is the raw
        # byte hash used by the prediction. Comparing those two directly was a
        # permanent false negative.
        "candidate_equals_delivered_patch": bool(
            candidate and delivered_identity and candidate == delivered_identity
        ),
        "delivered_patch_matches_report": bool(final and delivered_raw and final == delivered_raw),
        "validated_patch_bound": bool(candidate and last_attempt.get("patch_hash") == candidate),
        "target_tested_patch_bound": bool(
            candidate and closure.get("target_test_sha256") == candidate
        ),
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
            and regression_mode == "full"
            and unexecuted == 0
        ),
        "review_executed": review_executed,
        # An unexecuted review has an unknown issue count, not zero issues.
        "review_no_high_issues": bool(review_executed and review_known and review_high == 0),
        "review_approved": bool(review_executed and review.get("approved") is True),
        "review_bound_to_candidate": bool(
            review_executed
            and closure.get("review_bound_to_candidate") is True
            and reviewed is not None
            and reviewed == candidate
        ),
    }
    accepted = all(checks.values())
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "instance_id": instance,
        "label": label,
        "run_id": actual_run_id,
        "run_report": str(report_path),
        "accepted": accepted,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "checks": checks,
        "evidence": {
            "candidate_patch_sha256": candidate,
            "final_patch_sha256": final,
            "delivered_patch_identity_sha256": delivered_identity,
            "delivered_patch_sha256": delivered_raw,
            "candidate_equals_delivered_patch": checks["candidate_equals_delivered_patch"],
            "reviewed_patch_hash": reviewed,
            "review_approved": review.get("approved"),
            "review_executed": review_executed,
            "target_test_source": target_source,
            "target_tests": target_tests,
            "target_test_baseline_failures": closure.get("target_test_baseline_failures") or [],
            "reproduction_status": repro_status,
            "reproduction_reproduced": repro_status == "reproduced",
            "regression_new_failures": closure.get("regression_new_failures"),
            "regression_fixed_tests": closure.get("regression_fixed_tests") or [],
            "regression_mode": regression_mode,
            "regression_unexecuted_tests": unexecuted,
            "review_high_issues": review_high if review_executed else None,
            "prediction_sha256": (root_report.get("prediction") or {}).get("predictions_sha256"),
            # Legacy field: it means "deferred at acceptance time", never the
            # latest official verdict. The explicit semantics field disambiguates.
            "official_evaluation": "DEFERRED",
            "official_evaluation_semantics": "deferred_at_acceptance",
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = evaluate(args.root, instance=args.instance, label=args.label, run_id=args.run_id)
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
