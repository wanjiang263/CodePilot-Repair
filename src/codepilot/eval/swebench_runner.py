"""E-19：SWE-bench Runner —— 三层断点恢复、运行身份、预算与官方 Harness 恢复。

这个模块不实现修复逻辑，也不实现数据加载/预测写出/官方命令拼装：那些分别在
``swebench.py``（Owner A）、``swebench_prediction.py``（Owner A）、
``swebench_harness.py``（Owner D）与 ``swebench_env.py``（Owner C）。这里只做
**编排与记账**，并复用 Phase 10 已经验收过的语义与组件：

* :class:`~codepilot.eval.runner.ResultJournal`：已完成结果的持久账本（带内容摘要；
  被改动的记录拒绝复用），不重复实现 journal；
* :class:`~codepilot.eval.runner.CostLedger`：派发前预留、结束后回填的整批费用账本；
* :class:`~codepilot.eval.runner.TaskRecord`：``results.jsonl`` 的行格式（SWE-bench
  专有字段放进 ``artifact_refs``，因此旧 journal 行照常可读）；
* :mod:`codepilot.eval.attribution` 与 :mod:`codepilot.eval.metrics`：归因与指标纯函数；
* :mod:`codepilot.graph.checkpoint`：图级 checkpoint 的 schema 版本与恢复核对。

## 为什么是三层，而不是一个 ``--resume`` 开关

一次 SWE-bench 任务有三个可以各自独立的"已完成"：任务级（结论已落 journal 且证据完整）、
图级（M2 图跑到一半，checkpoint 可以续）、评测级（推理已完成、补丁已落盘，但官方
Harness 没跑完或结论没落 journal）。把它们压成一个布尔值，就会出现两种都很贵的错误：

* 只会任务级续跑：进程死在图上时没有任何结论可写，重启把已经付过钱的 LLM 请求与
  已经跑完的容器测试全部重来一遍；
* 只看"有补丁"就复用旧评测：换了补丁、换了 seed、换了 harness 版本之后，旧报告会被
  当成新结果发布 —— 而它看起来完全正常，只是属于另一个世界。

因此 :class:`ResumeLevel` 是**可叠加**的三档：``TASK`` 只跳过完整结论，``GRAPH`` 额外
允许用 checkpoint 续跑，``EVALUATION`` 再额外允许复用推理证据与完整且身份一致的官方
评测报告。默认调用方应显式传 ``ResumeLevel``，不要用一个默认值把"续跑到哪一层"藏起来。

## 身份绑定（少绑一项，续跑就会错）

:class:`RunIdentity` 绑 dataset / revision / split / subset hash / instance / repo /
base_commit / provider / model / config_hash / code_commit / seed / patch_hash /
harness_version。``patch_hash`` 在**规划阶段**还不知道，所以 journal 的主键用去掉
``patch_hash`` 的 :attr:`RunIdentity.journal_key`；等推理结束再把完整身份（含补丁
哈希）写进记录与现场 manifest。于是"同一任务重跑出不同补丁"不会被当成"已完成"跳过，
而"同一补丁"仍然可以在评测级复用报告。

## 未知费用不等于 0

恢复时**不**重新扣 token / 测试次数 / attempt / 费用：journal 里已有的记录直接跳过，
不再经过 :class:`CostLedger` 的 reserve/settle。恢复后的整批报告里，费用总额来自
全部记录（含历史完成项），而 ``ledger.spent`` 只反映**本次派发会话**真实新花的钱 —
两者在报告里分开写，否则一次续跑会把历史费用再算一遍。费用未知的任务记 ``None``
（不是 0），未知任务数单独计数并把总额声明为下界。

## 平台边界

官方 ``swebench==4.1.0`` 在原生 Windows 上不可 import（依赖 Unix-only 的
``resource`` 模块）。本模块从不 import swebench；官方执行由注入的 evaluator 完成，
适配层在不可执行时必须返回 ``ENV_ERROR / RESOURCE_BLOCKED`` 归因而不是抛裸异常。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from codepilot.errors import ErrorOrigin, FailureClass, classify_exception
from codepilot.eval.assembly import InferenceOutcome
from codepilot.eval.attribution import Attribution, EvaluationState
from codepilot.eval.metrics import (
    Ratio,
    TaskOutcome,
    VerificationMode,
    attribution_metrics,
    cost_metrics,
    iterations_metrics,
    reliability_metrics,
)
from codepilot.eval.runner import (
    RECORD_VERSION,
    CostLedger,
    ExecutionState,
    ResultJournal,
    TaskRecord,
    code_identity,
)
from codepilot.eval.swebench_harness import (
    HarnessCacheError,
    HarnessError,
    HarnessErrorKind,
    HarnessPlatformError,
    OfficialVerdict,
    derive_run_id,
)
from codepilot.eval.swebench_task import (
    DATASET_NAME,
    DATASET_REVISION,
    HARNESS_VERSION,
)
from codepilot.patch.diff import generate_git_diff
from codepilot.repo.gitcmd import GitError, run_git
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.usage import Usage

if TYPE_CHECKING:
    # 只为类型标注导入：运行时用不到这两个类，避免把 A 的 loader/预测模块的
    # 初始化顺序绑进 runner 的 import 链。
    from codepilot.eval.swebench import SWEBenchEvaluationTask
    from codepilot.eval.swebench_task import SWEBenchInferenceTask

__all__ = [
    "DATASET_NAME",
    "DATASET_REVISION",
    "HARNESS_VERSION",
    "LEGACY_ATTRIBUTION",
    "MANIFEST_SCHEMA_VERSION",
    "InferenceEvidenceAudit",
    "OfficialEvidenceAudit",
    "OfficialVerdict",
    "ResumeLevel",
    "RunIdentity",
    "RunReport",
    "SWEBenchAttribution",
    "SWEBenchEvaluation",
    "SWEBenchLoaderLike",
    "SWEBenchRunConfig",
    "SWEBenchRunner",
    "WorkspaceAudit",
    "audit_official_evidence",
    "audit_workspace",
    "classify_outcome",
    "harness_run_id",
    "recover_inference_evidence",
    "subset_hash",
]


# --------------------------------------------------------------------------- 固定事实

#: 数据集身份与常量统一从 Owner A 的 ``swebench_task.py`` 导入（单一真源），
#: 官方 verdict 从 Owner D 的 ``swebench_harness.py`` 导入。本模块在导入时再通过
#: :meth:`SWEBenchRunner._check_task` 与 loader 返回的任务对象交叉核对，不一致直接
#: 拒绝开跑。``DATASET_NAME`` / ``DATASET_REVISION`` / ``HARNESS_VERSION`` 仍从本模块
#: 转出（``__all__``），方便只依赖 runner 的脚本。

#: 每个 instance 现场目录里的 manifest 版本。加字段不必 +1；改语义必须 +1。
MANIFEST_SCHEMA_VERSION = 1

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX40_64 = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: 现场 manifest 与评测证据文件名。集中列出，拼错会成为显式错误而不是静默的空文件。
MANIFEST_NAME = "swebench_run.json"
PATCH_NAME = "model_patch.diff"
OFFICIAL_ENVELOPE_NAME = "official_eval.json"
OFFICIAL_REPORT_NAME = "official_report.json"

#: 官方评测"给出了可判定结论"的两档。
_DECIDABLE_VERDICTS = frozenset({"RESOLVED", "UNRESOLVED"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _sha256_text(text: str) -> str:
    # 与 D/A/Phase 10 统一用普通 UTF-8（`InferenceOutcome.digest` 也是 utf-8）：
    # 这些输入是 ASCII 十六进制/JSON/补丁文本，唯一差异只可能出现在非法代理字符边界，
    # 而跨模块摘要口径不一致会让"同一份证据算两个哈希"。
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_text(path: Path, text: str) -> Path:
    """同目录 ``*.tmp`` + ``replace``：一半写进去的文件不能冒充完整产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)
    return path


def _atomic_write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return path


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    return _atomic_write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _read_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读一个 JSON 对象；失败返回 ``(None, 原因)``，绝不把坏文件当空对象。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"无法读取 {path}: {type(exc).__name__}: {exc}"
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return None, f"{path} 不是合法 JSON: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path} 顶层不是 JSON 对象"
    return payload, None


# --------------------------------------------------------------------------- 恢复层级


class ResumeLevel(StrEnum):
    """三层恢复的**可叠加**档位。

    ``TASK`` < ``GRAPH`` < ``EVALUATION``。不要把它读成互斥的三选一：选择 ``GRAPH``
    同时意味着任务级跳过也生效。没有单独的 ``NONE``：全新批次本来就没有可复用的
    东西，三档中的任一档都会退化成"全部重新跑"。
    """

    __test__: ClassVar[bool] = False

    TASK = "task"
    GRAPH = "graph"
    EVALUATION = "evaluation"

    @property
    def rank(self) -> int:
        return {"task": 1, "graph": 2, "evaluation": 3}[self.value]

    def enables(self, level: ResumeLevel) -> bool:
        """``self`` 是否包含 ``level`` 这一层。"""
        return self.rank >= level.rank


class SWEBenchAttribution(StrEnum):
    """SWE-bench 场景的归因类别。互斥且完备。

    与 Phase 10 的 :class:`~codepilot.eval.attribution.Attribution` 的差别是这里把
    "官方评测说未解决"与"模型自己没给出可评测结果"分开：前者是能力读数，后者根本
    没测到能力。旧 journal 的 ``Attribution`` 字段仍然按
    :data:`LEGACY_ATTRIBUTION` 映射写入，保持向后兼容。
    """

    __test__: ClassVar[bool] = False

    MODEL_NOT_RESOLVED = "model_not_resolved"
    MODEL_INVALID_PLAN = "model_invalid_plan"
    PROVIDER_ERROR = "provider_error"
    HARNESS_INTERNAL_ERROR = "harness_internal_error"
    WORKSPACE_ERROR = "workspace_error"
    ENV_ERROR = "env_error"
    SANDBOX_ERROR = "sandbox_error"
    PATCH_INVALID = "patch_invalid"
    BUDGET_STOPPED = "budget_stopped"
    OFFICIAL_EVALUATION_ERROR = "official_evaluation_error"
    OFFICIAL_NOT_RESOLVED = "official_not_resolved"
    OFFICIAL_RESOLVED = "official_resolved"


#: 映射到 Phase 10 journal 的 ``attribution`` 字段。映射是**有损的**（新类别更细），
#: 所以细粒度值同时写进 ``artifact_refs["swebench_attribution"]``，报告以细粒度为准。
LEGACY_ATTRIBUTION: Mapping[SWEBenchAttribution, Attribution] = {
    SWEBenchAttribution.MODEL_NOT_RESOLVED: Attribution.EVALUATION_NOT_RUN,
    SWEBenchAttribution.MODEL_INVALID_PLAN: Attribution.MODEL_INVALID_PLAN,
    SWEBenchAttribution.PROVIDER_ERROR: Attribution.HARNESS_ERROR,
    SWEBenchAttribution.HARNESS_INTERNAL_ERROR: Attribution.HARNESS_ERROR,
    SWEBenchAttribution.WORKSPACE_ERROR: Attribution.HARNESS_ERROR,
    SWEBenchAttribution.ENV_ERROR: Attribution.ENV_ERROR,
    SWEBenchAttribution.SANDBOX_ERROR: Attribution.SANDBOX_ERROR,
    SWEBenchAttribution.PATCH_INVALID: Attribution.EVALUATION_NOT_RUN,
    SWEBenchAttribution.BUDGET_STOPPED: Attribution.BUDGET_STOPPED,
    SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR: Attribution.EVALUATION_ERROR,
    SWEBenchAttribution.OFFICIAL_NOT_RESOLVED: Attribution.MODEL_NOT_RESOLVED,
    SWEBenchAttribution.OFFICIAL_RESOLVED: Attribution.RESOLVED,
}

#: 归因里属于"基础设施/接入侧"的类别：它们留在端到端分母里，但仍不算模型失败。
INFRA_ATTRIBUTIONS: frozenset[SWEBenchAttribution] = frozenset(
    {
        SWEBenchAttribution.PROVIDER_ERROR,
        SWEBenchAttribution.HARNESS_INTERNAL_ERROR,
        SWEBenchAttribution.WORKSPACE_ERROR,
        SWEBenchAttribution.ENV_ERROR,
        SWEBenchAttribution.SANDBOX_ERROR,
        SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR,
        SWEBenchAttribution.BUDGET_STOPPED,
    }
)

_OFFICIAL_ERROR_KIND_MAP: Mapping[str, SWEBenchAttribution] = {
    "env_error": SWEBenchAttribution.ENV_ERROR,
    "environment_error": SWEBenchAttribution.ENV_ERROR,
    "resource_blocked": SWEBenchAttribution.ENV_ERROR,
    "platform_blocked": SWEBenchAttribution.ENV_ERROR,
    "docker_unavailable": SWEBenchAttribution.ENV_ERROR,
    "sandbox_error": SWEBenchAttribution.SANDBOX_ERROR,
    "workspace_error": SWEBenchAttribution.WORKSPACE_ERROR,
    "harness_internal_error": SWEBenchAttribution.HARNESS_INTERNAL_ERROR,
    "provider_error": SWEBenchAttribution.PROVIDER_ERROR,
}

#: 官方评测阶段（Owner D 的 ``HarnessErrorKind``）的失败形态：它们说明"评测没跑成/
#: 没测到"，不是模型没修好，也不是我们自己代码的内部缺陷，所以统一归到
#: ``OFFICIAL_EVALUATION_ERROR``。按键的小写匹配：D 的枚举值是大写，适配层可能原样
#: 透传（``error_kind=str(execution.error_kind)``）。
_OFFICIAL_STAGE_ERROR_KINDS: frozenset[str] = frozenset(
    {
        "harness_cache_error",
        "official_evaluation_error",
        "harness_input_error",
        "predictions_missing",
        "predictions_invalid",
        "patch_hash_mismatch",
        "dataset_mismatch",
        "cache_identity_mismatch",
        "cache_unverified",
        "process_failed",
        "timeout",
        "report_missing",
        "report_corrupted",
        "report_identity_mismatch",
        "instance_error",
        "internal_inconsistency",
    }
)


def classify_outcome(
    *,
    execution_state: str,
    official_verdict: str | None,
    official_resolved: bool | None,
    official_error_kind: str | None = None,
    patch_valid: bool,
    empty_patch: bool,
    invalid_patch: bool,
    stop_reason: str | None = None,
    error_class: str | None = None,
    error_origin: str | None = None,
) -> SWEBenchAttribution:
    """把一次 (instance, seed) 归到 :class:`SWEBenchAttribution` 的一类。**纯函数**。

    判定顺序即文档，先问"结论是什么"再问"谁让这次没结论"：

    1. 派发层（预算截停/未执行）——它先于一切；
    2. 官方判定——已有官方结论时以它为准（``RESOLVED`` / ``UNRESOLVED``），
       不允许用"内部觉得没修好"覆盖官方结论；
    3. 官方的失败形态（补丁应用失败/空补丁 → ``PATCH_INVALID``；错误类型可识别时
       按 ENV/SANDBOX/WORKSPACE/HARNESS 分层）；
    4. 推理侧错误（计划无效 / provider / workspace / harness / env / sandbox / 预算）；
    5. 补丁形态（空补丁、校验失败的补丁）；
    6. 官方评测试过但没有给出结论 → ``OFFICIAL_EVALUATION_ERROR``；
    7. 有合法补丁但没拿到官方结论 → ``MODEL_NOT_RESOLVED``（能力读数缺失，但至少
       是模型侧的一次未解决）。
    """
    if execution_state in {
        ExecutionState.BUDGET_BLOCKED.value,
        ExecutionState.NOT_EXECUTED.value,
    }:
        return SWEBenchAttribution.BUDGET_STOPPED

    verdict = (official_verdict or "").strip().upper()
    if official_resolved is True or verdict == OfficialVerdict.RESOLVED.value:
        return SWEBenchAttribution.OFFICIAL_RESOLVED
    if official_resolved is False or verdict == OfficialVerdict.UNRESOLVED.value:
        return SWEBenchAttribution.OFFICIAL_NOT_RESOLVED
    if verdict in {
        OfficialVerdict.PATCH_APPLY_FAILED.value,
        OfficialVerdict.EMPTY_PATCH.value,
    }:
        return SWEBenchAttribution.PATCH_INVALID

    kind = (official_error_kind or "").strip().lower()
    if kind in _OFFICIAL_ERROR_KIND_MAP:
        return _OFFICIAL_ERROR_KIND_MAP[kind]
    if kind in _OFFICIAL_STAGE_ERROR_KINDS:
        return SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR

    if error_class == FailureClass.PLAN_SEMANTIC_INVALID.value:
        return SWEBenchAttribution.MODEL_INVALID_PLAN
    if (
        error_class
        in {
            FailureClass.LLM_PROVIDER_ERROR.value,
            FailureClass.LLM_REQUEST_UNCERTAIN.value,
        }
        or error_origin == ErrorOrigin.PROVIDER.value
    ):
        return SWEBenchAttribution.PROVIDER_ERROR
    if error_class == FailureClass.WORKSPACE_ERROR.value or error_origin == (
        ErrorOrigin.WORKSPACE.value
    ):
        return SWEBenchAttribution.WORKSPACE_ERROR
    if error_class == FailureClass.HARNESS_INTERNAL_ERROR.value or error_origin == (
        ErrorOrigin.HARNESS.value
    ):
        return SWEBenchAttribution.HARNESS_INTERNAL_ERROR
    if stop_reason == "env_error":
        return SWEBenchAttribution.ENV_ERROR
    if stop_reason in {"sandbox_failure", "sandbox_error"}:
        return SWEBenchAttribution.SANDBOX_ERROR
    if (stop_reason or "").startswith("budget_"):
        return SWEBenchAttribution.BUDGET_STOPPED
    if empty_patch or invalid_patch or not patch_valid:
        return SWEBenchAttribution.PATCH_INVALID
    if verdict in {OfficialVerdict.ERROR.value, OfficialVerdict.MISSING.value}:
        return SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR
    return SWEBenchAttribution.MODEL_NOT_RESOLVED


# --------------------------------------------------------------------------- 运行身份


class RunIdentity(BaseModel):
    """一条结果的完整身份（契约 §10）。``patch_hash`` 在规划阶段为空串。

    ``key`` 是**完整**身份（含补丁）的摘要，用于"这份结果属于哪个补丁"；``journal_key``
    去掉补丁哈希，用作 journal 主键 —— 因为查 journal 的时候补丁还不存在，而"重跑出
    不同补丁"必须被识别成新结果而不是"已完成"。两个哈希都不可省。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: str
    revision: str
    split: str
    subset_hash: str
    instance_id: str
    repo: str
    base_commit: str
    provider: str
    model: str
    config_hash: str
    code_commit: str
    seed: int
    patch_hash: str
    harness_version: str

    @field_validator("revision")
    @classmethod
    def _validate_revision(cls, value: str) -> str:
        if not _HEX40_64.match(value):
            raise ValueError(f"dataset revision 必须是 40/64 位十六进制，收到 {value!r}")
        return value

    @field_validator("base_commit")
    @classmethod
    def _validate_base_commit(cls, value: str) -> str:
        if not _HEX40.match(value):
            raise ValueError(f"base_commit 必须是 40 位十六进制，收到 {value!r}")
        return value

    @field_validator("patch_hash")
    @classmethod
    def _validate_patch_hash(cls, value: str) -> str:
        if value and not _HEX64.match(value):
            raise ValueError(f"patch_hash 必须是 64 位十六进制或空串，收到 {value!r}")
        return value

    @field_validator("seed")
    @classmethod
    def _validate_seed(cls, value: int) -> int:
        if value < 0:
            raise ValueError("seed 不能为负")
        return value

    def payload(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "revision": self.revision,
            "split": self.split,
            "subset_hash": self.subset_hash,
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "provider": self.provider,
            "model": self.model,
            "config_hash": self.config_hash,
            "code_commit": self.code_commit,
            "seed": self.seed,
            "patch_hash": self.patch_hash,
            "harness_version": self.harness_version,
        }

    @property
    def key(self) -> str:
        return _sha256_text(_canonical(self.payload()))[:32]

    @property
    def journal_key(self) -> str:
        payload = self.payload()
        payload.pop("patch_hash")
        return _sha256_text(_canonical(payload))[:32]

    def as_dict(self) -> dict[str, Any]:
        payload = self.payload()
        payload["identity"] = self.key
        payload["journal_key"] = self.journal_key
        return payload


def subset_hash(instance_ids: Sequence[str]) -> str:
    """实例集合摘要。与顺序无关，因此同一子集换一种遍历顺序仍是同一身份。"""
    return _sha256_text("\n".join(sorted(instance_ids)))


def harness_run_id(instance_id: str, patch_sha256: str, seed: int) -> str:
    """官方 Harness 的 run_id；**唯一真源是 Owner D 的 :func:`derive_run_id`**。

    形态 ``cp-<12hex>-<seed>``，其中 12 位摘要 = ``sha256(canonical([[instance_id,
    patch_sha256]]))[:12]``（D 的 ``patch_set_digest``），不是"补丁哈希的前 12 位"。
    两个算法对同一份补丁会给出不同的 run_id，而 D 在启动官方 harness 前会校验这个摘要
    （``_enforce_run_id_digest`` → ``HarnessCacheError``），所以这里必须直接复用 D 的
    实现，不能各算一套。

    摘要含 ``instance_id``：批量评测时同一份补丁出现在多个实例上，也不会共用报告
    文件名；单实例评测与 ``derive_run_id({instance_id: patch_sha256}, seed=...)`` 完全
    一致。
    """
    if not instance_id.strip():
        raise ValueError("instance_id 不能为空")
    if not _HEX64.match(patch_sha256):
        raise ValueError(f"patch_sha256 必须是 64 位十六进制，收到 {patch_sha256!r}")
    if seed < 0:
        raise ValueError("seed 不能为负")
    return derive_run_id({instance_id: patch_sha256}, seed=str(seed))


@dataclass(frozen=True, slots=True)
class SWEBenchRunConfig:
    """一次 SWE-bench 批次的全部可复现输入（不含密钥）。"""

    dataset_name: str = DATASET_NAME
    revision: str = DATASET_REVISION
    split: str = "test"
    harness_version: str = HARNESS_VERSION
    model_identity: str = "FakeLLM"
    provider: str = "fake"
    model: str = "FakeLLM"
    provider_config_hash: str = "unconfigured"
    environment_identity: str | None = None
    """批量环境身份摘要（可选注入点）。

    调用方可以从 Owner C 的 ``SWEBenchEnvironmentIdentity`` 汇总出
    instance/env/base image ids + environment_hash 的摘要传进来。它**不进**
    ``config_hash``（换镜像不该让已完成的推理作废），但会写进官方评测信封：
    EVALUATION 级恢复时环境身份不一致就拒绝复用旧报告，重新评一次。
    未注入时信封记 ``None``，报告会标明"环境身份未验证"——这对应 D 的
    ``image_ids`` 为空元组时"仍可复用但强度降低"的缓存语义。
    """
    budget_currency: str = "CNY"
    max_total_cost: float | None = None
    task_reservation: float = 0.0
    per_task_budget: RepairBudget = field(default_factory=RepairBudget)
    max_in_flight: int = 1
    run_label: str = "swebench"

    def __post_init__(self) -> None:
        if not self.dataset_name.strip():
            raise ValueError("dataset_name 不能为空")
        if not _HEX40_64.match(self.revision):
            raise ValueError(f"revision 必须是 40/64 位十六进制，收到 {self.revision!r}")
        if self.split not in {"dev", "test"}:
            raise ValueError(f"split 只能是 dev/test，收到 {self.split!r}")
        if self.environment_identity is not None and not self.environment_identity.strip():
            raise ValueError("environment_identity 传了就必须非空（None 表示未注入）")
        if self.max_in_flight < 1:
            raise ValueError("max_in_flight 至少为 1")
        if self.max_total_cost is not None and self.max_total_cost < 0:
            raise ValueError("max_total_cost 不能为负")
        if self.task_reservation < 0:
            raise ValueError("task_reservation 不能为负")
        if self.max_total_cost is not None and self.task_reservation <= 0:
            # 与 Phase 10 同一条理由：并发派发不预留就会超发，顺序派发会在最后一个
            # 任务上直接爆预算。宁可拒绝构造，也不要静默地花超。
            raise ValueError("设置了整批上限却没有每任务预留额：并发派发会超发")
        if self.per_task_budget.budget_currency != self.budget_currency:
            raise ValueError(
                "per_task_budget.budget_currency 与 budget_currency 不一致："
                "两套币种相加会得到一个没有口径的数"
            )

    @property
    def config_hash(self) -> str:
        """影响"模型会怎么修"的配置指纹。

        刻意**不含**整批预算上限与在途并发数：它们只影响这一批跑多少任务，
        不改变任一已完成任务的结果，因此改预算不该让续跑把已完成任务重跑一遍。
        单任务预算（迭代/测试/费用上限）会改变行为，必须进来。
        """
        return _sha256_text(
            _canonical(
                {
                    "dataset": self.dataset_name,
                    "revision": self.revision,
                    "split": self.split,
                    "harness_version": self.harness_version,
                    "model_identity": self.model_identity,
                    "provider": self.provider,
                    "model": self.model,
                    "provider_config_hash": self.provider_config_hash,
                    "budget_currency": self.budget_currency,
                    "per_task_budget": self.per_task_budget.model_dump(mode="json"),
                }
            )
        )[:32]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset_name,
            "revision": self.revision,
            "split": self.split,
            "harness_version": self.harness_version,
            "model_identity": self.model_identity,
            "provider": self.provider,
            "model": self.model,
            "provider_config_hash": self.provider_config_hash,
            "environment_identity": self.environment_identity,
            "budget_currency": self.budget_currency,
            "max_total_cost": self.max_total_cost,
            "task_reservation": self.task_reservation,
            "per_task_budget": self.per_task_budget.model_dump(mode="json"),
            "max_in_flight": self.max_in_flight,
            "run_label": self.run_label,
            "config_hash": self.config_hash,
        }


# --------------------------------------------------------------------------- 注入接口


class SWEBenchLoaderLike(Protocol):
    """Owner A 的 ``SWEBenchLoader`` 在契约 §3 中的两个只读入口。

    只声明本模块用到的两个方法（最小接口）：runner 不关心缓存/manifest 细节。
    类型真源在 ``swebench_task.py`` / ``swebench.py``，这里用 ``TYPE_CHECKING``
    导入只为标注；真实 loader 与其返回对象完全按契约实现。
    """

    def get_inference_task(self, split: str, instance_id: str) -> SWEBenchInferenceTask: ...

    def get_evaluation_task(self, split: str, instance_id: str) -> SWEBenchEvaluationTask: ...


class InferenceAssemblyLike(Protocol):
    """``InferenceAssembly`` 在契约 §10 里的最小接口。"""

    def run(self, *, run_id: str, seed: int) -> InferenceOutcome: ...


class InferenceFactory(Protocol):
    """把 ``(task, seed)`` 装配成一个可跑一次的推理现场。

    ``resume`` 与 ``run_id`` 显式传给工厂，工厂据此决定是否开启 checkpoint：
    ``resume >= ResumeLevel.GRAPH`` 时必须用确定性 ``run_id`` 且 ``checkpoint=True``，
    否则"续跑"只会退化成一个名字。只传 ``(task, seed)`` 会让这层选择藏在闭包里，
    调用方与测试都无法断言它被正确传递。
    """

    def __call__(
        self,
        task: SWEBenchInferenceTask,
        seed: int,
        *,
        run_id: str,
        resume: ResumeLevel,
    ) -> InferenceAssemblyLike: ...


class SWEBenchEvaluatorLike(Protocol):
    """官方 Harness 适配层（Owner D 的 ``OfficialHarnessRunner`` 再包一层）。

    调用约定刻意是**单实例**的：runner 只对"这一条补丁"负责。适配层消费 D 的
    ``HarnessExecutionResult``：``decision=PLATFORM_BLOCKED`` 要映射成
    ``verdict=ERROR, error_kind="platform_blocked"``（runner 归因 ENV_ERROR），
    ``HarnessCacheError`` 映射成 ``verdict=ERROR, error_kind="harness_cache_error"``
    （runner 归因 OFFICIAL_EVALUATION_ERROR）；批量评测由适配层内部合并成一次
    Harness 调用，并把批次 run_id 与每条报告的可回查路径记下来。
    """

    def __call__(
        self,
        *,
        instance_id: str,
        evaluation_task: SWEBenchEvaluationTask,
        patch: str,
        patch_sha256: str,
        seed: int,
        harness_run_id: str,
        run_dir: Path,
    ) -> SWEBenchEvaluation: ...


@dataclass(frozen=True, slots=True)
class SWEBenchEvaluation:
    """一次官方评测的**规范化**结论（适配层负责从 HarnessResult 映射过来）。"""

    verdict: OfficialVerdict
    instance_id: str
    resolved: bool | None
    harness_run_id: str
    patch_sha256: str
    harness_version: str
    dataset_revision: str
    report_path: Path | None = None
    report_sha256: str | None = None
    error_kind: str | None = None
    error_detail: str | None = None
    runtime_seconds: float = 0.0
    real_docker: bool = False
    reused: bool = False
    from_cache: bool = False

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("instance_id 不能为空")
        if not _HEX64.match(self.patch_sha256):
            raise ValueError(f"patch_sha256 必须是 64 位十六进制，收到 {self.patch_sha256!r}")
        if not self.harness_run_id.strip():
            raise ValueError("harness_run_id 不能为空")
        if self.runtime_seconds < 0:
            raise ValueError("runtime_seconds 不能为负")
        if self.report_sha256 is not None and not _HEX64.match(self.report_sha256):
            raise ValueError("report_sha256 必须是 64 位十六进制")
        # 判定必须与三态结论一致：``resolved=True`` 配 ``ERROR`` 会让下游把一个
        # 基础设施故障读成一次成功修复，而报告里两处字段会互相"证明"对方。
        if self.verdict is OfficialVerdict.RESOLVED and self.resolved is not True:
            raise ValueError("verdict=RESOLVED 时 resolved 必须是 True")
        if self.verdict is OfficialVerdict.UNRESOLVED and self.resolved is not False:
            raise ValueError("verdict=UNRESOLVED 时 resolved 必须是 False")
        if self.verdict not in {OfficialVerdict.RESOLVED, OfficialVerdict.UNRESOLVED} and (
            self.resolved is not None
        ):
            raise ValueError("非 RESOLVED/UNRESOLVED 的 verdict 不能带 resolved")


# --------------------------------------------------------------------------- 现场证据核对


@dataclass(frozen=True, slots=True)
class WorkspaceAudit:
    """恢复前的现场核对结果。每一项都带**观测值**，拒绝时能给出证据。"""

    run_dir: Path
    workspace_present: bool
    base_commit_matches: bool
    patch_state: str
    """``match`` / ``mismatch`` / ``unknown``（现场里还没有补丁证据）。"""
    index_state: str
    """``usable`` / ``missing`` / ``corrupt``。缺失只提示（可重建），损坏则拒绝。"""
    artifacts_state: str
    """``readable`` / ``partial`` / ``unreadable``。"""
    checkpoint_state: str
    """``ok`` / ``absent`` / ``invalid``。"""
    config_matches: bool
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """所有**硬前提**都成立才允许续跑。"""
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "workspace_present": self.workspace_present,
            "base_commit_matches": self.base_commit_matches,
            "patch_state": self.patch_state,
            "index_state": self.index_state,
            "artifacts_state": self.artifacts_state,
            "checkpoint_state": self.checkpoint_state,
            "config_matches": self.config_matches,
            "ok": self.ok,
            "problems": list(self.problems),
            "notes": list(self.notes),
            "evidence": dict(self.evidence),
        }

    def describe(self) -> str:
        if self.ok:
            return "现场核对通过"
        return "现场核对失败：" + "; ".join(self.problems)


def _checkpoint_schema_version() -> int:
    """延迟导入，避免让只读本模块的轻量脚本被迫加载 langgraph。"""
    from codepilot.graph.checkpoint import CHECKPOINT_SCHEMA_VERSION

    return CHECKPOINT_SCHEMA_VERSION


def _sqlite_quick_check(path: Path) -> bool:
    """只读打开 SQLite 并跑 ``PRAGMA quick_check``。坏文件必须显式返回 False。"""
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return bool(row) and row[0] == "ok"


def _workspace_patch_sha(workspace: Path, base_commit: str) -> str | None:
    try:
        diff = generate_git_diff(workspace, base_commit)
    except (GitError, OSError, ValueError):
        return None
    return _sha256_text(diff.text)


def _identity_diffs(expected: RunIdentity, observed: RunIdentity) -> dict[str, Any]:
    """比较两份身份；``patch_hash`` 单独处理（规划阶段为空是合法的）。"""
    diffs: dict[str, Any] = {}
    expected_payload = expected.payload()
    observed_payload = observed.payload()
    for key, value in expected_payload.items():
        if key == "patch_hash":
            continue
        if observed_payload.get(key) != value:
            diffs[key] = {"expected": value, "observed": observed_payload.get(key)}
    return diffs


def audit_workspace(
    *,
    artifact_root: Path,
    run_id: str,
    expected_identity: RunIdentity,
    expected_patch_sha256: str | None = None,
) -> WorkspaceAudit:
    """恢复前核对现场：存在 / base 一致 / 补丁一致 / 索引可用 / 产物可读 / checkpoint
    schema 一致 / 配置一致。任何一项不一致都拒绝续跑并给出证据。

    这里**不**重新推导 ``repository_id``、也不改写现场：核对是只读的。真正的
    workspace/base/patch 证据复核由 Phase 10 的 ``resume_run`` 在装配层再做一遍，
    两层各自负责自己能看见的证据，不互相替代。
    """
    root = Path(artifact_root)
    run_dir = root / "runs" / run_id
    workspace = run_dir / "workspace"
    manifest_path = run_dir / MANIFEST_NAME
    problems: list[str] = []
    notes: list[str] = []
    evidence: dict[str, Any] = {
        "run_dir": str(run_dir),
        "manifest": str(manifest_path),
        "workspace": str(workspace),
    }

    # ---- 1. 现场目录与 manifest（配置身份的唯一来源）
    workspace_present = workspace.is_dir()
    if not run_dir.is_dir():
        problems.append(f"现场目录不存在：{run_dir}")
    if not workspace_present:
        problems.append(f"workspace 不存在：{workspace}")
    manifest, manifest_problem = _read_json_object(manifest_path)
    if manifest_problem is not None:
        problems.append(f"现场 manifest 不可读：{manifest_problem}")
    manifest_identity: RunIdentity | None = None
    if manifest is not None:
        try:
            manifest_identity = RunIdentity.model_validate(manifest.get("identity") or {})
        except ValueError as exc:
            problems.append(f"现场 manifest 的身份字段不合法：{exc}")

    # ---- 2. base commit
    base_commit_matches = False
    observed_head: str | None = None
    if workspace_present:
        try:
            observed_head = run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip() or None
        except (GitError, OSError) as exc:
            problems.append(f"无法读取 workspace HEAD：{type(exc).__name__}: {exc}")
        if observed_head is None:
            base_commit_matches = False
        else:
            base_commit_matches = observed_head == expected_identity.base_commit
            if not base_commit_matches:
                problems.append(
                    f"workspace HEAD {observed_head!r} 与 base_commit "
                    f"{expected_identity.base_commit!r} 不一致"
                )
    evidence["head"] = observed_head

    # ---- 3. 补丁哈希
    expected_patch = expected_patch_sha256
    if expected_patch is None and manifest_identity is not None and manifest_identity.patch_hash:
        expected_patch = manifest_identity.patch_hash
    observed_patch: str | None = None
    patch_state = "unknown"
    if expected_patch is not None:
        observed_patch = (
            _workspace_patch_sha(workspace, expected_identity.base_commit)
            if workspace_present
            else None
        )
        if observed_patch is None:
            patch_state = "mismatch"
            problems.append("无法计算 workspace 当前补丁哈希（补丁证据不可核对）")
        elif observed_patch == expected_patch:
            patch_state = "match"
        else:
            # diff 生成路径不同（例如复现测试目录策略）可能得到规范化形式不同的文本，
            # 再用规范化补丁哈希复核一次；仍然不同才是真的不一致。
            from codepilot.graph.stop_rules import patch_hash

            try:
                canonical_observed = patch_hash(
                    generate_git_diff(workspace, expected_identity.base_commit).text
                )
            except (GitError, OSError, ValueError):
                canonical_observed = None
            if canonical_observed is not None and canonical_observed == expected_patch:
                patch_state = "match"
                notes.append("补丁哈希通过规范化形式匹配")
            else:
                patch_state = "mismatch"
                problems.append(
                    f"workspace 补丁 {observed_patch!r} 与记录的补丁哈希 "
                    f"{expected_patch!r} 不一致；旧证据已失效"
                )
        notes.append("现场补丁证据已核对")
    else:
        notes.append("现场还没有补丁证据（推理尚未完成），跳过补丁哈希比对")
    evidence["patch_sha256_observed"] = observed_patch
    evidence["patch_sha256_expected"] = expected_patch

    # ---- 4. 索引可用性
    index_path = run_dir / "index.sqlite3"
    if not index_path.is_file():
        index_state = "missing"
        notes.append("索引库缺失；续跑会重建索引（不影响补丁证据，只多花一次索引时间）")
    elif _sqlite_quick_check(index_path):
        index_state = "usable"
    else:
        index_state = "corrupt"
        problems.append(f"索引库损坏：{index_path}")
    evidence["index"] = str(index_path)

    # ---- 5. 产物可读
    artifacts_state = "readable"
    known_artifacts = (
        MANIFEST_NAME,
        PATCH_NAME,
        OFFICIAL_ENVELOPE_NAME,
        OFFICIAL_REPORT_NAME,
        "events.jsonl",
        "llm_requests.jsonl",
        "workspace_identity.json",
        "checkpoint_manifest.json",
    )
    missing: list[str] = []
    unreadable: list[str] = []
    for name in known_artifacts:
        target = run_dir / name
        if not target.is_file():
            if name in {MANIFEST_NAME, OFFICIAL_ENVELOPE_NAME}:
                missing.append(name)
            continue
        try:
            target.read_bytes()
        except OSError as exc:
            unreadable.append(f"{name} ({type(exc).__name__})")
    if missing:
        artifacts_state = "partial" if manifest is not None else "unreadable"
        notes.append(f"现场缺少这些产物（不一定是坏证据）：{', '.join(sorted(missing))}")
    if unreadable:
        artifacts_state = "unreadable"
        problems.append(f"现场产物不可读：{', '.join(sorted(unreadable))}")
    evidence["artifact_files"] = (
        sorted(path.name for path in run_dir.iterdir() if path.is_file())
        if run_dir.is_dir()
        else []
    )

    # ---- 6. checkpoint schema（有 checkpoint 就必须有可解释的 manifest）
    checkpoint_path = run_dir / "checkpoints.sqlite"
    checkpoint_manifest_path = run_dir / "checkpoint_manifest.json"
    checkpoint_state = "absent"
    if checkpoint_path.is_file():
        checkpoint_manifest, checkpoint_problem = _read_json_object(checkpoint_manifest_path)
        if checkpoint_problem is not None or checkpoint_manifest is None:
            checkpoint_state = "invalid"
            problems.append(f"checkpoint 缺少可读的 manifest：{checkpoint_problem or '未知原因'}")
        else:
            try:
                schema_version = int(checkpoint_manifest.get("schema_version", -1))
            except (TypeError, ValueError):
                schema_version = -1
            expected_version = _checkpoint_schema_version()
            recorded_base = str(checkpoint_manifest.get("base_commit") or "")
            if schema_version != expected_version:
                checkpoint_state = "invalid"
                problems.append(
                    f"checkpoint schema {schema_version} 与当前代码 "
                    f"{expected_version} 不一致；不能拿旧 schema 猜新代码"
                )
            elif recorded_base != expected_identity.base_commit:
                checkpoint_state = "invalid"
                problems.append(
                    f"checkpoint 记录的 base {recorded_base!r} 与任务 "
                    f"{expected_identity.base_commit!r} 不一致"
                )
            else:
                checkpoint_state = "ok"
    evidence["checkpoint"] = str(checkpoint_path)
    evidence["checkpoint_manifest"] = str(checkpoint_manifest_path)

    # ---- 7. 配置身份一致
    config_matches = False
    if manifest_identity is not None:
        diffs = _identity_diffs(expected_identity, manifest_identity)
        config_matches = not diffs
        if diffs:
            problems.append(f"现场配置与本次运行不一致：{_canonical(diffs)}")
    evidence["manifest_identity"] = None if manifest is None else manifest.get("identity")
    evidence["identity_diffs"] = (
        {} if manifest_identity is None else _identity_diffs(expected_identity, manifest_identity)
    )

    return WorkspaceAudit(
        run_dir=run_dir,
        workspace_present=workspace_present,
        base_commit_matches=base_commit_matches,
        patch_state=patch_state,
        index_state=index_state,
        artifacts_state=artifacts_state,
        checkpoint_state=checkpoint_state,
        config_matches=config_matches,
        problems=tuple(problems),
        notes=tuple(notes),
        evidence=evidence,
    )


@dataclass(frozen=True, slots=True)
class InferenceEvidenceAudit:
    """评测级恢复：现场里"推理已完成"的证据是否成立且属于同一身份。"""

    run_id: str
    run_dir: Path
    manifest_path: Path
    identity: RunIdentity | None
    patch: str | None
    patch_sha256: str | None
    inference_summary: Mapping[str, Any]
    problems: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.problems and self.patch is not None and self.identity is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "usable": self.usable,
            "patch_sha256": self.patch_sha256,
            "identity": None if self.identity is None else self.identity.as_dict(),
            "problems": list(self.problems),
        }


def recover_inference_evidence(
    *,
    artifact_root: Path,
    run_id: str,
    expected_identity: RunIdentity,
) -> InferenceEvidenceAudit:
    """找回已完成的推理证据（补丁 + 推理摘要），用于"只缺评测"的恢复。

    只接受 manifest 状态为 ``inference_complete`` / ``evaluated`` 且补丁文件哈希与
    manifest 身份一致、身份其余字段与本次任务一致的现场。任何一项不符都返回
    ``usable=False`` 与具体原因，让调用方要么重跑推理、要么把原因写进报告。
    """
    run_dir = Path(artifact_root) / "runs" / run_id
    manifest_path = run_dir / MANIFEST_NAME
    problems: list[str] = []
    manifest, manifest_problem = _read_json_object(manifest_path)
    if manifest_problem is not None or manifest is None:
        return InferenceEvidenceAudit(
            run_id=run_id,
            run_dir=run_dir,
            manifest_path=manifest_path,
            identity=None,
            patch=None,
            patch_sha256=None,
            inference_summary={},
            problems=(manifest_problem or "现场 manifest 不可读",),
        )
    try:
        schema_version = int(manifest.get("schema_version", -1))
    except (TypeError, ValueError):
        schema_version = -1
    if schema_version != MANIFEST_SCHEMA_VERSION:
        problems.append(
            f"现场 manifest schema {schema_version} 与当前 {MANIFEST_SCHEMA_VERSION} 不一致"
        )

    identity: RunIdentity | None = None
    try:
        identity = RunIdentity.model_validate(manifest.get("identity") or {})
    except ValueError as exc:
        problems.append(f"现场 manifest 身份字段不合法：{exc}")

    diffs: dict[str, Any] = {}
    if identity is not None:
        diffs = _identity_diffs(expected_identity, identity)
        if diffs:
            problems.append(f"现场身份与本次任务不一致：{_canonical(diffs)}")

    status = str(manifest.get("status") or "")
    if status not in {"inference_complete", "evaluated"}:
        problems.append(f"现场状态 {status!r} 不是推理完成态，不能复用推理证据")

    summary = manifest.get("inference")
    inference_summary: Mapping[str, Any] = summary if isinstance(summary, dict) else {}
    if not inference_summary:
        problems.append("现场缺少推理摘要（token/费用/迭代次数），无法在不重跑推理的情况下记账")

    patch: str | None = None
    patch_sha256: str | None = None
    patch_path = run_dir / PATCH_NAME
    if not patch_path.is_file():
        problems.append(f"补丁文件不存在：{patch_path}")
    else:
        try:
            patch = patch_path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"补丁文件不可读：{type(exc).__name__}: {exc}")
        if patch is not None:
            patch_sha256 = _sha256_text(patch)
            recorded = identity.patch_hash if identity is not None else ""
            if recorded and recorded != patch_sha256:
                problems.append(
                    f"补丁文件哈希 {patch_sha256!r} 与 manifest 记录 {recorded!r} 不一致"
                )
            elif not recorded:
                problems.append("manifest 没有记录补丁哈希，无法确认补丁身份")

    return InferenceEvidenceAudit(
        run_id=run_id,
        run_dir=run_dir,
        manifest_path=manifest_path,
        identity=identity,
        patch=patch,
        patch_sha256=patch_sha256,
        inference_summary=inference_summary,
        problems=tuple(problems),
    )


@dataclass(frozen=True, slots=True)
class OfficialEvidenceAudit:
    """评测级恢复：现场里的官方评测证据是否完整且属于**同一补丁/同一 run_id**。"""

    run_id: str
    run_dir: Path
    envelope_path: Path
    report_path: Path | None
    envelope: Mapping[str, Any] | None
    verdict: str | None
    resolved: bool | None
    patch_sha256: str | None
    harness_run_id: str | None
    report_sha256: str | None
    environment_identity: str | None = None
    environment_identity_verified: bool = False
    problems: tuple[str, ...] = ()

    @property
    def decidable(self) -> bool:
        return self.verdict in _DECIDABLE_VERDICTS and self.resolved is not None

    @property
    def reusable(self) -> bool:
        """完整、身份一致、且给出可判定结论才允许复用。"""
        return not self.problems and self.decidable and self.report_path is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "envelope": str(self.envelope_path),
            "report": None if self.report_path is None else str(self.report_path),
            "verdict": self.verdict,
            "resolved": self.resolved,
            "patch_sha256": self.patch_sha256,
            "harness_run_id": self.harness_run_id,
            "report_sha256": self.report_sha256,
            "environment_identity": self.environment_identity,
            "environment_identity_verified": self.environment_identity_verified,
            "reusable": self.reusable,
            "problems": list(self.problems),
        }


def audit_official_evidence(
    *,
    artifact_root: Path,
    run_id: str,
    expected_instance_id: str,
    expected_patch_sha256: str,
    expected_harness_run_id: str,
    expected_harness_version: str,
    expected_dataset_revision: str,
    expected_seed: int,
    expected_config_hash: str,
    expected_environment_identity: str | None = None,
) -> OfficialEvidenceAudit:
    """核对现场里的官方评测证据。

    必须同时满足这些条件才算"可复用"，缺一条都会重跑评测（并让旧报告无法冒充新结果）：

    * 评测信封（``official_eval.json``）里的 instance / patch / harness run_id /
      harness 版本 / dataset revision / seed / config_hash 全部与本次一致；
    * 信封里的 ``report_sha256`` 与 ``official_report.json`` 的实际哈希一致；
    * 信封给出的是可判定结论（RESOLVED / UNRESOLVED）。

    只读；不修改现场。``problems`` 是给报告与拒绝信息用的证据，不是控制流。
    """
    run_dir = Path(artifact_root) / "runs" / run_id
    envelope_path = run_dir / OFFICIAL_ENVELOPE_NAME
    report_path = run_dir / OFFICIAL_REPORT_NAME
    problems: list[str] = []
    envelope, envelope_problem = _read_json_object(envelope_path)
    if envelope_problem is not None or envelope is None:
        return OfficialEvidenceAudit(
            run_id=run_id,
            run_dir=run_dir,
            envelope_path=envelope_path,
            report_path=None,
            envelope=None,
            verdict=None,
            resolved=None,
            patch_sha256=None,
            harness_run_id=None,
            report_sha256=None,
            problems=(f"官方评测信封不可读：{envelope_problem or '未知原因'}",),
        )

    expectations: dict[str, Any] = {
        "instance_id": expected_instance_id,
        "patch_sha256": expected_patch_sha256,
        "harness_run_id": expected_harness_run_id,
        "harness_version": expected_harness_version,
        "dataset_revision": expected_dataset_revision,
        "config_hash": expected_config_hash,
    }
    for key, expected in expectations.items():
        observed = envelope.get(key)
        if observed != expected:
            problems.append(f"评测信封 {key} {observed!r} 与本次 {expected!r} 不一致")
    observed_seed = envelope.get("seed")
    if observed_seed != expected_seed:
        problems.append(f"评测信封 seed {observed_seed!r} 与本次 {expected_seed!r} 不一致")

    try:
        schema_version = int(envelope.get("schema_version", -1))
    except (TypeError, ValueError):
        schema_version = -1
    if schema_version != MANIFEST_SCHEMA_VERSION:
        problems.append(f"评测信封 schema {schema_version} 与当前 {MANIFEST_SCHEMA_VERSION} 不一致")

    verdict = envelope.get("verdict")
    resolved_raw = envelope.get("resolved")
    resolved = resolved_raw if isinstance(resolved_raw, bool) else None
    report_sha256 = envelope.get("report_sha256")

    actual_report_sha: str | None = None
    usable_report_path: Path | None = None
    if report_path.is_file():
        try:
            actual_report_sha = _sha256_file(report_path)
        except OSError as exc:
            problems.append(f"官方报告不可读：{type(exc).__name__}: {exc}")
    else:
        problems.append(f"官方报告不存在：{report_path}")
    if isinstance(report_sha256, str) and actual_report_sha is not None:
        if report_sha256 != actual_report_sha:
            problems.append(
                f"官方报告哈希 {actual_report_sha!r} 与信封记录 {report_sha256!r} 不一致"
            )
        else:
            usable_report_path = report_path
    elif not isinstance(report_sha256, str):
        problems.append("评测信封没有记录官方报告哈希")

    if verdict in _DECIDABLE_VERDICTS and resolved is None:
        problems.append(f"verdict={verdict!r} 却没有三态 resolved 结论")

    # 环境身份（image ids 汇总）是可选的强校验：注入了就必须对得上，否则同一份补丁
    # 在另一套镜像里评出来的结论不能被当成当前环境的结论复用。
    observed_environment = envelope.get("environment_identity")
    environment_identity = observed_environment if isinstance(observed_environment, str) else None
    environment_identity_verified = False
    if expected_environment_identity is not None:
        if not environment_identity:
            problems.append("评测信封没有记录环境身份，无法确认报告属于当前镜像")
        elif environment_identity != expected_environment_identity:
            problems.append(
                f"评测信封环境身份 {environment_identity!r} 与本次 "
                f"{expected_environment_identity!r} 不一致"
            )
        else:
            environment_identity_verified = True

    return OfficialEvidenceAudit(
        run_id=run_id,
        run_dir=run_dir,
        envelope_path=envelope_path,
        report_path=usable_report_path,
        envelope=envelope,
        verdict=None if verdict is None else str(verdict),
        resolved=resolved,
        patch_sha256=None
        if envelope.get("patch_sha256") is None
        else str(envelope.get("patch_sha256")),
        harness_run_id=None
        if envelope.get("harness_run_id") is None
        else str(envelope.get("harness_run_id")),
        report_sha256=report_sha256 if isinstance(report_sha256, str) else None,
        environment_identity=environment_identity,
        environment_identity_verified=environment_identity_verified,
        problems=tuple(problems),
    )


# --------------------------------------------------------------------------- 运行时事实


@dataclass(frozen=True, slots=True)
class _InferenceFacts:
    """一次推理的记账事实。恢复时从现场 manifest 重建，**不重跑推理**。"""

    status: str = "not_executed"
    stop_reason: str | None = None
    final_patch_sha256: str | None = None
    iterations: int | None = None
    usage: Usage = field(default_factory=Usage)
    usage_uncertain: bool = True
    runtime_seconds: float | None = None
    simulated_llm: bool = False
    sandbox_backend: str = "none"
    verification_mode: str = VerificationMode.NONE.value
    agent_claims_success: bool = False
    empty_patch: bool = True
    invalid_patch: bool = False
    error_class: str | None = None
    error_origin: str | None = None
    failure_reasons: tuple[str, ...] = ()
    checkpointed: bool = False
    resumed_from_checkpoint: bool = False
    plan_corrections: int = 0
    plan_tool_audit: Mapping[str, Any] = field(default_factory=dict)
    llm_requests: Mapping[str, Any] = field(default_factory=dict)
    # Phase 11 闭环：内部自验证的逐阶段真相，与官方判分各记各的。恢复时这些数字要
    # 从现场 manifest 原样读回——读不回来就会把"已经审过"的现场写成"从未执行"。
    verification_level: str = ""
    review_executed: bool = False
    review_bound_to_candidate: bool = False
    review_high_issues: int = 0
    post_review_repair: bool = False
    unexecuted_stages: tuple[str, ...] = ()
    coding_stop_reason: str = ""


def _facts_from_outcome(outcome: InferenceOutcome) -> _InferenceFacts:
    return _InferenceFacts(
        status=str(outcome.status),
        stop_reason=outcome.stop_reason,
        final_patch_sha256=outcome.final_patch_sha256 or None,
        iterations=outcome.iterations,
        usage=outcome.usage.model_copy(deep=True),
        usage_uncertain=bool(outcome.usage_uncertain),
        runtime_seconds=outcome.runtime_seconds,
        simulated_llm=bool(outcome.simulated_llm),
        sandbox_backend=str(outcome.sandbox_backend),
        verification_mode=str(outcome.verification_mode),
        agent_claims_success=bool(outcome.agent_claims_success),
        empty_patch=bool(outcome.empty_patch),
        invalid_patch=bool(outcome.invalid_patch),
        error_class=outcome.error_class,
        error_origin=outcome.error_origin,
        failure_reasons=tuple(outcome.failure_reasons),
        checkpointed=bool(outcome.checkpointed),
        resumed_from_checkpoint=bool(outcome.resumed_from_checkpoint),
        plan_corrections=int(outcome.plan_corrections),
        plan_tool_audit=dict(outcome.plan_tool_audit),
        llm_requests=dict(outcome.llm_requests),
        verification_level=str(outcome.verification_level),
        review_executed=bool(outcome.review_executed),
        review_bound_to_candidate=bool(outcome.review_bound_to_candidate),
        review_high_issues=int(outcome.review_high_issues),
        post_review_repair=bool(outcome.post_review_repair),
        unexecuted_stages=tuple(outcome.unexecuted_stages),
        coding_stop_reason=str(outcome.coding_stop_reason),
    )


def _usage_to_payload(usage: Usage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cost": usage.cost,
        "cost_unknown_calls": usage.cost_unknown_calls,
        "llm_calls": usage.llm_calls,
        "currency": usage.currency,
    }


def _usage_from_payload(payload: Mapping[str, Any]) -> Usage:
    def _int(key: str) -> int:
        value = payload.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    def _float(key: str) -> float:
        value = payload.get(key)
        return float(value) if isinstance(value, (int, float)) else 0.0

    return Usage(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cached_input_tokens=_int("cached_input_tokens"),
        cost=_float("cost"),
        cost_unknown_calls=_int("cost_unknown_calls"),
        llm_calls=_int("llm_calls"),
        currency=str(payload.get("currency") or "CNY"),
    )


def _facts_to_payload(facts: _InferenceFacts) -> dict[str, Any]:
    return {
        "status": facts.status,
        "stop_reason": facts.stop_reason,
        "final_patch_sha256": facts.final_patch_sha256,
        "iterations": facts.iterations,
        "usage": _usage_to_payload(facts.usage),
        "usage_uncertain": facts.usage_uncertain,
        "runtime_seconds": facts.runtime_seconds,
        "simulated_llm": facts.simulated_llm,
        "sandbox_backend": facts.sandbox_backend,
        "verification_mode": facts.verification_mode,
        "agent_claims_success": facts.agent_claims_success,
        "empty_patch": facts.empty_patch,
        "invalid_patch": facts.invalid_patch,
        "error_class": facts.error_class,
        "error_origin": facts.error_origin,
        "failure_reasons": list(facts.failure_reasons),
        "checkpointed": facts.checkpointed,
        "resumed_from_checkpoint": facts.resumed_from_checkpoint,
        "plan_corrections": facts.plan_corrections,
        "plan_tool_audit": dict(facts.plan_tool_audit),
        "llm_requests": dict(facts.llm_requests),
        "verification_level": facts.verification_level,
        "review_executed": facts.review_executed,
        "review_bound_to_candidate": facts.review_bound_to_candidate,
        "review_high_issues": facts.review_high_issues,
        "post_review_repair": facts.post_review_repair,
        "unexecuted_stages": list(facts.unexecuted_stages),
        "coding_stop_reason": facts.coding_stop_reason,
    }


def _facts_from_payload(payload: Mapping[str, Any]) -> _InferenceFacts:
    usage_payload = payload.get("usage")
    usage = _usage_from_payload(usage_payload) if isinstance(usage_payload, dict) else Usage()
    reasons = payload.get("failure_reasons")
    plan_audit = payload.get("plan_tool_audit")
    llm_requests = payload.get("llm_requests")
    stages = payload.get("unexecuted_stages")
    return _InferenceFacts(
        status=str(payload.get("status") or "unknown"),
        stop_reason=payload.get("stop_reason"),
        final_patch_sha256=payload.get("final_patch_sha256"),
        iterations=payload.get("iterations")
        if isinstance(payload.get("iterations"), int)
        else None,
        usage=usage,
        usage_uncertain=bool(payload.get("usage_uncertain", True)),
        runtime_seconds=payload.get("runtime_seconds")
        if isinstance(payload.get("runtime_seconds"), (int, float))
        else None,
        simulated_llm=bool(payload.get("simulated_llm", False)),
        sandbox_backend=str(payload.get("sandbox_backend") or "none"),
        verification_mode=str(payload.get("verification_mode") or VerificationMode.NONE.value),
        agent_claims_success=bool(payload.get("agent_claims_success", False)),
        empty_patch=bool(payload.get("empty_patch", True)),
        invalid_patch=bool(payload.get("invalid_patch", False)),
        error_class=payload.get("error_class"),
        error_origin=payload.get("error_origin"),
        failure_reasons=tuple(str(item) for item in reasons) if isinstance(reasons, list) else (),
        checkpointed=bool(payload.get("checkpointed", False)),
        resumed_from_checkpoint=bool(payload.get("resumed_from_checkpoint", False)),
        plan_corrections=int(payload.get("plan_corrections") or 0),
        plan_tool_audit=dict(plan_audit) if isinstance(plan_audit, dict) else {},
        llm_requests=dict(llm_requests) if isinstance(llm_requests, dict) else {},
        verification_level=str(payload.get("verification_level") or ""),
        review_executed=bool(payload.get("review_executed", False)),
        review_bound_to_candidate=bool(payload.get("review_bound_to_candidate", False)),
        review_high_issues=int(payload.get("review_high_issues") or 0),
        post_review_repair=bool(payload.get("post_review_repair", False)),
        unexecuted_stages=(tuple(str(item) for item in stages) if isinstance(stages, list) else ()),
        coding_stop_reason=str(payload.get("coding_stop_reason") or ""),
    )


# --------------------------------------------------------------------------- 结果


@dataclass(frozen=True, slots=True)
class _TaskResult:
    """一次 run 里单个 instance 的会话级事实（记录本身进 journal）。"""

    record: TaskRecord
    reused: bool = False
    graph_resumed: bool = False
    evaluation_reused: bool = False
    audit: WorkspaceAudit | None = None


@dataclass(frozen=True, slots=True)
class RunReport:
    """一次 SWE-bench 批次的全部结果与指标（契约 §10 的 ``RunReport``）。"""

    run_id: str
    config: SWEBenchRunConfig
    seed: int
    resume: ResumeLevel
    subset_hash: str
    identity: Mapping[str, Any]
    records: tuple[TaskRecord, ...]
    declared_tasks: int
    ledger: Mapping[str, Any]
    reuse: Mapping[str, int]
    workspace_audits: Mapping[str, Mapping[str, Any]]
    stop_reason: str
    started_at: str
    finished_at: str

    # ------------------------------------------------------------------ 基础视图

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self.records:
            out[record.execution_state] = out.get(record.execution_state, 0) + 1
        return dict(sorted(out.items()))

    def attribution_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self.records:
            key = _swebench_attribution(record).value
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))

    def metric_inputs(self) -> list[TaskOutcome]:
        """给 Phase 10 指标纯函数用的任务级结论。"""
        return [record.metric_view() for record in self.records]

    # ------------------------------------------------------------------ 指标

    def integrity(self) -> str:
        declared = self.declared_tasks
        recorded = len(self.records)
        details: list[str] = []
        if recorded != declared:
            details.append(f"recorded {recorded}/{declared}")
        state_counts = self.counts()
        budget_blocked = state_counts.get(ExecutionState.BUDGET_BLOCKED.value, 0)
        inference_errors = state_counts.get(ExecutionState.INFERENCE_ERROR.value, 0)
        evaluation_errors = state_counts.get(ExecutionState.EVALUATION_ERROR.value, 0)
        if budget_blocked:
            details.append(f"budget-blocked {budget_blocked}")
        if inference_errors:
            details.append(f"inference-error {inference_errors}")
        if evaluation_errors:
            details.append(f"evaluation-error {evaluation_errors}")
        unknown_cost = self.ledger.get("unknown_cost_tasks", 0)
        if unknown_cost:
            details.append(f"unknown-cost {unknown_cost}")
        return "full" if not details else "partial: " + "; ".join(details)

    def metrics(self) -> dict[str, Any]:
        """指标全集。两个分母口径同时给出，且每个比例都带 ``numerator/denominator``。"""
        records = list(self.records)
        total = len(records)
        outcomes = self.metric_inputs()

        completed_inference = [
            record for record in records if record.execution_state == ExecutionState.COMPLETED.value
        ]
        evaluated = [
            record
            for record in records
            if record.evaluation_state == EvaluationState.EVALUATED.value
        ]
        resolved = [record for record in records if record.resolved is True]
        unresolved = [record for record in records if record.resolved is False]
        valid_predictions = [
            record
            for record in completed_inference
            if record.patch_sha256 and not record.empty_patch and not record.invalid_patch
        ]
        refused = [
            record
            for record in records
            if record.execution_state == ExecutionState.NOT_EXECUTED.value
        ]
        # 预算截停有两层：派发前被整批上限拦下（BUDGET_BLOCKED），以及单任务预算在
        # 执行中耗尽（归因 BUDGET_STOPPED）。两者都算"被预算截停"，但只有前者会
        # 让整批停止派发——混成一个数会看不出是哪一层出了问题。
        batch_budget_blocked = [
            record
            for record in records
            if record.execution_state == ExecutionState.BUDGET_BLOCKED.value
        ]
        budget_stopped = [
            record
            for record in records
            if record.execution_state == ExecutionState.BUDGET_BLOCKED.value
            or _swebench_attribution(record) is SWEBenchAttribution.BUDGET_STOPPED
        ]
        harness_errors = [
            record
            for record in records
            if _swebench_attribution(record)
            in {
                SWEBenchAttribution.HARNESS_INTERNAL_ERROR,
                SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR,
            }
        ]
        env_errors = [
            record
            for record in records
            if _swebench_attribution(record)
            in {
                SWEBenchAttribution.ENV_ERROR,
                SWEBenchAttribution.WORKSPACE_ERROR,
                SWEBenchAttribution.SANDBOX_ERROR,
            }
        ]
        claimed = [record for record in records if record.agent_claims_success]
        claimed_without_official = [
            record
            for record in claimed
            if record.evaluation_state != EvaluationState.EVALUATED.value
        ]
        resolved_and_valid = [record for record in resolved if record in valid_predictions]
        # 拿到官方判定、但环境/镜像身份未注入（= 未验证）的任务数。不能静默当成已验证。
        environment_identity_unverified = [
            record
            for record in evaluated
            if record.artifact_refs.get("environment_identity_verified") != "true"
        ]

        cost = cost_metrics(outcomes)
        reliability = reliability_metrics(outcomes)
        iterations = iterations_metrics(outcomes)
        legacy_attribution = attribution_metrics(outcomes)

        attempts = sum(1 for record in records if (record.llm_calls or 0) > 0)
        total_iterations = sum(record.iterations or 0 for record in records)
        official_runtime = 0.0
        for record in records:
            value = record.artifact_refs.get("official_runtime_seconds")
            if value:
                try:
                    official_runtime += float(value)
                except ValueError:
                    continue

        counts = {
            "declared_tasks": self.declared_tasks,
            "dispatched": total,
            "completed_inference": len(completed_inference),
            "valid_predictions": len(valid_predictions),
            "officially_evaluated": len(evaluated),
            "official_resolved": len(resolved),
            "official_unresolved": len(unresolved),
            "harness_error": len(harness_errors),
            "env_error": len(env_errors),
            "budget_stopped": len(budget_stopped),
            "batch_budget_blocked": len(batch_budget_blocked),
            "not_executed": len(refused),
            "patch_invalid": sum(
                1
                for record in records
                if _swebench_attribution(record) is SWEBenchAttribution.PATCH_INVALID
            ),
            "model_claimed_success": len(claimed),
            "model_claimed_success_without_official": len(claimed_without_official),
            "unknown_cost_tasks": cost.cost_tasks_unknown,
            "environment_identity_unverified_evaluations": len(environment_identity_unverified),
            "reused_tasks": self.reuse.get("reused_tasks", 0),
            "graph_resumed": self.reuse.get("graph_resumed", 0),
            "evaluation_reused": self.reuse.get("evaluation_reused", 0),
            "inference_rerun": self.reuse.get("inference_rerun", 0),
        }
        denominators = {
            "all_target_tasks": total,
            "officially_evaluated": len(evaluated),
            "completed_inference": len(completed_inference),
            "valid_predictions": len(valid_predictions),
            "note": (
                "官方解决率给两个口径：端到端口径分母=全部目标任务（含预算截停/基础设施"
                "故障），条件口径分母=真正拿到官方判定的任务。条件口径必须与 "
                "evaluation_coverage 一起读，覆盖率低时的条件解决率是幸存者偏差。"
            ),
        }
        ratios = {
            "evaluation_coverage": Ratio(len(evaluated), total).as_dict(),
            "official_resolved_rate": Ratio(len(resolved), total).as_dict(),
            "conditional_official_resolved_rate": Ratio(len(resolved), len(evaluated)).as_dict(),
            "official_resolved_rate_among_valid": Ratio(
                len(resolved_and_valid), len(valid_predictions)
            ).as_dict(),
            "patch_validity": Ratio(len(valid_predictions), len(completed_inference)).as_dict(),
            "patch_validity_end_to_end": Ratio(len(valid_predictions), total).as_dict(),
            "harness_error_rate": Ratio(len(harness_errors), total).as_dict(),
            "env_error_rate": Ratio(len(env_errors), total).as_dict(),
            "budget_stopped_rate": Ratio(len(budget_stopped), total).as_dict(),
        }
        tokens = {
            "input": cost.total_input_tokens,
            "output": cost.total_output_tokens,
            "total": cost.total_tokens,
            "mean_per_task": cost.mean_tokens_per_task,
            "tasks_recorded": cost.tokens_tasks_recorded,
            "llm_calls": cost.total_llm_calls,
        }
        cost_view = cost.as_dict()
        cost_view.update(
            {
                "attempts_recorded": attempts,
                "total_iterations_recorded": total_iterations,
                "mean_cost_per_attempt": (
                    cost.total_cost / attempts if cost.total_cost is not None and attempts else None
                ),
                "mean_cost_per_iteration": (
                    cost.total_cost / total_iterations
                    if cost.total_cost is not None and total_iterations
                    else None
                ),
                "note": (
                    "成本只含 LLM token 费用；官方 Harness 的 Docker 构建/执行成本另计，"
                    "两者不合并（币种与计量单位都不同）。总费用为下界时表示有任务费用未知。"
                ),
            }
        )
        runtime = {
            "total_seconds": (
                sum(item.runtime_seconds for item in outcomes if item.runtime_seconds is not None)
                if any(item.runtime_seconds is not None for item in outcomes)
                else None
            ),
            "mean_seconds": cost.mean_runtime_seconds,
            "tasks_recorded": cost.runtime_tasks_recorded,
            "official_evaluation_seconds": official_runtime,
        }

        return {
            "schema_version": 1,
            "run": {
                "run_id": self.run_id,
                "seed": self.seed,
                "resume_level": self.resume.value,
                "subset_hash": self.subset_hash,
                "dataset": self.config.dataset_name,
                "revision": self.config.revision,
                "split": self.config.split,
                "harness_version": self.config.harness_version,
                "provider": self.config.provider,
                "model": self.config.model,
                "model_identity": self.config.model_identity,
                "config_hash": self.config.config_hash,
                "provider_config_hash": self.config.provider_config_hash,
                "code_commit": str(self.identity.get("code_commit") or ""),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            },
            "integrity": self.integrity(),
            "stop_reason": self.stop_reason,
            "counts": counts,
            "denominators": denominators,
            "ratios": ratios,
            "tokens": tokens,
            "cost": cost_view,
            "runtime": runtime,
            "attribution_counts": self.attribution_counts(),
            "stop_reasons": reliability["stop_reasons"],
            "iterations": iterations.as_dict(),
            "budget": dict(self.ledger),
            "workspace_audits": {
                key: dict(value) for key, value in sorted(self.workspace_audits.items())
            },
            "legacy_metrics": {
                # 复用 Phase 10 已在测试里手算核对过的口径；注释明确它们是"旧口径视图"，
                # 不能替代上面的官方口径。
                "attribution": legacy_attribution.as_dict(),
                "iterations": iterations.as_dict(),
                "reliability": reliability,
            },
            "instances": [
                {
                    "instance_id": record.task_id,
                    "execution_state": record.execution_state,
                    "evaluation_status": record.evaluation_status,
                    "evaluation_state": record.evaluation_state,
                    "official_verdict": record.artifact_refs.get("official_verdict"),
                    "resolved": record.resolved,
                    "attribution": _swebench_attribution(record).value,
                    "patch_sha256": record.patch_sha256,
                    "agent_claims_success": record.agent_claims_success,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "llm_calls": record.llm_calls,
                    "cost": record.cost,
                    "cost_currency": record.cost_currency,
                    "runtime_seconds": record.runtime_seconds,
                    "iterations": record.iterations,
                    "stop_reason": record.stop_reason,
                }
                for record in records
            ],
            "data_integrity": {
                "every_number_computed_from_journal_records": True,
                "denominators_visible": True,
                "undefined_ratios_rendered_as_none": True,
                "infrastructure_failures_in_end_to_end_denominator": True,
                "reused_tasks_not_rebilled": True,
                "unknown_cost_not_recorded_as_zero": True,
                "old_official_reports_not_reused_for_different_patch": True,
                "environment_identity_not_assumed_verified": True,
            },
        }

    def as_dict(self) -> dict[str, Any]:
        return self.metrics()

    def write(self, directory: Path) -> Path:
        """把报告写成 ``swebench-report.json``（原子写）。"""
        target = Path(directory) / "swebench-report.json"
        return _atomic_write_json(target, self.metrics())


def _swebench_attribution(record: TaskRecord) -> SWEBenchAttribution:
    value = record.artifact_refs.get("swebench_attribution")
    if value:
        try:
            return SWEBenchAttribution(value)
        except ValueError:
            pass
    # 没有细粒度归因的旧记录按 Phase 10 归因反推，避免报告把类别写成空白。
    try:
        legacy = Attribution(record.attribution)
    except ValueError:
        return SWEBenchAttribution.HARNESS_INTERNAL_ERROR
    reverse = {legacy_value: key for key, legacy_value in LEGACY_ATTRIBUTION.items()}
    return reverse.get(legacy, SWEBenchAttribution.HARNESS_INTERNAL_ERROR)


# --------------------------------------------------------------------------- Runner


class SWEBenchRunner:
    """SWE-bench 批次编排：规划 → 三层恢复 → 推理 → 官方评测 → 报告。

    构造参数全部由调用方注入，因此同一套编排既能接真实的 Loader / M2 图 / 官方
    Harness，也能接离线 Fake——两种场合在报告里靠 ``simulated_llm`` 与
    ``real_docker_evaluation`` 区分，不互相冒充。

    ``max_in_flight > 1`` 时并行的是**不同 instance**：journal/费用账本自带锁，
    但注入的 ``inference_factory`` 与 ``evaluator`` 必须是线程安全的（真实 M2 图每个
    实例一份装配，官方 Harness 的 ``max_workers`` 由适配层控制）。
    """

    def __init__(
        self,
        *,
        loader: SWEBenchLoaderLike,
        inference_factory: InferenceFactory,
        evaluator: SWEBenchEvaluatorLike,
        journal_path: Path,
        artifact_root: Path,
        config: SWEBenchRunConfig | None = None,
        code_commit: str | None = None,
    ) -> None:
        self._loader = loader
        self._factory = inference_factory
        self._evaluator = evaluator
        self._config = config or SWEBenchRunConfig()
        self._artifact_root = Path(artifact_root)
        journal_target = Path(journal_path)
        if journal_target.is_dir() or journal_target.suffix == "":
            # 既接受"目录"也接受"sqlite 文件"：前者更常见，后者在测试里更直白。
            journal_target = journal_target / "journal.sqlite"
        self._journal_path = journal_target
        self._journal = ResultJournal(journal_target)
        self._code = dict(code_identity())
        self._code_label = code_commit or (
            f"{str(self._code.get('commit') or 'unknown')[:12]}"
            f"{'-dirty' if self._code.get('dirty') else ''}"
        )
        self._ledger = self._new_ledger()
        self._batch_run_id = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 只读视图

    @property
    def config(self) -> SWEBenchRunConfig:
        return self._config

    @property
    def journal_path(self) -> Path:
        return self._journal_path

    @property
    def artifact_root(self) -> Path:
        return self._artifact_root

    @property
    def ledger(self) -> CostLedger:
        return self._ledger

    def _new_ledger(self) -> CostLedger:
        return CostLedger(
            max_total_cost=self._config.max_total_cost,
            reservation=self._config.task_reservation,
            currency=self._config.budget_currency,
        )

    def run_dir(self, base: RunIdentity) -> Path:
        """某条实例的确定性现场目录（图级恢复靠它认回旧现场）。"""
        return self._artifact_root / "runs" / self._base_run_id(base)

    def _base_run_id(self, base: RunIdentity) -> str:
        return f"cp-swe-{base.journal_key[:12]}-s{base.seed}"

    @staticmethod
    def _fresh_run_id(base: RunIdentity) -> str:
        return f"cp-swe-{base.journal_key[:12]}-s{base.seed}-r{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ 入口

    def run(
        self,
        instance_ids: Sequence[str],
        *,
        seed: int,
        resume: ResumeLevel,
    ) -> RunReport:
        """跑一批 instance。

        派发顺序 = 调用方给的顺序（子集文件本身的顺序稳定）；身份与指标与顺序无关。
        单任务失败不会带走整批：任务级异常被记成一条 ``INFERENCE_ERROR`` 记录，
        评测级异常被记成 ``EVALUATION_ERROR``，其他任务继续。

        **恢复必须传同一组 instance**：``subset_hash`` 进入运行身份，只传"剩下的失败
        任务"会被当成另一个子集，不会命中旧结论（这是"结果属于哪个批次"的绑定，不是
        缺陷）。重跑同一个子集文件即可命中任务级/图级/评测级恢复。
        """
        if seed < 0:
            raise ValueError("seed 不能为负")
        if not isinstance(resume, ResumeLevel):
            raise TypeError(f"resume 必须是 ResumeLevel，收到 {type(resume).__name__}")
        instances = list(instance_ids)
        for instance_id in instances:
            if not instance_id.strip() or "/" in instance_id or "\\" in instance_id:
                raise ValueError(f"非法 instance_id: {instance_id!r}")
            if instance_id in {".", ".."}:
                raise ValueError(f"非法 instance_id: {instance_id!r}")
        duplicates = sorted({item for item in instances if instances.count(item) > 1})
        if duplicates:
            raise ValueError(f"重复的 instance_id: {duplicates[:5]}")

        subset = subset_hash(instances)
        started_at = _now()
        self._ledger = self._new_ledger()
        self._batch_run_id = f"{self._config.run_label}-{subset[:8]}-s{seed}-{resume.value}"

        planned: list[tuple[SWEBenchInferenceTask, RunIdentity, TaskRecord | None]] = []
        for instance_id in instances:
            task = self._loader.get_inference_task(self._config.split, instance_id)
            self._check_task(task, instance_id)
            base = self._base_identity(task, seed, subset)
            prior = self._journal.lookup(base.journal_key)
            planned.append((task, base, prior))

        results: list[_TaskResult]
        if len(planned) <= 1 or self._config.max_in_flight <= 1:
            results = [
                self._run_one(task, base, prior, seed=seed, resume=resume)
                for task, base, prior in planned
            ]
        else:
            workers = min(self._config.max_in_flight, len(planned))

            def work(item: tuple[Any, RunIdentity, TaskRecord | None]) -> _TaskResult:
                return self._run_one(item[0], item[1], item[2], seed=seed, resume=resume)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(work, planned))

        finished_at = _now()
        records = tuple(result.record for result in results)
        return RunReport(
            run_id=self._batch_run_id,
            config=self._config,
            seed=seed,
            resume=resume,
            subset_hash=subset,
            identity={
                "run_id": self._batch_run_id,
                "dataset": self._config.dataset_name,
                "revision": self._config.revision,
                "split": self._config.split,
                "subset_hash": subset,
                "seed": seed,
                "resume_level": resume.value,
                "model_identity": self._config.model_identity,
                "provider": self._config.provider,
                "model": self._config.model,
                "config_hash": self._config.config_hash,
                "provider_config_hash": self._config.provider_config_hash,
                "code_commit": self._code_label,
                "code": dict(self._code),
                "harness_version": self._config.harness_version,
                "journal": str(self._journal_path),
                "started_at": started_at,
                "finished_at": finished_at,
            },
            records=records,
            declared_tasks=len(instances),
            ledger=self._ledger.as_dict(),
            reuse={
                "reused_tasks": sum(1 for result in results if result.reused),
                "graph_resumed": sum(1 for result in results if result.graph_resumed),
                "evaluation_reused": sum(1 for result in results if result.evaluation_reused),
                "inference_rerun": sum(1 for result in results if not result.reused),
            },
            workspace_audits={
                result.record.task_id: result.audit.as_dict()
                for result in results
                if result.audit is not None
            },
            stop_reason=self._run_stop_reason(records),
            started_at=started_at,
            finished_at=finished_at,
        )

    # ------------------------------------------------------------------ 任务规划

    def _check_task(self, task: SWEBenchInferenceTask, instance_id: str) -> None:
        """把 loader 返回对象里的身份字段与本次配置交叉核对，不一致拒绝开跑。"""
        task_instance = str(getattr(task, "instance_id", ""))
        if task_instance != instance_id:
            raise ValueError(
                f"loader 返回的 instance_id {task_instance!r} 与请求 {instance_id!r} 不一致"
            )
        base_commit = str(getattr(task, "base_commit", ""))
        if not _HEX40.match(base_commit):
            raise ValueError(f"{instance_id} 的 base_commit 不是 40 位十六进制: {base_commit!r}")
        repo = str(getattr(task, "repo", ""))
        if "/" not in repo or ".." in repo:
            raise ValueError(f"{instance_id} 的 repo 不合法: {repo!r}")
        dataset = getattr(task, "dataset", None)
        if dataset is not None:
            observed = {
                "name": str(getattr(dataset, "name", "")),
                "revision": str(getattr(dataset, "revision", "")),
                "harness_version": str(getattr(dataset, "harness_version", "")),
                "split": str(getattr(dataset, "split", "")),
            }
            expected = {
                "name": self._config.dataset_name,
                "revision": self._config.revision,
                "harness_version": self._config.harness_version,
                "split": self._config.split,
            }
            if observed != expected:
                raise ValueError(
                    f"{instance_id} 的数据集身份与本次配置不一致: {observed} != {expected}"
                )

    def _base_identity(self, task: SWEBenchInferenceTask, seed: int, subset: str) -> RunIdentity:
        return RunIdentity(
            dataset=self._config.dataset_name,
            revision=self._config.revision,
            split=self._config.split,
            subset_hash=subset,
            instance_id=str(task.instance_id),
            repo=str(task.repo),
            base_commit=str(task.base_commit),
            provider=self._config.provider,
            model=self._config.model,
            config_hash=self._config.config_hash,
            code_commit=self._code_label,
            seed=seed,
            patch_hash="",
            harness_version=self._config.harness_version,
        )

    @staticmethod
    def _full_identity(base: RunIdentity, patch_sha256: str) -> RunIdentity:
        return base.model_copy(update={"patch_hash": patch_sha256})

    # ------------------------------------------------------------------ 单任务执行

    def _run_one(
        self,
        task: SWEBenchInferenceTask,
        base: RunIdentity,
        prior: TaskRecord | None,
        *,
        seed: int,
        resume: ResumeLevel,
    ) -> _TaskResult:
        # ---- 第 1 层：任务级复用（已完成且证据完整才跳过）
        if prior is not None:
            intact, _problems = self._record_evidence_intact(prior)
            if intact:
                return _TaskResult(record=prior, reused=True)

        # ---- 第 3 层：评测级。先看能否在**不重跑推理**的前提下补评测/复用报告。
        if resume.enables(ResumeLevel.EVALUATION):
            recovered = self._recover_inference(base, prior)
            if recovered is not None:
                reused_evaluation = self._recover_evaluation(base, recovered, seed=seed)
                if reused_evaluation is not None:
                    record = self._build_record(
                        base=base,
                        seed=seed,
                        run_id=recovered.run_id,
                        facts=_facts_from_payload(recovered.inference_summary),
                        patch_sha256=recovered.patch_sha256,
                        evaluation=reused_evaluation,
                        evaluation_reused=True,
                    )
                    self._journal.commit(record)
                    return _TaskResult(record=record, evaluation_reused=True, audit=None)
                record = self._finalize_inference(
                    base=base,
                    facts=_facts_from_payload(recovered.inference_summary),
                    patch=str(recovered.patch),
                    patch_sha256=str(recovered.patch_sha256),
                    seed=seed,
                    run_id=recovered.run_id,
                    recovered=True,
                )
                return _TaskResult(record=record)

        # ---- 第 2 层：图级。只有现场里真有 checkpoint 才允许续跑；否则重开现场。
        base_run_id = self._base_run_id(base)
        run_dir = self._artifact_root / "runs" / base_run_id
        checkpoint_exists = (run_dir / "checkpoints.sqlite").is_file()
        audit: WorkspaceAudit | None = None
        graph_resumable = resume.enables(ResumeLevel.GRAPH) and checkpoint_exists
        fresh_graph = resume.enables(ResumeLevel.GRAPH) and not run_dir.exists()
        if graph_resumable:
            audit = audit_workspace(
                artifact_root=self._artifact_root,
                run_id=base_run_id,
                expected_identity=base,
            )
            if not audit.ok:
                record = self._refusal_record(base=base, seed=seed, audit=audit)
                self._journal.commit(record)
                return _TaskResult(record=record, audit=audit)
            run_id = base_run_id
        elif fresh_graph:
            # 第一次跑：用确定性 run_id，这样下一次进程能找到这次留下的 checkpoint。
            run_id = base_run_id
        else:
            # 任务级重跑（或现场没有 checkpoint）：换一个 run_id，避免一头撞进旧现场。
            run_id = self._fresh_run_id(base)

        if not self._ledger.reserve():
            record = self._budget_record(base=base, seed=seed, run_id=run_id)
            self._journal.commit(record)
            return _TaskResult(record=record)

        run_dir = self._artifact_root / "runs" / run_id
        self._write_manifest(
            run_dir,
            status="inference_running",
            identity=base,
            inference=None,
        )
        # 工厂必须知道本次是否允许 checkpoint：首次 GRAPH/EVALUATION 跑也要开启，
        # 否则下一次进程根本没有现场可接，"图级恢复"永远只存在于参数里。
        factory_resume = resume if (graph_resumable or fresh_graph) else ResumeLevel.TASK
        try:
            assembly = self._factory(
                task,
                seed,
                run_id=run_id,
                resume=factory_resume,
            )
            outcome = assembly.run(run_id=run_id, seed=seed)
        except Exception as exc:  # 单任务失败不能带走整批
            failure = classify_exception(exc)
            self._ledger.settle(None)
            record = self._build_record(
                base=base,
                seed=seed,
                run_id=run_id,
                facts=_InferenceFacts(
                    status="error",
                    error_class=failure.failure_class.value,
                    error_origin=failure.origin.value,
                    failure_reasons=(failure.reason(),),
                ),
                patch_sha256=None,
                evaluation=None,
                execution_state=ExecutionState.INFERENCE_ERROR,
            )
            self._journal.commit(record)
            return _TaskResult(record=record)

        facts = _facts_from_outcome(outcome)
        patch_sha256 = facts.final_patch_sha256
        # 装配层在恢复核对不过时会返回一个"拒绝续跑"的终态；它不能被当成一次
        # 普通的空补丁（那会变成"模型没修好"），而要按现场证据问题归因。
        if any("resume refused" in item for item in facts.failure_reasons):
            self._ledger.settle(self._settle_cost(facts.usage))
            record = self._build_record(
                base=base,
                seed=seed,
                run_id=run_id,
                facts=facts,
                patch_sha256=None,
                evaluation=None,
                execution_state=ExecutionState.INFERENCE_ERROR,
                forced_error_class=FailureClass.WORKSPACE_ERROR.value,
                forced_error_origin=ErrorOrigin.WORKSPACE.value,
            )
            self._journal.commit(record)
            return _TaskResult(record=record)

        if patch_sha256 is None and outcome.final_patch:
            patch_sha256 = _sha256_text(outcome.final_patch)
        self._write_manifest(
            run_dir,
            status="inference_complete",
            identity=self._full_identity(base, patch_sha256 or ""),
            inference=facts,
            patch_sha256=patch_sha256,
        )

        if patch_sha256 is None or facts.empty_patch or facts.invalid_patch:
            self._ledger.settle(self._settle_cost(facts.usage))
            record = self._build_record(
                base=base,
                seed=seed,
                run_id=run_id,
                facts=facts,
                patch_sha256=patch_sha256,
                evaluation=None,
            )
            self._journal.commit(record)
            return _TaskResult(record=record)

        patch_path = run_dir / PATCH_NAME
        _atomic_write_text(patch_path, outcome.final_patch)
        record = self._finalize_inference(
            base=base,
            facts=facts,
            patch=outcome.final_patch,
            patch_sha256=patch_sha256,
            seed=seed,
            run_id=run_id,
            recovered=False,
        )
        self._ledger.settle(self._settle_cost(facts.usage))
        self._journal.commit(record)
        return _TaskResult(record=record, graph_resumed=bool(facts.resumed_from_checkpoint))

    # ------------------------------------------------------------------ 评测阶段

    def _finalize_inference(
        self,
        *,
        base: RunIdentity,
        facts: _InferenceFacts,
        patch: str,
        patch_sha256: str,
        seed: int,
        run_id: str,
        recovered: bool,
    ) -> TaskRecord:
        """补评测并生成记录。推理费用此时**已经**记账（或来自历史现场）。"""
        evaluation = self._run_evaluation(
            base=base,
            patch=patch,
            patch_sha256=patch_sha256,
            seed=seed,
            run_id=run_id,
        )
        record = self._build_record(
            base=base,
            seed=seed,
            run_id=run_id,
            facts=facts,
            patch_sha256=patch_sha256,
            evaluation=evaluation,
        )
        if recovered:
            # 评测级恢复：推理是上一个进程花的钱，本进程没有新的 LLM 费用。
            # 这里不调用 ledger.settle，避免把历史费用在本次会话里再扣一遍。
            pass
        return record

    def _run_evaluation(
        self,
        *,
        base: RunIdentity,
        patch: str,
        patch_sha256: str,
        seed: int,
        run_id: str,
    ) -> SWEBenchEvaluation:
        """调官方适配层并核对它返回的身份；返回的结论一定带可核对的信封/报告。"""
        run_dir = self._artifact_root / "runs" / run_id
        expected_run_id = harness_run_id(base.instance_id, patch_sha256, seed)
        try:
            evaluation_task = self._loader.get_evaluation_task(self._config.split, base.instance_id)
            result = self._evaluator(
                instance_id=base.instance_id,
                evaluation_task=evaluation_task,
                patch=patch,
                patch_sha256=patch_sha256,
                seed=seed,
                harness_run_id=expected_run_id,
                run_dir=run_dir,
            )
        except Exception as exc:
            # D 的异常类型带明确归因：平台不可执行 → ENV_ERROR（原生 Windows 不是
            # 模型失败），缓存身份不可证明 → 评测级错误（不假装是模型没修好），
            # 其余 HarnessError 按其 kind 透传。未识别异常才走通用分类（默认
            # HARNESS_INTERNAL_ERROR，方向性选择：不把未知错误算成模型错误）。
            if isinstance(exc, HarnessPlatformError):
                error_kind = HarnessErrorKind.ENV_ERROR.value
                detail = str(exc)
            elif isinstance(exc, HarnessCacheError):
                error_kind = "harness_cache_error"
                detail = str(exc)
            elif isinstance(exc, HarnessError):
                error_kind = exc.kind.value
                detail = exc.detail
            else:
                failure = classify_exception(exc)
                error_kind = failure.failure_class.value
                detail = failure.reason()
            return SWEBenchEvaluation(
                verdict=OfficialVerdict.ERROR,
                instance_id=base.instance_id,
                resolved=None,
                harness_run_id=expected_run_id,
                patch_sha256=patch_sha256,
                harness_version=self._config.harness_version,
                dataset_revision=self._config.revision,
                error_kind=error_kind,
                error_detail=detail,
            )

        problems = self._evaluation_identity_problems(
            result, base=base, patch_sha256=patch_sha256, expected_run_id=expected_run_id
        )
        if problems:
            return SWEBenchEvaluation(
                verdict=OfficialVerdict.ERROR,
                instance_id=base.instance_id,
                resolved=None,
                harness_run_id=expected_run_id,
                patch_sha256=patch_sha256,
                harness_version=self._config.harness_version,
                dataset_revision=self._config.revision,
                error_kind="identity_mismatch",
                error_detail="; ".join(problems),
            )

        # 只有可判定结论才要求"完整报告"；ERROR/MISSING 本来就没有报告可选。
        if result.verdict in {OfficialVerdict.RESOLVED, OfficialVerdict.UNRESOLVED}:
            captured = self._capture_official_report(run_dir, result)
            if isinstance(captured, str):
                return SWEBenchEvaluation(
                    verdict=OfficialVerdict.ERROR,
                    instance_id=base.instance_id,
                    resolved=None,
                    harness_run_id=expected_run_id,
                    patch_sha256=patch_sha256,
                    harness_version=self._config.harness_version,
                    dataset_revision=self._config.revision,
                    error_kind="report_invalid",
                    error_detail=captured,
                    runtime_seconds=result.runtime_seconds,
                    real_docker=result.real_docker,
                )
            result = replace(result, report_path=captured, report_sha256=_sha256_file(captured))

        self._write_official_envelope(
            run_dir,
            base=base,
            evaluation=result,
            patch_sha256=patch_sha256,
            seed=seed,
            expected_run_id=expected_run_id,
        )
        self._write_manifest(
            run_dir,
            # 评测没给出结论时状态仍是"推理完成"：之后评测级恢复还要靠这个现场
            # 找回补丁与推理摘要，"evaluation_error" 会让它被误判成不可复用。
            status="evaluated" if result.verdict in _DECIDABLE_VERDICTS else "inference_complete",
            identity=self._full_identity(base, patch_sha256),
            inference=None,
            patch_sha256=patch_sha256,
        )
        return result

    def _evaluation_identity_problems(
        self,
        evaluation: SWEBenchEvaluation,
        *,
        base: RunIdentity,
        patch_sha256: str,
        expected_run_id: str,
    ) -> tuple[str, ...]:
        problems: list[str] = []
        if evaluation.instance_id != base.instance_id:
            problems.append(
                f"评测结果 instance {evaluation.instance_id!r} 与任务 {base.instance_id!r} 不一致"
            )
        if evaluation.patch_sha256 != patch_sha256:
            problems.append(
                f"评测结果补丁 {evaluation.patch_sha256!r} 与本次 {patch_sha256!r} 不一致"
            )
        if evaluation.harness_run_id != expected_run_id:
            problems.append(
                f"评测结果 run_id {evaluation.harness_run_id!r} 与期望 {expected_run_id!r} 不一致"
            )
        if evaluation.harness_version != self._config.harness_version:
            problems.append(
                f"评测结果 harness 版本 {evaluation.harness_version!r} 与配置 "
                f"{self._config.harness_version!r} 不一致"
            )
        if evaluation.dataset_revision != self._config.revision:
            problems.append(
                f"评测结果 dataset revision {evaluation.dataset_revision!r} 与配置 "
                f"{self._config.revision!r} 不一致"
            )
        return tuple(problems)

    def _capture_official_report(self, run_dir: Path, evaluation: SWEBenchEvaluation) -> Path | str:
        """把官方报告复制进本 run 的现场并校验哈希；返回路径或失败原因。"""
        source = evaluation.report_path
        if source is None:
            return "官方判定声称有结论，但没有提供报告路径"
        source = Path(source)
        if not source.is_file():
            return f"官方报告不存在：{source}"
        try:
            data = source.read_bytes()
        except OSError as exc:
            return f"官方报告不可读：{type(exc).__name__}: {exc}"
        sha256 = _sha256_bytes(data)
        if evaluation.report_sha256 is not None and evaluation.report_sha256 != sha256:
            return f"官方报告哈希 {sha256!r} 与适配层声明 {evaluation.report_sha256!r} 不一致"
        target = run_dir / OFFICIAL_REPORT_NAME
        _atomic_write_bytes(target, data)
        return target

    def _write_official_envelope(
        self,
        run_dir: Path,
        *,
        base: RunIdentity,
        evaluation: SWEBenchEvaluation,
        patch_sha256: str,
        seed: int,
        expected_run_id: str,
    ) -> Path:
        """写评测信封：把"这份报告属于哪个补丁/哪次 harness"固定成可比对的事实。"""
        payload: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "instance_id": base.instance_id,
            "patch_sha256": patch_sha256,
            "harness_run_id": expected_run_id,
            "harness_version": self._config.harness_version,
            "dataset_name": self._config.dataset_name,
            "dataset_revision": self._config.revision,
            "split": self._config.split,
            "seed": seed,
            "config_hash": self._config.config_hash,
            "code_commit": self._code_label,
            "verdict": evaluation.verdict.value,
            "resolved": evaluation.resolved,
            "error_kind": evaluation.error_kind,
            "error_detail": evaluation.error_detail,
            "report_file": OFFICIAL_REPORT_NAME if evaluation.report_path is not None else None,
            "report_sha256": evaluation.report_sha256,
            "environment_identity": self._config.environment_identity,
            "official_runtime_seconds": evaluation.runtime_seconds,
            "official_reused": evaluation.reused,
            "official_from_cache": evaluation.from_cache,
            # 真实 Docker / 模拟必须跟着证据走：恢复后若缺这个标记，报告会把一次
            # Fake 评测说成"真实 Docker 评测"，那是最难事后纠正的一类误读。
            "real_docker": evaluation.real_docker,
            "written_at": _now(),
        }
        return _atomic_write_json(run_dir / OFFICIAL_ENVELOPE_NAME, payload)

    # ------------------------------------------------------------------ 恢复工具

    def _record_evidence_intact(self, record: TaskRecord) -> tuple[bool, tuple[str, ...]]:
        """终态记录连**证据**一起核对：拿不出证据的成功不算已完成。

        返回 ``(intact, problems)``。``intact=False`` 时调用方不得复用该记录，
        但也不把记录值当成结论发布——要么用身份一致的报告重建，要么重跑。
        """
        problems: list[str] = []
        if record.execution_state != ExecutionState.COMPLETED.value:
            problems.append(f"任务不是完成态（execution_state={record.execution_state}），不能跳过")
            return False, tuple(problems)
        attribution = _swebench_attribution(record)
        if attribution is SWEBenchAttribution.PATCH_INVALID:
            if not (record.empty_patch or record.invalid_patch):
                problems.append("归因为补丁无效，但记录里没有空/非法补丁标记")
            return (not problems), tuple(problems)
        if attribution not in {
            SWEBenchAttribution.OFFICIAL_RESOLVED,
            SWEBenchAttribution.OFFICIAL_NOT_RESOLVED,
        }:
            problems.append(f"任务没有拿到官方判定（归因 {attribution.value}），不能当作已完成跳过")
            return False, tuple(problems)
        run_rel = record.artifact_refs.get("run_dir")
        patch_rel = record.artifact_refs.get("patch_file")
        harness_version = record.artifact_refs.get("harness_version")
        dataset_revision = record.artifact_refs.get("dataset_revision")
        harness_id = record.artifact_refs.get("harness_run_id")
        seed_value = record.artifact_refs.get("seed")
        config_hash = record.artifact_refs.get("config_hash")
        instance_id = record.task_id
        missing = [
            name
            for name, value in (
                ("run_dir", run_rel),
                ("patch_file", patch_rel),
                ("harness_run_id", harness_id),
                ("harness_version", harness_version),
                ("dataset_revision", dataset_revision),
                ("seed", seed_value),
                ("config_hash", config_hash),
            )
            if not value
        ]
        if missing:
            return False, (f"记录缺少证据引用字段：{', '.join(missing)}",)
        assert run_rel is not None and patch_rel is not None and harness_id is not None
        assert harness_version is not None and dataset_revision is not None
        assert seed_value is not None and config_hash is not None

        patch_path = self._artifact_root / patch_rel
        if not patch_path.is_file():
            problems.append(f"补丁证据文件不存在：{patch_path}")
        else:
            try:
                observed = _sha256_file(patch_path)
            except OSError as exc:
                observed = None
                problems.append(f"补丁证据不可读：{type(exc).__name__}: {exc}")
            if observed is not None and record.patch_sha256 and observed != record.patch_sha256:
                problems.append(f"补丁证据哈希 {observed!r} 与记录 {record.patch_sha256!r} 不一致")
        try:
            expected_seed = int(seed_value)
        except ValueError:
            expected_seed = -1
        audit = audit_official_evidence(
            artifact_root=self._artifact_root,
            run_id=Path(run_rel).name,
            expected_instance_id=instance_id,
            expected_patch_sha256=str(record.patch_sha256 or ""),
            expected_harness_run_id=harness_id,
            expected_harness_version=harness_version,
            expected_dataset_revision=dataset_revision,
            expected_seed=expected_seed,
            expected_config_hash=config_hash,
            expected_environment_identity=self._config.environment_identity,
        )
        if not audit.reusable:
            problems.append(
                "官方评测证据不完整或身份不一致："
                + ("; ".join(audit.problems) if audit.problems else "结论不可判定")
            )
        return (not problems), tuple(problems)

    def _recover_inference(
        self, base: RunIdentity, prior: TaskRecord | None
    ) -> InferenceEvidenceAudit | None:
        """在评测级恢复里找回已完成的推理证据；找不到/证据不成立返回 None。"""
        candidates: list[str] = [self._base_run_id(base)]
        if prior is not None:
            run_rel = prior.artifact_refs.get("run_dir")
            if run_rel:
                candidates.append(Path(run_rel).name)
        seen: set[str] = set()
        for run_id in candidates:
            if run_id in seen:
                continue
            seen.add(run_id)
            audit = recover_inference_evidence(
                artifact_root=self._artifact_root,
                run_id=run_id,
                expected_identity=base,
            )
            if audit.usable:
                return audit
        return None

    def _recover_evaluation(
        self,
        base: RunIdentity,
        recovered: InferenceEvidenceAudit,
        *,
        seed: int,
    ) -> SWEBenchEvaluation | None:
        """复用完整、身份一致的官方评测报告；任何不符都返回 None 交给重评。"""
        assert recovered.patch_sha256 is not None
        audit = audit_official_evidence(
            artifact_root=self._artifact_root,
            run_id=recovered.run_id,
            expected_instance_id=base.instance_id,
            expected_patch_sha256=recovered.patch_sha256,
            expected_harness_run_id=harness_run_id(base.instance_id, recovered.patch_sha256, seed),
            expected_harness_version=self._config.harness_version,
            expected_dataset_revision=self._config.revision,
            expected_seed=seed,
            expected_config_hash=self._config.config_hash,
            expected_environment_identity=self._config.environment_identity,
        )
        if not audit.reusable or audit.verdict is None or audit.report_path is None:
            return None
        envelope = audit.envelope or {}
        return SWEBenchEvaluation(
            verdict=OfficialVerdict(audit.verdict),
            instance_id=base.instance_id,
            resolved=audit.resolved,
            harness_run_id=audit.harness_run_id
            or harness_run_id(base.instance_id, recovered.patch_sha256, seed),
            patch_sha256=recovered.patch_sha256,
            harness_version=self._config.harness_version,
            dataset_revision=self._config.revision,
            report_path=audit.report_path,
            report_sha256=audit.report_sha256,
            error_kind=None
            if envelope.get("error_kind") is None
            else str(envelope.get("error_kind")),
            error_detail=(
                None if envelope.get("error_detail") is None else str(envelope.get("error_detail"))
            ),
            runtime_seconds=float(envelope.get("official_runtime_seconds") or 0.0),
            real_docker=bool(envelope.get("real_docker", False)),
            reused=True,
            from_cache=True,
        )

    # ------------------------------------------------------------------ 记录构造

    def _settle_cost(self, usage: Usage) -> float | None:
        """本次推理在账本里要回填的费用。未知/部分未知一律记 ``None``。

        没调用 LLM 的运行不是"免费"，是"没有这条账"；服务商没回传用量时总费用
        只是下界——两种情况都按未知处理，整批报告的总额随之为下界。
        """
        if usage.llm_calls == 0:
            return None
        if usage.cost_is_lower_bound:
            return None
        return float(usage.cost)

    def _build_record(
        self,
        *,
        base: RunIdentity,
        seed: int,
        run_id: str,
        facts: _InferenceFacts,
        patch_sha256: str | None,
        evaluation: SWEBenchEvaluation | None,
        evaluation_reused: bool = False,
        execution_state: ExecutionState = ExecutionState.COMPLETED,
        forced_error_class: str | None = None,
        forced_error_origin: str | None = None,
        extra_error_details: tuple[str, ...] = (),
    ) -> TaskRecord:
        official_verdict: str | None
        if evaluation is not None and evaluation.verdict in {
            OfficialVerdict.RESOLVED,
            OfficialVerdict.UNRESOLVED,
        }:
            decided = True
            official_verdict = evaluation.verdict.value
            official_resolved: bool | None = evaluation.resolved
        else:
            decided = False
            official_verdict = None if evaluation is None else evaluation.verdict.value
            official_resolved = None
        patch_valid = bool(patch_sha256 and not facts.empty_patch and not facts.invalid_patch)
        error_class = forced_error_class or facts.error_class
        error_origin = forced_error_origin or facts.error_origin
        if evaluation is not None and not decided and evaluation.error_kind:
            # 记录里的 error_class 沿用 FailureClass 的小写口径（D 的枚举是大写，
            # 原始值仍保留在 artifact_refs["official_error_kind"]）。
            error_class = error_class or evaluation.error_kind.lower()
        attribution = classify_outcome(
            execution_state=execution_state.value,
            official_verdict=official_verdict,
            official_resolved=official_resolved,
            official_error_kind=None if evaluation is None else evaluation.error_kind,
            patch_valid=patch_valid,
            empty_patch=facts.empty_patch or patch_sha256 is None,
            invalid_patch=facts.invalid_patch,
            stop_reason=facts.stop_reason,
            error_class=error_class,
            error_origin=error_origin,
        )
        # 评测阶段"尝试过但没给出可判定结论"包含两种：官方 verdict=ERROR，以及
        # PLATFORM_BLOCKED/DOCKER_UNAVAILABLE/TIMEOUT/REPORT_MISSING 这类
        # verdict 仍是 NOT_EVALUATED、但 **带 error_kind** 的失败。后者不能被记成
        # NOT_RUN（那会让"评测根本没发生"与"评测发生了但坏了"混为一谈）。
        evaluation_errored = evaluation is not None and (
            official_verdict == OfficialVerdict.ERROR.value
            or (evaluation.error_kind is not None and not decided)
        )
        evaluation_state = (
            EvaluationState.EVALUATED
            if decided
            else EvaluationState.ERROR
            if execution_state is ExecutionState.EVALUATION_ERROR or evaluation_errored
            else EvaluationState.NOT_RUN
        )
        executed = execution_state is ExecutionState.COMPLETED
        usage = facts.usage
        cost: float | None
        if not executed or usage.llm_calls == 0 or usage.cost_is_lower_bound:
            cost = None
        else:
            cost = float(usage.cost)
        full_identity = self._full_identity(base, patch_sha256) if patch_sha256 else base
        run_rel = f"runs/{run_id}"
        artifact_refs: dict[str, str] = {
            "run_dir": run_rel,
            "identity_key": full_identity.key,
            "journal_key": base.journal_key,
            "swebench_attribution": attribution.value,
            "harness_version": self._config.harness_version,
            "dataset_revision": self._config.revision,
            "config_hash": self._config.config_hash,
            "provider": self._config.provider,
            "model": self._config.model,
            "seed": str(seed),
            "resume_level": self._config.run_label,
            # 环境身份未注入时**显式记"未验证"**，不能静默当成已验证。
            "environment_identity": self._config.environment_identity or "",
            "environment_identity_verified": (
                "true" if self._config.environment_identity else "false"
            ),
        }
        if patch_sha256:
            artifact_refs["patch_file"] = f"{run_rel}/{PATCH_NAME}"
            artifact_refs["patch_sha256"] = patch_sha256
        if evaluation is not None:
            artifact_refs["official_verdict"] = evaluation.verdict.value
            artifact_refs["harness_run_id"] = evaluation.harness_run_id
            artifact_refs["official_rerun_id"] = harness_run_id(
                base.instance_id, patch_sha256 or evaluation.patch_sha256, seed
            )
            artifact_refs["official_runtime_seconds"] = f"{evaluation.runtime_seconds:.6f}"
            artifact_refs["official_reused"] = "true" if evaluation.reused else "false"
            artifact_refs["official_from_cache"] = "true" if evaluation.from_cache else "false"
            if evaluation.error_kind:
                artifact_refs["official_error_kind"] = evaluation.error_kind
            if evaluation.report_path is not None:
                artifact_refs["official_envelope"] = f"{run_rel}/{OFFICIAL_ENVELOPE_NAME}"
                artifact_refs["official_report"] = f"{run_rel}/{OFFICIAL_REPORT_NAME}"
                if evaluation.report_sha256:
                    artifact_refs["official_report_sha256"] = evaluation.report_sha256
            if evaluation_reused:
                artifact_refs["evaluation_reused"] = "true"
        verification_mode = VerificationMode.FULL.value if decided else VerificationMode.NONE.value
        error_details = (
            tuple(facts.failure_reasons)
            + extra_error_details
            + (
                (evaluation.error_detail,)
                if evaluation is not None and evaluation.error_detail
                else ()
            )
        )
        return TaskRecord(
            task_id=base.instance_id,
            seed=seed,
            category="swebench",
            difficulty="unknown",
            multi_file=False,
            has_traceback=False,
            model_identity=self._config.model_identity,
            config_hash=base.config_hash,
            code_commit=base.code_commit,
            fixture_version=base.revision,
            base_commit=base.base_commit,
            run_id=run_id,
            # journal 主键必须是**去掉补丁哈希**的 journal_key：查 journal 的时候补丁
            # 还不存在，用完整身份当主键会让"已完成任务"永远查不回来。完整身份（含
            # patch_hash）记在 artifact_refs["identity_key"]，两条信息都不丢。
            identity=base.journal_key,
            execution_state=execution_state.value,
            evaluation_status=(
                official_verdict
                if official_verdict is not None
                else "PATCH_INVALID"
                if attribution is SWEBenchAttribution.PATCH_INVALID
                else "NOT_EVALUATED"
            ),
            resolved=official_resolved,
            first_attempt_resolved=None,
            agent_claims_success=facts.agent_claims_success,
            empty_patch=facts.empty_patch or patch_sha256 is None,
            invalid_patch=facts.invalid_patch,
            patch_sha256=patch_sha256,
            first_attempt_patch_sha256=None,
            iterations=facts.iterations,
            stop_reason=facts.stop_reason,
            input_tokens=None if not executed else usage.input_tokens,
            output_tokens=None if not executed else usage.output_tokens,
            total_tokens=None if not executed else usage.total_tokens,
            llm_calls=None if not executed else usage.llm_calls,
            # 费用未知不是 0：0 会让整批成本合计看起来更便宜。
            cost=cost,
            cost_currency=self._config.budget_currency,
            runtime_seconds=facts.runtime_seconds if executed else None,
            verification_mode=verification_mode,
            simulated_llm=facts.simulated_llm
            or self._config.model_identity == "FakeLLM"
            or self._config.provider == "fake",
            sandbox_backend=facts.sandbox_backend,
            real_docker_evaluation=bool(
                decided and evaluation is not None and evaluation.real_docker
            ),
            usage_uncertain=facts.usage_uncertain or usage.cost_is_lower_bound,
            artifact_refs=artifact_refs,
            error_details=error_details,
            finished_at=_now(),
            localization_files=(),
            localization_symbols=(),
            record_version=RECORD_VERSION,
            attribution=LEGACY_ATTRIBUTION[attribution].value,
            error_class=error_class,
            error_origin=error_origin,
            evaluation_state=evaluation_state.value,
            agent_status=facts.status,
            plan_corrections=facts.plan_corrections,
            plan_tool_audit=dict(facts.plan_tool_audit),
            llm_requests=dict(facts.llm_requests),
            checkpointed=facts.checkpointed,
            resumed_from_checkpoint=facts.resumed_from_checkpoint,
            verification_level=facts.verification_level,
            review_executed=facts.review_executed,
            review_bound_to_candidate=facts.review_bound_to_candidate,
            review_high_issues=facts.review_high_issues,
            post_review_repair=facts.post_review_repair,
            unexecuted_stages=facts.unexecuted_stages,
            coding_stop_reason=facts.coding_stop_reason,
        )

    def _refusal_record(self, *, base: RunIdentity, seed: int, audit: WorkspaceAudit) -> TaskRecord:
        """现场核对不过：拒绝续跑并留下**证据**，而不是将就着跑或假装没跑过。"""
        return self._build_record(
            base=base,
            seed=seed,
            run_id=self._base_run_id(base),
            facts=_InferenceFacts(
                status="refused",
                error_class=FailureClass.WORKSPACE_ERROR.value,
                error_origin=ErrorOrigin.WORKSPACE.value,
                failure_reasons=(audit.describe(),),
            ),
            patch_sha256=None,
            evaluation=None,
            execution_state=ExecutionState.INFERENCE_ERROR,
            forced_error_class=FailureClass.WORKSPACE_ERROR.value,
            forced_error_origin=ErrorOrigin.WORKSPACE.value,
            extra_error_details=(
                *audit.problems,
                f"evidence={_canonical(audit.as_dict())}",
            ),
        )

    def _budget_record(self, *, base: RunIdentity, seed: int, run_id: str) -> TaskRecord:
        return self._build_record(
            base=base,
            seed=seed,
            run_id=run_id,
            facts=_InferenceFacts(
                status="budget_blocked",
                stop_reason="budget_total",
                failure_reasons=("整批预算耗尽，派发前被拦截",),
            ),
            patch_sha256=None,
            evaluation=None,
            execution_state=ExecutionState.BUDGET_BLOCKED,
        )

    # ------------------------------------------------------------------ 现场写入

    def _write_manifest(
        self,
        run_dir: Path,
        *,
        status: str,
        identity: RunIdentity,
        inference: _InferenceFacts | None,
        patch_sha256: str | None = None,
    ) -> Path:
        payload: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "status": status,
            "batch_run_id": self._batch_run_id,
            "run_id": run_dir.name,
            # 只写原始字段：``RunIdentity.model_validate`` 是 extra="forbid"，
            # 把派生键（identity/journal_key）塞进 identity 会让恢复读不回来。
            "identity": identity.payload(),
            "journal_key": identity.journal_key,
            "identity_key": identity.key,
            "patch_sha256": patch_sha256 or identity.patch_hash or None,
            "inference": None if inference is None else _facts_to_payload(inference),
            "dataset": {
                "name": self._config.dataset_name,
                "revision": self._config.revision,
                "split": self._config.split,
            },
            "harness_version": self._config.harness_version,
            "config_hash": self._config.config_hash,
            "code_commit": self._code_label,
            "written_at": _now(),
        }
        if inference is None:
            # 评测阶段重写 manifest 时不能把推理摘要（token/费用/迭代）抹掉：
            # 那正是评测级恢复"不重跑推理也能如实记账"的唯一来源。
            previous, _problem = _read_json_object(run_dir / MANIFEST_NAME)
            if previous is not None and isinstance(previous.get("inference"), dict):
                payload["inference"] = previous["inference"]
        return _atomic_write_json(run_dir / MANIFEST_NAME, payload)

    # ------------------------------------------------------------------ 汇总

    def _run_stop_reason(self, records: Sequence[TaskRecord]) -> str:
        if not records:
            return "empty_target"
        ledger = self._ledger.as_dict()
        blocked = int(ledger.get("blocked_dispatches") or 0)
        if blocked:
            # 预留失败意味着"再派发一个任务就会超上限"；之后每个 task 都会同样失败，
            # 所以这就是整批预算耗尽，不需要再分"耗尽一部分"这种没有行动含义的状态。
            return "batch_budget_exhausted"
        states = {record.execution_state for record in records}
        if states & {ExecutionState.INFERENCE_ERROR.value, ExecutionState.EVALUATION_ERROR.value}:
            return "completed_with_task_errors"
        if ExecutionState.BUDGET_BLOCKED.value in states:
            return "completed_with_budget_blocks"
        return "completed"
