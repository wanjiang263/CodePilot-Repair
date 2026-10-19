"""SWE-bench Lite 的**推理侧**数据模型（E-11 / E-12）。

Phase 11 的硬约束不是"跑通官方 harness"，而是"推理阶段拿不到答案"（01「评测协议与
信息隔离」、03 §11.2）。因此这个模块只定义推理侧对象，字段集合就是信息边界：

* :class:`SWEBenchInferenceTask` 只有 issue、repo、base commit、版本、环境身份和一个
  :class:`~codepilot.sandbox.env_spec.EnvSpec`；
* gold patch、test patch、FAIL_TO_PASS / PASS_TO_PASS、``hints_text`` 一律**没有字段
  可以承载**。``extra="forbid"`` 让"顺手多传一个 patch"在构造期直接失败，而不是等到
  某个 prompt 里出现它才被发现；
* 评测侧对象故意放在 :mod:`codepilot.eval.swebench`——那个模块 import 了
  ``swbench_task``，反过来不存在路径，推理代码因此无法"只是 import 一下"就把 gold
  拉进来（Owner B 的 import graph 测试会静态检查这条闭包）。

为什么不把评测字段设成 ``None`` 的隐藏字段？那样一旦有人把对象序列化进 prompt，
字段名本身就会提示模型"这里应该有个 patch"。信息隔离要防的是**可用性**，
不是字段上的 ``None``。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from codepilot.sandbox.env_spec import EnvSpec

__all__ = [
    "DATASET_NAME",
    "DATASET_REVISION",
    "FORBIDDEN_INFERENCE_FIELDS",
    "HARNESS_VERSION",
    "INFERENCE_IDENTITY_FIELDS",
    "OFFICIAL_SPLIT_SIZES",
    "SWEBenchDatasetRef",
    "SWEBenchInferenceTask",
    "validate_base_commit",
    "validate_dataset_name",
    "validate_instance_id",
    "validate_repo_slug",
    "validate_revision",
    "validate_swebench_version",
]

#: 固定数据集与 revision（契约 §0）。不要在这里"升级"数据集：换 revision 会换掉
#: 整个快照身份，旧预测/旧报告必须一起失效，不能静默沿用。
DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
HARNESS_VERSION = "4.1.0"
OFFICIAL_SPLIT_SIZES: dict[str, int] = {"dev": 23, "test": 300}

#: 推理对象里绝对不允许出现的字段名。测试会拿它跟 ``model_fields`` 求交集；
#: 未来如果有人"临时"加一个 hints 字段，这里会先炸。
FORBIDDEN_INFERENCE_FIELDS: frozenset[str] = frozenset(
    {
        "patch",
        "gold_patch",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "fail_to_pass",
        "pass_to_pass",
        "hints_text",
        "gold_patch_sha256",
        "test_patch_sha256",
        "gold_files",
    }
)

#: 推理/评测两侧都有的**身份字段**。它们同时出现在两个对象里是允许的：
#: 评测侧要知道自己评的是哪个实例、哪个 base commit，否则报告无法与推理结果对齐。
INFERENCE_IDENTITY_FIELDS: frozenset[str] = frozenset(
    {
        "instance_id",
        "repo",
        "base_commit",
        "version",
        "environment_setup_commit",
        "dataset",
    }
)

_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$")
_REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DATASET_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_HARNESS_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_FORBIDDEN_PATH_PARTS = frozenset({"..", ".", ".git", "eval"})


def _reject_unsafe_text(value: str, *, what: str) -> str:
    """拒绝控制字符/首尾空白伪装出来的身份值。

    这些字段会参与缓存目录名、git 参数和 HTTP URL 拼接；``"a/b\nc"`` 这种值在
    pydantic 的默认 ``str`` 校验下是合法的，但拼进路径后行为不可预测。
    """
    if "\x00" in value:
        raise ValueError(f"{what} 不能含 NUL")
    if value != value.strip():
        raise ValueError(f"{what} 不能带首尾空白: {value!r}")
    if any(character in value for character in ("\r", "\n", "\t")):
        raise ValueError(f"{what} 不能含换行/制表符: {value!r}")
    return value


def validate_dataset_name(value: str) -> str:
    """数据集名必须是 ``owner/name``，且不能用到路径穿越的片段。"""
    normalized = _reject_unsafe_text(value, what="dataset_name")
    if _DATASET_SLUG.fullmatch(normalized) is None:
        raise ValueError(f"dataset_name 必须是 owner/name 形式: {value!r}")
    if _FORBIDDEN_PATH_PARTS & set(normalized.split("/")):
        raise ValueError(f"dataset_name 含非法路径片段: {value!r}")
    return normalized


def validate_revision(value: str) -> str:
    """revision 必须是 40/64 位小写十六进制 commit id。"""
    normalized = _reject_unsafe_text(value, what="revision")
    if _REVISION.fullmatch(normalized) is None:
        raise ValueError(f"revision 必须是 40/64 位十六进制: {value!r}")
    return normalized


def validate_base_commit(value: str) -> str:
    """base commit 固定为 40 位小写十六进制（SWE-bench 官方数据格式）。"""
    normalized = _reject_unsafe_text(value, what="base_commit")
    if _SHA40.fullmatch(normalized) is None:
        raise ValueError(f"base_commit 必须是 40 位十六进制: {value!r}")
    return normalized


def validate_swebench_version(value: str) -> str:
    """官方 ``version`` 字段：非空单行字符串，语义上由数据集决定。"""
    normalized = _reject_unsafe_text(value, what="version")
    if not normalized:
        raise ValueError("version 不能为空")
    return normalized


def validate_repo_slug(value: str) -> str:
    """``owner/name`` 仓库标识；拒绝 ``..``、空段、反斜杠与绝对路径。"""
    normalized = _reject_unsafe_text(value, what="repo")
    if _REPO_SLUG.fullmatch(normalized) is None:
        raise ValueError(f"repo 必须是 owner/name 形式: {value!r}")
    parts = normalized.split("/")
    if len(parts) != 2:
        raise ValueError(f"repo 必须恰好包含一个 '/': {value!r}")
    if _FORBIDDEN_PATH_PARTS & set(parts):
        raise ValueError(f"repo 含非法路径片段: {value!r}")
    if "\\" in normalized:
        raise ValueError(f"repo 不能含反斜杠: {value!r}")
    return normalized


def validate_instance_id(value: str) -> str:
    """官方 instance id 形态：``{owner}__{name}-{PR number}``。"""
    normalized = _reject_unsafe_text(value, what="instance_id")
    if _INSTANCE_ID.fullmatch(normalized) is None:
        raise ValueError(
            f"instance_id 必须形如 owner__name-<数字>（官方 SWE-bench 格式）: {value!r}"
        )
    return normalized


class SWEBenchDatasetRef(BaseModel):
    """数据集身份：名称 + 固定 revision + harness 版本 + split。

    ``frozen=True``：这个对象会出现在缓存键、预测 manifest 与评测报告里；任何一段
    代码中途改 split/revision 都会让"预测文件属于哪批数据"变成无法回答的问题。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = DATASET_NAME
    revision: str
    harness_version: str = HARNESS_VERSION
    split: Literal["dev", "test"]

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        return validate_revision(value)

    @field_validator("harness_version")
    @classmethod
    def _harness(cls, value: str) -> str:
        normalized = _reject_unsafe_text(value, what="harness_version")
        if _HARNESS_VERSION.fullmatch(normalized) is None:
            raise ValueError(f"harness_version 必须是 x.y.z 形式: {value!r}")
        return normalized

    @property
    def snapshot_dirname(self) -> str:
        """快照目录名。revision 是目录名的一部分，所以换 revision 必然换身份。"""
        return f"{self.name.replace('/', '__')}__{self.revision}"


class SWEBenchInferenceTask(BaseModel):
    """推理侧唯一允许持有的任务对象。

    字段集合 = 信息边界。这里没有、也不允许出现任何 gold/test 字段（见模块
    docstring）。``repository`` 属性返回远端 URL，:class:`InferenceAssembly` 通过
    ``WorkspaceManager`` 只材料化 base commit。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    version: str
    environment_setup_commit: str
    dataset: SWEBenchDatasetRef
    env_spec: EnvSpec
    mode: Literal["swebench"] = "swebench"

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        return validate_instance_id(value)

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str) -> str:
        return validate_repo_slug(value)

    @field_validator("base_commit")
    @classmethod
    def _base(cls, value: str) -> str:
        return validate_base_commit(value)

    @field_validator("environment_setup_commit")
    @classmethod
    def _setup_commit(cls, value: str) -> str:
        return validate_base_commit(value)

    @field_validator("problem_statement")
    @classmethod
    def _problem(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("problem_statement 不能含 NUL")
        if not value.strip():
            raise ValueError("problem_statement 不能为空")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        return validate_swebench_version(value)

    @model_validator(mode="after")
    def _identity_is_consistent(self) -> SWEBenchInferenceTask:
        """instance id 必须由 repo 派生，否则 loader 的 repo/instance 绑定是假的。

        官方格式是 ``owner__name-<PR number>``。把这条一致性写进模型而不是只写在
        loader 里：任何绕过 loader 手工构造的路径同样会失败。
        """
        prefix = self.repo.replace("/", "__")
        if not self.instance_id.startswith(f"{prefix}-"):
            raise ValueError(f"instance_id={self.instance_id!r} 与 repo={self.repo!r} 不一致")
        return self

    @property
    def task_id(self) -> str:
        return self.instance_id

    @property
    def issue(self) -> str:
        return self.problem_statement

    @property
    def repository(self) -> str:
        """assembly 通过它材料化 base；SWE-bench 使用 GitHub 远端 URL。"""
        return f"https://github.com/{self.repo}.git"
