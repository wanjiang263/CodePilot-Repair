"""Git history 特征（R-13 / R-14，02 §7.6）。

只读取 ``base_commit`` 的**祖先**提交。这是 03 §11.2 的硬性规则：base 之后
的提交里有修复本身，混进历史特征等于偷看答案。这里有三道独立的保证：

1. ``base_commit`` 必须是**完整 SHA**（40/64 位十六进制），拒绝 ``main`` 这类
   引用名——引用会随新提交移动，传 ``main`` 等于每次都读到最新历史。
2. 构造时 ``rev-parse --verify <base>^{commit}`` 确认它真实存在。
3. 所有查询都是 ``git log <sha>`` 族，git 本身只沿父链走祖先；**绝不**加
   ``--all``、``--branches`` 或任何 ref。

按 docs/05 §23.1 的决定，历史从**共享缓存**（bare clone）读，不从 workspace
读——copy 模式的 workspace 历史被截断过，而缓存里本来就有 base 之后的提交，
这正是"只走祖先"必须由本模块保证而不能靠目录布局保证的原因。

近似规则与边界（R-14 明确要求记录）：

- **rename 不追随**：numstat 的 rename 条目只把修改记到**新路径**上，旧路径
  的历史不转移。文件改名后，改名前的修改频率留在旧名字里。
- **merge commit 不产生文件条目**（git 默认不显示 merge 的 diff），但它占一个
  提交序号，"距 base 的提交数"按 ``git log`` 输出顺序数（base 自身为 0）。
- **symbol 归属按 base 版本的行范围近似**：历史 hunk 的行号是**那个提交**的
  坐标，直接拿来与 base 版本的 symbol 行范围求交。提交越老、base 与它之间的
  行漂移越大，归属误差越大——02 §7.6 明确允许"近似即可"。
- **module symbol 不参与归属**（它横跨整个文件，会吞掉所有 hunk）；落在任何
  class/function/method/test 范围之外的 hunk 记入 ``unattributed``（例如模块
  级语句、import 行、注释）。
- **纯删除 hunk**（``+c,0``）没有新侧行，归属到第 ``c`` 行（删除点之前的那行）
  所在的 symbol；``c=0``（文件开头删除）时归到第 1 行。
- **删除的文件**（``+++ /dev/null``）与不在 base 符号表中的路径无法归属，记入
  ``skipped``；含非 ASCII 等被 git C-style quoting 的路径会解码后正常归属。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from codepilot.repo.gitcmd import run_git
from codepilot.repo.models import Symbol

if TYPE_CHECKING:
    from codepilot.repo.workspace import Workspace, WorkspaceManager

__all__ = [
    "DEFAULT_MAX_COMMITS",
    "FileChurn",
    "GitHistory",
    "GitHistoryError",
    "SymbolChurn",
    "SymbolChurnResult",
]

#: 02 §7.6：默认统计最近 200 次提交范围内的修改。
DEFAULT_MAX_COMMITS = 200

_FULL_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_COMMIT_MARKER = "COMMIT:"
_NUMSTAT_COMMIT = re.compile(r"^COMMIT:([0-9a-f]{40}(?:[0-9a-f]{24})?)\x00?$")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class GitHistoryError(RuntimeError):
    """base_commit 无法解析或不是完整 SHA。"""


@dataclass(frozen=True, slots=True)
class FileChurn:
    """一个文件在统计窗口内的修改特征。"""

    path: str
    modifications: int
    """窗口内触及该文件的提交数（创建也算一次）。"""

    last_modified_distance: int
    """最近一次修改距 base 的提交数。0 表示 base 自己就是最后一次修改。"""


@dataclass(frozen=True, slots=True)
class SymbolChurn:
    """一个 symbol 在统计窗口内的修改特征（近似归属，见模块 docstring）。"""

    symbol_id: str
    qualified_name: str
    path: str
    modifications: int
    last_modified_distance: int


@dataclass(frozen=True, slots=True)
class SymbolChurnResult:
    """``symbol_churn`` 的完整产出。"""

    churn: dict[str, SymbolChurn]
    """按 symbol id 索引。窗口内没被触及的 symbol 不在其中。"""

    unattributed: dict[str, int]
    """路径 → 未命中任何非 module symbol 的 hunk 数（模块级代码、注释等）。"""

    skipped: dict[str, int]
    """路径 → 无法归属的 hunk 数（删除的文件、不在 base 符号表中的路径）。"""

    commits_examined: int
    """实际扫描的提交数（仓库历史不足窗口时小于 ``max_commits``）。"""


@dataclass(slots=True)
class _CommitTouched:
    """一次提交触及的文件集合（解析 ``--numstat`` 的中间态）。"""

    sha: str
    paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _CommitPatch:
    """一次提交的逐文件 hunk 新侧行范围。"""

    sha: str
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)


class GitHistory:
    """读取 base 祖先提交的文件级与 symbol 级修改频率。

    ``repo_path`` 通常是共享缓存（``WorkspaceManager.cache_path(source)``），
    也可以是任何含该提交的 git 仓库（bare 或工作副本）。本类只读，不写任何 ref。
    """

    def __init__(self, repo_path: Path, base_commit: str, *, timeout: float = 300.0) -> None:
        if not _FULL_SHA.match(base_commit):
            raise GitHistoryError(
                f"base_commit 必须是完整 SHA（40/64 位十六进制），收到: {base_commit!r}\n"
                "引用名（main、HEAD、tag）会随新提交移动，用它读历史会把 base 之后"
                "的提交混进来。请先用 WorkspaceManager.rev_parse 解析成完整 SHA。"
            )
        self.repo_path = Path(repo_path)
        self.base_commit = base_commit
        self.timeout = timeout
        resolved = run_git(
            ["rev-parse", "--verify", "--quiet", f"{base_commit}^{{commit}}"],
            cwd=self.repo_path,
            check=False,
            timeout=timeout,
        )
        if resolved.returncode != 0:
            raise GitHistoryError(
                f"base_commit 不在仓库中: {base_commit}\n  仓库: {self.repo_path}\n"
                f"  stderr: {resolved.stderr.strip()}"
            )

    @classmethod
    def for_workspace(
        cls, manager: WorkspaceManager, workspace: Workspace, *, timeout: float = 300.0
    ) -> GitHistory:
        """从 workspace 对应的**共享缓存**构造（docs/05 §23.1 的决定）。

        不从 workspace 读：copy 模式的 workspace 历史被截断到 base（本身可用但
        不是规定来源），worktree 模式则直接暴露缓存的全部 ref。统一走缓存让
        "只读祖先"这一约束只在本模块一处成立。
        """
        return cls(manager.cache_path(workspace.source), workspace.base_commit, timeout=timeout)

    # ------------------------------------------------------------ 文件级（R-13）

    def file_churn(self, *, max_commits: int = DEFAULT_MAX_COMMITS) -> dict[str, FileChurn]:
        """统计窗口内每个文件的修改次数与最近修改距 base 的提交数。

        只覆盖窗口内**被触及过**的文件；没被触及的文件视为 0 次（调用方按
        ``dict.get`` 处理缺失）。rename 只记新路径（见模块 docstring）。
        """
        result = run_git(
            [
                "log",
                f"--max-count={max_commits}",
                "--numstat",
                "-z",
                f"--format={_COMMIT_MARKER}%H",
                self.base_commit,
            ],
            cwd=self.repo_path,
            timeout=self.timeout,
        )
        churn: dict[str, FileChurn] = {}
        for distance, commit in enumerate(_parse_numstat(result.stdout)):
            for path in commit.paths:
                entry = churn.get(path)
                if entry is None:
                    churn[path] = FileChurn(path, 1, distance)
                else:
                    churn[path] = FileChurn(
                        path, entry.modifications + 1, entry.last_modified_distance
                    )
        return churn

    # ------------------------------------------------------------ symbol 级（R-14）

    def symbol_churn(
        self,
        symbols: list[Symbol] | tuple[Symbol, ...],
        *,
        max_commits: int = DEFAULT_MAX_COMMITS,
    ) -> SymbolChurnResult:
        """把窗口内每次提交的 diff hunk 近似映射到 base 版本的 symbol 行范围。

        ``symbols`` 是 base 版本（即索引结果）中的符号；module 类型的会被忽略
        （原因见模块 docstring）。归属规则与无法归属的边界都写在模块 docstring 里，
        调用方不应假设它是精确的。
        """
        by_path: dict[str, list[Symbol]] = {}
        for symbol in symbols:
            if symbol.kind == "module":
                continue
            by_path.setdefault(symbol.path, []).append(symbol)
        id_lookup = {s.id: s for s in symbols}
        for symbols_in_file in by_path.values():
            symbols_in_file.sort(key=lambda s: (s.start_line, s.end_line))

        result = run_git(
            [
                "log",
                f"--max-count={max_commits}",
                "-U0",
                "-p",
                f"--format={_COMMIT_MARKER}%H",
                self.base_commit,
            ],
            cwd=self.repo_path,
            timeout=self.timeout,
        )

        churn: dict[str, SymbolChurn] = {}
        unattributed: dict[str, int] = {}
        skipped: dict[str, int] = {}
        commits_examined = 0
        for distance, patch in enumerate(_parse_patch(result.stdout)):
            commits_examined += 1
            for path in patch.deleted:
                skipped[path] = skipped.get(path, 0) + 1
            for path, hunks in patch.hunks.items():
                path_symbols = by_path.get(path)
                if path_symbols is None:
                    skipped[path] = skipped.get(path, 0) + len(hunks)
                    continue
                hit_ids: set[str] = set()
                misses = 0
                for start, count in hunks:
                    hit = _attribute(path_symbols, start, count)
                    if not hit:
                        misses += 1
                        continue
                    hit_ids.update(s.id for s in hit)
                if misses:
                    unattributed[path] = unattributed.get(path, 0) + misses
                # 一个提交触及同一 symbol 多次仍算一次修改。
                for symbol_id in sorted(hit_ids):
                    symbol = id_lookup[symbol_id]
                    entry = churn.get(symbol_id)
                    if entry is None:
                        churn[symbol_id] = SymbolChurn(
                            symbol_id, symbol.qualified_name, path, 1, distance
                        )
                    else:
                        churn[symbol_id] = SymbolChurn(
                            symbol_id,
                            symbol.qualified_name,
                            path,
                            entry.modifications + 1,
                            entry.last_modified_distance,
                        )
        return SymbolChurnResult(churn, unattributed, skipped, commits_examined)


# ---------------------------------------------------------------- 解析


def _parse_numstat(output: str) -> list[_CommitTouched]:
    """解析 ``git log --numstat -z --format=COMMIT:%H`` 的输出。

    实测格式（git 2.51，Windows）：提交行 ``COMMIT:<sha>\\0\\n``；普通条目
    ``added\\tdeleted\\tpath\\0``；rename 条目 ``added\\tdeleted\\t\\0old\\0new\\0``
    （路径字段为空，随后两个 NUL 分隔的旧、新路径）。merge 提交没有任何条目。
    """
    commits: list[_CommitTouched] = []
    current: _CommitTouched | None = None
    tokens = output.split("\x00")
    i = 0
    while i < len(tokens):
        token = tokens[i].lstrip("\n")
        i += 1
        if not token:
            continue
        marker = _NUMSTAT_COMMIT.match(token)
        if marker:
            current = _CommitTouched(marker.group(1))
            commits.append(current)
            continue
        if current is None:
            continue
        parts = token.split("\t", 2)
        if len(parts) < 3:
            continue
        path = parts[2]
        if path:
            current.paths.append(path)
        elif i + 1 < len(tokens):
            # rename：随后两个 token 依次是旧路径与新路径；只记新路径。
            current.paths.append(tokens[i + 1])
            i += 2
    return commits


def _parse_patch(output: str) -> list[_CommitPatch]:
    """解析 ``git log -U0 -p --format=COMMIT:%H`` 的输出。

    只关心：提交边界、``--- a/<path>`` / ``+++ b/<path>``（``/dev/null`` 记为
    删除）、``@@`` 的新侧行范围。非 ASCII 路径在 diff 头里是 C-style quoting
    （实测 ``-z`` 不改变 patch 输出），路径行因此先解码再使用。
    """
    commits: list[_CommitPatch] = []
    current: _CommitPatch | None = None
    old_path: str | None = None
    new_path: str | None = None
    for line in output.splitlines():
        if line.startswith(_COMMIT_MARKER):
            sha = line[len(_COMMIT_MARKER) :].strip("\x00").strip()
            if _FULL_SHA.fullmatch(sha):
                current = _CommitPatch(sha)
                commits.append(current)
                old_path = None
                new_path = None
            continue
        if current is None:
            continue
        if line.startswith("diff --git "):
            old_path = None
            new_path = None
            continue
        if line.startswith("--- "):
            raw = line[4:]
            old_path = None if raw == "/dev/null" else _unquote_diff_path(raw)
            continue
        if line.startswith("+++ "):
            raw = line[4:]
            new_path = None if raw == "/dev/null" else _unquote_diff_path(raw)
            if new_path is None and old_path is not None:
                current.deleted.append(old_path)
            continue
        hunk = _HUNK_HEADER.match(line)
        if hunk and new_path is not None:
            start = int(hunk.group(3))
            count = int(hunk.group(4)) if hunk.group(4) is not None else 1
            current.hunks.setdefault(new_path, []).append((start, count))
    return commits


def _unquote_diff_path(raw: str) -> str:
    """``--- a/<path>`` / ``+++ b/<path>`` 行的路径解码。

    git 对含特殊字符的路径用 C-style quoting（``"b/r\\303\\251nzh\\303\\254.py"``）。
    引号形式按字节解码：八进制转义先还原成字节，再按 UTF-8 解码
    （``surrogateescape`` 兜底，不让一个怪路径中断整个历史统计）。
    """
    if raw.startswith(("a/", "b/")):
        return raw[2:]
    if not raw.startswith('"'):
        return raw
    inner = raw.strip().strip('"')
    out = bytearray()
    i = 0
    while i < len(inner):
        char = inner[i]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            i += 1
            continue
        nxt = inner[i + 1] if i + 1 < len(inner) else ""
        if nxt.isdigit():
            out.append(int(inner[i + 1 : i + 4], 8))
            i += 4
        else:
            escapes = {
                "n": 10,
                "t": 9,
                "r": 13,
                "\\": 92,
                '"': 34,
                "a": 7,
                "b": 8,
                "f": 12,
                "v": 11,
            }
            out.append(escapes.get(nxt, ord(nxt) if nxt else 92))
            i += 2
    text = out.decode("utf-8", "surrogateescape")
    return text[2:] if text.startswith(("a/", "b/")) else text


def _attribute(symbols: list[Symbol], start: int, count: int) -> list[Symbol]:
    """把一个 hunk 的新侧行范围映射到 symbol（近似规则见模块 docstring）。"""
    if count == 0:
        # 纯删除：归属到删除点之前那行；文件开头删除归到第 1 行。
        line = max(start, 1)
        return [s for s in symbols if s.start_line <= line <= s.end_line]
    last = start + count - 1
    return [s for s in symbols if s.start_line <= last and start <= s.end_line]
