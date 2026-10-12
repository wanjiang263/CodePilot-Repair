"""文件系统小工具（Windows 兼容层）。

单独成模块的理由只有一个，但它足够重要：**git 写的对象文件在 Windows 上是
只读的**（packfile 与 loose object 都带 R 属性）。于是 ``shutil.rmtree`` 会在
删除任何一个 git 目录时抛 ``PermissionError``。

这个失败特别容易蒙混过关，因为最常见的用法是
``shutil.rmtree(path, ignore_errors=True)``——它把异常吞掉，目录留下，
调用方以为删干净了。后果不是立刻报错，而是下一次 ``git clone`` 因为目标目录
非空而失败，错误信息指向"clone 失败"，与实际原因（上一轮的残骸）毫无关系。
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath

__all__ = [
    "is_link",
    "pid_alive",
    "remove_link",
    "remove_tree",
    "strip_reparse_points",
    "workspace_source_path",
]


def workspace_source_path(
    root: Path, relative: str, *, link_check: Callable[[Path], bool] | None = None
) -> Path:
    """Existing retrieval path policy, shared by read-only repository consumers."""
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in relative:
        raise ValueError(f"Unsafe indexed path: {relative}")
    target = root
    for part in path.parts:
        target = target / part
        if (link_check or is_link)(target):
            raise ValueError(f"Linked indexed path: {relative}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Indexed path escapes repository: {relative}")
    return target


#: ``GetExitCodeProcess`` 的"进程还活着"取值（Windows SDK 的 STILL_ACTIVE）。
_STILL_ACTIVE = 259


def pid_alive(pid: int) -> bool:
    """这个进程号现在是否被占用。

    只用来判断"别人留下的临时目录是不是还有主"。判断错一次的后果是多留或少删
    一个临时目录，不会损坏数据，所以这里刻意偏向保守：**拿不准就当作还活着**。

    不能用 ``os.kill(pid, 0)`` 判断：那在 POSIX 上是"发 0 号信号探测"，在
    Windows 上却会真的调 ``TerminateProcess``——等于为了检查一个进程是否存活
    把它杀掉。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # getattr 而非直接引用：直接写 ctypes.WinDLL 会被 Linux 平台（CI）的 mypy 判为无此属性。
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009  (见上行)
        # PROCESS_QUERY_LIMITED_INFORMATION 在 Vista 之后可用，权限要求比
        # PROCESS_QUERY_INFORMATION 低，足以读退出码。
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False  # 打不开 = 进程不存在（或已被回收）
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我的进程
    return True


def is_link(entry: Path) -> bool:
    """符号链接**或** Windows junction。

    ``Path.is_symlink()`` 对 junction 返回 ``False``——它是一个重解析点但不是
    符号链接。这个区别在 Windows 上会造成两条实实在在的漏洞，而两条都不会
    报错：

    1. ``os.walk(followlinks=False)`` 对 junction **照样递归**（它只查
       ``is_symlink``），于是 ``mklink /J`` 出来的目录既能读到工作区外的内容，
       也能构成环把遍历卡死。
    2. ``git worktree remove`` / ``git clean`` 这些 git 自己实现的删除会沿着
       junction 递归下去，把**目标目录**的内容删掉。实测见
       ``strip_reparse_points`` 的说明。

    junction 不需要管理员权限也不需要开发者模式，所以这是默认配置的 Windows
    上唯一能构造出来的目录逃逸。
    """
    if entry.is_symlink():
        return True
    # Python 3.12 才有 os.path.isjunction；用 getattr 让 3.11 上退化成只查符号链接。
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction is not None and is_junction(entry))


def remove_link(entry: Path) -> bool:
    """删掉重解析点**本身**，不碰它指向的目标。返回是否删掉了。

    目录符号链接与 junction 都要走 ``os.rmdir``：``Path.unlink()`` 在 POSIX 上
    对指向目录的链接会抛 ``IsADirectoryError``，而 Windows 上删 junction 也只
    有 ``rmdir`` 这一条路（它删除的是重解析点，目标目录不受影响）。
    """
    if not is_link(entry):
        return False
    try:
        entry.unlink()
        return True
    except OSError:
        pass
    try:
        entry.rmdir()
        return True
    except OSError:
        return False


def strip_reparse_points(root: Path) -> list[str]:
    """删除 ``root`` 子树内的全部符号链接与 junction，返回被删掉的相对路径。

    **在执行任何会递归删除的 git 命令之前调用它。** git 在 Windows 上把
    junction 当普通目录，沿它递归下去删的是目标目录的内容。实测（本机
    Windows 11 + git 2.51.1）：

    * ``shutil.rmtree`` 与 ``remove_tree`` **不会**穿过 junction——目标文件的
      内容完好，它们只删链接。
    * ``git worktree remove --force`` **会**穿过去，退出码还是 0，没有任何提示。
      把 junction 指向用户原仓库，一次 ``remove()`` 就能把用户的文件删掉。
    * ``git checkout -- .`` / ``git clean -fd`` 同理：前者沿链接把 base 版本
      写回目标目录（覆盖用户未提交的改动），后者删掉目标目录里未跟踪的文件。
      用户那边的 ``git status`` 事后是干净的，改动无声消失。

    ``os.walk(followlinks=False)`` 对 junction 照样会下降，所以这里必须显式把
    它们从 ``dirnames`` 里摘掉，否则边删边往下走。
    """
    removed: list[str] = []

    def drop(entry: Path) -> None:
        if remove_link(entry):
            try:
                removed.append(entry.relative_to(root).as_posix())
            except ValueError:  # pragma: no cover - os.walk 只会给出子树内的路径
                removed.append(str(entry))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            entry = current / name
            if is_link(entry):
                drop(entry)
            else:
                kept.append(name)
        # 摘掉链接之后再赋回去：不摘的话 os.walk 会顺着 junction 往下走，
        # 变成一边删一边遍历它指向的那个目录。
        dirnames[:] = kept
        for name in filenames:
            entry = current / name
            if is_link(entry):
                drop(entry)
    return removed


def _clear_readonly_and_retry(function: object, target: str, exc: BaseException) -> None:
    """``rmtree`` 的 ``onexc`` 回调：去掉只读位再试一次。

    重试仍失败就让原异常传播。``shutil`` 在 Windows 上还会因为文件被占用而失败
    （杀毒软件、索引服务、另一个进程刚打开过它），那种情况下重试无用，
    而静默吞掉会让调用方以为目录已经删干净了。
    """
    try:
        Path(target).chmod(stat.S_IWRITE)
        function(target)  # type: ignore[operator]
    except OSError:
        raise exc from None


def remove_tree(path: Path, *, ignore_errors: bool = False) -> bool:
    """删除目录树，能处理 git 的只读文件。

    返回 ``True`` 表示目录已消失（包括本来就不存在）。``ignore_errors=True``
    时永远返回 ``False`` 表示"删不掉"，而不是抛异常——调用方通常需要知道
    这一点（例如"清理失败，下一轮可能受影响"），但不想中断流程。
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return True
    try:
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    except OSError:
        if not ignore_errors:
            raise
        return False
    return not path.exists()
