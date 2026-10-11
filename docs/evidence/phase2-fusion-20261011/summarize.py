"""Summarize actual JUnit, exit codes and coverage; hash the retained evidence."""

import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

directory = Path(__file__).resolve().parent
root = directory.parents[2]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, encoding="utf-8").strip()


def check(label):
    record = read_json(directory / f"{label}.json")
    xml = directory / f"{label}.xml"
    if xml.exists():
        suites = ElementTree.parse(xml).getroot().findall("testsuite")
        totals = {key: sum(int(s.get(key, "0")) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
        totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
        matches = re.findall(r"(\d+) deselected", (directory / f"{label}.log").read_text(encoding="utf-8"))
        totals["deselected"] = int(matches[-1]) if matches else 0
        record["pytest"] = totals
        record["duration_seconds"] = sum(float(s.get("time", "0")) for s in suites)
    return record


labels = ["v14-red", "v14", "v14-green", "v15-red", "v15", "v16-red", "v16", "v17-red", "v17", "v18-red", "v18", "v19-red", "v19", "v20-red", "v20-first", "v20-k-red", "v20-options-red", "v20", "v20-final", "ruff", "format", "mypy", "full-pytest", "final-ruff", "final-format", "final-mypy", "final-pytest"]
checks = {label: check(label) for label in labels}
assert all(checks[label]["exit_code"] == 0 for label in ("final-ruff", "final-format", "final-mypy", "final-pytest"))
cases = ElementTree.parse(directory / "final-pytest.xml").getroot().findall("testsuite/testcase")
modules = Counter(case.get("classname") for case in cases)
tasks = {
    "V-14": ("3af52b7", ["tests.unit.retrieval.test_channel_config"]),
    "V-15": ("327c16d", ["tests.unit.retrieval.test_normalization"]),
    "V-16": ("126df8d", ["tests.unit.retrieval.test_weighted_fusion"]),
    "V-17": ("4cff042", ["tests.unit.retrieval.test_rrf"]),
    "V-18": ("7d3a0f1", ["tests.unit.retrieval.test_pinning"]),
    "V-19": ("9d43fc8", ["tests.unit.retrieval.test_aggregate"]),
    "V-20": ("652028a", ["tests.unit.retrieval.test_result", "tests.integration.test_retrieval_fusion"]),
}
task_records = {task: {"commit": git("rev-parse", sha), "test_modules": names, "new_tests": sum(modules[name] for name in names)} for task, (sha, names) in tasks.items()}
coverage = read_json(directory / "final-coverage.json")
module_coverage = {
    name.replace("\\", "/"): data["summary"] for name, data in coverage["files"].items()
    if name.replace("\\", "/").endswith(("retrieval/config.py", "retrieval/fusion.py", "retrieval/aggregate.py", "retrieval/result.py", "retrieval/service.py"))
}
frozen = ["src/codepilot/cli.py", "src/codepilot/repo/graph.py", "src/codepilot/retrieval/lexical.py", "src/codepilot/retrieval/semantic.py", "src/codepilot/retrieval/structural.py", "src/codepilot/retrieval/test_proximity.py", "src/codepilot/retrieval/history.py"]
frozen_changes = git("diff", "04132b8", "--name-only", "--", *frozen).splitlines()
assert frozen_changes == [], frozen_changes
original_head = git("rev-parse", "codex/phase2-structural-retrieval")
assert original_head == git("rev-parse", "04132b8")
gate_completion = {name: datetime.fromtimestamp((directory / f"final-{name}.json").stat().st_mtime).isoformat() for name in ("ruff", "format", "mypy")}
pytest_start = ElementTree.parse(directory / "final-pytest.xml").getroot().find("testsuite").get("timestamp")
acceptance = {
    "date": "2026-10-11", "branch": git("branch", "--show-current"),
    "base": original_head, "implementation_commit": git("rev-parse", "652028a"),
    "suggested_stacked_pr_base": "codex/phase2-structural-retrieval",
    "original_branch_head": original_head, "frozen_file_changes": frozen_changes,
    "tasks": task_records, "new_tests": sum(row["new_tests"] for row in task_records.values()),
    "checks": checks, "coverage": coverage["totals"], "module_coverage": module_coverage,
    "gate_completion_local": gate_completion, "pytest_suite_timestamp": pytest_start,
    "environment": {"python": platform.python_version(), "platform": platform.platform(), "executable": sys.executable, "versions": {name: importlib.metadata.version(name) for name in ("pydantic", "numpy", "pytest", "ruff", "mypy")}},
    "not_run": {"docker": "scope does not touch sandbox; offline marker excludes docker", "real_embedding_model": "not configured; actual pytest skip retained", "real_embedding_api": "offline fixtures only", "real_llm_and_L18": "outside scope; no paid calls", "linux_ci": "no push", "recall_at_k": "V-21/V-22 pending", "multichannel_cli_and_phase2_acceptance": "V-23/V-24/V-25 pending"},
    "phase2_complete": False, "push_or_pr_mutation": False,
}
(directory / "acceptance.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(directory.iterdir()) if path.is_file() and path.name != "manifest.json"}
(directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"new_tests": acceptance["new_tests"], "pytest": checks["final-pytest"], "coverage": coverage["totals"], "module_coverage": module_coverage, "gate_completion": gate_completion, "pytest_start": pytest_start}, ensure_ascii=False, indent=2))
