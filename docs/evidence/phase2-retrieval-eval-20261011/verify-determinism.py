"""Reproducible V-25 audit using real Git fixtures and separate CLI processes.

Run with the existing project Python. Outputs are never normalized or scrubbed
before byte comparison. ERROR logging is explicit in every recorded command;
human diagnostic timestamps at INFO are not part of machine-readable output.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))

from support.gitfixture import init_repo
from codepilot.repo.gitcmd import run_git
from codepilot.retrieval.input import RetrievalInput
from codepilot.retrieval.runner import indexed_repository
from codepilot.retrieval.result import validate_result
from codepilot.retrieval.aggregate import aggregate_files
from codepilot.schemas.retrieval import RetrievalResult


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    fixture = ROOT / "tests/fixtures/repos/mini_auth"
    files = {p.relative_to(fixture).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted(fixture.rglob("*")) if p.is_file()}
    repo = init_repo(output / "source", files)
    base = repo.head
    issue = ('test_verify_rejects_garbage verify_token Session.login\n'
             'File "src/miniauth/tokens.py", line 32, in verify_token\n'
             'File "src/miniauth/tokens.py", line 32, in verify_token\n'
             'File "src/miniauth/session.py", line 23, in login\n'
             'File "/third-party/library.py", line 23, in login')
    configs = {}
    for method in ("weighted", "rrf"):
        path = output / f"{method}.json"
        path.write_text(json.dumps({"channels": dict.fromkeys(
            ("bm25", "ripgrep", "semantic", "structural", "test", "history"), True),
            "method": method, "embedding": "offline-hash"}), encoding="utf-8")
        configs[method] = path
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CODEPILOT_CACHE_DIR": str(output / "cache"),
           "CODEPILOT_ARTIFACTS_DIR": str(output / "artifacts")}

    def invoke(label, args, seed):
        command = [sys.executable, "-m", "codepilot.cli", "--log-level", "ERROR", *args]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                                   env={**env, "PYTHONHASHSEED": str(seed)}, timeout=120)
        (output / f"{label}.stdout").write_bytes(completed.stdout)
        (output / f"{label}.stderr").write_bytes(completed.stderr)
        (output / f"{label}.command.json").write_text(json.dumps({"command": command,
            "exit_code": completed.returncode, "pythonhashseed": seed}, indent=2) + "\n", encoding="utf-8")
        assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
        return completed

    def retrieval_args(method):
        common = ["retrieve", "--repo", str(repo.path), "--base-commit", base]
        return [*common, issue] if method == "legacy" else [*common, "--issue", issue, "--config", str(configs[method])]

    first = {method: invoke(f"{method}-first", retrieval_args(method), 1)
             for method in ("weighted", "rrf", "legacy")}
    repo.write("future_gold.py", "def future_answer():\n    return 'GOLD_AFTER_BASE'\n")
    repo.write("src/miniauth/tokens.py", files["src/miniauth/tokens.py"] + "\n# GOLD_AFTER_BASE\n")
    future = repo.commit("future reference fix must remain invisible")
    comparisons = []
    second = {method: invoke(f"{method}-second", retrieval_args(method), 123)
              for method in ("weighted", "rrf", "legacy")}

    def compare(name, a, b):
        equal = a.stdout == b.stdout and a.stderr == b.stderr
        assert equal, f"Complete CLI bytes changed: {name}"
        comparisons.append({"scenario": name, "first_exit": a.returncode, "second_exit": b.returncode,
            "stdout_equal": a.stdout == b.stdout, "stderr_equal": a.stderr == b.stderr,
            "first_sha256": hashlib.sha256(a.stdout + a.stderr).hexdigest(),
            "second_sha256": hashlib.sha256(b.stdout + b.stderr).hexdigest()})

    for method in first:
        compare(method, first[method], second[method])
        data = json.loads(second[method].stdout)
        result = RetrievalResult.model_validate(data)
        assert RetrievalResult.model_validate_json(result.model_dump_json()) == result
        validate_result(result)
        assert all(s.file != "future_gold.py" for s in result.symbols)
        assert "GOLD_AFTER_BASE" not in result.model_dump_json()
        if method != "legacy":
            assert result.files == aggregate_files(result.fused_symbols)[:20]
            assert [s.pinned for s in result.symbols[:2]] == [True, True]
            assert all(s.score == 0 for s in result.symbols[:2])
            assert data["execution"]["base_commit"] == base
        else:
            assert result.fusion_method == "none" and result.files == []

    task = RetrievalInput(task_id="audit", repo=str(repo.path), base_commit=base, issue=issue)
    with indexed_repository(task, output / "cache") as indexed:
        inaccessible = run_git(["cat-file", "-e", future], cwd=indexed.root, check=False).returncode != 0
        assert inaccessible
        assert "future_gold.py" not in indexed.churn
        assert not any(s.path == "future_gold.py" for s in indexed.symbols)

    # External gold remains on the evaluation side; CLI indexes only source/base.
    case = json.loads((ROOT / "tests/fixtures/retrieval_cases.jsonl").read_text(encoding="utf-8").splitlines()[0])
    shutil.copyfile(ROOT / "tests/fixtures" / case["gold_patch"], output / "reference.patch")
    case.update(repo="source", base_commit=base, gold_patch="reference.patch")
    dataset = output / "cases.jsonl"
    dataset.write_text(json.dumps(case) + "\n", encoding="utf-8")
    report_path = output / "evaluation.json"
    eval_args = ["eval-retrieval", "--dataset", str(dataset), "--output", str(report_path)]
    a = invoke("eval-first", eval_args, 1)
    report_a = report_path.read_bytes()
    (output / "eval-first.json").write_bytes(report_a)
    b = invoke("eval-second", eval_args, 123)
    report_b = report_path.read_bytes()
    (output / "eval-second.json").write_bytes(report_b)
    compare("eval", a, b)
    assert report_a == report_b
    report = {"fixture": "real git + real index + real retrieval/fusion", "base_commit": base,
        "future_commit": future, "future_commit_unreachable": inaccessible,
        "source_status": repo.git("status", "--porcelain"), "gold_external": not (repo.path / "reference.patch").exists(),
        "evaluation_report_equal": report_a == report_b,
        "evaluation_sha256": hashlib.sha256(report_a).hexdigest(), "comparisons": comparisons,
        "source_manifest": {path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()}}
    (output / "determinism.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"comparisons": comparisons, "base_isolation": inaccessible}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args().output.resolve())
