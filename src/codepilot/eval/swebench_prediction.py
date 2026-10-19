"""官方 ``predictions.jsonl`` 写入器（E-15）。

SWE-bench 官方 harness 只认三字段：
``instance_id`` / ``model_name_or_path`` / ``model_patch``。这个模块负责把内部推理
结果安全地变成那个文件，并回答四个在真实实验里一定会被问的问题：

1. **哪些实例没有可评测补丁？** 空 patch、非法 patch、模型输出坏掉、流程停止、
   环境错误、预算耗尽都不是"补丁失败"的同一种东西。它们有自己的状态
   （:class:`PredictionStatus`），默认**不写进官方 jsonl**，但一定进 manifest；
   把 ENV_ERROR 写成"resolved=false"会污染模型能力的分母。
2. **写出去的那一行是否安全？** 模型补丁可能带绝对路径、``..``、``.git/``、
   ``eval/``（评测目录）或 Phase 6 的复现测试文件。这里重新解析 diff 并拒绝/剔除，
   而不是相信上游 PatchValidator "应该已经看过了"。
3. **中断后能否接着写？** manifest 记录 run_id、模型身份、数据集 revision、每个
   instance 的状态与 patch hash。恢复时先核对身份，再按 instance_id 去重覆盖，
   顺序稳定（升序），文件用同目录 ``*.tmp`` + ``os.replace`` 原子替换。
4. **文件是否被动过？** :meth:`PredictionWriter.verify_integrity` 用 manifest 里的
   sha256/字节数核对 JSONL；对不上就明确失败，不把损坏的预测送进官方 harness。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from enum import StrEnum
from json import JSONDecodeError
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from codepilot.eval.swebench_task import SWEBenchDatasetRef, validate_instance_id

__all__ = [
    "PatchValidationError",
    "PredictionEntry",
    "PredictionError",
    "PredictionIntegrityError",
    "PredictionManifest",
    "PredictionManifestEntry",
    "PredictionRecord",
    "PredictionStatus",
    "PredictionWriter",
    "normalize_patch",
    "patch_files",
    "patch_sha256",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
#: ``@@ -old_start,old_count +new_start,new_count @@ [context]``。omit 的 count=1。
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
#: Phase 6 的复现测试目录。DA-009 的交付策略是"它不进交付补丁"；模型补丁同理，
#: 否则官方 harness 会把 agent 自造的复现测试也一起应用，污染评测现场。
_REPRODUCTION_DIR = "_codepilot_repro"
#: 评测目录必须拒绝：它不属于任务仓库，且可能含 gold/test 内容。
_DENIED_PATH_PARTS = frozenset({".git", "eval"})
_SAFE_MODEL_NAME = re.compile(r"^[^\x00-\x1f\x7f]+$")


class PredictionError(ValueError):
    """预测写入/恢复/校验失败。"""


class PatchValidationError(PredictionError):
    """模型补丁不是可安全写出的 git diff。"""


class PredictionIntegrityError(PredictionError):
    """既有预测文件/manifest 不完整或身份不符——拒绝静默沿用。"""


class PredictionStatus(StrEnum):
    """一条推理结果在预测文件里的结局。

    这不是"成功/失败"二元组：官方 harness 对空 patch 和解析失败的处理不同，
    报告里的 evaluation coverage 也必须能分开统计。
    """

    PATCH_READY = "PATCH_READY"
    EMPTY_PATCH = "EMPTY_PATCH"
    INVALID_PATCH = "INVALID_PATCH"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    WORKFLOW_STOPPED = "WORKFLOW_STOPPED"
    ENV_ERROR = "ENV_ERROR"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

    @property
    def records_patch(self) -> bool:
        """该状态是否可能携带补丁文本（PATCH_READY 必带，INVALID_PATCH 可带原文）。"""
        return self in {PredictionStatus.PATCH_READY, PredictionStatus.INVALID_PATCH}

    @property
    def writable(self) -> bool:
        """默认写进官方 jsonl 的状态。EMPTY_PATCH 需要显式 ``include_empty=True``。"""
        return self is PredictionStatus.PATCH_READY

    @property
    def retryable(self) -> bool:
        """同一 run 内是否可能在后续尝试中被覆盖成更好/更差的结果。"""
        return self in {
            PredictionStatus.PATCH_READY,
            PredictionStatus.EMPTY_PATCH,
            PredictionStatus.INVALID_PATCH,
            PredictionStatus.MODEL_OUTPUT_INVALID,
        }


class PredictionRecord(BaseModel):
    """官方 jsonl 的一行：**只有**三个 key，多一个都会被 Loader/harness 判坏。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    model_name_or_path: str
    model_patch: str

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        return validate_instance_id(value)

    @field_validator("model_name_or_path")
    @classmethod
    def _model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_name_or_path 不能为空")
        if _SAFE_MODEL_NAME.fullmatch(value) is None:
            raise ValueError("model_name_or_path 不能含控制字符")
        return value

    @field_validator("model_patch")
    @classmethod
    def _patch(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("model_patch 不能含 NUL")
        return value


class PredictionEntry(BaseModel):
    """内部记录：状态 + 补丁 + hash + 来源 run 身份。

    ``patch`` 存的是**已经规范化**的文本（PATCH_READY 由 writer 再校验一次）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    status: PredictionStatus
    patch: str = ""
    run_id: str
    model_name_or_path: str
    patch_sha256: str | None = None
    error_kind: str | None = None
    error_detail: str | None = None
    source: str | None = None

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        return validate_instance_id(value)

    @field_validator("run_id")
    @classmethod
    def _run(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("run_id 不能为空")
        if "\x00" in value:
            raise ValueError("run_id 不能含 NUL")
        return value

    @field_validator("model_name_or_path")
    @classmethod
    def _model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_name_or_path 不能为空")
        if _SAFE_MODEL_NAME.fullmatch(value) is None:
            raise ValueError("model_name_or_path 不能含控制字符")
        return value

    @field_validator("patch_sha256")
    @classmethod
    def _patch_hash(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("patch_sha256 必须是 sha256")
        return value

    @field_validator("error_detail", "source")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("文本字段不能含 NUL")
        return value

    @model_validator(mode="after")
    def _status_invariants(self) -> PredictionEntry:
        """状态 → patch 形态的硬约束（状态机的核心）。

        * PATCH_READY：必须有非空 patch，hash 必须与内容一致（省略时自动补）；
        * EMPTY_PATCH：patch 必须为空；
        * INVALID_PATCH：可带被拒原文，也可只有错误信息；
        * 其余失败状态：不允许携带 patch，避免把未验证内容误写进官方文件。
        """
        if self.status is PredictionStatus.PATCH_READY:
            if not self.patch.strip():
                raise ValueError("PATCH_READY 必须带非空 patch")
            expected = patch_sha256(self.patch)
            if self.patch_sha256 is None:
                object.__setattr__(self, "patch_sha256", expected)
            elif self.patch_sha256 != expected:
                raise ValueError("patch_sha256 与 patch 内容不符")
            return self
        if self.status is PredictionStatus.EMPTY_PATCH:
            if self.patch:
                raise ValueError("EMPTY_PATCH 的 patch 必须为空")
            if self.patch_sha256 is not None:
                raise ValueError("EMPTY_PATCH 不应有 patch_sha256")
            return self
        if self.status is PredictionStatus.INVALID_PATCH:
            # 可能带被拒原文，也可能只有错误信息（断点恢复时原文在 artifact 里，
            # manifest 只保留当时的 hash 作为证据指针）。
            if self.patch:
                invalid_hash = patch_sha256(self.patch)
                if self.patch_sha256 is None:
                    object.__setattr__(self, "patch_sha256", invalid_hash)
                elif self.patch_sha256 != invalid_hash:
                    raise ValueError("INVALID_PATCH 的 patch_sha256 与 patch 内容不符")
            return self
        if self.patch:
            raise ValueError(f"{self.status.value} 不应携带 patch")
        if self.patch_sha256 is not None:
            raise ValueError(f"{self.status.value} 不应有 patch_sha256")
        return self

    @classmethod
    def patch_ready(
        cls,
        *,
        instance_id: str,
        run_id: str,
        model_name_or_path: str,
        patch: str,
        source: str | None = None,
    ) -> PredictionEntry:
        """便捷构造：可写出的补丁。"""
        return cls(
            instance_id=instance_id,
            status=PredictionStatus.PATCH_READY,
            patch=patch,
            run_id=run_id,
            model_name_or_path=model_name_or_path,
            source=source,
        )

    @classmethod
    def not_ready(
        cls,
        *,
        instance_id: str,
        run_id: str,
        model_name_or_path: str,
        status: PredictionStatus,
        patch: str = "",
        error_kind: str | None = None,
        error_detail: str | None = None,
        source: str | None = None,
    ) -> PredictionEntry:
        """便捷构造：不可写出/不该写出的结局（空 patch、非法、env 错误……）。"""
        if status is PredictionStatus.PATCH_READY:
            raise ValueError("PATCH_READY 请使用 patch_ready()")
        return cls(
            instance_id=instance_id,
            status=status,
            patch=patch,
            run_id=run_id,
            model_name_or_path=model_name_or_path,
            error_kind=error_kind,
            error_detail=error_detail,
            source=source,
        )

    @classmethod
    def from_inference(
        cls,
        *,
        instance_id: str,
        run_id: str,
        model_name_or_path: str,
        status: PredictionStatus,
        patch: str = "",
        error_kind: str | None = None,
        error_detail: str | None = None,
        source: str | None = None,
    ) -> PredictionEntry:
        """显式全字段构造（status 必填，模块不替调用方猜测结局）。"""
        return cls(
            instance_id=instance_id,
            status=status,
            patch=patch,
            run_id=run_id,
            model_name_or_path=model_name_or_path,
            error_kind=error_kind,
            error_detail=error_detail,
            source=source,
        )


class PredictionManifestEntry(BaseModel):
    """manifest 里每个 instance 的账目：状态、patch hash、来源 run。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    status: PredictionStatus
    patch_sha256: str | None = None
    run_id: str
    model_name_or_path: str
    written: bool
    error_kind: str | None = None

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        return validate_instance_id(value)

    @field_validator("patch_sha256")
    @classmethod
    def _hash(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("patch_sha256 必须是 sha256")
        return value


class PredictionManifest(BaseModel):
    """一次预测写出的完整账本；与官方 jsonl 一起落盘，用于恢复与完整性校验。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    run_id: str
    model_name_or_path: str
    dataset_name: str
    dataset_revision: str
    harness_version: str
    predictions_file: str
    predictions_sha256: str
    predictions_bytes: int
    include_empty: bool
    written: int
    skipped: int
    overwrites: int
    status_counts: dict[str, int]
    entries: tuple[PredictionManifestEntry, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("prediction manifest schema_version 必须是整数")
        if value != 1:
            raise ValueError(f"不支持的 prediction manifest schema_version: {value}")
        return value

    @field_validator("predictions_sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("predictions_sha256 必须是 sha256")
        return value

    @field_validator("predictions_bytes", "written", "skipped", "overwrites", mode="before")
    @classmethod
    def _counts(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("manifest 计数必须是非负整数")
        if value < 0:
            raise ValueError("manifest 计数不能为负")
        return value

    @model_validator(mode="after")
    def _check(self) -> PredictionManifest:
        if self.written + self.skipped != len(self.entries):
            raise ValueError("written + skipped 必须等于 entries 数量")
        if self.written != sum(1 for entry in self.entries if entry.written):
            raise ValueError("written 与 entries[].written 不一致")
        if len({entry.instance_id for entry in self.entries}) != len(self.entries):
            raise ValueError("manifest entries 含重复 instance_id")
        return self


# ---------------------------------------------------------------------------
# 补丁解析 / 规范化 / 安全校验
# ---------------------------------------------------------------------------


def patch_sha256(patch: str) -> str:
    """补丁内容摘要。调用方应先 :func:`normalize_patch` 再算，保证同内容同 hash。"""
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


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


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, payload: bytes) -> None:
    """同目录临时文件 + ``os.replace``：读者要么看到旧文件，要么看到新文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)  # noqa: PTH105 - 契约要求的就是 os.replace 语义
    except OSError as exc:
        raise PredictionError(f"原子写失败 {path}: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_text(path: Path, *, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PredictionIntegrityError(f"无法读取{what}: {path}") from exc


def _manifest_entry(entry: PredictionEntry, *, written: bool) -> PredictionManifestEntry:
    return PredictionManifestEntry(
        instance_id=entry.instance_id,
        status=entry.status,
        patch_sha256=entry.patch_sha256,
        run_id=entry.run_id,
        model_name_or_path=entry.model_name_or_path,
        written=written,
        error_kind=entry.error_kind,
    )


class PredictionWriter:
    """把内部结果写成官方 predictions.jsonl（可恢复、原子、去重）。"""

    def __init__(
        self,
        path: Path,
        *,
        model_name_or_path: str,
        run_id: str,
        dataset: SWEBenchDatasetRef,
        allow_existing: bool = True,
        include_empty: bool = False,
    ) -> None:
        self.path = Path(path)
        self.manifest_path = self.path.with_suffix(".manifest.json")
        self.model_name_or_path = model_name_or_path
        self.run_id = run_id
        self.dataset = dataset
        self.allow_existing = allow_existing
        self._include_empty = include_empty
        self._lock = threading.Lock()
        self._entries: dict[str, PredictionEntry] = {}
        self._overwrites = 0
        self._last_manifest: PredictionManifest | None = None
        if not Path(self.path).name:
            raise PredictionError(f"预测文件路径非法: {path!r}")
        if self._exists_any():
            if not allow_existing:
                raise PredictionError(f"预测文件已存在且 allow_existing=False: {self.path}")
            self._resume()

    # ------------------------------------------------------------------ 属性

    @property
    def include_empty(self) -> bool:
        return self._include_empty

    @property
    def entries(self) -> tuple[PredictionEntry, ...]:
        with self._lock:
            return tuple(sorted(self._entries.values(), key=lambda item: item.instance_id))

    def counts(self) -> dict[str, int]:
        """按状态统计（包含 manifest 会记录但默认不写出的条目）。"""
        with self._lock:
            return _status_counts(self._entries.values())

    def _exists_any(self) -> bool:
        return self.path.exists() or self.manifest_path.exists()

    # ------------------------------------------------------------------ 写入

    def add(self, entry: PredictionEntry) -> None:
        """登记一条结果；同一 instance 后写覆盖，并计入 overwrites。"""
        prepared = self._prepare(entry)
        with self._lock:
            if prepared.instance_id in self._entries:
                self._overwrites += 1
            self._entries[prepared.instance_id] = prepared

    def write(self, *, include_empty: bool | None = None) -> PredictionManifest:
        """原子写 jsonl + manifest；行按 instance_id 升序，重复 add 已被去重。"""
        effective = self._include_empty if include_empty is None else include_empty
        with self._lock:
            entries = sorted(self._entries.values(), key=lambda item: item.instance_id)
            records: list[PredictionRecord] = []
            for entry in entries:
                if entry.status is PredictionStatus.PATCH_READY:
                    records.append(
                        PredictionRecord(
                            instance_id=entry.instance_id,
                            model_name_or_path=entry.model_name_or_path,
                            model_patch=entry.patch,
                        )
                    )
                elif entry.status is PredictionStatus.EMPTY_PATCH and effective:
                    records.append(
                        PredictionRecord(
                            instance_id=entry.instance_id,
                            model_name_or_path=entry.model_name_or_path,
                            model_patch="",
                        )
                    )
            payload = _encode_records(records)
            _atomic_write(self.path, payload)
            written_ids = {record.instance_id for record in records}
            manifest = PredictionManifest(
                run_id=self.run_id,
                model_name_or_path=self.model_name_or_path,
                dataset_name=self.dataset.name,
                dataset_revision=self.dataset.revision,
                harness_version=self.dataset.harness_version,
                predictions_file=self.path.name,
                predictions_sha256=hashlib.sha256(payload).hexdigest(),
                predictions_bytes=len(payload),
                include_empty=effective,
                written=len(records),
                skipped=len(entries) - len(records),
                overwrites=self._overwrites,
                status_counts=_status_counts(entries),
                entries=tuple(
                    _manifest_entry(entry, written=entry.instance_id in written_ids)
                    for entry in entries
                ),
            )
            _atomic_write(self.manifest_path, _encode_manifest(manifest))
            self._last_manifest = manifest
            # write(include_empty=True) 是持久化的模式选择：否则下一次不带参数 write()
            # 会悄悄把空 patch 行又删掉，manifest 的 include_empty 也会来回翻转。
            self._include_empty = effective
            return manifest

    def verify_integrity(self) -> PredictionManifest:
        """核对磁盘上的 jsonl 与 manifest；不符即抛 :class:`PredictionIntegrityError`。"""
        with self._lock:
            if not self.path.is_file():
                raise PredictionIntegrityError(f"预测文件不存在: {self.path}")
            if not self.manifest_path.is_file():
                raise PredictionIntegrityError(f"预测 manifest 不存在: {self.manifest_path}")
            manifest = self._read_manifest()
            self._check_manifest_binding(manifest)
            payload = self.path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != manifest.predictions_sha256:
                raise PredictionIntegrityError("predictions.jsonl sha256 与 manifest 不符")
            if len(payload) != manifest.predictions_bytes:
                raise PredictionIntegrityError("predictions.jsonl 字节数与 manifest 不符")
            records = _decode_records(payload, path=self.path)
            self._check_records_against_manifest(records, manifest)
            self._last_manifest = manifest
            return manifest

    @staticmethod
    def load(path: Path) -> list[PredictionRecord]:
        """读取官方格式 jsonl；三字段之外的内容/重复 instance 都拒绝。"""
        target = Path(path)
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise PredictionIntegrityError(f"无法读取预测文件: {target}") from exc
        records = _decode_records(payload, path=target)
        seen: set[str] = set()
        for record in records:
            if record.instance_id in seen:
                raise PredictionIntegrityError(
                    f"预测文件含重复 instance_id: {record.instance_id!r}"
                )
            seen.add(record.instance_id)
        return records

    # ------------------------------------------------------------------ 恢复

    def _resume(self) -> None:
        """从既有 manifest + jsonl 恢复；任何身份/结构不符都拒绝。"""
        if not self.path.is_file() or not self.manifest_path.is_file():
            raise PredictionIntegrityError(
                "目标位置已有预测文件但 manifest/predictions 不完整：拒绝在身份不明的"
                f"文件上续写（{self.path} / {self.manifest_path}）"
            )
        manifest = self._read_manifest()
        if not self.allow_existing:  # pragma: no cover - 构造期已拦截
            raise PredictionIntegrityError("allow_existing=False 时不得恢复")
        self._check_manifest_binding(manifest)
        if manifest.include_empty != self._include_empty:
            raise PredictionIntegrityError(
                f"恢复时 include_empty={self._include_empty} 与既有 manifest "
                f"{manifest.include_empty} 不一致；请显式传入与原 run 相同的值"
            )
        records = self.load(self.path)
        self._check_records_against_manifest(records, manifest)
        by_id = {record.instance_id: record for record in records}
        restored: dict[str, PredictionEntry] = {}
        for item in manifest.entries:
            if item.written:
                record = by_id[item.instance_id]
                restored[item.instance_id] = PredictionEntry(
                    instance_id=item.instance_id,
                    status=item.status,
                    patch=record.model_patch,
                    run_id=self.run_id,
                    model_name_or_path=self.model_name_or_path,
                    patch_sha256=item.patch_sha256,
                    error_kind=item.error_kind,
                    source="resumed",
                )
            else:
                # 未写出的条目没有官方行可读回补丁原文；状态与 hash 保留在旧 manifest
                # 里，这里恢复状态即可。INVALID_PATCH 的原文属于 artifact 证据，不靠
                # 预测文件承载。
                restored[item.instance_id] = PredictionEntry(
                    instance_id=item.instance_id,
                    status=item.status,
                    run_id=self.run_id,
                    model_name_or_path=self.model_name_or_path,
                    patch_sha256=item.patch_sha256,
                    error_kind=item.error_kind,
                    source="resumed",
                )
        self._entries = restored
        self._overwrites = manifest.overwrites
        self._last_manifest = manifest

    # ------------------------------------------------------------------ 内部

    def _prepare(self, entry: PredictionEntry) -> PredictionEntry:
        if entry.run_id != self.run_id:
            raise PredictionError(
                f"entry.run_id={entry.run_id!r} 与 writer.run_id={self.run_id!r} 不符"
            )
        if entry.model_name_or_path != self.model_name_or_path:
            raise PredictionError("entry.model_name_or_path 与 writer 的模型身份不符")
        if entry.status is not PredictionStatus.PATCH_READY:
            return entry
        try:
            normalized = normalize_patch(entry.patch)
        except PatchValidationError as exc:
            raise PredictionError(
                f"instance {entry.instance_id!r} 的补丁未通过规范化/安全校验: {exc}"
            ) from exc
        return PredictionEntry(
            instance_id=entry.instance_id,
            status=entry.status,
            patch=normalized,
            run_id=entry.run_id,
            model_name_or_path=entry.model_name_or_path,
            patch_sha256=patch_sha256(normalized),
            error_kind=entry.error_kind,
            error_detail=entry.error_detail,
            source=entry.source,
        )

    def _read_manifest(self) -> PredictionManifest:
        text = _read_text(self.manifest_path, what="预测 manifest")
        try:
            payload = json.loads(text)
        except JSONDecodeError as exc:
            raise PredictionIntegrityError(
                f"预测 manifest 不是合法 JSON: {self.manifest_path}"
            ) from exc
        if not isinstance(payload, dict):
            raise PredictionIntegrityError("预测 manifest 顶层必须是对象")
        try:
            return PredictionManifest.model_validate(payload)
        except ValidationError as exc:
            raise PredictionIntegrityError(f"预测 manifest 结构无效: {exc}") from exc

    def _check_manifest_binding(self, manifest: PredictionManifest) -> None:
        mismatches: list[str] = []
        if manifest.run_id != self.run_id:
            mismatches.append(f"run_id {manifest.run_id!r} != {self.run_id!r}")
        if manifest.model_name_or_path != self.model_name_or_path:
            mismatches.append(
                f"model {manifest.model_name_or_path!r} != {self.model_name_or_path!r}"
            )
        if manifest.dataset_name != self.dataset.name:
            mismatches.append(f"dataset {manifest.dataset_name!r} != {self.dataset.name!r}")
        if manifest.dataset_revision != self.dataset.revision:
            mismatches.append(
                f"revision {manifest.dataset_revision!r} != {self.dataset.revision!r}"
            )
        if manifest.harness_version != self.dataset.harness_version:
            mismatches.append(
                f"harness {manifest.harness_version!r} != {self.dataset.harness_version!r}"
            )
        if manifest.predictions_file != self.path.name:
            mismatches.append(
                f"predictions_file {manifest.predictions_file!r} != {self.path.name!r}"
            )
        if mismatches:
            raise PredictionIntegrityError(
                "既有预测文件身份与本次 run 不符（不得静默沿用）: " + "; ".join(mismatches)
            )

    def _check_records_against_manifest(
        self,
        records: Sequence[PredictionRecord],
        manifest: PredictionManifest,
    ) -> None:
        written = [entry for entry in manifest.entries if entry.written]
        if len(records) != len(written):
            raise PredictionIntegrityError(
                f"predictions.jsonl 行数 {len(records)} != manifest written={len(written)}"
            )
        for expected, record in zip(written, records, strict=True):
            if expected.instance_id != record.instance_id:
                raise PredictionIntegrityError(
                    "predictions.jsonl 行顺序/instance_id 与 manifest 不一致: "
                    f"{record.instance_id!r} != {expected.instance_id!r}"
                )
            if record.model_name_or_path != manifest.model_name_or_path:
                raise PredictionIntegrityError(
                    f"{record.instance_id}: model_name_or_path 与 manifest 不一致"
                )
            if expected.status is PredictionStatus.PATCH_READY:
                if not record.model_patch:
                    raise PredictionIntegrityError(
                        f"{record.instance_id}: PATCH_READY 行却是空补丁"
                    )
                if expected.patch_sha256 != patch_sha256(record.model_patch):
                    raise PredictionIntegrityError(f"{record.instance_id}: patch_sha256 与内容不符")
                try:
                    normalized = normalize_patch(record.model_patch)
                except PatchValidationError as exc:
                    raise PredictionIntegrityError(
                        f"{record.instance_id}: 已有预测补丁不再通过安全校验: {exc}"
                    ) from exc
                if normalized != record.model_patch:
                    raise PredictionIntegrityError(
                        f"{record.instance_id}: 已有预测补丁不是规范化形态"
                    )
            elif expected.status is PredictionStatus.EMPTY_PATCH:
                if record.model_patch:
                    raise PredictionIntegrityError(f"{record.instance_id}: EMPTY_PATCH 行却带补丁")
                if expected.patch_sha256 is not None:
                    raise PredictionIntegrityError(
                        f"{record.instance_id}: EMPTY_PATCH 不应有 patch_sha256"
                    )
            else:
                raise PredictionIntegrityError(
                    f"{record.instance_id}: 状态 {expected.status.value} 不应写进官方预测文件"
                )


def _status_counts(entries: Iterable[PredictionEntry]) -> dict[str, int]:
    counter = Counter(entry.status.value for entry in entries)
    return {status.value: counter.get(status.value, 0) for status in PredictionStatus}


def _encode_records(records: Sequence[PredictionRecord]) -> bytes:
    lines = [
        json.dumps(record.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        for record in records
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def _encode_manifest(manifest: PredictionManifest) -> bytes:
    payload = manifest.model_dump(mode="json")
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def _decode_records(payload: bytes, *, path: Path) -> list[PredictionRecord]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PredictionIntegrityError(f"预测文件不是 UTF-8: {path}") from exc
    if not text.strip():
        return []
    records: list[PredictionRecord] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise PredictionIntegrityError(
                f"{path} 第 {line_number} 行为空；官方 predictions.jsonl 不允许空行"
            )
        try:
            data: Any = json.loads(line)
        except JSONDecodeError as exc:
            raise PredictionIntegrityError(f"{path} 第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise PredictionIntegrityError(f"{path} 第 {line_number} 行必须是对象")
        try:
            records.append(PredictionRecord.model_validate(data))
        except ValidationError as exc:
            raise PredictionIntegrityError(
                f"{path} 第 {line_number} 行字段无效（官方格式只允许 instance_id / "
                f"model_name_or_path / model_patch）: {exc}"
            ) from exc
    return records
