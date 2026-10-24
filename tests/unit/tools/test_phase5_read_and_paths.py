from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import codepilot.tools.read_tools as read_tools
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.read_tools import read_file_tool, search_code_tool


@pytest.mark.parametrize(
    "path",
    ["../outside.py", "/tmp/outside.py", "C:\\Windows\\win.ini", "\\\\server\\share\\x"],
)
def test_path_policy_rejects_escape_forms(tmp_path: Path, path: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    policy = WorkspacePathPolicy(root)
    with pytest.raises(ValueError):
        policy.resolve(path, must_exist=True)


def test_path_policy_rejects_git_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git/config").write_text("secret", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    policy = WorkspacePathPolicy(root)
    with pytest.raises(ValueError):
        policy.resolve(".git/config", must_exist=True)
    link = root / "link.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError):
        policy.resolve("link.py", must_exist=True)


def test_read_file_is_numbered_and_capped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "code.py").write_text("\n".join(f"line {i}" for i in range(1, 502)), encoding="utf-8")
    tool = read_file_tool(WorkspacePathPolicy(root))
    result = tool.execute({"path": "code.py", "start_line": 2, "end_line": 4})
    assert result.ok
    assert result.data["content"].splitlines() == ["2 | line 2", "3 | line 3", "4 | line 4"]
    assert not tool.execute({"path": "code.py", "start_line": 1, "end_line": 401}).ok


def test_search_code_is_deterministic_and_rejects_invalid_regex(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("needle = 1\n", encoding="utf-8")
    (root / "b.py").write_text("needle = 2\n", encoding="utf-8")
    tool = search_code_tool(WorkspacePathPolicy(root))
    result = tool.execute({"pattern": "needle", "path_glob": "*.py"})
    assert result.ok
    assert [item["path"] for item in result.data["matches"]] == ["a.py", "b.py"]
    invalid = tool.execute({"pattern": "[", "regex": True})
    assert not invalid.ok
    assert invalid.error is not None and invalid.error.code == "invalid_regex"


def test_search_code_falls_back_when_ripgrep_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("needle = 1\n", encoding="utf-8")

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("rg", 15)

    monkeypatch.setattr(read_tools.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(read_tools.subprocess, "run", timeout)
    result = search_code_tool(WorkspacePathPolicy(root)).execute(
        {"pattern": "needle", "path_glob": "*.py"}
    )

    assert result.ok
    assert [item["path"] for item in result.data["matches"]] == ["a.py"]
