"""Phase34 runtime generator lifecycle tests (evidence head, not current HEAD)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import phase33_runtime_results as p33
from scripts import phase34_runtime_results as runtime


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_manifest(folder: Path) -> None:
    lines = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(folder).as_posix()}")
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def seed_evidence(folder: Path) -> None:
    write(
        folder / "gate-unit-a-run1.log",
        "1 passed in 0.01s\n",
    )
    write(
        folder / "gate-unit-a-run1.xml",
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites><testsuite name="unit-a" tests="1" failures="0" errors="0" '
        'skipped="0"><testcase classname="tests.unit.test_ok" '
        'name="test_ok"/></testsuite></testsuites>\n',
    )
    gate = {
        "tool": "phase32-test-gate",
        "generated_at": "2026-11-05T00:00:00+00:00",
        "rg_path": "rg",
        "rg_version": "test",
        "preconditions": {"min_free_mb": 1},
        "blocked_runs": [],
        "failed_runs": [],
        "runs": [
            {
                "suite": "unit-a",
                "run": 1,
                "started_at": "2026-11-05T00:00:00+00:00",
                "exit_code": 0,
                "summary_line": "1 passed in 0.01s",
                "wall_s": 0.01,
                "log": "D:\\generation\\gate-unit-a-run1.log",
                "junit": "D:\\generation\\gate-unit-a-run1.xml",
                "basetemp": "D:\\generation\\pytest\\unit-a-run1-20261105T000000",
                "command": (
                    f"python -m pytest --junitxml={folder}/gate-unit-a-run1.xml "
                    f"--basetemp={folder}/pytest/unit-a-run1-20261105T000000"
                ),
                "memory_before": {"avail_phys_mb": 1},
                "memory_after": {"avail_phys_mb": 1},
            }
        ],
    }
    write(folder / "test-gate-unit-a.json", json.dumps(gate, indent=2) + "\n")
    write(
        folder / "static-checks.json",
        json.dumps({"file_count": 0, "files": [], "failed": [], "checks": []}) + "\n",
    )
    write(
        folder / "provider-matrix.json",
        json.dumps(
            {
                "real_mode": False,
                "real_provider": {
                    "real_requests": 0,
                    "real_cost_cny": 0.0,
                    "cost_known": True,
                    "rows": [],
                },
                "totals_from_junit": {
                    "tests": 1,
                    "failures": 0,
                    "errors": 0,
                    "skipped": 0,
                },
            }
        )
        + "\n",
    )
    write(
        folder / "verify-v10.json",
        json.dumps(
            {
                "exit_code": 0,
                "witness": "verified",
                "historical_integrity": "ok",
                "new_archive_status": "passed",
                "totals": {},
                "receipt": {},
                "historical_baseline": {
                    "manifest_path": "D:\\generation\\historical-baseline.json",
                    "files_checked": 0,
                    "mismatches": {},
                    "ok": True,
                },
            }
        )
        + "\n",
    )
    write(folder / "pre-fix-check.log", "before fix: exit 1\n")
    write_manifest(folder)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Phase34 fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    write(repo / "src/app.py", "value = 1\n")
    base = commit(repo, "base")
    write(repo / "src/app.py", "value = 2\n")
    code = commit(repo, "code change")
    write(repo / "docs/notes/phase.md", "evidence\n")
    tip = commit(repo, "docs change")
    scratch = repo / "docs" / "notes" / "phase34-evidence"
    seed_evidence(scratch)
    monkeypatch.setattr(runtime, "ROOT", repo)
    monkeypatch.setattr(p33, "ROOT", repo)
    return {
        "repo": repo,
        "base": base,
        "code": code,
        "tip": tip,
        "scratch": scratch,
        "out": tmp_path / "runtime.json",
    }


def run(env: dict[str, Any], mode: str, *extra: str) -> int:
    return runtime.main([mode, "--scratch", str(env["scratch"]), "--out", str(env["out"]), *extra])


def written(env: dict[str, Any]) -> dict[str, Any]:
    return json.loads(env["out"].read_text(encoding="utf-8"))


def test_write_records_lifecycle(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    assert payload["recorded_through"] == env["tip"]
    assert payload["recorded_head"] == env["tip"]
    assert payload["actual_head_at_write"] == env["tip"]
    assert {item["hash"] for item in payload["phase_commits"]} == {env["code"], env["tip"]}
    assert [item["hash"] for item in payload["code_commits"]] == [env["code"]]


def test_write_records_in_repo_evidence(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    assert payload["evidence_dir"] == "docs/notes/phase34-evidence"
    assert re.fullmatch(r"[0-9a-f]{64}", payload["evidence_manifest_sha256"])
    group = payload["final_regression"]["groups"][0]
    assert group["log"] == "docs/notes/phase34-evidence/gate-unit-a-run1.log"
    assert group["junit"] == "docs/notes/phase34-evidence/gate-unit-a-run1.xml"
    assert "/" not in group["basetemp"] and "\\" not in group["basetemp"]
    baseline = payload["archive_verify"]["historical_baseline"]
    assert baseline["manifest_path"] == "configs/harness/candidates/historical-baseline.json"
    assert "D:\\generation" not in json.dumps(payload)


def test_check_passes_at_recorded_through(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    assert run(env, "--check") == 0


def test_docs_only_commit_after_evidence_keeps_check_green(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    write(env["repo"] / "docs/notes/after.md", "later docs\n")
    commit(env["repo"], "docs after evidence")
    assert run(env, "--check") == 0


def test_code_commit_after_evidence_fails(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    write(env["repo"] / "src/app.py", "value = 3\n")
    commit(env["repo"], "code after evidence")
    assert run(env, "--check") == 1
    assert "code_changed_after_evidence" in capsys.readouterr().out


def test_tampered_recorded_head_fails(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    payload["recorded_head"] = env["base"]
    env["out"].write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    assert run(env, "--check") == 1
    assert "recorded_head_mismatch" in capsys.readouterr().out


def test_missing_manifest_is_evidence_missing_not_payload_mismatch(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    (env["scratch"] / "SHA256SUMS").unlink()
    assert run(env, "--check") == 1
    out = capsys.readouterr().out
    assert "evidence_missing" in out
    assert "payload_mismatch" not in out


def test_missing_consumed_evidence_file_is_evidence_missing(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    (env["scratch"] / "pre-fix-check.log").unlink()
    assert run(env, "--check") == 1
    out = capsys.readouterr().out
    assert "evidence_missing" in out
    assert "pre-fix-check.log" in out


def test_tampered_evidence_file_is_manifest_mismatch(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    write(env["scratch"] / "gate-unit-a-run1.log", "tampered\n")
    assert run(env, "--check") == 1
    out = capsys.readouterr().out
    assert "evidence_manifest_mismatch" in out
    assert "gate-unit-a-run1.log" in out


def test_manifest_path_traversal_is_rejected(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    manifest = env["scratch"] / "SHA256SUMS"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "0" * 64 + "  ../outside\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run(env, "--check") == 1
    assert "evidence_manifest_invalid" in capsys.readouterr().out


def test_double_write_is_byte_identical(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    first = env["out"].read_bytes()
    assert run(env, "--write") == 0
    assert env["out"].read_bytes() == first


def test_check_does_not_modify_json(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    before = env["out"].read_bytes()
    assert run(env, "--check") == 0
    assert env["out"].read_bytes() == before
