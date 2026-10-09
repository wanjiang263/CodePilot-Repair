"""git 命令行封装（02 §6）。

用 git CLI 而不是 ``dulwich``/``pygit2``：R-13 的 git history 需要
``rev-list`` / ``log --numstat`` 这类组合查询，用纯 Python 库重写等于把 git 的
历史语义再实现一遍，而实现错了不会报错、只会静默少算几个提交。CLI 是 git 的
原生接口，这里的每一行都能在终端里复现。

三条不变量：

1. **任何 git 调用都不允许弹出交互式提示**。``GIT_TERMINAL_PROMPT=0`` 让私有
   仓库的认证失败直接报错。在无人值守的 benchmark 里，一个等待输入的 git
   进程会让整次运行挂到墙钟超时，而且日志里什么都看不到。
2. **不继承用户全局配置中会改变语义的项**。``core.autocrlf`` 会让同一份
   workspace 在不同的机器上 checkout 出不同字节的内容，而代码检索与 patch
   导出都依赖字节级的一致。
3. **``check=False`` 的失败要把 stderr 原样带回**。git 的报错信息（"not a git
   repository"、"unknown revision"）是排查的唯一线索，压成一句"命令失败"
   会让每个调用方各自去猜。
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GitError", "GitResult", "run_git"]


class GitError(RuntimeError):
    """git 命令返回非 0，或根本无法执行。"""

    def __init__(
        self,
        message: str,
        *,
        args: Sequence[str] = (),
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.args_ = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def render(self) -> str:
        """给日志和 CLI 用的多行描述。"""
        lines = [str(self)]
        if self.args_:
            lines.append(f"  命令: git {' '.join(self.args_)}")
        if self.stderr.strip():
            lines.append(f"  stderr: {self.stderr.strip()}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class GitResult:
    """一次成功的 git 调用。"""

    args: list[str]
    stdout: str
    stderr: str
    returncode: int

    @property
    def lines(self) -> list[str]:
        """非空输出行。git 的输出常带尾随换行，逐行处理时不应带上空行。"""
        return [line for line in self.stdout.splitlines() if line.strip()]


#: 传给每个 git 调用的配置覆盖。``-c`` 的优先级高于所有配置文件，
#: 因此用户全局配置里的这两项无法改变本系统的行为。
_FORCED_CONFIG: tuple[tuple[str, str], ...] = (
    # 换行：统一按仓库中的字节处理，不做 CRLF 转换（02 §「Windows 环境差异」）。
    ("core.autocrlf", "false"),
    # 长路径：Windows 上默认会拒绝超过 260 字符的路径，而深层的仓库
    # （如 a/b/c/.../test_foo.py）在 workspace 里很容易超过。
    ("core.longpaths", "true"),
)

#: 禁止交互的最小环境。保留 PATH 与 HOME（git 需要后者找配置），
#: 其余变量原样继承而不是清空：清空会让 ``SSH_AUTH_SOCK`` 之类的
#: 合法认证渠道失效，那属于把"不要提示"做成了"不要认证"。
_FORBID_PROMPT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",  # 不提示用户名/密码
    "GIT_ASKPASS": "",  # 不弹图形化口令框
    "SSH_ASKPASS": "",
    "GIT_EDITOR": "true",  # 需要编辑器的子命令（如 rebase）直接失败
    "GCM_INTERACTIVE": "never",  # Git Credential Manager 不弹窗
}

#: 会**改变"当前是哪个仓库"**的环境变量，一律不继承。
#:
#: ``GIT_DIR`` 让 git 完全忽略 ``cwd`` 去找仓库：实测在本系统的 workspace 里
#: 执行 ``run_git(["rev-parse", "HEAD"], cwd=workspace)``，只要环境里有别人设的
#: ``GIT_DIR``，读回来的就是**另一个仓库**的 HEAD——不报错、不警告。从 git hook、
#: ``git bisect run``、``git rebase --exec`` 里启动 CLI 或 worker 都会带上它。
#: 那意味着 ``reset()`` 的 ``checkout -- .`` 会作用在用户的仓库上。
#:
#: 这与 docstring 里的不变量 2（"不继承用户配置中会改变语义的项"）是同一类
#: 问题，只是载体从配置文件换成了环境变量，所以处理方式也应当一样：显式剔除。
#: 剔除后 git 从 ``cwd`` 正常向上发现仓库——而 ``cwd`` 是本系统每次调用都显式
#: 指定的，正是我们想要的那份"哪个仓库"。
_HOSTILE_ENV: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_GRAFT_FILE",
        "GIT_SHALLOW_FILE",
        "GIT_QUARANTINE_PATH",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
        "GIT_ATTR_SOURCE",
    }
)

#: 杀掉进程树之后，最多再等这么久把管道读干净。
_KILL_GRACE_SECONDS = 5.0

#: POSIX 专有的三个符号。用 ``getattr`` 取而不是直接引用：直接写
#: ``os.killpg`` 会被 mypy 判定为"当前平台（Windows）没有这个属性"，而补一句
#: ``type: ignore`` 又会在 Linux 上被 ``warn_unused_ignores`` 判为多余——
#: 两边都不绿。取到 ``None`` 说明这个平台没有进程组，走 ``process.kill()``。
_KILLPG = getattr(os, "killpg", None)
_GETPGID = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", None)


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    """杀掉整棵进程**树**，而不是只杀直接子进程。

    ``timeout`` 只对直接子进程生效。``git clone`` 会派生 ``git-remote-https``
    或 ``ssh``，它们继承了 stdout/stderr 两个管道——而 ``subprocess`` 在超时
    之后要把管道读到 EOF 才算收工。子进程被杀了、孙进程还活着，读管道就会
    一直等下去。

    实测（本机 Windows 11）：让一个子进程派生一个继承管道、8 秒后退出的
    孙进程，然后 ``subprocess.run(..., timeout=2)``——**实际 8.2 秒**才抛
    ``TimeoutExpired``。如果孙进程永远不退出（挂住的 ssh），等待**没有上界**，
    墙上时间预算就形同虚设。
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        # taskkill /T 按父子关系杀整棵树。不用 CREATE_NEW_PROCESS_GROUP 是因为
        # 那解决的是 Ctrl+C 传播，与这里的"杀干净"是两件事。
        #
        # taskkill 自己也要限时：它是对着一个已经出问题的进程树跑的，卡住
        # 并不罕见（实测见过一次它被拦下来后阻塞了几十秒）。超时路径里放一个
        # 无上界的阻塞调用，等于把"超时"这两个字取消了。
        try:
            killed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=_KILL_GRACE_SECONDS,
            )
        except (subprocess.TimeoutExpired, OSError):
            killed = None
        if killed is None or killed.returncode != 0:
            process.kill()
    elif _KILLPG is not None and _GETPGID is not None and _SIGKILL is not None:
        # start_new_session=True 让子进程成了新进程组的组长，一次收掉整棵树。
        try:
            _KILLPG(_GETPGID(process.pid), _SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    else:  # pragma: no cover - 没有进程组的非 Windows 平台
        process.kill()
    # 内核级僵死时 wait 也会超时。这里没有别的办法了，只能往下走：调用方
    # 已经等了 timeout + 两个 grace，再等下去就是在拿整个运行冒险。
    with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover
        process.wait(timeout=_KILL_GRACE_SECONDS)


def run_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 300.0,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
    errors: str = "replace",
) -> GitResult:
    """执行一条 git 命令。

    ``cwd`` 为 ``None`` 时在进程当前目录执行——调用方应当总是传，
    否则一条 ``git status`` 的结果取决于"从哪里启动的 CLI"。

    ``input`` 走标准输入，给 ``check-ignore --stdin`` 这类批量子命令用：
    逐个路径起一个进程会把扫描一个中型仓库的代价从一次调用推到上千次。

    ``errors`` 默认 ``"replace"`` 让报错信息总能读；``check-ignore`` 需要
    ``"surrogateescape"``，因为它的**输出是文件名**，要跟 ``os.walk`` 给出的
    路径逐个比对——非 UTF-8 的文件名用 ``replace`` 会变成 U+FFFD，
    于是那条路径静默地匹配不上，该忽略的文件就进了索引。
    """
    # ``-c`` 与它的 ``key=value`` 必须是两个独立参数：这里不经过 shell，
    # 写成 "git -c core.autocrlf=false ..." 一整段会被当成一个可执行文件名。
    command = ["git"]
    for key, value in _FORCED_CONFIG:
        command += ["-c", f"{key}={value}"]
    command += list(args)

    run_env = {k: v for k, v in os.environ.items() if k not in _HOSTILE_ENV}
    run_env.update(_FORBID_PROMPT_ENV)
    if env:
        run_env.update(env)

    try:
        # 不用 subprocess.run：它在超时时只杀直接子进程，然后再去把管道读到
        # EOF，而继承了管道的孙进程会让那一步无限期地等下去（见
        # _terminate_tree 的实测数字）。这里自己管理生命周期。
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input is not None else None,
            text=True,
            encoding="utf-8",
            errors=errors,
            env=run_env,
            # POSIX 上让子进程成为新进程组的组长，这样 killpg 能一次收掉整棵树。
            # Windows 上没有这个参数（用 taskkill /T）。
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError as exc:
        raise GitError(
            "找不到 git 可执行文件。请确认 git 已安装并在 PATH 上"
            "（03 §2：不能用未安装的工具跑验收）。",
            args=list(args),
        ) from exc

    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_tree(process)
        # 杀掉整棵树之后管道应当立刻关闭。仍然给个上界，因为万一还有别的
        # 进程持着它，这里再等一次同样会没有尽头——而超时的意义就是给出
        # 一个确定的返回时间。
        with contextlib.suppress(subprocess.TimeoutExpired):  # pragma: no cover
            process.communicate(timeout=_KILL_GRACE_SECONDS)
        raise GitError(
            f"git 命令超过 {timeout:.0f} 秒未返回，已终止：git {' '.join(args)}",
            args=list(args),
        ) from exc

    result = GitResult(
        args=list(args),
        stdout=stdout,
        stderr=stderr,
        returncode=process.returncode,
    )
    if check and process.returncode != 0:
        raise GitError(
            f"git 命令失败（退出码 {process.returncode}）",
            args=list(args),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return result
