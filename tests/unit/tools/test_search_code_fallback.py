"""search_code 在 ripgrep 缺失时的回退行为（真实 GLM 实验发现）。

Planner 的 ``search_code`` 曾因宿主机没有 ``rg`` 直接返回
``dependency_missing``。工具不应把"可选二进制缺失"变成"搜索能力为零"；
这里锁住纯 Python 回退：结果形状、确定性排序、路径策略与上限都保持一致。
"""

from __future__ import annotations

from pathlib import Path

import codepilot.tools.read_tools as read_tools
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.read_tools import search_code_tool


def _no_ripgrep(monkeypatch) -> None:
    monkeypatch.setattr(read_tools.shutil, "which", lambda _name: None)


def test_python_fallback_when_ripgrep_missing(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("needle = 1\n", encoding="utf-8")
    (root / "b.py").write_text("needle = 2\n", encoding="utf-8")
    _no_ripgrep(monkeypatch)
    tool = search_code_tool(WorkspacePathPolicy(root))
    result = tool.execute({"pattern": "needle", "path_glob": "*.py"})
    assert result.ok
    assert [item["path"] for item in result.data["matches"]] == ["a.py", "b.py"]


def test_python_fallback_regex_and_denied_dirs(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("value = 42\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "secret.py").write_text("value = 42\n", encoding="utf-8")
    _no_ripgrep(monkeypatch)
    tool = search_code_tool(WorkspacePathPolicy(root))
    result = tool.execute({"pattern": r"value\s*=\s*\d+", "regex": True})
    assert result.ok
    assert [item["path"] for item in result.data["matches"]] == ["pkg/mod.py"]


def test_python_fallback_caps_and_truncates(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "many.txt").write_text("\n".join(["needle"] * 60), encoding="utf-8")
    _no_ripgrep(monkeypatch)
    tool = search_code_tool(WorkspacePathPolicy(root))
    result = tool.execute({"pattern": "needle"})
    assert result.ok
    assert len(result.data["matches"]) == 50
    assert result.data["truncated"] is True


def test_fallback_records_engine_and_reason(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("needle = 1\n", encoding="utf-8")
    _no_ripgrep(monkeypatch)
    result = search_code_tool(WorkspacePathPolicy(root)).execute({"pattern": "needle"})
    assert result.ok
    assert result.data["engine"] == "python_fallback"
    assert result.data["fallback_reason"] == "rg_missing"
    assert result.data["elapsed_ms"] >= 0
    assert result.data["max_matches"] == 50


def test_ripgrep_timeout_falls_back_with_diagnostics(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("needle = 1\n", encoding="utf-8")
    monkeypatch.setattr(read_tools.shutil, "which", lambda _name: "rg")

    def timeout(*_args, **_kwargs):
        raise read_tools.subprocess.TimeoutExpired("rg", 15)

    monkeypatch.setattr(read_tools.subprocess, "run", timeout)
    result = search_code_tool(WorkspacePathPolicy(root)).execute({"pattern": "needle"})
    assert result.ok
    assert result.data["fallback_reason"] == "rg_timeout"
    assert [item["path"] for item in result.data["matches"]] == ["a.py"]


def test_ripgrep_nonzero_exit_is_not_silently_replaced(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(read_tools.shutil, "which", lambda _name: "rg")

    class Completed:
        returncode = 2
        stdout = ""
        stderr = "regex parse error"

    monkeypatch.setattr(read_tools.subprocess, "run", lambda *_a, **_k: Completed())
    result = search_code_tool(WorkspacePathPolicy(root)).execute({"pattern": "[", "regex": True})
    # invalid regex is rejected before rg; use a valid pattern to reach the runner
    result = search_code_tool(WorkspacePathPolicy(root)).execute({"pattern": "x"})
    assert not result.ok
    assert result.error is not None and result.error.code == "search_failed"
    assert result.error.details["engine"] == "ripgrep"
    assert result.error.details["exit_code"] == 2


def test_fallback_skips_binary_and_oversized_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ok.txt").write_text("needle\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"needle\x00\x01")
    (root / "huge.txt").write_bytes(b"needle" + b"x" * (read_tools._MAX_SEARCH_FILE_BYTES))
    _no_ripgrep(monkeypatch)
    result = search_code_tool(WorkspacePathPolicy(root)).execute({"pattern": "needle"})
    assert [item["path"] for item in result.data["matches"]] == ["ok.txt"]


def test_fallback_deadline_reports_partial_results(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for index in range(20):
        (root / f"{index}.txt").write_text("needle\n", encoding="utf-8")
    _no_ripgrep(monkeypatch)
    monkeypatch.setattr(read_tools, "_SEARCH_DEADLINE_SECONDS", -1.0)
    result = search_code_tool(WorkspacePathPolicy(root)).execute({"pattern": "needle"})
    assert result.ok
    assert result.data["fallback_timed_out"] is True
    assert result.data["truncated"] is True


def test_invalid_regex_still_rejected_without_ripgrep(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _no_ripgrep(monkeypatch)
    tool = search_code_tool(WorkspacePathPolicy(root))
    result = tool.execute({"pattern": "[", "regex": True})
    assert not result.ok
    assert result.error is not None and result.error.code == "invalid_regex"
