"""Summarize executed checks and raw byte comparisons; preserve failures and skips."""

import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

directory = Path(__file__).resolve().parent
root = directory.parents[2]


def read(name):
    return json.loads((directory / name).read_text(encoding="utf-8"))


def git(*args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout.strip()


def suite(path):
    result = ET.parse(path).getroot().find("testsuite")
    assert result is not None
    cases = result.findall("testcase")
    skipped = int(result.attrib["skipped"])
    failures = int(result.attrib["failures"])
    errors = int(result.attrib["errors"])
    return cases, {"passed": len(cases) - skipped - failures - errors,
                   "failed": failures, "errors": errors, "skipped": skipped}


steps = ("red", "green", "root-red", "green-final", "targeted", "pytest-full-first", "logging-isolation", "pytest-full")
tests = {name: suite(directory / f"{name}.xml")[1] for name in steps}
for name in steps:
    tests[name]["exit_code"] = read(f"{name}.json")["exit_code"]
assert tests["red"]["failed"] == 15 and tests["red"]["passed"] == 2
assert tests["green"]["failed"] == 4 and tests["root-red"]["failed"] == 1
assert tests["green-final"]["passed"] == 18
assert tests["pytest-full-first"]["failed"] == 5 and tests["pytest-full-first"]["passed"] == 1142
assert tests["logging-isolation"]["passed"] == 16 and tests["logging-isolation"]["exit_code"] == 0
assert all(tests[name]["failed"] == tests[name]["errors"] == tests[name]["exit_code"] == 0
           for name in ("green-final", "targeted", "pytest-full"))
previous = root / "docs/evidence/phase2-retrieval-eval-20261011"
before, _ = suite(previous / "pytest-full.xml")
after, _ = suite(directory / "pytest-full.xml")
ids = lambda cases: {(case.attrib["classname"], case.attrib["name"]) for case in cases}
assert ids(before) <= ids(after)
new_ids = ids(after) - ids(before)
assert len(new_ids) == 18
for name in ("pytest-full-first", "pytest-full"):
    log = (directory / f"{name}.log").read_text(encoding="utf-8")
    tests[name]["deselected"] = int(re.search(r"(\d+) deselected", log)[1])
commands = {name: read(name + ".json") for name in
    ("ruff-check", "ruff-format", "mypy", "pytest-full", "cli-retrieve", "cli-eval",
     "failure-cli", "normal-verification", "reproduction-final")}
assert all(command["exit_code"] == 0 for command in commands.values())
normal = read("normal-comparison.json")
assert normal["report_bytes_equal"] and normal["retrieve_stdout_bytes_equal"]
failure_cli = read("failure-cli-verification.json")
assert all(c["first_exit"] == c["second_exit"] == 1 and c["stdout_equal"] and c["stderr_equal"]
           for c in failure_cli["comparisons"])
coverage = read("coverage.json")["totals"]
assert coverage["percent_covered"] >= 80
branches = {name: git("rev-parse", name) for name in
    ("codex/phase2-structural-retrieval", "codex/phase2-fusion")}
assert branches == {
    "codex/phase2-structural-retrieval": "04132b829fca2ef2ec6296092052100e0068eca8",
    "codex/phase2-fusion": "96883889868934613f3c55aea781a0a6a6d0c609",
}
sources = ["src/codepilot/retrieval/runner.py", "src/codepilot/retrieval/evaluation.py",
           "src/codepilot/cli.py", "tests/integration/test_retrieval_index_failures.py"]
summary = {
    "date": "2026-10-11", "branch": git("branch", "--show-current"),
    "validated_worktree_parent": git("rev-parse", "a097005"),
    "source_sha256": {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in sources},
    "preserved_branches": branches, "tests": tests, "new_tests": len(new_ids),
    "coverage": coverage, "first_full_coverage": read("coverage-first.json")["totals"],
    "previous_coverage": json.loads((previous / "coverage.json").read_text(encoding="utf-8"))["totals"]["percent_covered"],
    "commands": commands, "reproduction": read("reproduction-verification.json"),
    "normal_comparison": normal, "failure_cli": failure_cli,
    "p1": "resolved in validated local worktree; no remote publication performed",
    "not_run": ["real LLM / L-18", "trained semantic model", "Embedding API", "GPU",
                "product Docker Sandbox", "remote CI", "formal real-issue benchmark"],
    "scope": "one local fix(retrieval) commit; no Push, PR actions, history rewriting, or Phase 3",
}
(directory / "acceptance.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
manifest = {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "manifest.json"}
(directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"tests": tests, "new_tests": len(new_ids), "coverage": coverage["percent_covered"]}, indent=2))
