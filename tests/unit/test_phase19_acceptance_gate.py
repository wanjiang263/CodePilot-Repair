"""Phase 19: identity-bound acceptance and official-gate tests (not executed)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import l2cal10e_acceptance as acceptance  # noqa: E402
import l2cal10e_official as official  # noqa: E402

from codepilot.graph.stop_rules import patch_hash  # noqa: E402

PATCH = (
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "--- a/src/pkg/core.py\n"
    "+++ b/src/pkg/core.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def normalize(value: int) -> int:\n"
    "-    return value - 1\n"
    "+    return value\n"
)
INSTANCE = "pytest-dev__pytest-11148"
LABEL = "l2cal10e-a12"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_run(tmp_path: Path, *, run_id: str = "run-a", review_approved: bool = True) -> Path:
    root = tmp_path
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    candidate = patch_hash(PATCH)
    final = _sha(PATCH)
    review = {
        "approved": review_approved,
        "reviewed_patch_hash": candidate,
        "issues": [],
        "summary": "checked",
    }
    report = {
        "run_id": run_id,
        "task_id": INSTANCE,
        "git_diff": PATCH,
        "patch_hash": final,
        "reproduction": {"status": "reproduced", "kind": "valid_assertion"},
        "attempts": [
            {
                "iteration": 1,
                "patch_hash": candidate,
                "validation": {"passed": True},
            }
        ],
        "review": review,
        "regression": {"mode": "full", "unexecuted_tests": []},
        "closure": {
            "patch_present": True,
            "candidate_patch_sha256": candidate,
            "validator_executed": True,
            "validator_passed": True,
            "target_tests_executed": True,
            "target_tests_status": "PASS",
            "target_test_sha256": candidate,
            "target_test_source": "planner",
            "target_tests": ["tests/test_core.py::test_normalize"],
            "regression_executed": True,
            "regression_complete": True,
            "regression_mode": "full",
            "regression_unexecuted_tests": 0,
            "regression_new_failures": 0,
            "review_executed": True,
            "review_approved": review_approved,
            "review_high_issues": 0,
            "review_high_issues_known": True,
            "review_bound_to_candidate": True,
        },
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "run_label": LABEL,
                "inference": {
                    "simulated_llm": False,
                    "sandbox_backend": "docker",
                    "final_patch_sha256": final,
                },
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_candidate_and_delivered_hashes_are_compared_in_the_same_normal_form(
    tmp_path: Path,
) -> None:
    _write_run(tmp_path)
    payload = acceptance.evaluate(tmp_path, instance=INSTANCE, label=LABEL, run_id="run-a")

    assert payload["accepted"] is True, payload["checks"]
    assert payload["checks"]["candidate_equals_delivered_patch"] is True
    assert payload["checks"]["delivered_patch_matches_report"] is True
    assert payload["checks"]["validated_patch_bound"] is True
    assert payload["checks"]["target_tested_patch_bound"] is True


def test_unexecuted_review_has_unknown_high_issue_count(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    report["review"] = None
    report["closure"].update(
        review_executed=False,
        review_approved=None,
        review_high_issues=0,
        review_high_issues_known=False,
        review_bound_to_candidate=False,
    )
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

    payload = acceptance.evaluate(tmp_path, instance=INSTANCE, label=LABEL, run_id="run-a")

    assert payload["accepted"] is False
    assert payload["checks"]["review_no_high_issues"] is False
    assert payload["evidence"]["review_high_issues"] is None


def test_review_of_a_different_patch_is_rejected(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path)
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    report["review"]["reviewed_patch_hash"] = "f" * 64
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

    payload = acceptance.evaluate(tmp_path, instance=INSTANCE, label=LABEL, run_id="run-a")

    assert payload["accepted"] is False
    assert payload["checks"]["review_bound_to_candidate"] is False


def test_ambiguous_reports_require_an_explicit_run_id(tmp_path: Path) -> None:
    _write_run(tmp_path, run_id="run-a")
    _write_run(tmp_path, run_id="run-b")

    with pytest.raises(ValueError, match="ambiguous"):
        acceptance.evaluate(tmp_path, instance=INSTANCE, label=LABEL)

    payload = acceptance.evaluate(tmp_path, instance=INSTANCE, label=LABEL, run_id="run-b")
    assert payload["run_id"] == "run-b"


def test_recheck_must_be_schema_two_and_bind_the_same_hashes(tmp_path: Path) -> None:
    _write_run(tmp_path, review_approved=False)
    rejected = acceptance.evaluate(tmp_path, instance=INSTANCE, label=LABEL, run_id="run-a")
    assert rejected["accepted"] is False
    evidence = rejected["evidence"]
    checks = dict(rejected["checks"])
    checks["review_approved"] = True
    recheck = {
        "schema_version": 2,
        "accepted": True,
        "instance_id": INSTANCE,
        "label": LABEL,
        "run_id": "run-a",
        "run_report": rejected["run_report"],
        "candidate_patch_sha256": evidence["candidate_patch_sha256"],
        "final_patch_sha256": evidence["final_patch_sha256"],
        "delivered_patch_sha256": evidence["delivered_patch_sha256"],
        "reviewed_patch_hash": evidence["candidate_patch_sha256"],
        "checks": checks,
    }
    path = tmp_path / f"acceptance-{LABEL}-recheck.json"
    path.write_text(json.dumps(recheck), encoding="utf-8")

    assert official._accepted_or_rechecked(
        tmp_path,
        instance=INSTANCE,
        label=LABEL,
        run_id="run-a",
        acceptance=rejected,
    )
    recheck["candidate_patch_sha256"] = "0" * 64
    path.write_text(json.dumps(recheck), encoding="utf-8")
    assert not official._accepted_or_rechecked(
        tmp_path,
        instance=INSTANCE,
        label=LABEL,
        run_id="run-a",
        acceptance=rejected,
    )


def test_bare_same_run_recheck_is_not_a_bypass(tmp_path: Path) -> None:
    _write_run(tmp_path, review_approved=False)
    rejected = acceptance.evaluate(tmp_path, instance=INSTANCE, label=LABEL, run_id="run-a")
    path = tmp_path / f"acceptance-{LABEL}-recheck.json"
    path.write_text(
        json.dumps({"schema_version": 1, "accepted": True, "run_id": "run-a"}),
        encoding="utf-8",
    )
    assert not official._accepted_or_rechecked(
        tmp_path,
        instance=INSTANCE,
        label=LABEL,
        run_id="run-a",
        acceptance=rejected,
    )


def test_official_gate_rejects_a_prediction_with_different_patch_bytes(
    tmp_path: Path,
) -> None:
    prediction_path = tmp_path / "predictions" / "run-a.jsonl"
    prediction_path.parent.mkdir(parents=True)
    prediction_path.write_text(
        json.dumps(
            {
                "instance_id": INSTANCE,
                "model_name_or_path": "glm-5.3-flash-main",
                "model_patch": PATCH + "\n# tampered\n",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    raw = hashlib.sha256(prediction_path.read_bytes()).hexdigest()
    prediction = {
        "path": str(prediction_path),
        "predictions_sha256": raw,
        "manifest": {
            "run_id": "run-a",
            "model_name_or_path": "glm-5.3-flash-main",
            "predictions_sha256": raw,
            "entries": [
                {
                    "instance_id": INSTANCE,
                    "patch_sha256": _sha(PATCH + "\n# tampered\n"),
                }
            ],
        },
    }
    with pytest.raises(ValueError, match="prediction patch bytes"):
        official._verify_prediction(
            tmp_path,
            run_id="run-a",
            instance=INSTANCE,
            prediction=prediction,
            expected_patch_sha256=_sha(PATCH),
            expected_model="glm-5.3-flash-main",
        )
