"""Summarize actual completed evidence, asserting gates rather than inventing PASS."""

import gzip
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[2]


def read(name):
    return json.loads((DIRECTORY / name).read_text(encoding="utf-8"))


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout.strip()


suite = ET.parse(DIRECTORY / "pytest-full.xml").getroot().find("testsuite")
assert suite is not None
tests = suite.findall("testcase")
old_suite = ET.parse(ROOT / "docs/evidence/phase2-fusion-20261011/final-pytest.xml").getroot().find("testsuite")
assert old_suite is not None
old_ids = {(test.attrib["classname"], test.attrib["name"]) for test in old_suite.findall("testcase")}
new_ids = {(test.attrib["classname"], test.attrib["name"]) for test in tests} - old_ids
log = (DIRECTORY / "pytest-full.log").read_text(encoding="utf-8")
deselected = int(re.search(r"/ (\d+) deselected", log)[1])
coverage = read("coverage.json")["totals"]
commands = {name: read(name + ".json") for name in (
    "ruff-check", "ruff-format", "mypy", "pytest-full", "cli-retrieve", "cli-eval")}
assert all(command["exit_code"] == 0 for command in commands.values())
assert int(suite.attrib["failures"]) == int(suite.attrib["errors"]) == 0

raw_path = ROOT / "artifacts/phase2/retrieval-evaluation.json"
raw = raw_path.read_bytes()
(DIRECTORY / "retrieval-evaluation.json.gz").write_bytes(gzip.compress(raw, mtime=0))
evaluation = json.loads(raw)
assert evaluation["total_tasks"] >= 10
assert [row["name"] for row in evaluation["rows"]] == ["bm25", "ripgrep", "semantic", "structural", "test", "history", "fused"]
assert all(row["status"] == "RUN" and row["failures"] == {} for row in evaluation["rows"])
determinism = read("determinism/determinism.json")
assert determinism["future_commit_unreachable"] and determinism["evaluation_report_equal"]
assert all(c["stdout_equal"] and c["stderr_equal"] for c in determinism["comparisons"])

modules = {
    "V-21": ("3394744", "tests.unit.retrieval.test_retrieval_eval"),
    "V-22": ("3773f08", "tests.unit.retrieval.test_retrieval_dataset"),
    "V-23": ("44d2a57", "tests.integration.test_cli_multichannel_retrieve"),
    "V-24": ("1f10641", "tests.integration.test_cli_retrieval_evaluation"),
    "V-25": ("5d2b763", "tests.integration.test_retrieval_determinism"),
}
summary = {
    "date": "2026-10-11", "branch": git("branch", "--show-current"),
    "base": git("rev-parse", "9688388"), "validated_implementation_commit": git("rev-parse", "a7c7e52"),
    "suggested_stacked_pr_base": "codex/phase2-fusion",
    "preserved_branches": {name: git("rev-parse", name) for name in (
        "codex/phase2-fusion", "codex/phase2-structural-retrieval")},
    "tasks": {task: {"commit": git("rev-parse", commit), "test_module": module,
                     "new_tests": sum(name == module for name, _ in new_ids)}
              for task, (commit, module) in modules.items()},
    "audit_fix_commit": git("rev-parse", "a7c7e52"),
    "pytest": {"passed": len(tests) - int(suite.attrib["skipped"]),
               "failed": int(suite.attrib["failures"]), "errors": int(suite.attrib["errors"]),
               "skipped": int(suite.attrib["skipped"]), "deselected": deselected,
               "new_tests": len(new_ids), "exit_code": commands["pytest-full"]["exit_code"]},
    "coverage": coverage,
    "quality_gates": commands,
    "recall": [{"channel": row["name"], "metrics": row["metrics"], "failures": row["failures"]}
               for row in evaluation["rows"]],
    "dataset_kind": evaluation["dataset_kind"], "embedding_description": evaluation["embedding_description"],
    "raw_report_sha256": hashlib.sha256(raw).hexdigest(),
    "determinism": determinism["comparisons"],
    "phase2_local_offline_exit": "PASS (04 Phase 2 commands, pure functions, tables and deterministic JSON)",
    "not_run": ["trained local semantic embedding model", "real Embedding API", "GPU", "product Docker Sandbox", "real LLM", "remote CI", "formal real-issue retrieval benchmark"],
    "scope": "V-21 to V-25 only; no Push, no PR actions, no Phase 3",
}
(DIRECTORY / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
manifest = {path.relative_to(DIRECTORY).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(DIRECTORY.rglob("*")) if path.is_file() and path.name != "manifest.json"}
(DIRECTORY / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"pytest": summary["pytest"], "coverage": coverage["percent_covered"],
                  "tasks": summary["tasks"]}, indent=2))
