"""仓库文件扫描（R-04）。

产出"这个仓库里有哪些文件值得索引"：相对路径、字节数、sha256、行数。后续的
解析（R-05）与增量重建（R-16）都以这里的结果为准。

三个决定值得说明：

**`.gitignore` 交给 git 自己判断**（``git check-ignore``），不在 Python 里重写一遍
模式匹配。gitignore 的语义比看上去复杂（``**`` 的多级匹配、``!`` 取反的顺序敏感、
尾斜杠只匹配目录、前导斜杠锚定、子目录里的 .gitignore 会叠加），自己实现几乎必然
在某些仓库上悄悄少排除几个文件——而症状是索引里混进了 ``build/`` 下的产物，
很难联想到忽略规则。一次 ``git check-ignore --stdin`` 调用可以判定全部路径，
代价是一个子进程。

**符号链接一律跳过**，不区分它指向哪里。理由有二：一是逃逸（03 §13 要求拒绝
符号链接逃逸），二是**确定性**——同一个仓库在 ``core.symlinks=true`` 的 Linux 上
checkout 出符号链接，在 Windows 上则是内容为目标的普通文件，两边索引出的
文件集合会不同，从而让基准数字不可比。记录在 ``symlinks`` 里而不是静默丢弃。

**二进制按 NUL 字节判定**，与 git 自己的启发式一致：只看前 8KB。不按扩展名
判定，因为仓库里真实存在无扩展名的文本（``Makefile``、``Dockerfile``）和
扩展名骗人的文件。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from codepilot.fsutil import is_link
from codepilot.repo.gitcmd import GitError, run_git

__all__ = [
    "BINARY_SNIFF_BYTES",
    "DEFAULT_IGNORED_DIRS",
    "DEFAULT_MAX_FILE_BYTES",
    "RepositoryScanner",
    "ScanResult",
    "ScannedFile",
    "is_test_path",
]

#: 目录名黑名单。只作用于**目录**：``build`` 作为目录是产物，作为文件则不是。
DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
        "node_modules",
    }
)

#: 单文件上限 1 MiB（R-04）。超过它的几乎都是数据文件或生成的代码，
#: 解析它们只会拖慢索引，而检索时也不会有人去改一个 1MB 的生成文件。
DEFAULT_MAX_FILE_BYTES = 1024 * 1024

#: 判定二进制时只看开头这么多字节。与 git 的做法一致。
BINARY_SNIFF_BYTES = 8192

#: pytest 默认收集的测试目录名。
_TEST_DIR_NAMES = frozenset({"test", "tests"})


def is_test_path(relpath: str) -> bool:
    """按路径约定判断是否测试文件。

    ``is_test`` 是 ``FileNode`` 的字段，检索通道（V-12 test proximity）与回归
    判定都要用它。这里只做路径判断——测试函数**内部**的语义是 R-06/R-12 的事。

    判断倾向"宁可多标"：漏标一个测试会让它的用例进不了 ``TestBaseline``，
    于是回归判定看不见这个测试被改坏（这是静默的错误）；而多标一个普通文件
    几乎没有代价——``test_function`` 的类型判定还要求函数名本身以 ``test_``
    开头，普通文件的函数不会被误当成测试。

    命名规则对齐 pytest 的默认值 ``test_*.py`` 与 ``*_test.py``：只认前者
    会让用 ``xxx_test.py`` 命名的仓库里，测试全被当成源码。
    """
    path = Path(relpath)
    if path.suffix != ".py":
        return False
    if any(part in _TEST_DIR_NAMES for part in path.parts[:-1]):
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """一个待索引的文件。"""

    path: str
    """相对仓库根目录，统一用 ``/`` 分隔（02 §「数据模型」）。"""

    size: int
    sha256: str
    line_count: int
    is_test: bool

    def __str__(self) -> str:
        return self.path


@dataclass(frozen=True, slots=True)
class ScanResult:
    """一次扫描的全部结果，含被跳过的项。

    跳过项**不静默丢弃**：``codepilot index`` 要能把它们打印出来，否则
    "为什么这个文件没被索引"永远只能靠猜。R-04 的验收明确要求"跳过并计数"。
    """

    files: tuple[ScannedFile, ...]
    ignored: tuple[str, ...] = ()
    """被 ``.gitignore``（或 git 的其他忽略来源）排除。"""

    too_large: tuple[str, ...] = ()
    binary: tuple[str, ...] = ()
    symlinks: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    """读不了的路径（权限、被占用、路径过长）。索引不该因为这些中断。"""

    _by_path: dict[str, ScannedFile] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        # dataclass 是 frozen 的，用 object.__setattr__ 建索引。
        object.__setattr__(self, "_by_path", {f.path: f for f in self.files})

    def __len__(self) -> int:
        return len(self.files)

    def get(self, path: str) -> ScannedFile | None:
        return self._by_path.get(path)

    def counts(self) -> dict[str, int]:
        """给 ``codepilot index`` 打印用的计数。"""
        return {
            "files": len(self.files),
            "ignored": len(self.ignored),
            "too_large": len(self.too_large),
            "binary": len(self.binary),
            "symlinks": len(self.symlinks),
            "unreadable": len(self.unreadable),
        }

    @property
    def skipped(self) -> tuple[str, ...]:
        return (*self.ignored, *self.too_large, *self.binary, *self.symlinks, *self.unreadable)


class RepositoryScanner:
    """遍历仓库，产出可索引的文件清单。"""

    def __init__(
        self,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
        timeout: float = 120.0,
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.ignored_dirs = ignored_dirs
        self.timeout = timeout

    def scan(self, root: Path) -> ScanResult:
        """扫描 ``root`` 下的全部文件。

        ``root`` 通常是一个 workspace（由 ``WorkspaceManager`` 创建）。
        扫描本身**只读**：不写任何文件，也不改 git 状态。
        """
        root = Path(root).resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"扫描根目录不存在或不是目录: {root}")

        candidates, symlinks, unreadable_dirs = self._walk(root)
        ignored = self._ignored_by_git(root, candidates)

        files: list[ScannedFile] = []
        too_large: list[str] = []
        binary: list[str] = []
        unreadable: list[str] = list(unreadable_dirs)

        for relpath in candidates:
            if relpath in ignored:
                continue
            absolute = root / relpath
            try:
                size = absolute.stat().st_size
            except OSError:
                unreadable.append(relpath)
                continue
            if size > self.max_file_bytes:
                too_large.append(relpath)
                continue
            try:
                raw = absolute.read_bytes()
            except OSError:
                unreadable.append(relpath)
                continue
            if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
                binary.append(relpath)
                continue
            files.append(
                ScannedFile(
                    path=relpath,
                    size=len(raw),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    line_count=_count_lines(raw),
                    is_test=is_test_path(relpath),
                )
            )

        return ScanResult(
            # 排序让同样的仓库在两次扫描中产出同样的顺序。增量索引与 golden
            # test 都依赖这一点：文件系统返回的顺序在不同机器上并不一致。
            files=tuple(sorted(files, key=lambda f: f.path)),
            ignored=tuple(sorted(ignored)),
            too_large=tuple(sorted(too_large)),
            binary=tuple(sorted(binary)),
            symlinks=tuple(sorted(symlinks)),
            unreadable=tuple(sorted(unreadable)),
        )

    # ------------------------------------------------------------ 遍历

    def _walk(self, root: Path) -> tuple[list[str], list[str], list[str]]:
        """返回 ``(候选文件, 跳过的符号链接, 读不了的目录)``。

        ``os.walk(followlinks=False)`` 不会递归进符号链接目录，但**会**把它
        列在 ``dirnames`` 里——不显式记录的话它就从结果里静默消失了。

        读不了的目录同样要显式记录。``os.walk`` 的 ``onerror`` 默认是
        ``None``，意思是"把一个目录的 ``scandir`` 失败直接吞掉、连一个空目录
        都不产出"——整个子树于是从结果里凭空消失，而调用方看到的是
        "这个仓库里没有那些文件"。对定位阶段来说，"文件不存在"和"没能去看"
        是两件完全不同的事，前者会把 agent 直接引向错误的结论。
        """
        candidates: list[str] = []
        symlinks: list[str] = []
        unreadable: list[str] = []

        def record_unreadable(error: OSError) -> None:
            # CPython 把 scandir 的参数放进 err.filename，取不到就无从归属。
            # 记成 "." 而不是丢掉：这个字段的语义是"有哪些地方没能去看"，
            # 少一条等于把"没去看"说成"不存在"。
            path = Path(error.filename) if error.filename else root
            # is_relative_to 是纯词法比较。越界的路径不写进去——这个字段的
            # 每一条都该是仓库内的相对路径，混进绝对路径会让下游拼接出错。
            unreadable.append(_rel(path, root) if path.is_relative_to(root) else ".")

        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=record_unreadable
        ):
            current = Path(dirpath)

            kept_dirs = []
            for name in dirnames:
                entry = current / name
                relpath = _rel(entry, root)
                if name in self.ignored_dirs:
                    continue
                if is_link(entry):
                    symlinks.append(relpath)
                    continue
                kept_dirs.append(name)
            # 就地排序让遍历顺序确定；乱序的文件系统会让索引结果的顺序漂移。
            dirnames[:] = sorted(kept_dirs)

            for name in sorted(filenames):
                entry = current / name
                relpath = _rel(entry, root)
                if name == ".git":
                    # worktree 里 .git 是一个**文件**（内容是指向缓存仓库的
                    # 路径），只按目录名过滤会漏掉它，于是它的内容会被当成
                    # 源码索引进去。
                    continue
                if is_link(entry):
                    symlinks.append(relpath)
                    continue
                candidates.append(relpath)

        return candidates, symlinks, unreadable

    # ------------------------------------------------------------ 忽略规则

    def _ignored_by_git(self, root: Path, candidates: list[str]) -> set[str]:
        """用 ``git check-ignore`` 判定被忽略的路径。

        非 git 目录（例如单测里直接扫一个普通目录）时退回空集合：调用方是
        ``WorkspaceManager`` 时它永远是仓库，而单测里不该为了跑一次扫描
        先建一个仓库。
        """
        if not candidates:
            return set()
        if not (root / ".git").exists():
            return set()

        # ``--stdin -z`` 的输入必须是 **NUL 分隔**：用换行分隔时 git 会把整段
        # 输入当成一个路径，于是永远匹配不到东西、又以退出码 1 正常返回——
        # 表现为"忽略规则偶尔不生效"。输出同样是 NUL 分隔，因为文件名里可以有换行。
        payload = "".join(path + "\0" for path in candidates)
        try:
            result = run_git(
                [
                    # 只让仓库自己的 .gitignore 生效。用户全局的
                    # core.excludesFile（默认 ~/.config/git/ignore）会让同一份
                    # 代码在不同的机器上索引出不同的文件集合，而基准数字
                    # 必须可复现（NFR-01）。指向一个不存在的文件即可关闭它。
                    "-c",
                    f"core.excludesFile={root / '.git' / 'codepilot-no-global-excludes'}",
                    "check-ignore",
                    "--stdin",
                    "-z",
                ],
                cwd=root,
                input=payload,
                errors="surrogateescape",
                timeout=self.timeout,
                check=False,
            )
        except GitError:
            # git 不可用时退回"只按内置目录黑名单过滤"。不抛异常：扫描是尽力
            # 而为的工作，因为一条可选优化让整个索引失败不划算。调用方可以从
            # ``ignored`` 为空看出来它没生效。
            return set()

        # 退出码 1 表示"没有任何路径被忽略"，不是错误。
        if result.returncode not in (0, 1):
            return set()
        return {path for path in result.stdout.split("\0") if path}


def _rel(entry: Path, root: Path) -> str:
    """相对路径，统一 POSIX 分隔符。"""
    return entry.relative_to(root).as_posix()


def _count_lines(raw: bytes) -> int:
    """行数。

    用 ``splitlines()`` 而不是数 ``\\n``：仓库里存在用 ``\\r`` 单独换行的旧文件，
    也有末尾没有换行的文件，逐字节数换行会把两者都算错，而 ``end_line`` 是
    要写进报告给用户看的。
    """
    return len(raw.decode("utf-8", errors="replace").splitlines())
