"""构造真实 git 仓库的测试工具。

刻意**不用 mock**：R 簇的每一行都在跟 git 的真实行为打交道——换行符转换、
worktree 的 HEAD 语义、bare 仓库的引用布局。用 mock 断言"我调用了
``git worktree add``"只能证明代码写了自己想写的东西，证明不了它做对了。
一个真实的 fixture 仓库每次运行只要几十毫秒。

所有仓库都在 pytest 的临时目录里创建，并且显式 ``-c`` 掉用户全局配置，
否则开发机上 ``core.autocrlf=true`` 而 CI 上是 false，同一份测试会有两种结果。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["FixtureRepo", "GitFixtureError", "init_repo"]


class GitFixtureError(RuntimeError):
    """fixture 搭建失败。测试基础设施坏掉时必须响亮地失败。"""


@dataclass(frozen=True, slots=True)
class FixtureRepo:
    """一个建好的 fixture 仓库。"""

    path: Path
    """工作副本目录（模拟"用户提供的本地路径"）。"""

    commits: list[str]
    """提交 SHA，按时间顺序。``commits[0]`` 是第一个提交。"""

    @property
    def head(self) -> str:
        return self.commits[-1]

    def write(self, relpath: str, content: str) -> Path:
        target = self.path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return target

    def read(self, relpath: str) -> str:
        return (self.path / relpath).read_text(encoding="utf-8")

    def exists(self, relpath: str) -> bool:
        return (self.path / relpath).exists()

    def git(self, *args: str) -> str:
        """在 fixture 仓库里跑一条 git 命令，返回 stdout。"""
        return _git(self.path, *args)

    def commit(self, message: str = "c") -> str:
        """把当前改动提交，返回新 SHA。"""
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


def _env() -> dict[str, str]:
    import os

    return {
        **os.environ,
        # 不依赖开发机的 git 身份配置；也不允许任何交互提示。
        "GIT_AUTHOR_NAME": "CodePilot Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "CodePilot Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        # 固定时间戳让提交 SHA 在同样的文件内容下可复现。不固定的话，
        # 每次运行 SHA 都不同，golden test 就没法写死期望值。
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise GitFixtureError(
            f"fixture git 命令失败: git {' '.join(args)}\n"
            f"  cwd: {cwd}\n  stderr: {completed.stderr.strip()}"
        )
    return completed.stdout


def init_repo(root: Path, files: dict[str, str] | None = None) -> FixtureRepo:
    """在 ``root`` 建一个含初始提交的 git 仓库。"""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    # 显式写进本地配置，让后续所有 git 调用（包括系统自己发起的）行为一致。
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "core.longpaths", "true")
    _git(root, "config", "commit.gpgsign", "false")

    repo = FixtureRepo(path=root, commits=[])
    for relpath, content in (files or {"README.md": "# fixture\n"}).items():
        repo.write(relpath, content)
    sha = repo.commit("initial")
    return FixtureRepo(path=root, commits=[sha])
