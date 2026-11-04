"""Phase34 T01: the runtime JSON records evidence through a commit, not HEAD.

The Phase33 defect: the JSON was written at C3, but --check recomputed
``base..HEAD`` and compared against it, so the docs/receipt commit C4 became an
extra ``code_commits`` entry and the check could never pass at the final HEAD.
These tests pin the corrected lifecycle on throwaway repositories:

- ``recorded_through``/``head_commit`` come from the written file;
- ``phase_commits`` = ``base..recorded_through``;
- ``code_commits`` is path-filtered (docs/archive commits never count);
- docs-only commits after the evidence keep --check green;
- code commits after the evidence fail with ``code_changed_after_evidence``;
- tampering a recorded commit or hash fails;
- two --write runs are byte-identical and --check never writes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import phase33_runtime_results as runtime


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


def store(env: dict[str, Any], payload: dict[str, Any]) -> None:
    env["out"].write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_write_records_lifecycle_and_filters_code_commits(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    assert payload["recorded_through"] == env["tip"]
    assert payload["head_commit"] == env["tip"]
    assert {item["hash"] for item in payload["phase_commits"]} == {env["code"], env["tip"]}
    # the docs commit is inside the phase window but must not be a code commit
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


def test_missing_recorded_through_fails(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    payload.pop("recorded_through")
    store(env, payload)
    assert run(env, "--check") == 1
    assert "recorded_through_missing" in capsys.readouterr().out


def test_unknown_recorded_through_fails(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    payload["recorded_through"] = "0" * 40
    store(env, payload)
    assert run(env, "--check") == 1
    assert "recorded_through_unknown" in capsys.readouterr().out


def test_tampered_code_commit_hash_fails(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    payload["code_commits"][0]["hash"] = env["tip"]
    payload["code_commits"][0]["short"] = env["tip"][:7]
    store(env, payload)
    assert run(env, "--check") == 1
    output = capsys.readouterr().out
    assert "payload_mismatch" in output
    assert "code_commits" in output


def test_tampered_head_commit_fails(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    payload = written(env)
    payload["head_commit"] = env["base"]
    store(env, payload)
    assert run(env, "--check") == 1
    assert "head_recorded_mismatch" in capsys.readouterr().out


def test_double_write_is_byte_identical(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    first = env["out"].read_bytes()
    assert run(env, "--write") == 0  # base is read back from the written file
    assert env["out"].read_bytes() == first


def test_check_does_not_modify_json(env: dict[str, Any]) -> None:
    assert run(env, "--write", "--base", env["base"]) == 0
    before = env["out"].read_bytes()
    assert run(env, "--check") == 0
    assert env["out"].read_bytes() == before
