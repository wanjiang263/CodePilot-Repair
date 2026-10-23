"""模型补丁的结构解析与路径安全校验（P2）。

这一层只回答"这份 diff 动了哪些仓库内文件"，因此推理侧（目标测试选择）可以
直接使用，而不必依赖评测侧的 ``codepilot.eval``。官方预测写入器仍然复用同一套
解析：两个入口对同一份补丁必须得到同一组文件，否则目标测试与交付补丁会悄悄分叉。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from pathlib import PurePosixPath

__all__ = ["PatchValidationError", "normalize_patch", "patch_files"]

_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
#: ``@@ -old_start,old_count +new_start,new_count @@ [context]``。omit 的 count=1。
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
#: Phase 6 的复现测试目录。DA-009 的交付策略是"它不进交付补丁"；模型补丁同理，
#: 否则官方 harness 会把 agent 自造的复现测试也一起应用，污染评测现场。
_REPRODUCTION_DIR = "_codepilot_repro"
#: 评测目录必须拒绝：它不属于任务仓库，且可能含 gold/test 内容。
_DENIED_PATH_PARTS = frozenset({".git", "eval"})


class PatchValidationError(ValueError):
    """模型补丁不是可安全写出的 git diff。"""


def _validate_repo_path(raw: str, *, where: str) -> str:
    """校验补丁里的仓库相对路径；越界/危险路径直接拒绝。"""
    if not raw:
        raise PatchValidationError(f"{where}: 路径为空")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise PatchValidationError(f"{where}: 路径含控制字符")
    if "\\" in raw:
        raise PatchValidationError(f"{where}: 路径不得含反斜杠: {raw!r}")
    if raw.startswith("/") or _DRIVE_PATH.match(raw) or raw.startswith("//"):
        raise PatchValidationError(f"{where}: 拒绝绝对路径: {raw!r}")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise PatchValidationError(f"{where}: 拒绝绝对路径: {raw!r}")
    parts = pure.parts
    if not parts or raw.endswith("/"):
        raise PatchValidationError(f"{where}: 路径非法: {raw!r}")
    if ".." in parts or "." in parts:
        raise PatchValidationError(f"{where}: 拒绝 '..'/'.' 路径片段: {raw!r}")
    if _DENIED_PATH_PARTS & set(parts):
        raise PatchValidationError(f"{where}: 拒绝 .git/ 或 eval/ 下的路径: {raw!r}")
    return pure.as_posix()


def _is_reproduction_path(path: str) -> bool:
    return _REPRODUCTION_DIR in PurePosixPath(path).parts


def _parse_diff_git_header(line: str) -> str:
    rest = line[len("diff --git ") :]
    if rest.startswith('"'):
        try:
            tokens = shlex.split(rest, posix=True)
        except ValueError as exc:
            raise PatchValidationError(f"无法解析 diff --git 头: {line!r}") from exc
        if len(tokens) != 2:
            raise PatchValidationError(f"无法解析 diff --git 头: {line!r}")
        old_token, new_token = tokens
    else:
        marker = " b/"
        index = rest.find(marker)
        if index <= 0:
            raise PatchValidationError(f"无法解析 diff --git 头: {line!r}")
        old_token = rest[:index]
        new_token = rest[index + 1 :]
    if not old_token.startswith("a/") or not new_token.startswith("b/"):
        raise PatchValidationError(f"diff --git 头必须以 a/ b/ 前缀标注路径: {line!r}")
    old_path = _validate_repo_path(old_token[2:], where="diff --git")
    new_path = _validate_repo_path(new_token[2:], where="diff --git")
    if old_path != new_path:
        # 重命名不做支持：官方 harness 的 apply 语义与我们对 path 的安全假设都
        # 更复杂，宁可 fail closed 让调用方标记 INVALID_PATCH。
        raise PatchValidationError(
            f"暂不支持重命名 diff（a/b 路径不同）: {old_path!r} -> {new_path!r}"
        )
    return old_path


def _parse_marker_path(line: str, prefix: str, *, where: str) -> str | None:
    value = line[len(prefix) :].strip()
    if not value:
        raise PatchValidationError(f"{where}: 路径为空")
    if value.startswith('"'):
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError as exc:
            raise PatchValidationError(f"{where}: 无法解析路径: {value!r}") from exc
        if len(tokens) != 1:
            raise PatchValidationError(f"{where}: 无法解析路径: {value!r}")
        value = tokens[0]
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return _validate_repo_path(value, where=where)


def _parse_section(lines: Sequence[str]) -> tuple[str, list[str]]:
    """解析一个 diff 段落，返回 (路径, 段落行)。

    既接受带 ``diff --git`` 头的标准格式，也接受模型常见的手写 unified diff
    （直接 ``--- a/f`` / ``+++ b/f``）。后者是 ``git apply`` 合法输入，若一律判非法，
    会把本可评测的补丁记成 INVALID_PATCH。多文件且没有 ``diff --git`` 头时无法可靠
    切分边界，明确拒绝而不是猜。

    hunk 边界靠 ``@@ -old,count +new,count @@`` 的行数计数判断，而不是"下一行像不像
    文件标记"：删除一行内容为 ``-- a/comment``（SQL/Lua 注释）时，diff 行恰好是
    ``--- a/comment``，按前缀猜测会把它误判成新文件。已知限制（Phase 12 再评估）：
    重命名 diff、binary diff 暂不支持；纯 mode 变更（old/new mode 无 hunk）支持。
    """
    if not lines:
        raise PatchValidationError("空 diff 段落")
    header_path: str | None = None
    body_start = 0
    if lines[0].startswith("diff --git "):
        header_path = _parse_diff_git_header(lines[0])
        body_start = 1
    old_marker: str | None = None
    new_marker: str | None = None
    seen_old = False
    seen_new = False
    has_body = False
    in_hunk = False
    old_remaining = 0
    new_remaining = 0
    #: 上一行是 hunk 内容行（含 `\\ No newline...` 标记）。用于识别 hunk 结束后的
    #: 换行标记，同时不把普通行误当标记。
    after_hunk_content = False
    for line in lines[body_start:]:
        if in_hunk and not line.startswith("@@"):
            if line == "":
                # 有些工具把空行当"空上下文行"（规范写法是单个空格）。
                old_remaining -= 1
                new_remaining -= 1
            elif line[0] == " ":
                old_remaining -= 1
                new_remaining -= 1
            elif line[0] == "+":
                new_remaining -= 1
            elif line[0] == "-":
                old_remaining -= 1
            elif line[0] == "\\":
                pass  # `\ No newline at end of file`，不消耗行数
            else:
                raise PatchValidationError(f"hunk 内容行前缀非法: {line[:80]!r}")
            after_hunk_content = True
            if old_remaining < 0 or new_remaining < 0:
                # 行数写多了：结束当前 hunk，保留原文交给 git apply 归因。
                in_hunk = False
                old_remaining = 0
                new_remaining = 0
            elif old_remaining == 0 and new_remaining == 0:
                in_hunk = False
            continue
        match = _HUNK_HEADER.match(line)
        if line.startswith("@@"):
            if match is None:
                raise PatchValidationError(f"非法 hunk 头: {line!r}")
            if seen_old != seen_new:
                raise PatchValidationError("diff 段落的 --- / +++ 标记不完整")
            has_body = True
            old_remaining = int(match.group(2) or 1)
            new_remaining = int(match.group(4) or 1)
            in_hunk = old_remaining > 0 or new_remaining > 0
            after_hunk_content = False
            continue
        if line.startswith("--- "):
            if header_path is None and seen_old and seen_new:
                raise PatchValidationError("多文件补丁必须带 diff --git 头，无法安全切分段落")
            if seen_old:
                raise PatchValidationError("diff 段落出现重复的 --- 标记")
            seen_old = True
            old_marker = _parse_marker_path(line, "--- ", where="---")
            after_hunk_content = False
            continue
        if line.startswith("+++ "):
            if seen_new:
                raise PatchValidationError("diff 段落出现重复的 +++ 标记")
            seen_new = True
            new_marker = _parse_marker_path(line, "+++ ", where="+++")
            after_hunk_content = False
            continue
        if line.startswith("\\") and after_hunk_content:
            # hunk 结束后仍可能出现 `\ No newline...`（例如最后一条是上下文行）。
            continue
        if line.startswith(
            (
                "new file mode ",
                "deleted file mode ",
                "old mode ",
                "new mode ",
                "similarity index ",
                "rename from ",
                "rename to ",
                "index ",
                "Binary files ",
            )
        ):
            if line.startswith(("rename from ", "rename to ", "similarity index ")):
                raise PatchValidationError("暂不支持重命名 diff（Phase 12 再评估）")
            after_hunk_content = False
            continue
        if not line.strip():
            after_hunk_content = False
            continue
        raise PatchValidationError(f"无法识别的 diff 行: {line[:80]!r}")
    # 截断的 hunk（内容行数少于 @@ 头声明）不在写入器里判死：保留原文，让官方
    # harness 的 git apply 失败归因为 PATCH_APPLY_FAILED，而不是把结构判定从官方
    # 手里抢过来。这里的行数计数只用于可靠地切分 hunk 边界。
    if seen_old != seen_new:
        raise PatchValidationError("diff 段落的 --- / +++ 标记不完整")
    if header_path is None:
        if old_marker is not None and new_marker is not None and old_marker != new_marker:
            raise PatchValidationError(
                f"无 diff --git 头的补丁不支持重命名: {old_marker!r} -> {new_marker!r}"
            )
        resolved = old_marker if old_marker is not None else new_marker
        if resolved is None:
            raise PatchValidationError("diff 缺少 --- / +++ 路径标记")
        path = resolved
    else:
        if old_marker is not None and old_marker != header_path:
            raise PatchValidationError(
                f"--- 标记路径与 diff --git 不一致: {old_marker!r} != {header_path!r}"
            )
        if new_marker is not None and new_marker != header_path:
            raise PatchValidationError(
                f"+++ 标记路径与 diff --git 不一致: {new_marker!r} != {header_path!r}"
            )
        path = header_path
    if not has_body and not any(
        line.startswith(
            (
                "new file mode ",
                "deleted file mode ",
                "old mode ",
                "new mode ",
            )
        )
        for line in lines
    ):
        raise PatchValidationError(f"diff 段落没有 hunk 或 mode 变更: {path!r}")
    return path, list(lines)


def _split_sections(text: str) -> list[list[str]]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts:
        marker_start = next(
            (index for index, line in enumerate(lines) if line.startswith("--- ")), None
        )
        if marker_start is None:
            raise PatchValidationError(
                "不是 git diff：缺少 'diff --git' 或 '--- a/...' 头"
                "（模型输出可能只是代码片段或空文本）"
            )
        preamble = [line for line in lines[:marker_start] if line.strip()]
        if preamble:
            raise PatchValidationError("diff 之前存在多余内容，拒绝写入")
        return [lines[marker_start:]]
    preamble = [line for line in lines[: starts[0]] if line.strip()]
    if preamble:
        raise PatchValidationError("diff 之前存在多余内容，拒绝写入")
    sections: list[list[str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        sections.append(lines[start:end])
    return sections


def normalize_patch(patch: str) -> str:
    """规范化并校验模型补丁；返回可直接写进官方 jsonl 的文本。

    规范化只做**不改变语义**的事：CRLF→LF、去掉 diff 前的杂物、保证单个结尾换行、
    剔除 ``tests/_codepilot_repro/`` 段落。缩进、行尾空白、hunk 内容原样保留——
    补丁的空白本身是内容，重排会让它 apply 失败。
    """
    if not isinstance(patch, str):
        raise PatchValidationError("patch 必须是字符串")
    if "\x00" in patch:
        raise PatchValidationError("patch 不能含 NUL")
    if not patch.strip():
        raise PatchValidationError("patch 为空")
    text = patch.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[list[str]] = []
    dropped: list[str] = []
    for section in _split_sections(text):
        path, _ = _parse_section(section)
        if _is_reproduction_path(path):
            dropped.append(path)
            continue
        kept.append(section)
    if not kept:
        raise PatchValidationError(
            "补丁只包含 CodePilot 复现测试文件（tests/_codepilot_repro/），已全部排除"
        )
    return "\n".join("\n".join(section) for section in kept).rstrip("\n") + "\n"


def patch_files(patch: str) -> tuple[str, ...]:
    """按出现顺序返回补丁修改的文件（应先规范化；这里也会重新校验路径安全）。"""
    if not patch.strip():
        raise PatchValidationError("patch 为空")
    text = patch.replace("\r\n", "\n").replace("\r", "\n")
    files: list[str] = []
    for section in _split_sections(text):
        path, _ = _parse_section(section)
        files.append(path)
    return tuple(files)
