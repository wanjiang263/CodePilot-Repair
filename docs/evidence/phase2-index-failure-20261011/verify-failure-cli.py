"""Filesystem fault injection, actual CLI/index/evaluation in independent processes."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


def fault_child():
    from codepilot.cli import app

    read = Path.read_bytes

    def deny(path):
        if path.as_posix().endswith("/workspace/src/miniauth/plugins.py"):
            raise PermissionError("P1 CLI injected non-Gold read denied")
        return read(path)

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    with patch.object(Path, "read_bytes", deny):
        app()


def verify():
    from codepilot.retrieval.evaluation import RetrievalEvaluation

    directory = Path(__file__).resolve().parent
    root = directory.parents[2]
    artifacts = root / "artifacts/phase2-index-failure-20261011/cli"
    artifacts.mkdir(parents=True, exist_ok=True)
    report_path = artifacts / "failed.json"
    repo = "tests/fixtures/repos/mini_auth"
    scenarios = {
        "legacy": ["retrieve", "--repo", repo, "verify_token"],
        "single": ["retrieve", "--repo", repo, "--issue", "verify_token"],
        "multi": ["retrieve", "--repo", repo, "--issue", "verify_token", "--channel", "bm25,ripgrep"],
        "eval": ["eval-retrieval", "--dataset", "tests/fixtures/retrieval_cases.jsonl", "--output", str(report_path)],
    }
    comparisons = []
    for name, args in scenarios.items():
        outputs = []
        for label, seed in (("first", "1"), ("second", "123")):
            command = [sys.executable, str(Path(__file__).resolve()), "--fault", "--log-level", "ERROR", *args]
            completed = subprocess.run(command, cwd=root, capture_output=True, timeout=180,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": seed,
                     "CODEPILOT_CACHE_DIR": str(artifacts / "cache"),
                     "CODEPILOT_ARTIFACTS_DIR": str(artifacts)})
            assert completed.returncode == 1, (completed.stdout + completed.stderr).decode("utf-8")
            for stream, data in (("stdout", completed.stdout), ("stderr", completed.stderr)):
                (directory / f"failure-{name}-{label}.{stream}").write_bytes(data)
            outputs.append((completed.stdout, completed.stderr,
                            report_path.read_bytes() if name == "eval" else None))
            (directory / f"failure-{name}-{label}.json").write_text(json.dumps({
                "command": command, "exit_code": completed.returncode, "pythonhashseed": seed,
                "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
            }, indent=2) + "\n", encoding="utf-8")
        assert outputs[0] == outputs[1]
        if name == "eval":
            assert outputs[0][1] == b"" and b"environment/index/unreadable" in outputs[0][0]
            raw = outputs[0][2]
            report = RetrievalEvaluation.model_validate_json(raw)
            assert report.total_tasks == 12
            assert RetrievalEvaluation.model_validate_json(report.model_dump_json()) == report
            for row in report.rows:
                assert len(row.failures) == len(row.failure_details) == 12
                assert row.results == {} and row.metrics.total_tasks == 12
                assert row.metrics.file_hits == row.metrics.function_hits == {1: 0, 5: 0, 10: 0}
                assert all(d.scan_counts["unreadable"] == 1 and d.stage == "index"
                           for d in row.failure_details.values())
            (directory / "failure-evaluation.json").write_bytes(raw)
        else:
            assert outputs[0][0] == b"" and b"src/miniauth/plugins.py" in outputs[0][1]
        comparisons.append({"scenario": name, "first_exit": 1, "second_exit": 1,
            "stdout_equal": True, "stderr_equal": True,
            "report_equal": outputs[0][2] == outputs[1][2] if name == "eval" else None})
    (directory / "failure-cli-verification.json").write_text(json.dumps({
        "mode": "Mock filesystem PermissionError; real index and CLI, no mocked retrieval results",
        "comparisons": comparisons,
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(comparisons, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fault":
        fault_child()
    else:
        verify()
