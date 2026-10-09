"""R-18：``codepilot index`` CLI 走真实路径（真 git 仓库 + 真 workspace + 真 SQLite）。

不在这里写死任何仓库绝对路径：仓库在 ``tmp_path`` 里现建，数据库与缓存也指到
``tmp_path``，测完即弃。断言的是 CLI 的**外部契约**——退出码 0 与四类计数出现，
而不是内部实现细节（那些由 ``tests/unit/repo/test_index.py`` 钉住）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codepilot.cli import app
from codepilot.config import reset_settings_cache
from support.gitfixture import init_repo

runner = CliRunner()

_REPO_FILES = {
    "src/app/__init__.py": "",
    "src/app/core.py": (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def total(n: int) -> int:\n"
        "    return add(n, 1)\n"
    ),
    "tests/test_core.py": (
        "from app.core import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"
    ),
    "README.md": "# cli fixture\n",
}


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把缓存/产物/数据库都关进 ``tmp_path``，别污染仓库根的 ``./cache`` 与默认 db。"""
    monkeypatch.setenv("CODEPILOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEPILOT_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    reset_settings_cache()
    try:
        yield
    finally:
        reset_settings_cache()


def test_index_cli_prints_four_counts(tmp_path: Path, isolated_settings: None) -> None:
    repo = init_repo(tmp_path / "repo", _REPO_FILES)
    db = tmp_path / "index.db"

    result = runner.invoke(
        app,
        [
            "index",
            str(repo.path),
            "--base-commit",
            repo.head,
            "--database-url",
            f"sqlite:///{db.as_posix()}",
        ],
    )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    for label in ("Files", "Symbols", "Edges", "Tests"):
        assert label in result.stdout
    # 索引确实落了库，而不是只打印了一份内存统计。
    assert db.exists() and db.stat().st_size > 0
    # 临时 workspace 默认清掉了：cli-workspaces 下不留残余目录。
    workspaces = list((tmp_path / "cache" / "cli-workspaces").glob("index-*"))
    assert workspaces == []


def test_index_cli_defaults_to_head_and_reports_tests(
    tmp_path: Path, isolated_settings: None
) -> None:
    """不给 --base-commit 时默认 HEAD；四类计数带非零值。"""
    repo = init_repo(tmp_path / "repo", _REPO_FILES)
    db = tmp_path / "index2.db"

    result = runner.invoke(
        app,
        ["index", str(repo.path), "--database-url", f"sqlite:///{db.as_posix()}"],
    )

    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert repo.head in result.stdout  # base commit 被解析成完整 SHA 并回显
    assert "Tests       : 1" in result.stdout


def test_index_cli_rejects_non_git_directory(tmp_path: Path, isolated_settings: None) -> None:
    """普通目录不是 git 仓库：以退出码 1 失败并解释原因，而不是崩栈或伪造成功。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["index", str(plain), "--database-url", f"sqlite:///{(tmp_path / 'x.db').as_posix()}"],
    )

    assert result.exit_code == 1
    assert "workspace" in (result.stdout + (result.stderr or "")).lower()


def test_index_cli_rejects_unknown_mode(tmp_path: Path, isolated_settings: None) -> None:
    repo = init_repo(tmp_path / "repo", _REPO_FILES)
    result = runner.invoke(
        app,
        [
            "index",
            str(repo.path),
            "--base-commit",
            repo.head,
            "--mode",
            "bogus",
            "--database-url",
            f"sqlite:///{(tmp_path / 'm.db').as_posix()}",
        ],
    )
    assert result.exit_code == 2
    assert "workspace 模式" in (result.stdout + (result.stderr or ""))
