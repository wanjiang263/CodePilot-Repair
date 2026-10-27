"""Run the official harness for a byte-verified legal candidate.

Candidate receipts bind provenance, instance/base, patch and prediction bytes
independently of internal acceptance. The historical accepted/recheck path
remains available for older runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT / "src"))

from codepilot.eval.harness_payload import harness_payload  # noqa: E402

_RECHECK_SCHEMA_VERSION = 2
_RECHECK_CHECKS = frozenset(
    {
        "real_run",
        "patch_present",
        "candidate_equals_delivered_patch",
        "delivered_patch_matches_report",
        "validated_patch_bound",
        "target_tested_patch_bound",
        "patch_validator",
        "reproduce_recorded",
        "target_test_executed",
        "target_test_passed",
        "target_test_present",
        "target_test_source_visible",
        "regression_clean",
        "review_executed",
        "review_no_high_issues",
        "review_approved",
        "review_bound_to_candidate",
    }
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _accepted_or_rechecked(
    root: Path,
    *,
    instance: str,
    label: str,
    run_id: str,
    acceptance: dict[str, Any],
) -> bool:
    if acceptance.get("accepted") is True and acceptance.get("run_id") == run_id:
        return True
    recheck_path = root / f"acceptance-{label}-recheck.json"
    if not recheck_path.is_file():
        return False
    try:
        recheck = json.loads(recheck_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(recheck, dict):
        return False
    evidence = acceptance.get("evidence") or {}
    checks = recheck.get("checks")
    return bool(
        recheck.get("schema_version") == _RECHECK_SCHEMA_VERSION
        and recheck.get("accepted") is True
        and recheck.get("instance_id") == instance
        and recheck.get("label") == label
        and recheck.get("run_id") == run_id
        and recheck.get("run_report") == acceptance.get("run_report")
        and recheck.get("candidate_patch_sha256") == evidence.get("candidate_patch_sha256")
        and recheck.get("final_patch_sha256") == evidence.get("final_patch_sha256")
        and recheck.get("delivered_patch_sha256") == evidence.get("delivered_patch_sha256")
        and recheck.get("reviewed_patch_hash") == evidence.get("candidate_patch_sha256")
        and isinstance(checks, dict)
        and set(checks) >= _RECHECK_CHECKS
        and all(checks.get(key) is True for key in _RECHECK_CHECKS)
    )


def _verify_prediction(
    root: Path,
    *,
    run_id: str,
    instance: str,
    prediction: dict[str, Any],
    expected_patch_sha256: str,
    expected_model: str,
) -> tuple[Path, dict[str, Any]]:
    """Bind the actual jsonl bytes to the accepted delivered patch."""
    path = Path(str(prediction.get("path") or ""))
    if not path.is_file():
        path = root / "predictions" / f"{run_id}.jsonl"
    if not path.is_file():
        raise ValueError(f"prediction file is missing for {run_id}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"prediction file escapes the attempt root: {path}")
    actual = _sha256_file(resolved)
    reported = prediction.get("predictions_sha256")
    manifest = prediction.get("manifest") or {}
    if reported and reported != actual:
        raise ValueError("prediction file sha256 does not match the run report")
    if manifest.get("predictions_sha256") != actual:
        raise ValueError("prediction file sha256 does not match its manifest")
    if manifest.get("run_id") != run_id:
        raise ValueError("prediction manifest run_id does not match the attempt")
    if manifest.get("model_name_or_path") != expected_model:
        raise ValueError("prediction manifest model does not match the internal report")

    from codepilot.eval.swebench_prediction import PredictionWriter, patch_sha256

    records = PredictionWriter.load(resolved)
    if len(records) != 1 or records[0].instance_id != instance:
        raise ValueError("prediction file must contain exactly the accepted instance")
    patch = records[0].model_patch
    if patch_sha256(patch) != expected_patch_sha256:
        raise ValueError("prediction patch bytes do not match the accepted delivered patch")
    entries = [
        item
        for item in manifest.get("entries") or []
        if isinstance(item, dict) and item.get("instance_id") == instance
    ]
    if len(entries) != 1 or entries[0].get("patch_sha256") != expected_patch_sha256:
        raise ValueError("prediction manifest entry does not match the delivered patch")
    return resolved, {
        "predictions_sha256": actual,
        "patch_sha256": expected_patch_sha256,
        "bytes": len(resolved.read_bytes()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--harness-python", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--candidate-receipt", type=Path, default=None)
    return parser


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    from codepilot.artifacts import ArtifactStore
    from codepilot.eval import swebench_harness as harness

    if getattr(args, "candidate_receipt", None) is not None:
        return evaluate_legal_candidate(args)

    from l2cal10e_acceptance import _root_report, _run_report
    from l2cal10e_acceptance import evaluate as evaluate_acceptance

    found = _run_report(args.root, instance=args.instance, label=args.label, run_id=args.run_id)
    if found is None:
        raise ValueError(f"no internal run report for {args.instance} label={args.label}")
    report_path, run = found
    run_id = str(run.get("run_id") or "")
    acceptance = evaluate_acceptance(
        args.root, instance=args.instance, label=args.label, run_id=run_id
    )
    if not _accepted_or_rechecked(
        args.root,
        instance=args.instance,
        label=args.label,
        run_id=run_id,
        acceptance=acceptance,
    ):
        raise ValueError(
            "internal closure is not ACCEPTED; the official harness is forbidden "
            f"for {args.instance} label={args.label} run_id={run_id}"
        )
    root_report = _root_report(args.root, run_id)
    prediction = root_report.get("prediction") or {}
    manifest = prediction.get("manifest") or {}
    dataset = root_report.get("dataset") or {}
    model = manifest.get("model_name_or_path") or (root_report.get("runtime") or {}).get(
        "model_identity"
    )
    if not model:
        raise ValueError("model identity is missing from the internal report")
    evidence = acceptance.get("evidence") or {}
    expected_patch = evidence.get("delivered_patch_sha256") or evidence.get("final_patch_sha256")
    if not expected_patch:
        raise ValueError("acceptance does not record a delivered patch hash")
    predictions_path, prediction_evidence = _verify_prediction(
        args.root,
        run_id=run_id,
        instance=args.instance,
        prediction=prediction,
        expected_patch_sha256=str(expected_patch),
        expected_model=str(model),
    )
    snapshot = args.snapshot_root / "test.jsonl"
    revision = str(dataset.get("revision") or "")
    if revision:
        snapshot = args.snapshot_root / f"princeton-nlp__SWE-bench_Lite__{revision}" / "test.jsonl"
    if not snapshot.is_file():
        matches = sorted(args.snapshot_root.glob(f"*__{revision}/test.jsonl"))
        if not matches:
            raise ValueError(f"dataset snapshot is missing: {snapshot}")
        snapshot = matches[0]

    config = harness.HarnessConfig(
        predictions_path=predictions_path,
        run_id=run_id,
        report_dir=args.root / "runs" / run_id / "official-report",
        max_workers=1,
        timeout_seconds=args.timeout,
        dataset_name="princeton-nlp/SWE-bench_Lite",
        split="test",
        instance_ids=(args.instance,),
        harness_python=args.harness_python,
        harness_version="4.1.0",
        model_name_or_path=str(model),
        dataset_revision=revision,
        dataset_snapshot=snapshot,
        cache_level="instance",
    )
    runner = harness.OfficialHarnessRunner(config, artifacts=ArtifactStore(args.root, run_id))
    execution = runner.run(dry_run=False)
    payload = harness_payload(execution)
    verdict = execution.verdict_for(args.instance).value
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "instance_id": args.instance,
        "label": args.label,
        "run_id": run_id,
        "run_report": str(report_path),
        "acceptance_report": str(args.root / f"acceptance-{args.label}.json"),
        "predictions_path": str(predictions_path),
        "prediction": prediction_evidence,
        "official_verdict": verdict,
        "official": payload,
    }


def evaluate_legal_candidate(args: argparse.Namespace) -> dict[str, Any]:
    from codepilot.artifacts import ArtifactStore
    from codepilot.eval import swebench_harness as harness
    from codepilot.eval.candidate import verify_candidate

    project = Path(__file__).resolve().parents[1]
    receipt = json.loads(args.candidate_receipt.read_text(encoding="utf-8"))
    if receipt["instance_id"] != args.instance or receipt["run_id"] != args.run_id:
        raise ValueError("official candidate CLI identity mismatch")
    prediction, patch_sha = verify_candidate(project, receipt)
    revision = receipt["dataset_revision"]
    snapshot = args.snapshot_root / f"princeton-nlp__SWE-bench_Lite__{revision}/test.jsonl"
    config = harness.HarnessConfig(
        predictions_path=prediction,
        run_id=args.run_id,
        report_dir=args.root / "runs" / args.run_id / "official-report",
        max_workers=1,
        timeout_seconds=args.timeout,
        dataset_name="princeton-nlp/SWE-bench_Lite",
        split="test",
        instance_ids=(args.instance,),
        harness_python=args.harness_python,
        harness_version="4.1.0",
        model_name_or_path=json.loads(prediction.read_text(encoding="utf-8"))["model_name_or_path"],
        dataset_revision=revision,
        dataset_snapshot=snapshot,
        cache_level="instance",
    )
    execution = harness.OfficialHarnessRunner(
        config, artifacts=ArtifactStore(args.root, args.run_id)
    ).run(dry_run=False)
    return {
        "schema_version": 3,
        "instance_id": args.instance,
        "run_id": args.run_id,
        "patch_sha256": patch_sha,
        "candidate_receipt": str(args.candidate_receipt),
        "internal_status": receipt["internal_status"],
        "official_verdict": execution.verdict_for(args.instance).value,
        "official": harness_payload(execution),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output or (args.root / f"official-evaluation-{args.label}.json")
    try:
        payload = evaluate(args)
    except Exception as exc:
        failure = {
            "schema_version": 2,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "instance_id": args.instance,
            "label": args.label,
            # No Harness verdict exists when this script refuses or fails.
            "status": "ERROR",
            "official_verdict": "NOT_EVALUATED",
            "error": f"{type(exc).__name__}: {exc}",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(failure, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["official_verdict"] in {"RESOLVED", "UNRESOLVED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
