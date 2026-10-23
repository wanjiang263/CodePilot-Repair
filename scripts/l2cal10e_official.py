"""Run the official harness only for an already-accepted internal attempt."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import swebench_smoke as smoke  # noqa: E402
from l2cal10e_acceptance import (  # noqa: E402
    _root_report,
    _run_report,
    evaluate as evaluate_acceptance,
)


def _accepted_or_rechecked(root: Path, *, instance: str, label: str, run_id: str) -> bool:
    """Official evaluation is gated by the recorded internal closure.

    The acceptance is recomputed from the run report. A human recheck artifact
    is honored only when it targets the same run, which keeps the documented
    sklearn a7 recheck valid without trusting a stale file.
    """
    acceptance = evaluate_acceptance(root, instance=instance, label=label)
    if acceptance.get("accepted") is True:
        return True
    recheck_path = root / f"acceptance-{label}-recheck.json"
    if not recheck_path.is_file():
        return False
    try:
        recheck = json.loads(recheck_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(
        isinstance(recheck, dict)
        and recheck.get("accepted") is True
        and recheck.get("run_id") == run_id
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--harness-python", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    from codepilot.artifacts import ArtifactStore
    from codepilot.eval import swebench_harness as harness

    found = _run_report(args.root, instance=args.instance, label=args.label)
    if found is None:
        raise ValueError(f"no internal run report for {args.instance} label={args.label}")
    _, run = found
    run_id = str(run.get("run_id") or "")
    accepted = _accepted_or_rechecked(
        args.root, instance=args.instance, label=args.label, run_id=run_id
    )
    if not accepted:
        raise ValueError(
            "internal closure is not ACCEPTED; the official harness is forbidden "
            f"for {args.instance} label={args.label}"
        )
    root_report = _root_report(args.root, run_id)
    prediction = root_report.get("prediction") or {}
    predictions_path = Path(str(prediction.get("path") or ""))
    if not predictions_path.is_file():
        predictions_path = args.root / "predictions" / f"{run_id}.jsonl"
    if not predictions_path.is_file():
        raise ValueError(f"prediction file is missing for {run_id}")
    manifest = prediction.get("manifest") or {}
    dataset = root_report.get("dataset") or {}
    model = manifest.get("model_name_or_path") or (root_report.get("runtime") or {}).get(
        "model_identity"
    )
    if not model:
        raise ValueError("model identity is missing from the internal report")
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
    payload = smoke._harness_payload(execution)
    verdict = execution.verdict_for(args.instance).value
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "instance_id": args.instance,
        "label": args.label,
        "run_id": run_id,
        "predictions_path": str(predictions_path),
        "official_verdict": verdict,
        "official": payload,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output or (args.root / f"official-evaluation-{args.label}.json")
    try:
        payload = evaluate(args)
    except Exception as exc:
        failure = {
            "schema_version": 1,
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
