"""Replay the unchanged P1 reproducer; its old zero-failure assertion must fail."""

import hashlib
import json
import runpy
import traceback
from pathlib import Path
from unittest.mock import patch

from codepilot.retrieval import evaluation

directory = Path(__file__).resolve().parent
root = directory.parents[2]
original = root / "artifacts/phase2-final-review-20261011-a097005"
script = original / "reproduce-index-diagnostics.py"
before = (original / "file-read-failure-reproduction.json").read_bytes()
(directory / "reproduction-before.json").write_bytes(before)
script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
reports = []
evaluate = evaluation.run_evaluation


def capture(*args, **kwargs):
    report = evaluate(*args, **kwargs)
    reports.append(report)
    return report


with patch.object(evaluation, "run_evaluation", capture):
    try:
        runpy.run_path(str(script), run_name="__main__")
    except AssertionError as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        assert frame.line == "assert all(not row.failures for row in report.rows)"
    else:
        raise AssertionError("The original P1 zero-failure assertion still holds")

assert len(reports) == 1
report = reports[0]
for row in report.rows:
    assert len(row.failures) == len(row.failure_details) == 1
    assert row.results == {} and row.execution == {}
    detail = next(iter(row.failure_details.values()))
    assert detail.code == "unreadable" and detail.stage == "index"
    assert detail.paths == ["src/miniauth/plugins.py"]
    assert detail.count == detail.scan_counts["unreadable"] == 1
    assert detail.scan_counts["files"] == 13
    assert row.metrics.total_tasks == 1
    assert row.metrics.file_hits == row.metrics.function_hits == {1: 0, 5: 0, 10: 0}
assert hashlib.sha256(script.read_bytes()).hexdigest() == script_hash
assert (original / "file-read-failure-reproduction.json").read_bytes() == before
(directory / "reproduction-after.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
summary = {
    "mode": "Original Mock PermissionError injection; real scanner/index/evaluation, no fake candidates",
    "original_script": str(script.relative_to(root)),
    "original_script_sha256": script_hash,
    "original_script_unchanged": True,
    "old_zero_failure_assertion_failed_as_expected": True,
    "original_before_evidence_unchanged": True,
    "row_failures": {row.name: len(row.failures) for row in report.rows},
    "row_result_count": {row.name: len(row.results) for row in report.rows},
    "scan_counts": next(iter(report.rows[0].failure_details.values())).scan_counts,
    "denominator": report.total_tasks,
    "verifier_exit_code": 0,
}
(directory / "reproduction-verification.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, indent=2))
