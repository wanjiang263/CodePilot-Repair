"""Phase34 runtime generator lifecycle tests (evidence head, not current HEAD)."""

from __future__ import annotations

import json
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
    monkeypatch.setattr(runtime, "ROOT", repo)
    monkeypatch.setattr(p33, "ROOT", repo)
    return {
        "repo": repo,
        "base": base,
        "code": code,
        "tip": tip,
        "scratch": tmp_path / "scratch",
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
