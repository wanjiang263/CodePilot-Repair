"""E-12 / E-13：Gold 隔离、Gold Sentinel、Import Graph 与工具层越界审计。

这个模块回答一个问题：**推理侧到底有没有可能在结构上看见答案**。它不检查"模型
有没有猜到答案"——那需要人读；它检查的是可以机械判定的边界：

* **Gold Sentinel 唯一且只在评测侧。** 评测侧对象/文件里植入一个随机 token，
  推理侧的任何可观测面（Prompt、State、Deps、工具输出、Artifact、Checkpoint、
  恢复现场）都不应出现它。token 随机生成，所以"恰好没出现"不是靠约定，而是
  因为推理侧根本没有来源。
* **workspace 与 git 历史只到 base。** 真实仓库里 base 之后很可能就有修复提交；
  ``fetch`` 模式只拉 base 的祖先，但如果对象库里残留了未来提交（reflog、
  未清理的对象、共享对象库的 worktree），``git show <sha>:file`` 就能读到答案。
  这里直接扫**对象库里的全部 commit 对象**，而不是只看 refs —— 只看 refs 会漏掉
  "引用已删、对象还在"的形态。
* **工具层读不到评测目录。** 路径穿越、``..``、符号链接/junction、绝对路径
  都试一遍；失败即可，成功一次就意味着模型能自己把答案取出来。
* **推理依赖闭包不含评测模块。** 这是唯一能在**运行前**发现"推理入口静态 import
  了 EvaluationTask / Gold Loader / 官方 Harness"的检查；否则泄漏会在几百次运行
  之后表现为"成功率莫名偏高"。

失败语义统一为 **fail closed**：任何一项审计不通过都抛 :class:`IsolationViolation`，
由调用方拒绝开跑/拒绝写报告。异常消息里**不出现原始 token**（只出现脱敏指纹），
因为异常会被写进日志、artifact 和报告，那本身就是一条泄漏路径。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
from collections.abc import Container, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from codepilot.eval.synthetic import INFERENCE_DENIED_DIRECTORIES
from codepilot.fsutil import is_link
from codepilot.repo.gitcmd import GitError, run_git
from codepilot.tools.registry import ToolRegistry

__all__ = [
    "DYNAMIC_IMPORT_MARKER",
    "EVALUATION_DIRECTORY_NAMES",
    "EVALUATION_FILE_NAMES",
    "EVALUATION_MARKERS",
    "GOLD_SENTINEL",
    "GOLD_SENTINEL_FORMAT",
    "GOLD_SENTINEL_PATTERN",
    "GOLD_SENTINEL_PREFIX",
    "PARSE_ERROR_MARKER",
    "GoldSentinel",
    "IsolationViolation",
    "assert_inference_import_closure",
    "audit_artifact_tree",
    "audit_checkpoint",
    "audit_git_history",
    "audit_inference_request",
    "audit_tool_registry",
    "audit_workspace",
    "build_import_graph",
    "dynamic_import_modules",
    "find_sentinels",
    "import_closure",
    "redact_sentinel",
]

#: Sentinel 的固定前缀。**唯一**的判别依据：只要推理侧出现这个前缀，就说明
#: 评测侧的 token 流进了推理侧，具体是哪一个 token 已经不重要了。
GOLD_SENTINEL_PREFIX = "CP-GOLD-SENTINEL"

#: 契约第 6 节写的 token 形态。真正生成由 :meth:`GoldSentinel.generate` 负责
#: （两段都带 128 bit 随机后缀，不能只靠 run_id/instance_id 拼）；
#: 这个常量只用于文档与报告里的格式说明。
GOLD_SENTINEL_FORMAT = f"{GOLD_SENTINEL_PREFIX}-<run_id>-<instance_id>"

#: 兼容契约里的命名：``GOLD_SENTINEL`` 指这个哨兵机制的前缀/格式，而不是一个
#: 固定 token——固定 token 等于让推理侧可以"猜出自己应该看不见什么"。
GOLD_SENTINEL = GOLD_SENTINEL_FORMAT

#: 一个合法的 sentinel。生成侧只用 ``[A-Za-z0-9_.-]``；保留宽一点的字符集是为了
#: 让"人工构造的哨兵"也能被同一套检查抓到。
GOLD_SENTINEL_PATTERN = re.compile(r"CP-GOLD-SENTINEL-[A-Za-z0-9_.-]{4,}")

_SENTINEL_TOKEN = re.compile(rf"^{GOLD_SENTINEL_PREFIX}-[A-Za-z0-9_.-]{{8,}}$")
_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

#: 评测目录名。``eval`` 是合成 fixture 的布局，``evaluation`` 是 SWE-bench / Phase 10
#: 的产物布局；两者出现在推理 workspace 里都意味着边界破了。
EVALUATION_DIRECTORY_NAMES: frozenset[str] = frozenset({"eval", "evaluation"})

#: 评测侧文件名。推理 workspace / artifact 里出现它们，等同于把 gold/test patch 摆在
#: 模型面前，即使内容被截断也一样。
EVALUATION_FILE_NAMES: frozenset[str] = frozenset(
    {
        "gold.patch",
        "test.patch",
        "gold.diff",
        "test.diff",
        "gold_patch.diff",
        "test_patch.diff",
        "tests.json",
    }
)

#: 一旦出现在 **推理侧可见文本**（prompt / artifact / checkpoint）里就说明评测内容
#: 越界的结构化标记。刻意用精确的字段名而不是 "evaluation" 这类普通单词：后者在
#: 正常的代码与讨论里到处都是，误报率高了检查器就会被忽略。
EVALUATION_MARKERS: tuple[str, ...] = (
    "fail_to_pass",
    "pass_to_pass",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "gold_patch",
    "gold.patch",
    "test_patch",
    "test.patch",
    "tests.json",
    "SWEBenchEvaluationTask",
    "EvaluationTask(",
    "codepilot.eval.swebench",
    "codepilot.sandbox.swebench_env",
)

#: 静态解析不了（非字面量参数）的动态 import 调用点。它本身不是违规——像
#: :func:`codepilot.graph.checkpoint.state_allowed_modules` 那样按注解取模块名的
#: 调用是完全合法的——所以闭包检查只把它记录下来，供报告显示"有 N 处无法静态证明"。
DYNAMIC_IMPORT_MARKER = "<dynamic-import>"

#: 无法解析的模块（语法错误、读不出来）。**在推理闭包里出现即为违规**：无法解析
#: 的文件无法证明它没有 import 评测模块，fail closed。
PARSE_ERROR_MARKER = "<unparsable-module>"

#: 递归扫描文件时的块大小。用分块搜索而不是 ``read_text``：artifact/checkpoint 里
#: 有二进制（sqlite、junit），按 UTF-8 读会抛错，而"读不出来所以跳过"正是泄漏能
#: 藏住的地方。
_SCAN_CHUNK = 1 << 20

#: 工具注册表里一旦出现这些片段，就说明可能多了一个"读评测内容"的工具。命中不必
#: 精确——评测读取工具的名字里几乎一定带着这些词之一。
_FORBIDDEN_TOOL_FRAGMENTS: tuple[str, ...] = (
    "eval",
    "gold",
    "oracle",
    "swebench",
    "harness",
    "hidden",
)

#: 路径穿越尝试。``read_file`` / ``search_code`` 每一次都必须失败；成功一次就是
#: 工具层把评测目录交出去了。父级目录用 ``../`` 与反斜杠两种写法，Windows 上
#: ``PureWindowsPath`` 与 ``PurePosixPath`` 的判定路径不同，漏一种就漏一种逃逸。
_EVALUATION_READ_ATTEMPTS: tuple[str, ...] = (
    "eval/gold.patch",
    "eval/test.patch",
    "eval/tests.json",
    "evaluation/gold.patch",
    "evaluation/test.patch",
    "evaluation/tests.json",
    "../eval/gold.patch",
    "../evaluation/test.patch",
    "..\\evaluation\\test.patch",
    "../../evaluation/gold.patch",
    "./eval/../eval/gold.patch",
)


class IsolationViolation(RuntimeError):  # noqa: N818 - 契约固定名，调用方按它捕获
    """一项隔离审计不通过。**必须**让调用方失败，不允许降级继续跑。

    ``kind`` 是稳定的机器可读分类（报表分组、CI 过滤都用它）；``where`` 指出
    违规发生在哪个面（prompt / workspace / git / tools / artifact / checkpoint /
    import-graph）。``detail`` 已经脱敏：构造时会把其中出现的 sentinel 原文替换成
    指纹，因为异常字符串会进入日志与 artifact。
    """

    def __init__(self, kind: str, detail: str, *, where: str = "") -> None:
        self.kind = kind
        self.where = where
        self.detail = _redact_text(detail)
        location = f" @ {where}" if where else ""
        super().__init__(f"[{kind}]{location} {self.detail}")

    def event(self) -> dict[str, str]:
        """脱敏后的审计事件，可直接写进 events.jsonl / 报告。"""
        return {"kind": self.kind, "where": self.where, "detail": self.detail}


def _raise(kind: str, detail: str, *, where: str = "") -> NoReturn:
    raise IsolationViolation(kind, detail, where=where)


def _redact_text(text: str) -> str:
    return GOLD_SENTINEL_PATTERN.sub(lambda match: redact_sentinel(match.group(0)), text)


def redact_sentinel(token: str) -> str:
    """把 token 换成不可逆指纹。日志/异常里只允许出现这个形态。"""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"{GOLD_SENTINEL_PREFIX}-<redacted:{digest}>"


def find_sentinels(text: str) -> tuple[str, ...]:
    """文本里出现的所有 sentinel 原文（去重保序）。

    这个函数的返回值**只允许在内存里比较**：它的用途是"有没有"和"是哪一类"，
    把它写进报告就等于把 token 发出去。
    """
    return tuple(dict.fromkeys(GOLD_SENTINEL_PATTERN.findall(text)))


def _assert_no_sentinel(text: str, *, where: str) -> None:
    """文本里出现任何 sentinel 前缀都算违规（不必是本次生成的那一个）。

    只看"本次 token"会漏掉一种真实形态：上一次运行的 artifact/checkpoint 没清，
    这次读回来了。哨兵前缀是全局唯一的判别依据，任何一条都不该出现在推理侧。
    """
    found = find_sentinels(text)
    if found:
        fingerprints = [redact_sentinel(token) for token in found]
        _raise(
            "gold_sentinel_in_inference_side",
            f"推理侧文本里出现 Gold Sentinel: {fingerprints}",
            where=where,
        )


def _assert_no_evaluation_markers(text: str, *, where: str) -> None:
    hits = sorted({marker for marker in EVALUATION_MARKERS if marker in text})
    if hits:
        _raise(
            "evaluation_content_in_inference_side",
            f"推理侧文本里出现评测侧标记: {hits}",
            where=where,
        )


# --------------------------------------------------------------------- Sentinel


class GoldSentinel:
    """只在评测侧存在的唯一 token。

    token 由随机字节生成而不是"run id + 实例号"这种可猜形式：可猜的 token 等于
    让推理侧能通过"猜一个"来伪造"我没看到答案"的证明。token 随机之后，
    ``token in text`` 就等于"评测侧内容真的流过来了"。
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or _SENTINEL_TOKEN.fullmatch(token) is None:
            _raise(
                "invalid_sentinel",
                f"sentinel 必须匹配 {_SENTINEL_TOKEN.pattern}，收到 {type(token).__name__}",
                where="GoldSentinel.__init__",
            )
        self._token = token

    @property
    def token(self) -> str:
        """原文。调用前请确认自己站在评测侧。"""
        return self._token

    def __repr__(self) -> str:
        return f"GoldSentinel({redact_sentinel(self._token)!r})"

    def __str__(self) -> str:
        return redact_sentinel(self._token)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GoldSentinel) and hmac_compare(self._token, other._token)

    def __hash__(self) -> int:
        return hash(self._token)

    @classmethod
    def generate(cls, *, run_id: str | None = None, instance_id: str | None = None) -> GoldSentinel:
        """生成一个新 token。

        ``run_id`` / ``instance_id`` 只作为**可读线索**参与拼接，长度不做保证；
        真正保证唯一性的是两段 ``secrets``（128 bit）。不传时两段都是随机的。
        """
        return cls(f"{GOLD_SENTINEL_PREFIX}-{cls._component(run_id)}-{cls._component(instance_id)}")

    @staticmethod
    def _component(value: str | None) -> str:
        if value is None:
            return secrets.token_hex(12)
        cleaned = _TOKEN_UNSAFE.sub("-", value).strip("-")[:48]
        # 线索部分被清洗后可能太短或为空（例如 run_id=".."）：补随机，不能让
        # "可读线索"削弱唯一性。
        return f"{cleaned}-{secrets.token_hex(6)}" if cleaned else secrets.token_hex(12)

    # ------------------------------------------------------------- 植入

    def plant_in_evaluation_task(self, task: Any) -> Any:
        """把 token 植入**评测侧**对象并返回副本。

        绝不允许植入推理对象：调用方拿到的是 :class:`~codepilot.eval.synthetic.InferenceTask`
        时这里会失败（它没有任何评测字段），这正是我们想要的——把误用变成一个
        立即失败的编程错误，而不是一次静默的答案泄漏。

        通过"更新映射 + 拷贝"落地，而不是先拷贝再 ``setattr``：评测对象（合成侧与
        Owner A 的 SWE-bench 侧）都可能是 pydantic ``frozen=True``，赋值会抛
        ``Instance is frozen``。``model_copy(update=...)`` 不改原对象，也不需要
        对象可写。
        """
        fields = _evaluation_fields(task)
        if not fields:
            _raise(
                "sentinel_scope",
                "只能把 Gold Sentinel 植入评测侧对象；目标对象没有任何评测字段"
                f"（{type(task).__name__}）",
                where="GoldSentinel.plant_in_evaluation_task",
            )
        updates = self._planted_updates(task, fields)
        if not updates:
            _raise(
                "sentinel_scope",
                "评测侧对象的 gold_patch/test_patch/目标测试清单都不是可写容器，无法植入",
                where="GoldSentinel.plant_in_evaluation_task",
            )
        return _copy_with_updates(task, updates)

    def _planted_updates(self, task: Any, fields: tuple[str, ...]) -> dict[str, Any]:
        """算出"哪些字段要换成带 token 的值"，不改对象。"""
        marker = f"  # {self._token}"
        updates: dict[str, Any] = {}
        for name in ("gold_patch", "test_patch"):
            if name not in fields:
                continue
            value = getattr(task, name)
            if isinstance(value, str):
                updates[name] = f"{value.rstrip()}\n{marker}\n"
        if updates:
            return updates
        # 只有目标测试清单可写时，把 token 作为清单里的最后一项。它会让清单不再是
        # 一个合法 NodeID，但本方法的用途是负控制，不是产出可执行对象。
        for name in ("fail_to_pass", "pass_to_pass"):
            if name not in fields:
                continue
            value = getattr(task, name)
            if isinstance(value, tuple):
                updates[name] = (*value, self._token)
            elif isinstance(value, list):
                updates[name] = [*value, self._token]
        return updates

    def plant_in_file(self, path: Path) -> Path:
        """把 token 写进评测侧文件（追加一行注释），返回该路径。

        只允许写评测侧文件由调用方保证——本模块无法从路径判断归属，但审计函数
        会立刻发现"推理侧出现了它"。测试用它构造"eval/gold.patch 里有哨兵、
        推理侧仍然读不到"的负控制。
        """
        target = Path(path)
        try:
            existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        except (OSError, UnicodeDecodeError) as exc:
            _raise("sentinel_plant_failed", f"无法读取待植入文件: {exc}", where=str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{existing.rstrip()}\n# {self._token}\n", encoding="utf-8", newline="\n")
        return target

    # ------------------------------------------------------------- 断言

    def assert_absent(self, text: str, *, where: str) -> None:
        """断言文本里没有 sentinel。失败即 fail closed。"""
        if not isinstance(text, str):
            _raise(
                "invalid_input",
                f"待检查内容必须是 str，收到 {type(text).__name__}",
                where=where,
            )
        if self._token in text:
            _raise(
                "gold_sentinel_in_inference_side",
                f"推理侧文本里出现本次运行的 Gold Sentinel: {redact_sentinel(self._token)}",
                where=where,
            )
        _assert_no_sentinel(text, where=where)


def hmac_compare(left: str, right: str) -> bool:
    """常量时间比较。token 比较不走 ``==`` 是为了不给"逐位猜"留时间侧信道。"""
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _evaluation_fields(task: Any) -> tuple[str, ...]:
    """对象上出现了哪些**评测侧**字段名。

    字段名取自契约（文档第 2 节）：``gold_patch`` / ``test_patch`` /
    ``fail_to_pass`` / ``pass_to_pass``。推理对象刻意不含这些，所以"没有评测字段"
    是推理对象的正常状态。
    """
    candidates = ("gold_patch", "test_patch", "fail_to_pass", "pass_to_pass")
    return tuple(name for name in candidates if hasattr(task, name))


def _copy_with_updates(task: Any, updates: Mapping[str, Any]) -> Any:
    """拷贝评测对象并应用字段更新（pydantic / dataclass / 普通对象）。

    pydantic 走 ``model_copy(update=...)``：frozen 模型也能用，且不会触碰原对象。
    dataclass 走 ``dataclasses.replace``；普通对象才用 ``deepcopy + setattr``。
    """
    copier = getattr(task, "model_copy", None)
    if callable(copier):
        return copier(update=dict(updates), deep=True)

    import copy
    import dataclasses

    if dataclasses.is_dataclass(task) and not isinstance(task, type):
        return dataclasses.replace(task, **updates)

    try:
        cloned = copy.deepcopy(task)
        for name, value in updates.items():
            setattr(cloned, name, value)
        return cloned
    except (TypeError, ValueError) as exc:  # pragma: no cover - 不可拷贝对象
        _raise(
            "sentinel_plant_failed",
            f"无法拷贝评测对象 {type(task).__name__}: {exc}",
            where="GoldSentinel.plant_in_evaluation_task",
        )


# --------------------------------------------------------------------- Prompt


def audit_inference_request(
    prompt: str,
    *,
    where: str = "prompt",
    sentinel: GoldSentinel | None = None,
) -> None:
    """审计一段即将发给模型的文本。

    三类信号都算越界：

    1. 任何 Gold Sentinel（本次的、上次残留的都算）；
    2. 评测侧结构化标记（``FAIL_TO_PASS`` / ``gold_patch`` / 评测模块名……）；
    3. 显式传入 ``sentinel`` 时，另外做一次精确匹配（更早失败、错误更具体）。

    ``where`` 用于定位（``planner`` / ``coder`` / ``diagnoser`` / ``reviewer`` /
    ``localizer`` 或 ``llm_request[3]``），写进异常与事件。
    """
    if not isinstance(prompt, str):
        _raise(
            "invalid_input",
            f"prompt 必须是 str，收到 {type(prompt).__name__}",
            where=where,
        )
    if sentinel is not None:
        sentinel.assert_absent(prompt, where=where)
    _assert_no_sentinel(prompt, where=where)
    _assert_no_evaluation_markers(prompt, where=where)


# ------------------------------------------------------------------ Workspace


def audit_workspace(
    workspace: Path,
    *,
    sentinel: GoldSentinel,
    denied_names: Iterable[str] | None = None,
) -> None:
    """审计推理 workspace：内容、目录名、链接与逃逸。

    与"一律拒绝读文件"相反，这个函数只拒绝**结构性**越界：

    * 根不是目录、根本身是符号链接/junction、路径是 UNC、路径里带 ``..``；
    * 树内出现 ``eval`` / ``evaluation`` 目录或评测侧文件名；
    * 树内出现任何链接（符号链接/junction 可以指到 workspace 外的评测目录；
      Windows 上 junction 不需要管理员权限，是最现实的逃逸手法）；
    * 任一条目的 realpath 跑出 workspace 根（链接之外的别名）；
    * 任一文件内容里出现 Gold Sentinel。

    Base 源码与 Base 已有测试**不会**被拒绝——它们没有命中任何一条。测试里同时
    验证"Base 可读"，否则这个函数可以靠"拒绝一切"通过。
    """
    raw = str(workspace)
    if _is_unc(raw):
        _raise("workspace_unc_path", f"workspace 根是 UNC 路径: {raw}", where="workspace")
    if ".." in PurePosixPath(raw.replace("\\", "/")).parts:
        _raise("workspace_path_escape", f"workspace 路径含 '..': {raw}", where="workspace")

    root = Path(workspace)
    if is_link(root):
        _raise(
            "workspace_root_is_link",
            "workspace 根本身是符号链接/junction，无法证明它指向哪里",
            where="workspace",
        )
    if not root.is_dir():
        _raise("workspace_missing", f"workspace 不存在或不是目录: {root}", where="workspace")
    resolved_root = root.resolve(strict=True)

    denied = (
        {name.casefold() for name in INFERENCE_DENIED_DIRECTORIES}
        | {name.casefold() for name in EVALUATION_DIRECTORY_NAMES}
        if denied_names is None
        else {name.casefold() for name in denied_names}
    )
    sentinel_bytes = sentinel.token.encode("utf-8")

    for current, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        base = Path(current)
        for name in sorted(dirnames):
            entry = base / name
            relative = _relative(entry, resolved_root)
            if is_link(entry):
                _raise(
                    "workspace_link",
                    f"workspace 内存在符号链接/junction，可指向评测目录: {relative}",
                    where="workspace",
                )
            if name.casefold() in denied:
                _raise(
                    "evaluation_directory_in_workspace",
                    f"workspace 内存在评测目录: {relative}",
                    where="workspace",
                )
            if not _resolved_inside(entry, resolved_root):
                _raise(
                    "workspace_path_escape",
                    f"workspace 内条目的真实路径逃出根目录: {relative}",
                    where="workspace",
                )
        for name in sorted(filenames):
            entry = base / name
            relative = _relative(entry, resolved_root)
            if is_link(entry):
                _raise(
                    "workspace_link",
                    f"workspace 内存在符号链接/junction: {relative}",
                    where="workspace",
                )
            if name.casefold() in EVALUATION_FILE_NAMES:
                _raise(
                    "evaluation_file_in_workspace",
                    f"workspace 内出现评测文件: {relative}",
                    where="workspace",
                )
            if not _resolved_inside(entry, resolved_root):
                _raise(
                    "workspace_path_escape",
                    f"workspace 内文件的真实路径逃出根目录: {relative}",
                    where="workspace",
                )
            if _file_contains(entry, sentinel_bytes):
                _raise(
                    "gold_sentinel_in_workspace",
                    f"workspace 文件内容含 Gold Sentinel: {relative}"
                    f"（指纹 {redact_sentinel(sentinel.token)}）",
                    where="workspace",
                )


# ------------------------------------------------------------------ Git 历史


def audit_git_history(workspace: Path, *, base_commit: str) -> None:
    """审计推理仓库的 git 历史是否只暴露 base 及其祖先。

    检查项按"能从哪儿读到答案"排列，任何一条命中都等于隔离破了：

    1. ``.git`` 必须是目录（worktree 的 ``.git`` 是文件，与缓存共享对象库与全部
       refs，未来提交直接可见），且不能有 ``objects/info/alternates``；
    2. base 必须能解析到；
    3. 不允许 remote（否则一次 fetch 就能把 gold 拉回来）；
    4. 不允许 tag（tag 常指向发布提交，而发布提交可能就在 base 之后）；
    5. 所有 ref 解析出的 commit 必须是 base 的祖先；
    6. **对象库里的每一个 commit 对象**都必须是 base 的祖先。这一条比 5 更强：
       "引用已删、对象还在"是最常见的半清理状态，只查 refs 会漏掉它，而
       ``git show <sha>:path`` 仍然读得到。
    7. reflog 里不允许出现 base 之外的提交（``git log -g`` 也能读到）。
    """
    if not isinstance(base_commit, str) or _SHA.fullmatch(base_commit) is None:
        _raise(
            "invalid_base_commit",
            f"base_commit 必须是 40/64 位完整 SHA，收到 {base_commit!r}",
            where="git",
        )
    root = Path(workspace)
    if not root.is_dir():
        _raise("workspace_missing", f"workspace 不存在: {root}", where="git")
    dot_git = root / ".git"
    if dot_git.is_file():
        _raise(
            "git_not_self_contained",
            "workspace 的 .git 是文件（worktree 模式），与缓存共享对象库，base 之后的提交可见",
            where="git",
        )
    if not dot_git.is_dir():
        _raise("not_a_git_workspace", f"workspace 不是 git 仓库: {root}", where="git")
    alternates = dot_git / "objects" / "info" / "alternates"
    if alternates.exists():
        _raise(
            "git_shared_object_store",
            "workspace 的对象库带 alternates，共享的对象库可能含 base 之后的提交",
            where="git",
        )

    base = _rev_parse(root, base_commit)
    if base is None:
        _raise(
            "base_commit_unreachable",
            f"base commit 在 workspace 里不可达: {base_commit}",
            where="git",
        )
    ancestors = set(_git_lines(root, ["rev-list", base]))

    remotes = _git_lines(root, ["remote"])
    if remotes:
        _raise(
            "git_remote_present",
            f"推理仓库配置了 remote（可以 fetch 回 base 之后的提交）: {remotes}",
            where="git",
        )

    for line in _git_lines(root, ["for-each-ref", "--format=%(refname) %(objectname)"]):
        name, _, oid = line.partition(" ")
        if name.startswith("refs/remotes/"):
            _raise("git_remote_ref", f"推理仓库存在远端引用: {name}", where="git")
        if name.startswith("refs/tags/"):
            _raise(
                "git_tag_present",
                f"推理仓库存在 tag（指向的发布提交可能在 base 之后）: {name}",
                where="git",
            )
        commit = _rev_parse(root, oid)
        if commit is None or commit not in ancestors:
            _raise(
                "git_ref_outside_base",
                f"引用 {name} 指向 base 之外的提交: {oid}",
                where="git",
            )

    # 对象库全量扫描：``--batch-all-objects`` 列出 loose + packed 的全部对象，
    # 包括不可达的。只对比 commit 对象——tree/blob 数量在真实仓库里是百万级。
    object_lines = _git_lines(
        root, ["cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"]
    )
    future: list[str] = []
    for line in object_lines:
        oid, _, kind = line.partition(" ")
        if kind.strip() != "commit":
            continue
        if oid not in ancestors:
            future.append(oid)
    if future:
        _raise(
            "git_future_commit_object",
            f"对象库里存在 base 之外的提交 {future[:5]}（共 {len(future)} 个）；"
            "git show / git log 能读到它们",
            where="git",
        )

    reflog = run_git(["reflog", "show", "--all", "--format=%H"], cwd=root, check=False)
    stale = sorted({oid for oid in reflog.lines if oid and oid not in ancestors})
    if stale:
        _raise(
            "git_reflog_outside_base",
            f"reflog 里存在 base 之外的提交: {stale[:5]}",
            where="git",
        )


def _rev_parse(repo: Path, rev: str) -> str | None:
    result = run_git(
        ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], cwd=repo, check=False
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha if _SHA.fullmatch(sha) is not None else None


def _git_lines(repo: Path, args: Sequence[str]) -> list[str]:
    try:
        return run_git(args, cwd=repo).lines
    except GitError as exc:
        _raise(
            "git_command_failed",
            f"git {' '.join(args)} 失败: {(exc.stderr or str(exc)).strip()[:500]}",
            where="git",
        )


# ------------------------------------------------------------- 工具注册表


def audit_tool_registry(registry: Any, *, sentinel: GoldSentinel) -> None:
    """审计工具注册表：没有评测读取工具、读不到评测目录、不夹带 sentinel。

    三条断言：

    1. **工具集合白名单**。推理侧只允许 Phase 3/5 定义的那 11 个工具（planner 6 个
       的子集 + coder 的补充）。多出来的工具就是"多一条读取面"，名字里再带
       eval/gold/oracle 一类字样更是直接判违规。
    2. **工具描述/参数 schema 不得夹带 sentinel 或评测标记**。工具描述会原样进
       prompt，一个被污染的 description 就是 prompt 泄漏。
    3. **实际尝试读取评测目录**。``read_file`` / ``search_code`` 对 ``eval/*``、
       ``../evaluation/*``、反斜杠形态、绝对路径的每一次调用都必须失败；成功一次
       即违规。成功的响应里若出现 sentinel，同样违规。
    """
    names = _registry_names(registry)
    if not names:
        _raise("tool_registry_empty", "工具注册表为空，无法证明推理侧可读范围", where="tools")

    allowed = ToolRegistry.PLANNER_NAMES | ToolRegistry.CODER_NAMES
    unknown = sorted(set(names) - allowed)
    if unknown:
        _raise(
            "unexpected_tool_registered",
            f"推理工具注册表含契约之外的（可能是评测读取）工具: {unknown}",
            where="tools",
        )
    suspicious = sorted(
        name
        for name in names
        if any(fragment in name.casefold() for fragment in _FORBIDDEN_TOOL_FRAGMENTS)
    )
    if suspicious:
        _raise(
            "evaluation_tool_registered",
            f"工具名带评测读取语义: {suspicious}",
            where="tools",
        )

    specs = _registry_specs(registry)
    for spec in specs:
        payload = _spec_payload(spec)
        # 用 ``sentinel.assert_absent`` 而不是通用前缀扫描：它先做本次 token 的精确
        # 匹配（错误信息更具体），再做前缀兜底（也抓上次运行残留的 token）。
        sentinel.assert_absent(payload, where=f"tool_spec:{getattr(spec, 'name', '?')}")
        _assert_no_evaluation_markers(payload, where=f"tool_spec:{getattr(spec, 'name', '?')}")

    for tool_name, arguments in _tool_read_attempts(sentinel):
        if tool_name not in names:
            continue
        try:
            response = _execute_tool(registry, tool_name, arguments)
        except Exception:  # 工具拒绝/抛错也算"没读到"；下面失败分支会独立判定
            continue
        payload = _response_payload(response)
        sentinel.assert_absent(payload, where=f"tool:{tool_name}")
        if not bool(getattr(response, "ok", False)):
            # 失败响应不做标记扫描：拒绝信息本身就会回显我们尝试的路径（例如
            # ``path is denied by policy: eval/gold.patch``），把它判成"评测内容
            # 泄漏"是对审计器自身输入的误报。真正要断的是"没读到"。
            continue
        if tool_name == "search_code":
            # 搜索"成功"只代表它跑完了，不代表命中了内容；只有命中才说明越界。
            matches = _search_matches(response)
            if matches:
                _raise(
                    "tool_reaches_evaluation",
                    f"search_code({arguments!r}) 命中了本该不可达的内容: "
                    f"{[str(item.get('path')) for item in matches][:5]}",
                    where="tools",
                )
            continue
        _raise(
            "tool_reaches_evaluation",
            f"{tool_name}({arguments!r}) 成功读到了 workspace 之外/评测目录的内容",
            where="tools",
        )


def _registry_names(registry: Any) -> list[str]:
    getter = getattr(registry, "names", None)
    if not callable(getter):
        _raise("invalid_tool_registry", "registry 没有可调用的 names()", where="tools")
    try:
        raw = getter()
    except TypeError:
        # 部分实现要求显式 role；审计要看的是"全部已注册工具"，再给它一次机会。
        raw = getter(None)
    return [str(name) for name in raw]


def _registry_specs(registry: Any) -> list[Any]:
    getter = getattr(registry, "specs", None)
    if not callable(getter):
        return []
    try:
        return list(getter())
    except TypeError:
        return list(getter(None))


def _spec_payload(spec: Any) -> str:
    dumper = getattr(spec, "model_dump_json", None)
    if callable(dumper):
        return str(dumper())
    return repr(spec)


def _execute_tool(registry: Any, name: str, arguments: dict[str, Any]) -> Any:
    execute = getattr(registry, "execute", None)
    if not callable(execute):
        _raise("invalid_tool_registry", "registry 没有可调用的 execute()", where="tools")
    return execute(name, arguments)


def _response_payload(response: Any) -> str:
    """把工具响应变成可扫描的文本。

    ``ToolResponse.model_dump_json()`` 可能失败：工具参数校验失败时 ``detail`` 里会带
    pydantic 的 ``ctx``，而 ``ctx['error']`` 是一个 ``ValueError`` 实例（不可序列化）。
    审计不能因为"被审计对象自己序列化不了"就跳过——``repr`` 仍然能拿到全部可见文本。
    """
    dumper = getattr(response, "model_dump_json", None)
    if callable(dumper):
        try:
            return str(dumper())
        except Exception:
            # 任何序列化失败都退到 repr：跳过审计等于把"读不出来"当成"没泄漏"。
            pass
    try:
        return repr(response)
    except Exception:  # pragma: no cover - repr 理论上不会失败
        return type(response).__name__


def _search_matches(response: Any) -> list[dict[str, Any]]:
    """从 ``search_code`` 的响应里取命中列表（不同实现可能放在 data 或 details 里）。"""
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        return []
    matches = data.get("matches")
    if not isinstance(matches, list):
        return []
    return [item for item in matches if isinstance(item, dict)]


def _tool_read_attempts(
    sentinel: GoldSentinel,
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """构造"试图读评测内容"的工具调用集合。

    ``read_file`` 的每次调用都必须失败；``search_code`` 的每次调用都不得**命中**
    （"跑完了但没找到"是正常结果，不是越界）。搜索尝试里带上真实 sentinel：
    如果它会出现在工作区里，全仓搜索是最容易无意中把它翻出来的通道。
    """
    attempts: list[tuple[str, dict[str, Any]]] = []
    for path in _EVALUATION_READ_ATTEMPTS:
        attempts.append(("read_file", {"path": path}))
        attempts.append(("search_code", {"pattern": "def ", "path_glob": path}))
    # 绝对路径：Windows 盘符与 POSIX 根各一个。绝对路径在 schema 层就应当被拒，
    # 但万一某个工具把 path 直接交给 open()，这里必须抓住。
    attempts.append(("read_file", {"path": str(Path.cwd() / "eval" / "gold.patch")}))
    attempts.append(("read_file", {"path": "/etc/passwd"}))
    attempts.append(("search_code", {"pattern": sentinel.token}))
    attempts.append(("search_code", {"pattern": GOLD_SENTINEL_PREFIX}))
    return tuple(attempts)


# -------------------------------------------------------- Artifact / Checkpoint


def _is_audit_probe_artifact(entry: Path) -> bool:
    """True for tool-result artifacts produced by the isolation audit's own probes.

    ``audit_tool_registry`` deliberately calls ``read_file``/``search_code`` on
    eval paths. The tool layer records the request in a ``tool-results/*.json``
    artifact, so the attempted path (and a sentinel pattern for the search probe)
    appears there by construction. A failed probe, or a successful ``search_code``
    that returned no matches, carries no evaluation content — the tool audit
    already asserts the read failed / the search was empty. A successful
    ``read_file`` or a non-empty search is still scanned and fails the audit.
    """
    if entry.suffix != ".json" or entry.parent.name != "tool-results":
        return False
    try:
        payload = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    if not isinstance(result, dict):
        return False
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        return False
    pattern = arguments.get("pattern")
    if isinstance(pattern, str) and GOLD_SENTINEL_PREFIX in pattern:
        # audit_tool_registry probes search_code with the sentinel token/prefix;
        # the request artifact necessarily contains that token.
        return True
    raw = arguments.get("path") or arguments.get("path_glob") or ""
    probe = str(raw).replace("\\", "/")
    if not any(attempt.replace("\\", "/") in probe for attempt in _EVALUATION_READ_ATTEMPTS):
        return False
    if result.get("ok") is False:
        return True
    if result.get("ok") is True and payload.get("tool") == "search_code":
        data = result.get("data")
        return isinstance(data, dict) and not data.get("matches")
    return False


def audit_artifact_tree(
    root: Path,
    *,
    sentinel: GoldSentinel,
    check_markers: bool = True,
) -> None:
    """审计一次运行的 artifact 目录：不得夹带评测内容。

    Artifact 是"推理侧可以读回来"的面：``events.jsonl`` / ``llm_requests.jsonl``
    / ``attempts/*/junit.xml`` 都可能被下一轮工具读到。因此这里像 workspace 一样
    扫描文件内容（分块读 bytes，不用 text 模式——二进制里同样能藏 token）。

    额外拒绝：目录名为 ``eval``/``evaluation``、文件名为评测侧文件名、以及任何
    符号链接（artifact 目录里的链接可以把评测目录挂进来）。
    """
    target = Path(root)
    if is_link(target):
        _raise(
            "artifact_root_is_link",
            "artifact 根是符号链接/junction",
            where="artifact",
        )
    if not target.is_dir():
        _raise("artifact_missing", f"artifact 目录不存在: {target}", where="artifact")
    resolved_root = target.resolve(strict=True)
    sentinel_bytes = sentinel.token.encode("utf-8")

    for current, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        base = Path(current)
        for name in sorted(dirnames):
            entry = base / name
            relative = _relative(entry, resolved_root)
            if is_link(entry):
                _raise(
                    "artifact_link",
                    f"artifact 树内存在符号链接/junction: {relative}",
                    where="artifact",
                )
            if name.casefold() in EVALUATION_DIRECTORY_NAMES:
                _raise(
                    "evaluation_directory_in_artifact",
                    f"artifact 树内存在评测目录: {relative}",
                    where="artifact",
                )
        for name in sorted(filenames):
            entry = base / name
            relative = _relative(entry, resolved_root)
            if is_link(entry):
                _raise(
                    "artifact_link",
                    f"artifact 树内存在符号链接/junction: {relative}",
                    where="artifact",
                )
            if name.casefold() in EVALUATION_FILE_NAMES:
                _raise(
                    "evaluation_file_in_artifact",
                    f"artifact 树内出现评测文件: {relative}",
                    where="artifact",
                )
            if not _resolved_inside(entry, resolved_root):
                _raise(
                    "artifact_path_escape",
                    f"artifact 文件的真实路径逃出根目录: {relative}",
                    where="artifact",
                )
            if _is_audit_probe_artifact(entry):
                continue
            if _file_contains(entry, sentinel_bytes):
                _raise(
                    "gold_sentinel_in_artifact",
                    f"artifact 文件含 Gold Sentinel: {relative}"
                    f"（指纹 {redact_sentinel(sentinel.token)}）",
                    where="artifact",
                )
            if check_markers:
                _assert_file_markers_free(entry, where=f"artifact:{relative}")


def audit_checkpoint(
    path: Path,
    *,
    sentinel: GoldSentinel,
    check_markers: bool = True,
) -> None:
    """审计 checkpoint（文件或 run 目录）里没有评测内容。

    Checkpoint 的特殊之处是它**会被恢复路径原样读回**：一旦某个字段里混进了
    gold/test patch，下一次进程恢复时那条内容就成了推理输入。检查直接用原始字节
    扫描，因为 LangGraph 用 msgpack 存 state——字符串在 msgpack 里就是 UTF-8 字节，
    扫描比反序列化更可靠（也不受 schema 版本影响）。

    ``-wal`` / ``-shm`` 也算：SQLite 的 WAL 文件在提交前就含数据，漏掉它等于
    "只看主库文件"，而进程被硬杀后 WAL 里往往正是最新内容。
    """
    target = Path(path)
    if target.is_dir():
        candidates = sorted(
            item
            for item in target.rglob("*")
            if item.is_file()
            and (
                item.name.startswith("checkpoint")
                or ".sqlite" in item.name
                or item.suffix in {".db", ".sqlite3"}
            )
        )
        if not candidates:
            _raise(
                "checkpoint_missing",
                f"run 目录里没有任何 checkpoint 文件: {target}",
                where="checkpoint",
            )
    elif target.is_file():
        candidates = [target]
    else:
        _raise(
            "checkpoint_missing",
            f"checkpoint 不存在（fail closed：缺少证据不等于没有泄漏）: {target}",
            where="checkpoint",
        )

    sentinel_bytes = sentinel.token.encode("utf-8")
    for item in candidates:
        if is_link(item):
            _raise(
                "checkpoint_link",
                f"checkpoint 文件是符号链接: {item.name}",
                where="checkpoint",
            )
        if _file_contains(item, sentinel_bytes):
            _raise(
                "gold_sentinel_in_checkpoint",
                f"checkpoint 文件含 Gold Sentinel: {item.name}"
                f"（指纹 {redact_sentinel(sentinel.token)}）",
                where="checkpoint",
            )
        if check_markers:
            _assert_file_markers_free(item, where=f"checkpoint:{item.name}")


# ------------------------------------------------------------- Import Graph


def build_import_graph(package_root: Path) -> dict[str, set[str]]:
    """解析一棵包目录的 AST，返回 ``模块全名 -> 依赖模块全名集合``。

    覆盖契约要求的全部入口形态：

    * 普通 ``import a.b`` / ``from a.b import c``（``c`` 是子模块时也算依赖）；
    * 相对 import：``from . import x`` / ``from .mod import y`` / ``from ..pkg import z``；
    * 包 ``__init__`` 再导出：``__init__.py`` 被建模成包节点，闭包计算时**任何**
      子模块都会把祖先包节点拉进来——这正是"``__init__`` 里 re-export 了一个
      评测模块，于是 import 任何子模块都执行了它"的那条路径；
    * 动态 import：``__import__("...")`` 与 ``importlib.import_module("...")`` 的
      **字面量参数**会解析成真实依赖；非字面量记录成 :data:`DYNAMIC_IMPORT_MARKER`。

    无法解析的文件记录成 :data:`PARSE_ERROR_MARKER`；闭包检查把它当违规处理
    （无法证明没有评测 import，就当作有）。函数级 import 也会被收集——它们在调用
    时会真的执行，而且常常正是"绕开静态检查"的写法。
    """
    root = Path(package_root)
    if not root.is_dir():
        _raise(
            "package_root_missing",
            f"包根目录不存在: {root}",
            where="import-graph",
        )
    modules = _discover_modules(root)
    if not modules:
        _raise(
            "package_root_empty",
            f"包根目录下没有任何 .py 文件: {root}",
            where="import-graph",
        )
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        graph[name] = _parse_module_imports(path, name, set(modules))
    return graph


def _discover_modules(root: Path) -> dict[str, Path]:
    prefix = _module_prefix(root)
    discovered: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join([prefix, *parts]) if parts else prefix
        discovered[name] = path
    return discovered


def _module_prefix(root: Path) -> str:
    """推出包的全名。``src/codepilot`` → ``codepilot``；嵌套包向上补全。

    不能只取 ``root.name`` 就完事：调用方可能把 ``src/codepilot/eval`` 当根传进来，
    那样 key 会变成 ``eval.assembly``，与真实模块名 ``codepilot.eval.assembly``
    对不上，forbidden prefix 就永远匹配不到——一个永远为空的检查器比没有更糟。
    """
    current = root.resolve()
    parts = [current.name]
    parent = current.parent
    while (parent / "__init__.py").is_file():
        parts.insert(0, parent.name)
        parent = parent.parent
    return ".".join(parts)


def _parse_module_imports(path: Path, module: str, known_modules: Container[str]) -> set[str]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _raise(
            "module_unreadable",
            f"无法读取模块 {module}: {exc}",
            where="import-graph",
        )
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return {PARSE_ERROR_MARKER}

    is_package = path.name == "__init__.py"
    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # 外部依赖（faiss、sentence_transformers……）也记下来：它们在闭包里
                # 没有出边，但保留"有这条依赖"这个事实，报告才能解释动态 import 探测行为。
                deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, module, is_package=is_package)
            if base is None:
                continue
            deps.add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}"
                if candidate in known_modules:
                    deps.add(candidate)
        elif isinstance(node, ast.Call):
            target = _dynamic_import_target(node)
            if target is not None:
                deps.add(target)
    return deps


def _resolve_from(node: ast.ImportFrom, module: str, *, is_package: bool) -> str | None:
    if node.level == 0:
        return node.module
    parts = module.split(".")
    package_parts = parts if is_package else parts[:-1]
    up = node.level - 1
    if up >= len(package_parts) and not (up == 0 and package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - up] if up else package_parts
    if node.module:
        return ".".join([*base_parts, *node.module.split(".")])
    return ".".join(base_parts) if base_parts else None


def _dynamic_import_target(node: ast.Call) -> str | None:
    func = node.func
    is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
    is_import_module = isinstance(func, ast.Attribute) and func.attr == "import_module"
    if not (is_dunder_import or is_import_module):
        return None
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    # ``importlib.import_module(name="...")`` 与位置参数等价；只认名字叫 name 的
    # 关键字参数，别的一律当作无法静态解析。
    for keyword in node.keywords:
        if keyword.arg != "name":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return DYNAMIC_IMPORT_MARKER


def _ancestor_packages(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(len(parts) - 1, 0, -1))


def import_closure(graph: Mapping[str, Iterable[str]], entries: Sequence[str]) -> set[str]:
    """从 ``entries`` 出发的可达模块集合（含祖先包节点的 ``__init__`` 依赖）。"""
    pending = list(entries)
    seen: set[str] = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        for parent in _ancestor_packages(node):
            if parent in graph:
                pending.append(parent)
        pending.extend(graph.get(node, ()))
    return seen


def dynamic_import_modules(
    graph: Mapping[str, Iterable[str]], entries: Sequence[str]
) -> tuple[str, ...]:
    """闭包里"存在无法静态解析的动态 import"的模块名（稳定排序）。

    这些位置**不默认判违规**：像 :func:`codepilot.graph.checkpoint.state_allowed_modules`
    那样按 ``RepairState`` 注解动态取模块名，是合法且更抗漂移的写法。静态检查能做的是
    把它们列出来（报告/HUMAN_REVIEW），并在 ``assert_inference_import_closure`` 传
    ``strict_dynamic=True`` 时把它们当违规——"无法证明"和"已证明没有"必须能被区分，
    不能靠默认值把前者当成后者。
    """
    reachable = import_closure(graph, entries)
    return tuple(
        sorted(
            module
            for module in reachable
            if module in graph and DYNAMIC_IMPORT_MARKER in graph.get(module, ())
        )
    )


def assert_inference_import_closure(
    graph: Mapping[str, Iterable[str]],
    *,
    forbidden_prefixes: Sequence[str],
    entries: Sequence[str] | None = None,
    strict_dynamic: bool = False,
) -> None:
    """断言推理依赖闭包里没有评测模块。

    ``entries`` 是推理侧入口模块。不传时检查**整张图**（适用于"这个包只该含推理
    代码"的场合，是最严格模式）。匹配规则是**点分层级**前缀：``codepilot.eval.swebench``
    会命中 ``codepilot.eval.swebench`` 和其子模块，但**不会**误伤
    ``codepilot.eval.swebench_task``（契约第 2 节点名它是推理安全模块）。

    失败分类：

    * ``import_closure_violation``：闭包里出现 forbidden 模块（字面量动态 import
      也在这里：``importlib.import_module("codepilot.eval.swebench")`` 能被解析出来，
      按真实依赖处理）；
    * ``import_closure_dynamic``：仅在 ``strict_dynamic=True`` 时触发，闭包里存在
      非字面量动态 import 调用点（无法证明它没 import 评测模块，按 fail closed
      口径当作违规）；
    * ``import_closure_unparsable``：闭包里有读不出/解析不了的模块。

    默认不把非字面量动态 import 判违规，是因为代码库里存在合法的"按注解取模块名"
    写法（见 :func:`dynamic_import_modules`）；需要零容忍的场合显式传
    ``strict_dynamic=True``，不要用"隐式放行"换"默认全绿"。
    """
    selected = tuple(entries) if entries is not None else tuple(sorted(graph))
    if entries is not None:
        missing = [entry for entry in selected if entry not in graph]
        if missing:
            _raise(
                "import_entry_missing",
                f"推理入口模块不在包内: {missing}",
                where="import-graph",
            )
    reachable = import_closure(graph, selected)

    if PARSE_ERROR_MARKER in reachable:
        _raise(
            "import_closure_unparsable",
            "推理依赖闭包里存在无法解析的模块，无法证明它没有 import 评测模块",
            where="import-graph",
        )
    if strict_dynamic:
        dynamic = dynamic_import_modules(graph, selected)
        if dynamic:
            _raise(
                "import_closure_dynamic",
                "strict_dynamic=True 且推理依赖闭包里存在无法静态解析的动态 import: "
                f"{list(dynamic)}（入口: {list(selected)}）",
                where="import-graph",
            )
    violations = sorted(
        module for module in reachable if _matches_forbidden(module, forbidden_prefixes)
    )
    if violations:
        _raise(
            "import_closure_violation",
            f"推理依赖闭包中出现评测模块: {violations}（入口: {list(selected)}）",
            where="import-graph",
        )


def _matches_forbidden(module: str, prefixes: Sequence[str]) -> bool:
    for prefix in prefixes:
        if not prefix:
            continue
        if module == prefix or module.startswith(f"{prefix}."):
            return True
    return False


# --------------------------------------------------------------------- 文件系统辅助


def _is_unc(raw: str) -> bool:
    """Windows UNC 路径（``\\\\server\\share`` 或 ``//server/share``）。"""
    normalized = raw.replace("/", "\\")
    return normalized.startswith("\\\\") or normalized.startswith("//")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - os.walk 只会给出子树内的路径
        return str(path)


def _resolved_inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return resolved.is_relative_to(root)


def _file_contains(path: Path, needle: bytes) -> bool:
    """分块判断文件内容里是否出现 ``needle``。

    跨越块边界的情况要处理：把上一块的尾部 ``len(needle)-1`` 字节接到下一块前面。
    不这么做的话，"token 恰好跨 1MB 边界"就成了一条静默的漏检路径。
    """
    if not needle:
        return False
    try:
        with path.open("rb") as handle:
            carry = b""
            while True:
                chunk = handle.read(_SCAN_CHUNK)
                if not chunk:
                    return False
                window = carry + chunk
                if needle in window:
                    return True
                carry = window[-(len(needle) - 1) :] if len(needle) > 1 else b""
    except OSError:
        # 读不出来的文件不能当作"干净"：它可能是个权限受限的评测文件。
        _raise(
            "file_unreadable",
            f"无法读取待审计文件: {path.name}",
            where="filesystem",
        )


def _assert_file_markers_free(path: Path, *, where: str) -> None:
    """文本/二进制文件里不得出现评测标记。只对足够小的文件做全量扫描。"""
    try:
        size = path.stat().st_size
    except OSError:
        _raise("file_unreadable", f"无法读取文件属性: {path.name}", where=where)
    if size > 64 << 20:  # 64MB 以上视为二进制大对象，跳过标记扫描（token 扫描不受限）
        return
    try:
        payload = path.read_bytes()
    except OSError:
        _raise("file_unreadable", f"无法读取文件: {path.name}", where=where)
    text = payload.decode("utf-8", errors="ignore")
    _assert_no_evaluation_markers(text, where=where)
