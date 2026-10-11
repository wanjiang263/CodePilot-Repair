"""V-25: independent CLI processes, real Git/index/fusion, future commit isolation."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_end_to_end_cli_determinism_certificate(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "docs/evidence/phase2-retrieval-eval-20261011/verify-determinism.py"
    run = subprocess.run(
        [sys.executable, str(script), "--output", str(tmp_path / "evidence")],
        cwd=root,
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=240,
    )
    assert run.returncode == 0, (run.stdout + run.stderr).decode("utf-8", errors="replace")
    report = json.loads((tmp_path / "evidence/determinism.json").read_text(encoding="utf-8"))
    assert report["fixture"] == "real git + real index + real retrieval/fusion"
    assert report["future_commit_unreachable"] is True
    assert report["source_status"] == ""
    assert report["gold_external"] is True
    assert report["evaluation_report_equal"] is True
    assert {item["scenario"] for item in report["comparisons"]} == {
        "weighted",
        "rrf",
        "legacy",
        "eval",
    }
    for comparison in report["comparisons"]:
        assert comparison["first_exit"] == comparison["second_exit"] == 0
        assert comparison["stdout_equal"] and comparison["stderr_equal"]
        assert comparison["first_sha256"] == comparison["second_sha256"]
    for method in ("weighted", "rrf"):
        first = (tmp_path / f"evidence/{method}-first.stdout").read_bytes()
        second = (tmp_path / f"evidence/{method}-second.stdout").read_bytes()
        assert first == second
        result = json.loads(first)
        assert result["execution"]["base_commit"] == report["base_commit"]
        assert len(result["per_channel"]) == 6 and any(f["pinned"] for f in result["files"])
        pins = [s for s in result["symbols"] if s["pinned"]]
        assert len(pins) == 2 and all(s["score"] == 0 and s["reason"] for s in pins)
        assert all(s["file"] != "future_gold.py" for s in result["fused_symbols"])
    for method in ("weighted", "rrf", "legacy", "eval"):
        assert (tmp_path / f"evidence/{method}-first.stderr").read_bytes() == b""
