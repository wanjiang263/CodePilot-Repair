"""R-01 的基础设施：git 命令封装。

最重要的是**超时真的是超时**。这一条曾经是假的：``subprocess.run`` 在超时
之后只杀直接子进程，然后再去把 stdout/stderr 读到 EOF，而继承了这两个管道的
孙进程会一直握着它们。``git clone`` 恰好就是这种形状——它派生
``git-remote-https`` 或 ``ssh``。于是"300 秒超时"在挂住的克隆上会变成
"等到 ssh 自己放弃"，而墙上时间预算完全指望不上。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from codepilot.repo.gitcmd import GitError, run_git
from support.gitfixture import init_repo


@pytest.fixture
def spawn_instead_of_git(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把 ``run_git`` 要执行的 git 换成一段 python 脚本，其余一字不改。

    只拦 **git 那条命令**，别的 ``Popen`` 原样放行——尤其不能碰
    ``_terminate_tree`` 里的 ``taskkill``。第一版就是拦得太宽，把 taskkill
    也换成了同一个脚本，于是超时路径里又派生出一棵挂着 25 秒的进程树，
    测试自己制造了失败，看上去却像是产品代码没杀掉孙进程。
    """
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    real_popen = subprocess.Popen

    def fake_popen(command: list[str], **kwargs: object) -> object:
        if command[:1] == ["git"]:
            return real_popen([sys.executable, "-c", script], **kwargs)  # type: ignore[arg-type]
        return real_popen(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    yield


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def started_process_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Handshake ensures both processes exist before the timeout clock starts."""
    handshake = tmp_path / "grandchild.pid"
    grandchild = f"import os,pathlib,time; pathlib.Path({str(handshake)!r}).write_text(str(os.getpid())); time.sleep(60)"
    script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(60)"
    real_popen = subprocess.Popen
    pids = []

    def fake_popen(command, **kwargs):
        if command[:1] != ["git"]:
            return real_popen(command, **kwargs)
        process = real_popen([sys.executable, "-c", script], **kwargs)
        pids.append(process.pid)
        deadline = time.monotonic() + 20
        while not handshake.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert handshake.exists(), "process startup handshake timed out"
        pids.append(int(handshake.read_text()))
        assert all(_pid_alive(pid) for pid in pids)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    yield pids
    for pid in reversed(pids):
        if _pid_alive(pid):
            os.kill(pid, signal.SIGTERM)


# --------------------------------------------------------------- 基本


def test_successful_command_returns_stdout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    result = run_git(["rev-parse", "HEAD"], cwd=repo.path)
    assert result.returncode == 0
    assert result.stdout.strip() == repo.head


def test_lines_drops_blank_lines(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    result = run_git(["branch", "--format=%(refname)"], cwd=repo.path)
    assert result.lines == ["refs/heads/main"]


def test_failure_raises_with_stderr_attached(tmp_path: Path) -> None:
    """git 的报错信息是排查的唯一线索，不能被压成一句"命令失败"。"""
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    with pytest.raises(GitError) as info:
        run_git(["rev-parse", "deadbeefdeadbeef"], cwd=repo.path)
    assert info.value.returncode != 0
    assert "deadbeef" in info.value.stderr or info.value.stderr.strip()
    assert "命令名" not in info.value.render()  # render() 不该崩
    assert "git rev-parse" in info.value.render()


def test_check_false_returns_the_failure(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    result = run_git(["rev-parse", "deadbeefdeadbeef"], cwd=repo.path, check=False)
    assert result.returncode != 0


def test_missing_executable_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(GitError, match="找不到 git 可执行文件"):
        run_git(["status"])


def test_stdin_input_is_passed_through(tmp_path: Path) -> None:
    """``check-ignore --stdin`` 需要它，否则 scanner 要逐个路径起一个进程。"""
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n", ".gitignore": "*.log\n"})
    (repo.path / "b.log").write_text("noise\n", encoding="utf-8")

    result = run_git(
        ["check-ignore", "--stdin", "-z"],
        cwd=repo.path,
        input="a.py\0b.log\0",
        errors="surrogateescape",
        check=False,
    )
    assert result.returncode == 0
    assert [p for p in result.stdout.split("\0") if p] == ["b.log"]


def test_forced_config_overrides_local_config(tmp_path: Path) -> None:
    """``-c`` 的优先级高于仓库配置。换行符必须钉死，否则同一份 patch
    在开发机与 CI 上字节不同。"""
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    repo.git("config", "core.autocrlf", "true")
    result = run_git(["config", "core.autocrlf"], cwd=repo.path)
    assert result.stdout.strip() == "false"


def test_interactive_prompts_are_disabled(tmp_path: Path) -> None:
    """无人值守的 benchmark 里，一个等待输入的 git 会挂到墙钟超时，
    而且日志里什么都看不到。"""
    repo = init_repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    result = run_git(
        ["-c", "alias.probe=!echo $GIT_TERMINAL_PROMPT/$GIT_ASKPASS/$GCM_INTERACTIVE", "probe"],
        cwd=repo.path,
    )
    assert result.stdout.strip() == "0//never"


def test_git_error_render_includes_command_and_stderr() -> None:
    error = GitError(
        "git 命令失败（退出码 128）",
        args=["status"],
        returncode=128,
        stderr="fatal: not a git repository\n",
    )
    rendered = error.render()
    assert "退出码 128" in rendered
    assert "git status" in rendered
    assert "not a git repository" in rendered


# --------------------------------------------------------------- 超时


def test_timeout_returns_within_the_timeout_plus_a_small_grace(
    tmp_path: Path, spawn_instead_of_git: None
) -> None:
    """超时必须给出**确定的返回时间**，哪怕有孙进程握着管道。

    用一段真的会派生孙进程的脚本来验证，而不是 mock 掉整条 ``subprocess``：
    出问题的正是真实的管道继承行为，mock 掉之后这条测试只能证明"我调用了
    自己打算调用的命令"。

    打桩之前实测：``timeout=2`` 实际 **8.2 秒**才抛 ``TimeoutExpired``，
    多出来的时间全花在等孙进程关闭它继承的管道上。

    阈值取得比 grace（5 秒）紧：进程树杀干净时管道立刻关闭，第二次
    ``communicate`` 马上就返回，总耗时约等于 ``timeout``；只杀直接子进程时
    要白白耗满 grace。两者相差 5 秒，3.5 秒这条线落在中间。
    """
    started = time.monotonic()
    with pytest.raises(GitError, match="已终止"):
        run_git(["whatever"], cwd=tmp_path, timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 3.5, (
        f"超时后等了 {elapsed:.1f} 秒才返回，而 timeout 只有 1 秒。"
        "多出来的时间是在等一个继承了管道的孙进程——说明进程树没有被杀干净。"
    )


@pytest.mark.parametrize("load", [0, 2])
def test_timeout_kills_the_grandchild_not_just_the_child(
    tmp_path: Path, started_process_tree, load
) -> None:
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; end=time.monotonic()+30; exec('while time.monotonic()<end: pass')",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(load)
    ]
    try:
        with pytest.raises(GitError):
            run_git(["x"], cwd=tmp_path, timeout=1.0)
        deadline = time.monotonic() + 10
        while any(_pid_alive(pid) for pid in started_process_tree) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(started_process_tree) == 2
        assert not any(_pid_alive(pid) for pid in started_process_tree), (
            "child/grandchild remains alive"
        )
    finally:
        for worker in workers:
            worker.kill()
            worker.wait(timeout=10)


def test_process_tree_probe_detects_surviving_grandchild(
    tmp_path, started_process_tree, monkeypatch
):
    """Negative control: deliberately kill only the direct child."""
    monkeypatch.setattr("codepilot.repo.gitcmd._terminate_tree", lambda process: process.kill())
    with pytest.raises(GitError):
        run_git(["x"], cwd=tmp_path, timeout=1.0)
    assert len(started_process_tree) == 2
    assert _pid_alive(started_process_tree[1]), "negative control did not leave a grandchild"


def test_timeout_message_names_the_command(tmp_path: Path, spawn_instead_of_git: None) -> None:
    with pytest.raises(GitError) as info:
        run_git(["clone", "https://example.invalid/a/b"], cwd=tmp_path, timeout=1.0)
    assert "clone" in str(info.value)
    assert "超过 1 秒未返回" in str(info.value)


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows 的 taskkill 路径")
def test_taskkill_call_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawn_instead_of_git: None
) -> None:
    """``taskkill`` 是在一棵已经出问题的进程树上跑的，它自己也可能卡住。

    超时处理路径里放一个无上界的阻塞调用，等于把"超时"这两个字取消了。
    ``timeout=`` 是 ``subprocess.run`` 自己实现的，所以这里替换掉它、并且
    **故意不替它实现超时**：如果 ``run_git`` 没传 ``timeout``，真实世界里
    那次调用就会一直等下去。

    这里不去真的睡满，因为那样一旦回归就是挂 300 秒被 pytest 掐死，看不出
    是哪一条。直接断言参数，失败信息更直接。
    """
    real_run = subprocess.run
    taskkill_calls: list[dict[str, object]] = []

    def wedged_taskkill(command: list[str], **kwargs: object) -> object:
        taskkill_calls.append({"command": list(command), **kwargs})
        # 模拟：卡了一会儿之后内核把它了结掉。
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout") or 0)

    def guarded_run(command: list[str], **kwargs: object) -> object:
        if command[:1] == ["taskkill"]:
            return wedged_taskkill(command, **kwargs)
        return real_run(command, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", guarded_run)

    with pytest.raises(GitError):
        run_git(["x"], cwd=tmp_path, timeout=1.0)

    assert taskkill_calls, "没有走到 taskkill——这条测试就没在测它想测的东西"
    passed = taskkill_calls[0].get("timeout")
    assert isinstance(passed, int | float), (
        "调用 taskkill 时没有传 timeout。它本来就是对着一个出问题的进程树跑的，"
        "无上界的等待会让整条超时路径失去意义。"
    )
    assert 0 < passed <= 60, f"taskkill 的超时 {passed} 不合理"


# --------------------------------------------------------------- 环境隔离


@pytest.mark.parametrize(
    "variable",
    ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"],
)
def test_repository_redirecting_env_vars_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """指向别的仓库的环境变量一律不继承。

    实测修复前：环境里设了 ``GIT_DIR=<另一个仓库>/.git`` 之后，
    ``run_git(["rev-parse", "HEAD"], cwd=workspace)`` 读回来的是**那个仓库**的
    HEAD，不报错也不警告。从 git hook、``git bisect run``、``git rebase --exec``
    里启动 CLI 或 worker 都会带上它——而那些正是自动化流程里很常见的入口。
    后果不只是读错数据：``reset()`` 的 ``checkout -- .`` 会作用在用户的仓库上。
    """
    mine = init_repo(tmp_path / "mine", {"f.py": "MINE\n"})
    other = init_repo(tmp_path / "other", {"f.py": "OTHER\n"})
    assert mine.head != other.head

    monkeypatch.setenv(variable, str(other.path / ".git"))

    result = run_git(["rev-parse", "HEAD"], cwd=mine.path)
    assert result.stdout.strip() == mine.head, (
        f"{variable} 被继承了：读到了另一个仓库的 HEAD。"
        "本系统每次调用都显式指定 cwd，'哪个仓库'只应来自 cwd。"
    )


def test_authentication_env_is_still_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """剔除的只能是"改变是哪个仓库"的变量。

    认证相关的渠道要保留：把 ``SSH_AUTH_SOCK`` 一起清掉，就把"不要提示密码"
    做成了"不要认证"——私有仓库会直接连不上，而报错看起来像权限配置问题。
    """
    repo = init_repo(tmp_path / "repo", {"f.py": "x = 1\n"})
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")

    result = run_git(
        ["-c", "alias.probe=!echo sock=$SSH_AUTH_SOCK", "probe"],
        cwd=repo.path,
    )
    assert result.stdout.strip() == "sock=/tmp/fake-agent.sock"
