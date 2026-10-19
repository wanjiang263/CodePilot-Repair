"""E-16：调用官方 SWE-bench harness（``swebench==4.1.0``）评测模型补丁。

**版本与来源（已用真实子进程核实，不是照抄文档）**

* 官方包：``swebench==4.1.0``（PyPI 包名 ``swebench``，仓库 swe-bench/SWE-bench），
  安装在项目的 ``.venv-swebench``，**不进主 venv**。
* CLI 入口：``python -m swebench.harness.run_evaluation``，
  源码 ``.venv-swebench/Lib/site-packages/swebench/harness/run_evaluation.py``。
  ``--help`` 的真实输出、argparse 结构、``main()`` 签名见
  ``tests/fixtures/swebench_harness/official-help-4.1.0.txt`` 与
  ``official-cli-4.1.0.json``。
* 汇总报告：``swebench.harness.reporting.make_run_report``；``schema_version=2`` 的
  真实输出见 ``tests/fixtures/swebench_harness/official-report-4.1.0.json``。
* 单实例报告：``swebench.harness.grading.get_eval_report``（字段
  ``patch_is_None/patch_exists/patch_successfully_applied/resolved/tests_status``）。
* 数据集快照：``cache/swebench/<dataset>__<revision>/<split>.jsonl``（本模块只读它做
  实例归属与 revision 固定校验，不重新下载）。

**为什么原生 Windows 上不能直接 import**

``swebench.harness.prepare_images`` 顶层 ``import resource``（Unix-only），因此主 venv
里 import 官方包会在解释器层失败。本模块**不 import swebench**：只拼命令、解析 JSON
报告，并通过 ``importlib.util.find_spec("resource")`` 探测平台。真要在 Windows 上读取
官方 Python 对象时，必须用 ``.venv-swebench`` 子进程并先注入 ``resource`` 桩
（``cache/verify/gen_assets.py`` 的做法），本模块已把该做法用于生成夹具。

**三个容易写错的官方事实（都来自 4.1.0 源码，不是猜测）**

1. 汇总报告文件名是 ``<model_name_or_path 的 / 换成 __>.<run_id>.json``；路径由
   ``Path(...)`` 相对**进程 cwd** 计算。``--report_dir`` 参数在 4.1.0 里只被
   ``mkdir``，**没有**参与写报告。所以本模块把子进程 ``cwd`` 设为 ``report_dir``，
   让报告与 ``logs/run_evaluation/`` 证据都落在我们的目录下。
2. 汇总报告是 ``schema_version=2``，15 个必选字段：
   ``total_instances/submitted_instances/completed_instances/resolved_instances/
   unresolved_instances/empty_patch_instances/error_instances/completed_ids/
   incomplete_ids/empty_patch_ids/submitted_ids/resolved_ids/unresolved_ids/
   error_ids/schema_version``。另有三键只在 ``make_run_report(client=None)`` 时写入
   （``unstopped_instances/unstopped_containers/unremoved_images``），真实本地执行
   总会传 docker client，因此这三键**必须按可选字段**解析。官方 ``error_ids`` 有
   两个来源：report.json 存在但解析失败（同时计入 completed），以及 report.json
   根本不存在（**不**计入 completed）——所以 ``error_ids`` 只要求是
   ``submitted_ids`` 的子集，不能强求在 completed 里。
3. 官方 ``get_dataset_from_preds`` 会用 ``ValueError`` 拒绝不属于当前数据集的
   ``instance_id``，但它要等子进程下载完数据集才发生。我们把这道校验前移到
   ``build_command``/``run`` 之前（fail closed），并把本地快照路径作为 ``-d``
   传给官方 CLI——官方 CLI 4.1.0 **没有** revision 参数，传本地快照是唯一能真正
   固定 revision 的方式。

**接口增量（契约 §7 之外的扩展，已由 Lead 接受并写回契约）**

契约 §7 冻结的是核心字段；下面是并行开发后补充、**全部为增量、不破坏原签名**的
接口。后来人读代码时请把它们当作契约的一部分：

* ``HarnessConfig`` 增 ``dataset_revision``（缓存身份必需，契约 §7 要求身份含
  dataset_revision，但原字段列表没给来源）与 ``dataset_snapshot``（官方 CLI 没有
  revision 参数，传 ``<snapshot>/<split>.jsonl`` 是唯一能固定 revision 的方式）。
* ``OfficialHarnessRunner.__init__`` 增可选 ``artifacts``（ArtifactStore，证据落
  ``artifacts/runs/<run_id>/swebench_harness/``）、``dataset_instances``（无快照时
  注入数据集归属集合）、``image_ids``（镜像身份；**建议注入来源是 Owner C 的
  ``SWEBenchEnvironmentIdentity`` 的 instance/env/base image 标识**，不注入时身份
  记空元组、复用强度下降）。
* ``run(self, *, dry_run: bool = False)`` 增可选 kw；新增 ``preview_command()``：
  smoke ``--dry-run`` 专用，不读 predictions、不 import swebench、不启动子进程，
  真实执行永远走 ``build_command``。
* 新增 :class:`HarnessExecutionResult`（``run()`` 返回：官方汇总 report + 逐实例
  verdicts + decision/error_kind/reused/evidence/缓存身份/输出尾部），以及
  :class:`HarnessErrorKind`、:class:`HarnessRunDecision`、:class:`HarnessControlKind`、
  :class:`HarnessDiscrepancy` 四个归因/审计枚举。
* ``HarnessResult.resolved`` 三态口径：只有官方 **RESOLVED→True、UNRESOLVED→False**；
  ``PATCH_APPLY_FAILED``/``EMPTY_PATCH``/``ERROR``/``MISSING``/``NOT_EVALUATED`` 一律
  ``None``。前两者是评测完整性形态（与 Owner F ``SWEBenchEvaluation`` 的"非
  RESOLVED/UNRESOLVED 不得带 resolved"硬校验一致），官方聚合口径看
  ``HarnessExecutionResult.report``（官方 ``unresolved_instances`` 把 apply-failure
  计入，``empty_patch_instances`` 单列）。**禁止**用内部 ``m2_verified`` 反推官方 bool。

**缓存身份（负控制）**

允许复用一份已完成报告的条件是：``patch_set_sha256`` + ``run_id`` +
``dataset_revision`` + ``harness_version`` + ``image_ids`` 全部一致，并且
``report.submitted_ids`` 与报告规模能对上当前输入。任何一项不符都**不复用**：

* 报告/身份 sidecar 缺失或无法解析 → 抛 :class:`HarnessCacheError`（要求换新
  ``run_id``），因为没法证明旧报告对应哪份补丁，继续跑同 ``run_id`` 会被官方
  ``run_instance`` 的"report.json 已存在就跳过"逻辑污染；
* 身份不符（典型：同 ``run_id`` 换了补丁）→ 抛 :class:`HarnessCacheError`；
  不同补丁必须用 :func:`derive_run_id` 生成新 ``run_id``；
* 身份一致但汇总报告损坏 → 隔离损坏文件后重跑（同一补丁重跑是安全的）；
* 身份一致且报告可解析 → 直接复用，不启动子进程。

**ORACLE_CONTROL（只在评测侧的正向控制）**

gold patch 只允许由评测侧调用方传入 :func:`run_oracle_control`，写进**独立的**
oracle predictions 文件（模型名固定 ``codepilot-oracle-control``、独立 run_id、
独立报告），返回结果标记 ``control_kind=ORACLE_CONTROL`` 且
``included_in_resolved_rate=False``。它永远不写主 predictions、不参与推理输入、
不计入模型 resolved rate——只用来验证"官方 harness + 镜像链路能跑通、gold 能被判
resolved"。

**内部 M2 与官方结论**

:func:`compare_with_internal_m2` 只做差异分类，永不把内部 ``m2_verified`` 升级成
``official_resolved=True``；每个 :class:`HarnessResult` 的 ``resolved`` 只来自官方
报告或官方报告 ID 集合。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform as platform_module
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from codepilot.artifacts import ArtifactStore

__all__ = [
    "DATASET_NAME",
    "DATASET_REVISION",
    "HARNESS_VERSION",
    "ORACLE_CONTROL_MODEL_NAME",
    "HarnessCacheError",
    "HarnessCacheIdentity",
    "HarnessCommand",
    "HarnessConfig",
    "HarnessControlKind",
    "HarnessDiscrepancy",
    "HarnessError",
    "HarnessErrorKind",
    "HarnessExecutionResult",
    "HarnessInputError",
    "HarnessPlatformError",
    "HarnessReport",
    "HarnessResult",
    "HarnessRunDecision",
    "LocalSubprocessRunner",
    "OfficialHarnessRunner",
    "OfficialVerdict",
    "PlatformInfo",
    "SubprocessOutcome",
    "SubprocessRunner",
    "compare_with_internal_m2",
    "default_harness_python",
    "derive_run_id",
    "oracle_control_run_id",
    "patch_set_digest",
    "run_oracle_control",
    "write_oracle_predictions",
]

# ---------------------------------------------------------------------------
# 契约 §0 / §2 的固定事实唯一真源在 Owner A 的 ``swebench_task`` 里。这里只做再导出：
# 两份常量一定会漂移，而 revision 漂移会让缓存身份静默失效，所以宁可多一条 import。
# ---------------------------------------------------------------------------
from codepilot.eval.swebench_task import (
    DATASET_NAME,
    DATASET_REVISION,
    HARNESS_VERSION,
    OFFICIAL_SPLIT_SIZES,
)

#: 官方 CLI 入口（4.1.0 真实模块路径）。
RUN_EVALUATION_MODULE = "swebench.harness.run_evaluation"
#: 官方 report 的 schema 版本（报告字段见 module docstring）。
REPORT_SCHEMA_VERSION = 2
#: 官方 predictions 的三字段（`get_predictions_from_file` 只强校验 instance_id，
#: 但 Writing 契约要求三字段齐全，我们 fail closed）。
PREDICTION_KEYS: tuple[str, str, str] = ("instance_id", "model_name_or_path", "model_patch")
#: 官方 instance_id 形态：`owner__repo-N`（如 astropy__astropy-12907）。
INSTANCE_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+")
#: 契约 §7 的 run_id 形态：`cp-<patch_set_sha256[:12]>-<seed>`。
RUN_ID_PATTERN = re.compile(r"cp-[0-9a-f]{12}-(?P<seed>[A-Za-z0-9._-]+)")
#: 官方报告文件名里只替换 `/`；其余字符会进入文件名，必须挡住 Windows 非法字符，
#: 否则报告写不出来，错误还会发生在子进程里（很难归因）。
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_HEX_40_OR_64 = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
#: 这些字符串出现在子进程输出里就归因为 Docker 不可用（4.1.0 在 import 阶段就会
#: 因为连不上 daemon 抛 `DockerException`，堆栈里一定有这些片段）。
_DOCKER_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "docker.errors",
    "dockerexception",
    "error while fetching server api version",
    "cannot connect to the docker daemon",
    "docker: command not found",
    "no such file or directory: 'docker'",
)
#: 官方 `make_run_report` 用第一条 prediction 的 model 字段做文件名；模型名必须能
#: 安全转成文件名（只允许 `/` 作为分隔符，映射成 `__`）。
_MODEL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]*(?:/[A-Za-z0-9._@+-]+)*")
#: 本模块默认在仓库根下的官方 venv 里找解释器（与契约 §0 一致）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_HARNESS_VENV = _REPO_ROOT / ".venv-swebench"
#: 数据集快照根目录。仅作为**调用方**的便利常量：生产代码仍应显式把
#: ``<snapshot>/<split>.jsonl`` 传给 ``HarnessConfig.dataset_snapshot`` 来固定
#: revision；本模块不做自动发现（否则"跑的是哪个 revision"会隐式取决于本机缓存）。
DEFAULT_SNAPSHOT_ROOT = _REPO_ROOT / "cache" / "swebench"
#: 官方 4.1.0 CLI 的 `-n/--namespace` 默认值。非空 → 实例镜像是**远端预构建镜像**
#: （`<namespace>/sweb.eval...`，实例 id 里的 `__` 替换成 `_1776_`）；`none` → 本地构建。
#: 必须显式传给官方，同时与 ``codepilot.sandbox.swebench_env.instance_image_key`` 的
#: 默认值一致，否则 preflight 查的镜像名和官方实际用的镜像名不是同一个。
OFFICIAL_NAMESPACE = "swebench"
#: 官方 `--cache_level` 取值与默认值（默认 `env`：跑完会删掉本次拉取/构建的实例镜像）。
CACHE_LEVELS: tuple[str, ...] = ("none", "base", "env", "instance")
OFFICIAL_CACHE_LEVEL = "env"
#: 官方给远端镜像名做的前缀替换标记（`swebench/sweb.eval.x86_64.astropy_1776_astropy-1`）。
_NAMESPACE_MARKER = "_1776_"
#: Docker 仓库名组件规则（小写），用于校验 namespace 不会破坏镜像名。
_DOCKER_NAME_COMPONENT = re.compile(r"[a-z0-9][a-z0-9._-]*")
#: 超时被杀的子进程没有退出码，用哨兵值把"超时"和"正常非零退出"分开。
TIMEOUT_EXIT_CODE = -1
#: 保存到 artifact 的 stdout/stderr 尾部长度；完整日志落盘，模型只看尾部。
_OUTPUT_TAIL_LIMIT = 4000
#: 每次子进程运行都在 `report_dir` 下留一份 codepilot 侧证据（命令/日志/结果）。
_EVIDENCE_DIRNAME = "_harness"
#: ArtifactStore 内的相对目录（`artifacts/runs/<run_id>/swebench_harness/`）。
_ARTIFACT_DIRNAME = "swebench_harness"
_IDENTITY_SUFFIX = ".identity.json"


# ---------------------------------------------------------------------------
# 错误与枚举
# ---------------------------------------------------------------------------
class HarnessErrorKind(StrEnum):
    """一次性可枚举的归因类别。``HarnessError.kind`` 与结果里的 ``error_kind`` 共用。"""

    ENV_ERROR = "ENV_ERROR"
    RESOURCE_BLOCKED = "RESOURCE_BLOCKED"
    PREDICTIONS_MISSING = "PREDICTIONS_MISSING"
    PREDICTIONS_INVALID = "PREDICTIONS_INVALID"
    PATCH_HASH_MISMATCH = "PATCH_HASH_MISMATCH"
    DATASET_MISMATCH = "DATASET_MISMATCH"
    CACHE_IDENTITY_MISMATCH = "CACHE_IDENTITY_MISMATCH"
    CACHE_UNVERIFIED = "CACHE_UNVERIFIED"
    PROCESS_FAILED = "PROCESS_FAILED"
    TIMEOUT = "TIMEOUT"
    DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
    REPORT_MISSING = "REPORT_MISSING"
    REPORT_CORRUPTED = "REPORT_CORRUPTED"
    REPORT_IDENTITY_MISMATCH = "REPORT_IDENTITY_MISMATCH"
    INSTANCE_ERROR = "INSTANCE_ERROR"
    INTERNAL_INCONSISTENCY = "INTERNAL_INCONSISTENCY"


class OfficialVerdict(StrEnum):
    """官方侧对**单个实例**的结论。三态 ``resolved`` 见 :meth:`resolved_value`。"""

    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"
    EMPTY_PATCH = "EMPTY_PATCH"
    ERROR = "ERROR"
    MISSING = "MISSING"
    NOT_EVALUATED = "NOT_EVALUATED"

    @property
    def resolved_value(self) -> bool | None:
        """映射成 ``HarnessResult.resolved`` 的三态。

        只有官方明确给出 **RESOLVED / UNRESOLVED** 时才返回 ``bool``；其余一律
        ``None``，原因各不相同但都重要：

        * ``PATCH_APPLY_FAILED``：补丁根本没打上，官方没测到"模型改完后仓库的行为"，
          它是一条**评测完整性**结论（Owner F 的 ``SWEBenchEvaluation`` 也把
          "非 RESOLVED/UNRESOLVED 不得带 resolved"写成硬校验）；
        * ``EMPTY_PATCH``：官方在评测前就过滤掉空补丁，报告里单列 empty_patch_ids，
          它既不是 unresolved 也不是 resolved；
        * ``ERROR/MISSING/NOT_EVALUATED``：没测到，写成 ``False`` 会把"没测到"冒充
          "模型没修好"。

        官方聚合口径仍以 :class:`HarnessReport` 为准：``unresolved_instances`` 包含
        patch-apply-failure（官方把它们算作 unresolved），``empty_patch_instances``
        单列；统计层用 report 的数字，单实例归因用 verdict。
        """
        if self is OfficialVerdict.RESOLVED:
            return True
        if self is OfficialVerdict.UNRESOLVED:
            return False
        return None

    @property
    def is_evaluated(self) -> bool:
        """官方是否真的给出了判定（不含 MISSING/NOT_EVALUATED）。"""
        return self not in (OfficialVerdict.MISSING, OfficialVerdict.NOT_EVALUATED)


class HarnessRunDecision(StrEnum):
    """这次 ``run()`` 实际发生了什么。审计报告里要用它区分"跑了"和"读了缓存"。"""

    EXECUTED = "EXECUTED"
    REUSED = "REUSED"
    REBUILT = "REBUILT"
    DRY_RUN = "DRY_RUN"
    PLATFORM_BLOCKED = "PLATFORM_BLOCKED"


class HarnessControlKind(StrEnum):
    """结果属于模型主评测还是评测侧正向控制（ORACLE_CONTROL）。"""

    PRIMARY = "PRIMARY"
    ORACLE_CONTROL = "ORACLE_CONTROL"


class HarnessDiscrepancy(StrEnum):
    """官方结论与内部 M2 状态的关系（只分类，不修改官方结论）。"""

    NONE = "NONE"
    OFFICIAL_DENIED_INTERNAL_VERIFIED = "OFFICIAL_DENIED_INTERNAL_VERIFIED"
    OFFICIAL_RESOLVED_INTERNAL_NOT_VERIFIED = "OFFICIAL_RESOLVED_INTERNAL_NOT_VERIFIED"
    OFFICIAL_NOT_EVALUATED = "OFFICIAL_NOT_EVALUATED"
    INTERNAL_UNKNOWN = "INTERNAL_UNKNOWN"


class HarnessError(RuntimeError):
    """带归因类别的 harness 错误基类。"""

    kind: HarnessErrorKind
    detail: str

    def __init__(self, kind: HarnessErrorKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind.value}: {detail}")


class HarnessInputError(HarnessError):
    """输入（predictions/config/数据集归属）在启动子进程前就被拒绝。"""


class HarnessCacheError(HarnessError):
    """缓存身份无法证明或与当前输入不符；**拒绝复用**并要求换新 run_id。"""


class HarnessPlatformError(HarnessError):
    """当前平台无法执行官方 harness（原生 Windows / 无 WSL）。"""


# ---------------------------------------------------------------------------
# 平台与子进程抽象（可注入，测试不需要真实进程 / 真实平台）
# ---------------------------------------------------------------------------
class PlatformInfo(BaseModel):
    """执行平台事实。生产用 :meth:`detect`，测试直接构造。

    官方 harness 只在 Linux 上真正可执行；Windows 必须经 WSL2（``wsl.exe -e ...``）。
    macOS 没有 ``resource`` 也不在官方支持范围（契约 §0：WSL2 或 Linux 才可执行）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    release: str = ""
    machine: str = ""
    #: 当前解释器是否能 import 标准库 ``resource``（原生 Windows 为 False）。
    has_resource_module: bool
    wsl_executable: Path | None = None
    use_wsl: bool = False

    @classmethod
    def detect(
        cls,
        *,
        use_wsl: bool = False,
        wsl_executable: Path | None = None,
    ) -> PlatformInfo:
        system = platform_module.system()
        resolved_wsl: Path | None = wsl_executable
        if use_wsl and resolved_wsl is None and system == "Windows":
            found = shutil.which("wsl")
            resolved_wsl = Path(found) if found is not None else None
        return cls(
            system=system,
            release=platform_module.release(),
            machine=platform_module.machine(),
            has_resource_module=importlib.util.find_spec("resource") is not None,
            wsl_executable=resolved_wsl,
            use_wsl=use_wsl,
        )

    @property
    def is_windows(self) -> bool:
        return self.system.lower() == "windows"

    @property
    def official_execution_supported(self) -> bool:
        """官方 harness 是否能在本平台启动（Linux 直接跑；Windows 仅 WSL2）。"""
        if not self.has_resource_module and not self.is_windows:
            return False
        if self.system.lower() == "linux":
            return True
        if self.is_windows:
            return self.use_wsl and self.wsl_executable is not None
        return False

    def describe(self) -> str:
        wsl = f"wsl={self.wsl_executable}" if self.wsl_executable is not None else "wsl=None"
        return (
            f"system={self.system} release={self.release} machine={self.machine} "
            f"resource={self.has_resource_module} use_wsl={self.use_wsl} {wsl}"
        )


class SubprocessOutcome(BaseModel):
    """一次子进程执行的结果。``timed_out=True`` 时 ``returncode`` 为 -1 哨兵。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_seconds: float = 0.0


class SubprocessRunner(Protocol):
    """子进程执行器协议。测试用 Fake 注入，生产用 :class:`LocalSubprocessRunner`。

    约定：``env`` 只包含**显式注入**的变量；执行器负责与父进程环境合并（避免把
    API key 之类写进 ``HarnessCommand``/artifact）。``timeout_seconds`` 必须 > 0，
    超时实现应杀掉子进程并返回 ``timed_out=True``，而不是抛异常。
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> SubprocessOutcome: ...


class LocalSubprocessRunner:
    """真实子进程执行器（``subprocess.run`` + 超时 + 归并环境）。"""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_seconds: int,
    ) -> SubprocessOutcome:
        merged_env = {**os.environ, **dict(env)}
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(cwd),
                env=merged_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return SubprocessOutcome(
                returncode=TIMEOUT_EXIT_CODE,
                stdout=_decode_output(error.stdout),
                stderr=_decode_output(error.stderr),
                timed_out=True,
                duration_seconds=time.monotonic() - started,
            )
        except OSError as error:
            raise HarnessError(
                HarnessErrorKind.PROCESS_FAILED,
                f"无法启动官方 harness 子进程: {error}",
            ) from error
        return SubprocessOutcome(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            timed_out=False,
            duration_seconds=time.monotonic() - started,
        )


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# ---------------------------------------------------------------------------
# 配置 / 命令 / 缓存身份
# ---------------------------------------------------------------------------
class HarnessConfig(BaseModel):
    """官方 harness 一次评测的冻结配置。

    契约 §7 原字段：``predictions_path/run_id/report_dir/max_workers/
    timeout_seconds/dataset_name/split/instance_ids/harness_python/
    harness_version/model_name_or_path``。另外两个字段是**必要扩展**（已向 Lead
    报备）：``dataset_revision`` 是缓存身份组成（契约 §7 要求），
    ``dataset_snapshot`` 是官方 CLI 4.1.0 唯一能固定 revision 的输入方式——官方
    ``-d`` 没有 revision 参数，传本地快照路径才能避免 HF 上数据集漂移。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    predictions_path: Path
    run_id: str
    report_dir: Path
    max_workers: int = 4
    timeout_seconds: int = 1800
    dataset_name: str = DATASET_NAME
    split: str = "test"
    instance_ids: tuple[str, ...] = ()
    harness_python: Path | None = None
    harness_version: str = HARNESS_VERSION
    model_name_or_path: str
    # --- 必要扩展 ---
    dataset_revision: str = DATASET_REVISION
    dataset_snapshot: Path | None = None
    namespace: str | None = OFFICIAL_NAMESPACE
    cache_level: str = OFFICIAL_CACHE_LEVEL

    @field_validator("max_workers", "timeout_seconds", mode="before")
    @classmethod
    def _strict_int(cls, value: object) -> object:
        """拒绝 ``True``/``4.0`` 这类会被隐式转成整数的值（Phase 4 的教训）。"""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("必须是整数（bool/float/字符串会被拒绝）")
        return value

    @field_validator("max_workers")
    @classmethod
    def _check_workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_workers 必须 >= 1")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _check_timeout(cls, value: int) -> int:
        if not 1 <= value <= 86_400:
            raise ValueError("timeout_seconds 必须在 [1, 86400]")
        return value

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise ValueError("run_id 不能为空且不能有首尾空白")
        if _UNSAFE_FILENAME_CHARS.search(value) or "/" in value or value in {".", ".."}:
            raise ValueError(f"run_id 不能包含路径分隔符/Windows 非法字符: {value!r}")
        return value

    @field_validator("split")
    @classmethod
    def _check_split(cls, value: str) -> str:
        if value not in OFFICIAL_SPLIT_SIZES:
            raise ValueError(f"split 必须是 {sorted(OFFICIAL_SPLIT_SIZES)}，收到 {value!r}")
        return value

    @field_validator("dataset_name")
    @classmethod
    def _check_dataset_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dataset_name 不能为空")
        return value

    @field_validator("harness_version")
    @classmethod
    def _check_harness_version(cls, value: str) -> str:
        if value != HARNESS_VERSION:
            raise ValueError(f"只支持锁定的 swebench=={HARNESS_VERSION}，收到 {value!r}")
        return value

    @field_validator("dataset_revision")
    @classmethod
    def _check_revision(cls, value: str) -> str:
        if _HEX_40_OR_64.fullmatch(value) is None:
            raise ValueError("dataset_revision 必须是 40/64 位小写十六进制")
        return value

    @field_validator("model_name_or_path")
    @classmethod
    def _check_model_name(cls, value: str) -> str:
        if _MODEL_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "model_name_or_path 必须是可安全转文件名的模型标识"
                f"（允许 '/' 分层，不能有反斜杠/空格/Windows 非法字符）: {value!r}"
            )
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError(f"model_name_or_path 不能含空段/'.'/'..': {value!r}")
        return value

    @field_validator("instance_ids")
    @classmethod
    def _check_instance_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("instance_ids 有重复")
        for instance_id in value:
            if INSTANCE_ID_PATTERN.fullmatch(instance_id) is None:
                raise ValueError(f"非法 instance_id: {instance_id!r}")
        return value

    @field_validator("namespace")
    @classmethod
    def _check_namespace(cls, value: str | None) -> str | None:
        """官方把字符串 `"none"` 解析成 None，所以我们只接受 None 或合法命名空间。"""
        if value is None:
            return None
        if value.strip() != value or not value:
            raise ValueError("namespace 不能为空或有首尾空白；本地构建请传 namespace=None")
        if value.lower() == "none":
            raise ValueError('官方把 "none" 解析成 None；请直接传 namespace=None，避免两种写法')
        if _DOCKER_NAME_COMPONENT.fullmatch(value) is None:
            raise ValueError(f"namespace 必须是合法的小写镜像名组件: {value!r}")
        if "__" in value:
            raise ValueError(
                f"namespace 不能含 '__'（官方会把它替换成 '{_NAMESPACE_MARKER}'，"
                "会让镜像身份与 instance_image_key 不一致）"
            )
        return value

    @field_validator("cache_level")
    @classmethod
    def _check_cache_level(cls, value: str) -> str:
        if value not in CACHE_LEVELS:
            raise ValueError(f"cache_level 必须是 {CACHE_LEVELS}，收到 {value!r}")
        return value

    @model_validator(mode="after")
    def _check_predictions_path(self) -> Self:
        suffix = self.predictions_path.suffix.lower()
        if suffix not in {".json", ".jsonl"}:
            raise ValueError("predictions_path 必须是 .json 或 .jsonl（官方 4.1.0 限制）")
        if self.dataset_snapshot is not None and self.dataset_snapshot.suffix.lower() not in {
            ".json",
            ".jsonl",
        }:
            raise ValueError("dataset_snapshot 必须是 .json 或 .jsonl")
        return self

    @property
    def expected_report_name(self) -> str:
        """官方 4.1.0 的文件名规则：``<model 的 / → __>.<run_id>.json``。"""
        return f"{self.model_slug}.{self.run_id}.json"

    @property
    def model_slug(self) -> str:
        return self.model_name_or_path.replace("/", "__")


class HarnessCommand(BaseModel):
    """要执行的官方命令。``env`` 只含显式注入变量（执行器负责合并父进程环境）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    expected_report: Path

    @field_validator("argv")
    @classmethod
    def _check_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("argv 不能为空")
        if any(not item for item in value):
            raise ValueError("argv 不能有空参数")
        return value

    @property
    def identity_sidecar(self) -> Path:
        """缓存身份 sidecar 路径（与官方汇总报告同目录）。"""
        return self.expected_report.with_name(self.expected_report.name + _IDENTITY_SUFFIX)

    @property
    def per_instance_log_root(self) -> Path:
        """官方 ``RUN_EVALUATION_LOG_DIR`` 是相对路径 ``logs/run_evaluation``（4.1.0），
        因此按 cwd 解析。"""
        return self.cwd / "logs" / "run_evaluation"

    def per_instance_report(self, instance_id: str, model_slug: str, run_id: str) -> Path:
        return self.per_instance_log_root / run_id / model_slug / instance_id / "report.json"


class HarnessCacheIdentity(BaseModel):
    """允许复用官方报告的完整身份。

    契约 §7 要求 ``patch_hash + run_id + dataset_revision + harness_version +
    image ids``；这里再加 ``model_name_or_path/dataset_name/split``，因为它们同样
    决定报告里的数字含义。``image_ids`` 由调用方（Owner C 的环境身份）注入；未提供
    时只记录空元组，此时无法证明镜像身份——复用仍会比对"双方都为空"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    patch_hashes: tuple[tuple[str, str], ...]
    model_name_or_path: str
    dataset_name: str
    dataset_revision: str
    split: str
    harness_version: str
    #: 远端/本地构建决定了报告是在哪套镜像上判分的；没有 `image_ids` 注入时，它是
    #: 唯一能区分两种环境的字段。**必填**：Phase 11 之前写的 sidecar 没有这个字段，
    #: 解析即失败 → 拒绝复用（当时也没有任何真实官方报告，代价为零）。
    namespace: str | None
    image_ids: tuple[tuple[str, str], ...] = ()

    @field_validator("patch_hashes", "image_ids")
    @classmethod
    def _check_pairs(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        keys = [key for key, _ in value]
        if len(set(keys)) != len(keys):
            raise ValueError("身份映射的 key 有重复")
        for key, digest in value:
            if not key:
                raise ValueError("身份映射的 key 不能为空")
            if not digest:
                raise ValueError(f"身份映射 {key!r} 的 value 不能为空")
        return tuple(sorted(value))

    @property
    def patch_set_sha256(self) -> str:
        return patch_set_digest(dict(self.patch_hashes))

    @property
    def fingerprint(self) -> str:
        """整个身份的规范哈希（规范 JSON，排序键）。"""
        return hashlib.sha256(
            _canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()

    def patch_hash_for(self, instance_id: str) -> str | None:
        return dict(self.patch_hashes).get(instance_id)

    def image_identity_for(self, instance_id: str) -> str | None:
        return dict(self.image_ids).get(instance_id)

    def mismatches(self, other: HarnessCacheIdentity) -> tuple[str, ...]:
        """逐字段列出差异，作为"为什么不能复用"的可读依据。"""
        reasons: list[str] = []
        if self.run_id != other.run_id:
            reasons.append(f"run_id {other.run_id!r} != {self.run_id!r}")
        if self.patch_set_sha256 != other.patch_set_sha256:
            reasons.append(
                f"patch_set_sha256 {other.patch_set_sha256[:12]} != {self.patch_set_sha256[:12]}"
            )
        for field in ("model_name_or_path", "dataset_name", "dataset_revision", "split"):
            mine = getattr(self, field)
            theirs = getattr(other, field)
            if mine != theirs:
                reasons.append(f"{field} {theirs!r} != {mine!r}")
        if self.harness_version != other.harness_version:
            reasons.append(f"harness_version {other.harness_version!r} != {self.harness_version!r}")
        if self.namespace != other.namespace:
            reasons.append(f"namespace {other.namespace!r} != {self.namespace!r}")
        if self.image_ids != other.image_ids:
            reasons.append(f"image_ids {dict(other.image_ids)!r} != {dict(self.image_ids)!r}")
        return tuple(reasons)


def patch_set_digest(patch_hashes: Mapping[str, str]) -> str:
    """补丁集合的规范摘要：按 instance_id 排序后对 ``(id, sha256)`` 数组做 sha256。"""
    ordered = [[key, patch_hashes[key]] for key in sorted(patch_hashes)]
    return hashlib.sha256(_canonical_json(ordered).encode("utf-8")).hexdigest()


def derive_run_id(patch_hashes: Mapping[str, str], *, seed: str) -> str:
    """契约 §7 的 run_id 形态；**不同补丁必须得到不同 run_id**。"""
    if not seed or _UNSAFE_FILENAME_CHARS.search(seed) or "/" in seed:
        raise ValueError(f"非法 seed: {seed!r}")
    return f"cp-{patch_set_digest(patch_hashes)[:12]}-{seed}"


def default_harness_python(root: Path | None = None) -> Path | None:
    """默认从项目 ``.venv-swebench`` 找官方解释器（Scripts 或 bin）。"""
    venv = DEFAULT_HARNESS_VENV if root is None else Path(root) / ".venv-swebench"
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 官方报告模型与解析
# ---------------------------------------------------------------------------
class HarnessReport(BaseModel):
    """``swebench.harness.reporting.make_run_report`` 的 schema_version=2 报告。

    ``unstopped_*`` 三键在真实本地执行（传了 docker client）时**不存在**，因此
    全部可选；缺失不等于损坏。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    total_instances: int
    submitted_instances: int
    completed_instances: int
    resolved_instances: int
    unresolved_instances: int
    empty_patch_instances: int
    error_instances: int
    completed_ids: tuple[str, ...]
    incomplete_ids: tuple[str, ...]
    empty_patch_ids: tuple[str, ...]
    submitted_ids: tuple[str, ...]
    resolved_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    error_ids: tuple[str, ...]
    unstopped_instances: int | None = None
    unstopped_containers: tuple[str, ...] = ()
    unremoved_images: tuple[str, ...] = ()
    #: 解析来源证据（由 parse_report_file 填充），用于缓存审计。
    source_path: Path | None = None
    source_sha256: str | None = None

    @model_validator(mode="after")
    def _check_counts(self) -> Self:
        if self.schema_version != REPORT_SCHEMA_VERSION:
            raise ValueError(
                f"只支持官方报告 schema_version={REPORT_SCHEMA_VERSION}，收到 {self.schema_version}"
            )
        pairs = (
            ("submitted_instances", self.submitted_instances, self.submitted_ids),
            ("completed_instances", self.completed_instances, self.completed_ids),
            ("resolved_instances", self.resolved_instances, self.resolved_ids),
            ("unresolved_instances", self.unresolved_instances, self.unresolved_ids),
            ("empty_patch_instances", self.empty_patch_instances, self.empty_patch_ids),
            ("error_instances", self.error_instances, self.error_ids),
        )
        for name, count, ids in pairs:
            if count < 0:
                raise ValueError(f"{name} 不能为负")
            if count != len(ids):
                raise ValueError(f"{name}={count} 与该 ID 列表长度 {len(ids)} 不一致")
        if self.total_instances < 0:
            raise ValueError("total_instances 不能为负")
        if self.total_instances < len(self.submitted_ids):
            raise ValueError("total_instances 小于 submitted_ids 数量")
        for name, ids in (
            ("completed_ids", self.completed_ids),
            ("incomplete_ids", self.incomplete_ids),
            ("empty_patch_ids", self.empty_patch_ids),
            ("submitted_ids", self.submitted_ids),
            ("resolved_ids", self.resolved_ids),
            ("unresolved_ids", self.unresolved_ids),
            ("error_ids", self.error_ids),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} 有重复 instance_id")
        if not set(self.completed_ids) <= set(self.submitted_ids):
            raise ValueError("completed_ids 必须是 submitted_ids 的子集")
        if set(self.incomplete_ids) & set(self.submitted_ids):
            raise ValueError("incomplete_ids 与 submitted_ids 不能相交")
        if not set(self.empty_patch_ids) <= set(self.submitted_ids):
            raise ValueError("empty_patch_ids 必须是 submitted_ids 的子集")
        if set(self.empty_patch_ids) & set(self.completed_ids):
            raise ValueError("empty_patch_ids 不能出现在 completed_ids（官方在评测前过滤空补丁）")
        if not set(self.resolved_ids) <= set(self.completed_ids):
            raise ValueError("resolved_ids 必须是 completed_ids 的子集")
        if not set(self.unresolved_ids) <= set(self.completed_ids):
            raise ValueError("unresolved_ids 必须是 completed_ids 的子集")
        # 官方 reporting.py 里 error_ids 有两个来源：report.json 存在但解析失败
        # （此时也在 completed_ids）、以及 report.json 根本不存在（不在 completed）。
        # 所以 error_ids 只必须是 submitted_ids 的子集，不能强求在 completed 里。
        if not set(self.error_ids) <= set(self.submitted_ids):
            raise ValueError("error_ids 必须是 submitted_ids 的子集")
        for left, right in (
            (self.resolved_ids, self.unresolved_ids),
            (self.resolved_ids, self.error_ids),
            (self.unresolved_ids, self.error_ids),
        ):
            if set(left) & set(right):
                raise ValueError("resolved/unresolved/error 三类 ID 必须互斥")
        if len(self.resolved_ids) + len(self.unresolved_ids) > len(self.completed_ids):
            raise ValueError("resolved/unresolved 计数之和超过 completed")
        return self

    @property
    def resolved_rate(self) -> float | None:
        """官方口径 ``resolved_instances / total_instances``（全量任务分母）。"""
        if self.total_instances <= 0:
            return None
        return self.resolved_instances / self.total_instances

    @property
    def evaluation_coverage(self) -> float | None:
        """已完成官方判定的比例（completed/submitted）。"""
        if self.submitted_instances <= 0:
            return None
        return self.completed_instances / self.submitted_instances

    def verdict_for(self, instance_id: str) -> OfficialVerdict:
        """从官方 ID 集合推导单实例结论。

        优先级：空补丁 → 已 resolved → 已 unresolved → error → completed 但无归类
        （视为 ERROR，官方报告自身不一致时不猜成 resolved）→ submitted 但没完成
        （MISSING）→ 根本没提交（NOT_EVALUATED）。
        """
        if instance_id in self.empty_patch_ids:
            return OfficialVerdict.EMPTY_PATCH
        if instance_id in self.resolved_ids:
            return OfficialVerdict.RESOLVED
        if instance_id in self.unresolved_ids:
            return OfficialVerdict.UNRESOLVED
        if instance_id in self.error_ids:
            return OfficialVerdict.ERROR
        if instance_id in self.completed_ids:
            return OfficialVerdict.ERROR
        if instance_id in self.submitted_ids:
            return OfficialVerdict.MISSING
        return OfficialVerdict.NOT_EVALUATED


def _parse_report_file(path: Path) -> HarnessReport:
    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise HarnessError(
            HarnessErrorKind.REPORT_MISSING, f"读不到官方报告 {path}: {error}"
        ) from error
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HarnessError(
            HarnessErrorKind.REPORT_CORRUPTED, f"官方报告不是合法 UTF-8 JSON {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise HarnessError(HarnessErrorKind.REPORT_CORRUPTED, f"官方报告顶层不是对象: {path}")
    raw: dict[str, Any] = payload
    try:
        report = HarnessReport(
            schema_version=_require_int(raw, "schema_version"),
            total_instances=_require_int(raw, "total_instances"),
            submitted_instances=_require_int(raw, "submitted_instances"),
            completed_instances=_require_int(raw, "completed_instances"),
            resolved_instances=_require_int(raw, "resolved_instances"),
            unresolved_instances=_require_int(raw, "unresolved_instances"),
            empty_patch_instances=_require_int(raw, "empty_patch_instances"),
            error_instances=_require_int(raw, "error_instances"),
            completed_ids=_require_id_list(raw, "completed_ids"),
            incomplete_ids=_require_id_list(raw, "incomplete_ids"),
            empty_patch_ids=_require_id_list(raw, "empty_patch_ids"),
            submitted_ids=_require_id_list(raw, "submitted_ids"),
            resolved_ids=_require_id_list(raw, "resolved_ids"),
            unresolved_ids=_require_id_list(raw, "unresolved_ids"),
            error_ids=_require_id_list(raw, "error_ids"),
            unstopped_instances=_optional_int(raw, "unstopped_instances"),
            unstopped_containers=_optional_id_list(raw, "unstopped_containers"),
            unremoved_images=_optional_id_list(raw, "unremoved_images"),
            source_path=path,
            source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )
    except ValidationError as error:
        raise HarnessError(
            HarnessErrorKind.REPORT_CORRUPTED,
            f"官方报告 schema_version=2 校验失败 {path}: {error}",
        ) from error
    return report


def _require_int(raw: Mapping[str, Any], key: str) -> int:
    if key not in raw:
        raise HarnessError(HarnessErrorKind.REPORT_CORRUPTED, f"官方报告缺字段 {key!r}")
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise HarnessError(HarnessErrorKind.REPORT_CORRUPTED, f"官方报告字段 {key!r} 必须是整数")
    return value


def _optional_int(raw: Mapping[str, Any], key: str) -> int | None:
    if key not in raw or raw[key] is None:
        return None
    return _require_int(raw, key)


def _require_id_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    if key not in raw:
        raise HarnessError(HarnessErrorKind.REPORT_CORRUPTED, f"官方报告缺字段 {key!r}")
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HarnessError(
            HarnessErrorKind.REPORT_CORRUPTED, f"官方报告字段 {key!r} 必须是字符串数组"
        )
    return tuple(value)


def _optional_id_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    if key not in raw or raw[key] is None:
        return ()
    return _require_id_list(raw, key)


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------
class HarnessResult(BaseModel):
    """单个实例的官方评测结论（契约 §7 字段，一个不多一个不少）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: OfficialVerdict
    instance_id: str
    resolved: bool | None
    report_path: Path | None
    report_sha256: str | None
    error_kind: HarnessErrorKind | None
    error_detail: str | None
    runtime_seconds: float
    command: HarnessCommand
    cache_identity: HarnessCacheIdentity


class HarnessExecutionResult(BaseModel):
    """一次 ``run()`` 的完整结果（官方汇总 + 逐实例结论 + 证据路径）。

    ``control_kind=ORACLE_CONTROL`` 的结果**不得**进入模型 resolved rate
    （``included_in_resolved_rate=False``）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    command: HarnessCommand
    decision: HarnessRunDecision
    exit_code: int | None
    timed_out: bool = False
    runtime_seconds: float
    report: HarnessReport | None = None
    report_path: Path | None = None
    report_sha256: str | None = None
    #: 执行层的缓存身份（契约 §7 也要求每个 HarnessResult 带一份；这里放一份便于
    #: smoke/runner 直接读，不必从 verdicts 里取）。
    cache_identity: HarnessCacheIdentity | None = None
    verdicts: tuple[HarnessResult, ...]
    error_kind: HarnessErrorKind | None = None
    error_detail: str | None = None
    reused: bool = False
    reused_reason: str | None = None
    warnings: tuple[str, ...] = ()
    control_kind: HarnessControlKind = HarnessControlKind.PRIMARY
    included_in_resolved_rate: bool = True
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    evidence_dir: Path | None = None
    platform: PlatformInfo | None = None

    def verdict_for(self, instance_id: str) -> OfficialVerdict:
        for item in self.verdicts:
            if item.instance_id == instance_id:
                return item.verdict
        raise KeyError(f"结果里没有实例 {instance_id!r}")

    @property
    def resolved_ids(self) -> tuple[str, ...]:
        return tuple(
            item.instance_id for item in self.verdicts if item.verdict is OfficialVerdict.RESOLVED
        )

    @property
    def unresolved_ids(self) -> tuple[str, ...]:
        return tuple(
            item.instance_id for item in self.verdicts if item.verdict is OfficialVerdict.UNRESOLVED
        )

    @property
    def partial(self) -> bool:
        """官方是否只完成了一部分（有 ERROR/MISSING/未评测实例）。"""
        return any(
            item.verdict in (OfficialVerdict.ERROR, OfficialVerdict.MISSING)
            or not item.verdict.is_evaluated
            for item in self.verdicts
        )

    @property
    def counts_toward_resolved_rate(self) -> bool:
        return self.included_in_resolved_rate and self.control_kind is HarnessControlKind.PRIMARY


# ---------------------------------------------------------------------------
# ORACLE_CONTROL：只在评测侧使用 gold 的正向控制
# ---------------------------------------------------------------------------
ORACLE_CONTROL_MODEL_NAME = "codepilot-oracle-control"


def oracle_control_run_id(gold_patches: Mapping[str, str], *, seed: str = "oracle") -> str:
    """ORACLE_CONTROL 专用 run_id，与模型主评测的 run_id 空间分开。"""
    if not seed or _UNSAFE_FILENAME_CHARS.search(seed) or "/" in seed:
        raise ValueError(f"非法 seed: {seed!r}")
    digest = patch_set_digest(gold_patches)[:12]
    return f"cp-oracle-{digest}-{seed}"


def write_oracle_predictions(
    gold_patches: Mapping[str, str],
    path: Path,
    *,
    model_name_or_path: str = ORACLE_CONTROL_MODEL_NAME,
) -> Path:
    """把 gold patch 写成**独立** predictions 文件（绝不碰主 predictions）。

    调用方（评测侧）从 ``SWEBenchEvaluationTask.gold_patch`` 取值；本模块不读取
    数据集/gold 本身，避免推理侧经由本模块接触 gold。
    """
    if not gold_patches:
        raise HarnessInputError(HarnessErrorKind.PREDICTIONS_INVALID, "gold_patches 不能为空")
    lines: list[str] = []
    for instance_id in sorted(gold_patches):
        patch = gold_patches[instance_id]
        if not isinstance(patch, str) or not patch.strip():
            raise HarnessInputError(
                HarnessErrorKind.PREDICTIONS_INVALID,
                f"gold patch 为空或非字符串: {instance_id!r}",
            )
        lines.append(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": model_name_or_path,
                    "model_patch": patch,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def run_oracle_control(
    config: HarnessConfig,
    gold_patches: Mapping[str, str],
    *,
    oracle_predictions_path: Path | None = None,
    runner: SubprocessRunner | None = None,
    platform: PlatformInfo | None = None,
    artifacts: ArtifactStore | None = None,
    image_ids: Mapping[str, str] | None = None,
) -> HarnessExecutionResult:
    """ORACLE_CONTROL：用 gold patch 调官方 harness 的正向控制（仅评测侧）。

    隔离保证（全部由代码强制，不靠约定）：

    1. gold 写进独立 predictions 文件，路径不得等于 ``config.predictions_path``；
    2. 模型名固定 ``codepilot-oracle-control``、run_id 固定 ``cp-oracle-...``，
       报告文件/日志目录与主评测天然不重叠；
    3. 返回结果标记 ``control_kind=ORACLE_CONTROL`` 且
       ``included_in_resolved_rate=False``——统计层拿不到可混入的正数；
    4. gold 不进入推理：本函数只被评测侧调用，调用方传入的是评测任务对象里的字段。

    为什么需要它：官方 harness 在原生 Windows 上跑不了（ENV_ERROR），在 Linux 上
    第一次真实评测时也可能因为镜像/环境问题全 ERROR。gold 都判不过，说明问题在
    评测链路而不在模型——这个正向控制必须先跑通，否则任何 resolved rate 都不能信。
    """
    oracle_run_id = oracle_control_run_id(gold_patches)
    if oracle_predictions_path is None:
        oracle_predictions_path = config.report_dir / "oracle-control" / f"{oracle_run_id}.jsonl"
    if oracle_predictions_path.resolve() == config.predictions_path.resolve():
        raise HarnessInputError(
            HarnessErrorKind.PREDICTIONS_INVALID,
            "ORACLE_CONTROL 的 predictions 不能与主 predictions 同文件",
        )
    write_oracle_predictions(gold_patches, oracle_predictions_path)
    oracle_config = config.model_copy(
        update={
            "predictions_path": oracle_predictions_path,
            "run_id": oracle_run_id,
            "model_name_or_path": ORACLE_CONTROL_MODEL_NAME,
            "instance_ids": tuple(sorted(gold_patches)),
        }
    )
    oracle_runner = OfficialHarnessRunner(
        oracle_config,
        runner=runner,
        platform=platform,
        artifacts=artifacts,
        image_ids=image_ids,
    )
    result = oracle_runner.run()
    return result.model_copy(
        update={
            "control_kind": HarnessControlKind.ORACLE_CONTROL,
            "included_in_resolved_rate": False,
        }
    )


def compare_with_internal_m2(
    result: HarnessResult | HarnessExecutionResult, *, m2_verified: bool | None
) -> HarnessDiscrepancy:
    """比较官方结论与内部 M2 状态，**只分类、不修改官方结论**。

    ``m2_verified=True`` 的语义是"内部复现测试/回归/review 都没发现新失败"，它是
    局部证据；官方 SWE-bench 判定才是 benchmark 口径。两者不一致时以官方结论为
    ``official_resolved``，这里只是把差异显式记下来（避免报告里把内部验证写成
    "已解决"，也避免把官方 unresolved 当成内部失败而误判 Agent 能力）。
    """
    resolved: bool | None
    if isinstance(result, HarnessExecutionResult):
        if len(result.verdicts) != 1:
            raise ValueError("compare_with_internal_m2 只接受单实例结果")
        resolved = result.verdicts[0].resolved
    else:
        resolved = result.resolved
    if resolved is None:
        return HarnessDiscrepancy.OFFICIAL_NOT_EVALUATED
    if m2_verified is None:
        return HarnessDiscrepancy.INTERNAL_UNKNOWN
    if resolved is False and m2_verified:
        return HarnessDiscrepancy.OFFICIAL_DENIED_INTERNAL_VERIFIED
    if resolved is True and not m2_verified:
        return HarnessDiscrepancy.OFFICIAL_RESOLVED_INTERNAL_NOT_VERIFIED
    return HarnessDiscrepancy.NONE


# ---------------------------------------------------------------------------
# 官方 runner
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _CacheDecision:
    """缓存检查结论：要么可复用报告，要么带原因继续执行。"""

    report: HarnessReport | None
    reason: str
    rebuilt: bool = False
    quarantined: Path | None = None


@dataclass(frozen=True)
class _Prediction:
    """已校验的官方 prediction 行（内部类型，对应三字段契约）。"""

    instance_id: str
    model_name_or_path: str
    model_patch: str


class _PredictionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    model_name_or_path: str
    model_patch: str


class OfficialHarnessRunner:
    """调用锁定的官方 harness 并解析/缓存其报告。

    典型用法::

        config = HarnessConfig(
            predictions_path=run_dir / "predictions.jsonl",
            run_id=derive_run_id(patch_hashes, seed="20261019"),
            report_dir=run_dir / "official-eval",
            model_name_or_path="codepilot/glm-4.5",
            dataset_snapshot=snapshot_path,
        )
        runner = OfficialHarnessRunner(config, artifacts=artifact_store)
        command = runner.build_command(patch_hashes=patch_hashes)  # dry-run 只到这一步
        result = runner.run()                                      # 真执行/读缓存
    """

    def __init__(
        self,
        config: HarnessConfig,
        *,
        runner: SubprocessRunner | None = None,
        platform: PlatformInfo | None = None,
        artifacts: ArtifactStore | None = None,
        dataset_instances: Collection[str] | None = None,
        image_ids: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._runner = runner if runner is not None else LocalSubprocessRunner()
        self._platform = platform if platform is not None else PlatformInfo.detect()
        self._artifacts = artifacts
        if dataset_instances is not None:
            self._dataset_instances: frozenset[str] | None = frozenset(dataset_instances)
        else:
            self._dataset_instances = self._load_snapshot_instances()
        # 镜像身份由调用方注入。**建议来源是 Owner C 的
        # ``SWEBenchEnvironmentIdentity``**（取 instance_image_key/env_image_key/
        # base_image_key，映射成 {instance_id: 合并后的镜像身份字符串}）：官方镜像
        # 能被重建/换 tag，patch+revision+harness_version 相同也不代表评测环境相同。
        # 不注入时记空元组——复用仍要求"双方都为空"，但无法证明镜像身份。
        self._image_ids: tuple[tuple[str, str], ...] = (
            tuple(sorted(image_ids.items())) if image_ids else ()
        )

    # -- 只读属性，便于 smoke/runner 层取证据 --------------------------------
    @property
    def config(self) -> HarnessConfig:
        return self._config

    @property
    def platform(self) -> PlatformInfo:
        return self._platform

    # -- 对外主流程 ----------------------------------------------------------
    def build_command(self, *, patch_hashes: Mapping[str, str]) -> HarnessCommand:
        """构造官方命令；同时做全部启动前校验（不执行、不写文件、不 import swebench）。

        参数 ``patch_hashes`` 的 key 是 ``instance_id``，value 是 ``model_patch`` 的
        sha256。它必须与 predictions 文件内容逐条一致——这样缓存身份里的
        ``patch_hash`` 才有资格代表"官方实际会评测的那份补丁"。空补丁不参与
        ``patch_hash``（官方在评测前就把它们过滤掉、只在报告里记 empty_patch），
        预测集合本身的完整性由报告 ``submitted_ids`` 校验兜住。
        """
        predictions = self._load_predictions()
        self._validate_membership(item.instance_id for item in predictions)
        self._validate_patch_hashes(predictions, patch_hashes)
        return self._assemble_command()

    def preview_command(self) -> HarnessCommand:
        """dry-run 专用：不读 predictions，只按 config 拼命令。

        存在的理由：E-17 smoke 的 ``--dry-run`` 要在**还没有 predictions 文件**时
        验证装配与命令拼装；用 :meth:`build_command` 会被"predictions 不存在"挡下。
        这个方法不放松任何真实执行的校验——``run()`` 永远走 build_command。
        """
        return self._assemble_command()

    def _assemble_command(self) -> HarnessCommand:
        python, _ = self._resolve_harness_python()
        dataset_argument = self._dataset_argument()
        report_dir = self._config.report_dir.resolve()
        argv: list[str] = [
            str(python),
            "-m",
            RUN_EVALUATION_MODULE,
            "-d",
            dataset_argument,
            "-s",
            self._config.split,
        ]
        if self._config.instance_ids:
            argv.extend(["-i", *self._config.instance_ids])
        argv.extend(
            [
                "-p",
                str(self._config.predictions_path.resolve()),
                "-id",
                self._config.run_id,
                "--max_workers",
                str(self._config.max_workers),
                "-t",
                str(self._config.timeout_seconds),
                "--report_dir",
                str(report_dir),
                # 显式传官方默认值，而不是"什么都不传、依赖官方默认"：这两个参数决定
                # 用的是远端预构建镜像还是本地构建镜像、以及跑完是否删除实例镜像，
                # 都必须出现在记录下来的命令里，镜像身份才经得起复核。
                "-n",
                self._config.namespace or "none",
                "--cache_level",
                self._config.cache_level,
            ]
        )
        if self._platform.use_wsl and self._platform.is_windows:
            # 契约 §7：WSL 通过 wsl.exe -e ... 执行。路径必须已是 WSL 可见路径，
            # 由调用方在 harness_python/predictions/report_dir 上保证。
            if self._platform.wsl_executable is None:
                raise HarnessPlatformError(
                    HarnessErrorKind.ENV_ERROR, "use_wsl=True 但找不到 wsl.exe"
                )
            argv = [str(self._platform.wsl_executable), "-e", *argv]
        return HarnessCommand(
            argv=tuple(argv),
            cwd=report_dir,
            env=_injected_env(),
            expected_report=report_dir / self._config.expected_report_name,
        )

    def run(self, *, dry_run: bool = False) -> HarnessExecutionResult:
        """执行官方 harness（或复用已验证缓存）。``dry_run=True`` 只构造命令不执行。"""
        predictions = self._load_predictions()
        patch_hashes = {
            item.instance_id: _sha256_text(item.model_patch)
            for item in predictions
            if item.model_patch != ""
        }
        command = self.build_command(patch_hashes=patch_hashes)
        identity = self._build_identity(patch_hashes)
        warnings = self._build_warnings(patch_hashes)
        python, python_warning = self._resolve_harness_python()
        if python_warning is not None:
            warnings = (*warnings, python_warning)

        if dry_run:
            verdicts = self._fallback_verdicts(
                predictions,
                command=command,
                identity=identity,
                verdict=OfficialVerdict.NOT_EVALUATED,
                error_kind=None,
                error_detail="dry-run：只构造命令，未执行官方 harness",
                runtime_seconds=0.0,
            )
            return self._execution_result(
                command=command,
                identity=identity,
                decision=HarnessRunDecision.DRY_RUN,
                exit_code=None,
                timed_out=False,
                runtime_seconds=0.0,
                report=None,
                verdicts=verdicts,
                error_kind=None,
                error_detail="dry-run",
                reused=False,
                reused_reason=None,
                warnings=warnings,
            )

        self._enforce_run_id_digest(identity)
        cache = self._check_cache(identity, predictions)
        if cache.report is not None:
            verdicts = self._verdicts_from_report(
                cache.report,
                predictions=predictions,
                command=command,
                identity=identity,
                runtime_seconds=0.0,
            )
            result = self._execution_result(
                command=command,
                identity=identity,
                decision=HarnessRunDecision.REUSED,
                exit_code=None,
                timed_out=False,
                runtime_seconds=0.0,
                report=cache.report,
                verdicts=verdicts,
                error_kind=None,
                error_detail=None,
                reused=True,
                reused_reason=cache.reason,
                warnings=warnings,
            )
            return result.model_copy(
                update={
                    "evidence_dir": self._save_evidence(
                        result=result, identity=identity, stdout="", stderr=""
                    )
                }
            )

        decision = HarnessRunDecision.REBUILT if cache.rebuilt else HarnessRunDecision.EXECUTED
        if not self._platform.official_execution_supported:
            detail = self._platform_error_detail()
            verdicts = self._fallback_verdicts(
                predictions,
                command=command,
                identity=identity,
                verdict=OfficialVerdict.NOT_EVALUATED,
                error_kind=HarnessErrorKind.ENV_ERROR,
                error_detail=detail,
                runtime_seconds=0.0,
            )
            result = self._execution_result(
                command=command,
                identity=identity,
                decision=HarnessRunDecision.PLATFORM_BLOCKED,
                exit_code=None,
                timed_out=False,
                runtime_seconds=0.0,
                report=None,
                verdicts=verdicts,
                error_kind=HarnessErrorKind.ENV_ERROR,
                error_detail=detail,
                reused=False,
                reused_reason=cache.reason,
                warnings=(*warnings, self._platform.describe()),
            )
            return result.model_copy(
                update={
                    "evidence_dir": self._save_evidence(
                        result=result, identity=identity, stdout="", stderr=""
                    )
                }
            )

        if not python.is_file():
            detail = f"找不到官方 harness 解释器: {python}"
            verdicts = self._fallback_verdicts(
                predictions,
                command=command,
                identity=identity,
                verdict=OfficialVerdict.NOT_EVALUATED,
                error_kind=HarnessErrorKind.RESOURCE_BLOCKED,
                error_detail=detail,
                runtime_seconds=0.0,
            )
            result = self._execution_result(
                command=command,
                identity=identity,
                decision=HarnessRunDecision.PLATFORM_BLOCKED,
                exit_code=None,
                timed_out=False,
                runtime_seconds=0.0,
                report=None,
                verdicts=verdicts,
                error_kind=HarnessErrorKind.RESOURCE_BLOCKED,
                error_detail=detail,
                reused=False,
                reused_reason=cache.reason,
                warnings=warnings,
            )
            return result.model_copy(
                update={
                    "evidence_dir": self._save_evidence(
                        result=result, identity=identity, stdout="", stderr=""
                    )
                }
            )

        command.cwd.mkdir(parents=True, exist_ok=True)
        outcome = self._runner.run(
            command.argv,
            cwd=command.cwd,
            env=command.env,
            timeout_seconds=self._config.timeout_seconds,
        )
        report, report_error_kind, report_error_detail, _ = self._collect_report(command)
        error_kind, error_detail = self._classify_execution(
            outcome, report, report_error_kind, report_error_detail
        )
        if outcome.timed_out or outcome.returncode != 0:
            # 官方只在正常结束后写汇总报告（make_run_report 是 main 的最后一步）。
            # 异常退出时目录里即使有报告，也绝不能当成本次结果。
            report = None
        if report is not None:
            mismatch = self._verify_report_matches_inputs(report, predictions)
            if mismatch is not None:
                error_kind = HarnessErrorKind.REPORT_IDENTITY_MISMATCH
                error_detail = mismatch
                report = None
        if report is not None:
            verdicts = self._verdicts_from_report(
                report,
                predictions=predictions,
                command=command,
                identity=identity,
                runtime_seconds=outcome.duration_seconds,
            )
        else:
            verdicts = self._fallback_verdicts(
                predictions,
                command=command,
                identity=identity,
                verdict=OfficialVerdict.MISSING,
                error_kind=error_kind,
                error_detail=error_detail,
                runtime_seconds=outcome.duration_seconds,
            )
        result = self._execution_result(
            command=command,
            identity=identity,
            decision=decision,
            exit_code=outcome.returncode,
            timed_out=outcome.timed_out,
            runtime_seconds=outcome.duration_seconds,
            report=report,
            verdicts=verdicts,
            error_kind=error_kind,
            error_detail=error_detail,
            reused=False,
            reused_reason=cache.reason,
            warnings=warnings,
            stdout_tail=_tail(outcome.stdout),
            stderr_tail=_tail(outcome.stderr),
        )
        if (
            report is not None
            and outcome.returncode == 0
            and not outcome.timed_out
            and error_kind is None
        ):
            # 只有"官方正常结束 + 报告通过身份校验"才写身份 sidecar；否则下次
            # 不允许被当成可复用缓存。
            self._write_identity(identity, command.identity_sidecar)
        return result.model_copy(
            update={
                "evidence_dir": self._save_evidence(
                    result=result, identity=identity, stdout=outcome.stdout, stderr=outcome.stderr
                )
            }
        )

    @staticmethod
    def parse_report_file(path: Path) -> HarnessReport:
        """解析 schema_version=2 官方报告；损坏/缺字段抛 ``REPORT_CORRUPTED``。"""
        return _parse_report_file(path)

    @staticmethod
    def load_saved_result(store: ArtifactStore) -> HarnessExecutionResult | None:
        """从 ArtifactStore 恢复一次结果（升级/重放证据用）。"""
        relpath = f"{_ARTIFACT_DIRNAME}/result.json"
        if not store.exists(relpath):
            return None
        try:
            return HarnessExecutionResult.model_validate(store.read_json(relpath))
        except (OSError, ValueError) as error:
            raise HarnessError(
                HarnessErrorKind.INTERNAL_INCONSISTENCY, f"artifact 结果无法恢复: {error}"
            ) from error

    # -- predictions / 归属校验 ---------------------------------------------
    def _load_predictions(self) -> tuple[_Prediction, ...]:
        path = self._config.predictions_path
        if not path.is_file():
            raise HarnessInputError(
                HarnessErrorKind.PREDICTIONS_MISSING, f"predictions 文件不存在: {path}"
            )
        try:
            raw_bytes = path.read_bytes()
        except OSError as error:
            raise HarnessInputError(
                HarnessErrorKind.PREDICTIONS_MISSING, f"读不到 predictions: {error}"
            ) from error
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HarnessInputError(
                HarnessErrorKind.PREDICTIONS_INVALID, f"predictions 不是 UTF-8: {error}"
            ) from error
        if path.suffix.lower() == ".jsonl":
            rows: list[object] = []
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if not line.strip():
                    # 官方 `[json.loads(line) for line in f]` 遇到空行会抛；这里显式
                    # 报错而不是静默跳过（空行说明写出方有 bug）。
                    raise HarnessInputError(
                        HarnessErrorKind.PREDICTIONS_INVALID, f"predictions 第 {index + 1} 行为空"
                    )
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise HarnessInputError(
                        HarnessErrorKind.PREDICTIONS_INVALID,
                        f"predictions 第 {index + 1} 行不是合法 JSON: {error}",
                    ) from error
        else:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as error:
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID, f"predictions 不是合法 JSON: {error}"
                ) from error
            if isinstance(payload, dict):
                # 官方 4.1.0 兼容 SWE-agent 的 {instance_id: prediction} 形态。
                rows = list(payload.values())
            elif isinstance(payload, list):
                rows = payload
            else:
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID,
                    "predictions 必须是 list[record] 或 dict[instance_id, record]",
                )
        if not rows:
            raise HarnessInputError(
                HarnessErrorKind.PREDICTIONS_INVALID,
                "predictions 为空：官方 make_run_report 会 IndexError，必须在评测前拒绝",
            )
        predictions: list[_Prediction] = []
        seen: set[str] = set()
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID, f"第 {index} 条 prediction 不是对象"
                )
            extra_keys = set(row) - set(PREDICTION_KEYS)
            missing_keys = set(PREDICTION_KEYS) - set(row)
            if extra_keys or missing_keys:
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID,
                    f"第 {index} 条 prediction 字段必须是 {PREDICTION_KEYS}，"
                    f"多 {sorted(extra_keys)} 少 {sorted(missing_keys)}",
                )
            try:
                model = _PredictionModel.model_validate(row)
            except ValidationError as error:
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID, f"第 {index} 条 prediction 非法: {error}"
                ) from error
            if INSTANCE_ID_PATTERN.fullmatch(model.instance_id) is None:
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID,
                    f"第 {index} 条 prediction 的 instance_id 非法: {model.instance_id!r}",
                )
            if model.model_name_or_path != self._config.model_name_or_path:
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID,
                    "prediction 的 model_name_or_path 与配置不一致："
                    f"{model.model_name_or_path!r} != {self._config.model_name_or_path!r}"
                    "（官方按该字段决定报告文件名，混写会让报告无法归属）",
                )
            if model.instance_id in seen:
                raise HarnessInputError(
                    HarnessErrorKind.PREDICTIONS_INVALID,
                    f"predictions 重复 instance_id: {model.instance_id!r}",
                )
            seen.add(model.instance_id)
            predictions.append(
                _Prediction(
                    instance_id=model.instance_id,
                    model_name_or_path=model.model_name_or_path,
                    model_patch=model.model_patch,
                )
            )
        return tuple(predictions)

    def _validate_membership(self, instance_ids: Iterable[str]) -> None:
        ids = set(instance_ids)
        if self._config.instance_ids:
            requested = set(self._config.instance_ids)
            extra = ids - requested
            if extra:
                raise HarnessInputError(
                    HarnessErrorKind.DATASET_MISMATCH,
                    f"predictions 含未请求的 instance_id: {sorted(extra)}",
                )
            if self._dataset_instances is not None:
                missing = requested - self._dataset_instances
                if missing:
                    raise HarnessInputError(
                        HarnessErrorKind.DATASET_MISMATCH,
                        f"请求的 instance_id 不属于当前数据集: {sorted(missing)}",
                    )
        if self._dataset_instances is not None:
            outside = ids - self._dataset_instances
            if outside:
                raise HarnessInputError(
                    HarnessErrorKind.DATASET_MISMATCH,
                    "predictions 含不属于当前数据集/revision 的 instance_id: "
                    f"{sorted(outside)}（声明 revision={self._config.dataset_revision}）",
                )

    def _load_snapshot_instances(self) -> frozenset[str] | None:
        snapshot = self._config.dataset_snapshot
        if snapshot is None:
            return None
        if not snapshot.is_file():
            raise HarnessInputError(
                HarnessErrorKind.DATASET_MISMATCH, f"dataset_snapshot 不存在: {snapshot}"
            )
        self._validate_snapshot_manifest(snapshot)
        instances = _read_dataset_instance_ids(snapshot)
        if not instances:
            raise HarnessInputError(
                HarnessErrorKind.DATASET_MISMATCH, f"dataset_snapshot 里没有实例: {snapshot}"
            )
        return frozenset(instances)

    def _validate_snapshot_manifest(self, snapshot: Path) -> None:
        """快照目录里的 manifest.json 是 revision/name/split 的权威声明。

        没有 manifest 就跳过（允许手写测试快照），有就必须一致——否则会出现
        "命令用 A revision 的快照，缓存身份写 B revision"的静默错配。
        """
        if snapshot.stem != self._config.split:
            raise HarnessInputError(
                HarnessErrorKind.DATASET_MISMATCH,
                f"dataset_snapshot 文件名 {snapshot.name!r} 与 split {self._config.split!r} 不一致",
            )
        manifest_path = snapshot.parent / "manifest.json"
        if not manifest_path.is_file():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HarnessInputError(
                HarnessErrorKind.DATASET_MISMATCH, f"manifest.json 无法解析: {error}"
            ) from error
        if not isinstance(manifest, dict):
            raise HarnessInputError(HarnessErrorKind.DATASET_MISMATCH, "manifest.json 顶层不是对象")
        # manifest 的 key 是官方式命名（``revision``），配置字段是 ``dataset_revision``；
        # 显式映射，别再靠 getattr 猜字段名（E-17 smoke dry-run 抓到的真实 AttributeError）。
        expected = {
            "dataset_name": self._config.dataset_name,
            "revision": self._config.dataset_revision,
            "split": self._config.split,
        }
        for key, value in expected.items():
            if key in manifest and manifest[key] != value:
                raise HarnessInputError(
                    HarnessErrorKind.DATASET_MISMATCH,
                    f"dataset_snapshot 的 {key}={manifest[key]!r} 与配置 {value!r} 不一致",
                )

    def _dataset_argument(self) -> str:
        snapshot = self._config.dataset_snapshot
        if snapshot is None:
            return self._config.dataset_name
        resolved = snapshot.resolve()
        self._validate_snapshot_manifest(resolved)
        return str(resolved)

    def _validate_patch_hashes(
        self,
        predictions: Sequence[_Prediction],
        patch_hashes: Mapping[str, str],
    ) -> None:
        expected = {
            item.instance_id: _sha256_text(item.model_patch)
            for item in predictions
            if item.model_patch != ""
        }
        if dict(patch_hashes) != expected:
            missing = sorted(set(expected) - set(patch_hashes))
            extra = sorted(set(patch_hashes) - set(expected))
            changed = sorted(
                instance_id
                for instance_id in set(expected) & set(patch_hashes)
                if expected[instance_id] != patch_hashes[instance_id]
            )
            raise HarnessInputError(
                HarnessErrorKind.PATCH_HASH_MISMATCH,
                f"patch_hashes 与 predictions 内容不一致：缺 {missing} 多 {extra} 值不同 {changed}",
            )

    def _resolve_harness_python(self) -> tuple[Path, str | None]:
        if self._config.harness_python is not None:
            return self._config.harness_python, None
        default = default_harness_python()
        if default is not None:
            return default, None
        warning = (
            "找不到 .venv-swebench 解释器，回退到当前解释器；原生 Windows 上官方 "
            "harness 仍会 ENV_ERROR（需要 WSL2/Linux）"
        )
        return Path(sys.executable), warning

    # -- 缓存 ----------------------------------------------------------------
    def _build_identity(self, patch_hashes: Mapping[str, str]) -> HarnessCacheIdentity:
        return HarnessCacheIdentity(
            run_id=self._config.run_id,
            patch_hashes=tuple(sorted(patch_hashes.items())),
            model_name_or_path=self._config.model_name_or_path,
            dataset_name=self._config.dataset_name,
            dataset_revision=self._config.dataset_revision,
            split=self._config.split,
            harness_version=self._config.harness_version,
            namespace=self._config.namespace,
            image_ids=self._image_ids,
        )

    def _build_warnings(self, patch_hashes: Mapping[str, str]) -> tuple[str, ...]:
        warnings: list[str] = []
        if self._config.dataset_snapshot is None:
            warnings.append(
                "未提供 dataset_snapshot：官方 CLI 4.1.0 没有 revision 参数，本次 -d "
                "使用数据集名，HF 端 revision 漂移无法被命令层面固定"
            )
        if not patch_hashes:
            warnings.append("predictions 全部为空补丁：官方 harness 会跳过评测，只记录 empty_patch")
        if self._dataset_instances is None:
            warnings.append(
                "未提供 dataset_snapshot/dataset_instances：实例归属只能靠官方子进程再次校验"
            )
        return tuple(warnings)

    def _enforce_run_id_digest(self, identity: HarnessCacheIdentity) -> None:
        """run_id 若符合契约形态，其摘要必须等于当前补丁集合摘要。

        这条负控制解决"拿到旧 run_id 直接复用、换了补丁却没换 run_id"的污染：
        官方 ``run_instance`` 见到 ``report.json`` 已存在会直接返回旧结论。
        """
        match = RUN_ID_PATTERN.fullmatch(self._config.run_id)
        if match is None:
            return
        expected = identity.patch_set_sha256[:12]
        actual = self._config.run_id.split("-")[1]
        if actual != expected:
            raise HarnessCacheError(
                HarnessErrorKind.CACHE_IDENTITY_MISMATCH,
                f"run_id 摘要 {actual!r} 与当前补丁集合摘要 {expected!r} 不一致；"
                "不同补丁必须使用 derive_run_id() 生成新 run_id",
            )
        if not match.group("seed"):
            raise HarnessCacheError(HarnessErrorKind.CACHE_IDENTITY_MISMATCH, "run_id 缺少 seed 段")

    def _check_cache(
        self, identity: HarnessCacheIdentity, predictions: Sequence[_Prediction]
    ) -> _CacheDecision:
        report_path = self._config.report_dir.resolve() / self._config.expected_report_name
        sidecar = report_path.with_name(report_path.name + _IDENTITY_SUFFIX)
        if not report_path.exists():
            if sidecar.exists():
                recorded = self._read_identity(sidecar)
                reasons = identity.mismatches(recorded)
                if reasons:
                    raise HarnessCacheError(
                        HarnessErrorKind.CACHE_IDENTITY_MISMATCH,
                        "存在旧身份 sidecar 但报告已缺失，且身份不符（"
                        + "; ".join(reasons)
                        + "）；请使用新 run_id",
                    )
            return _CacheDecision(report=None, reason="报告不存在，执行官方 harness")
        if not sidecar.exists():
            raise HarnessCacheError(
                HarnessErrorKind.CACHE_UNVERIFIED,
                f"报告 {report_path.name} 存在但身份 sidecar 缺失：无法证明它对应哪份补丁；"
                "拒绝复用。请换新 run_id 或删除旧报告",
            )
        recorded = self._read_identity(sidecar)
        reasons = identity.mismatches(recorded)
        if reasons:
            raise HarnessCacheError(
                HarnessErrorKind.CACHE_IDENTITY_MISMATCH,
                "缓存身份不符，拒绝复用（" + "; ".join(reasons) + "）；请使用新 run_id",
            )
        try:
            report = self.parse_report_file(report_path)
        except HarnessError as error:
            quarantined = self._quarantine_report(report_path)
            return _CacheDecision(
                report=None,
                reason=f"缓存报告损坏（{error.kind.value}），已隔离 {quarantined.name}，重跑重建",
                rebuilt=True,
                quarantined=quarantined,
            )
        mismatch = self._verify_report_matches_inputs(report, predictions)
        if mismatch is not None:
            raise HarnessCacheError(
                HarnessErrorKind.CACHE_IDENTITY_MISMATCH,
                f"缓存报告内容与当前输入不一致，拒绝复用：{mismatch}；请使用新 run_id",
            )
        return _CacheDecision(report=report, reason="缓存身份一致，复用官方报告")

    def _read_identity(self, path: Path) -> HarnessCacheIdentity:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return HarnessCacheIdentity.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise HarnessCacheError(
                HarnessErrorKind.CACHE_UNVERIFIED,
                f"身份 sidecar 无法解析 {path}: {error}；拒绝复用，请换新 run_id",
            ) from error

    def _write_identity(self, identity: HarnessCacheIdentity, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(identity.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _quarantine_report(path: Path) -> Path:
        target = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
        try:
            path.replace(target)
        except OSError as error:
            raise HarnessError(
                HarnessErrorKind.INTERNAL_INCONSISTENCY, f"无法隔离损坏报告 {path}: {error}"
            ) from error
        return target

    def _verify_report_matches_inputs(
        self, report: HarnessReport, predictions: Sequence[_Prediction]
    ) -> str | None:
        """报告能否对应本次输入。返回差异说明，``None`` 表示一致。"""
        expected_submitted = {item.instance_id for item in predictions}
        actual_submitted = set(report.submitted_ids)
        if actual_submitted != expected_submitted:
            return (
                "报告 submitted_ids 与当前 predictions 不一致："
                f"缺 {sorted(expected_submitted - actual_submitted)} "
                f"多 {sorted(actual_submitted - expected_submitted)}"
            )
        expected_total = self._expected_total_instances()
        if expected_total is not None and report.total_instances != expected_total:
            return (
                f"报告 total_instances={report.total_instances} 与当前数据集/revision "
                f"期望 {expected_total} 不一致（数据集或 revision 变了）"
            )
        return None

    def _expected_total_instances(self) -> int | None:
        if self._config.instance_ids:
            return len(self._config.instance_ids)
        if self._dataset_instances is not None:
            return len(self._dataset_instances)
        return None

    # -- 报告收集 / 执行归因 ------------------------------------------------
    def _collect_report(
        self, command: HarnessCommand
    ) -> tuple[HarnessReport | None, HarnessErrorKind | None, str | None, str | None]:
        path = command.expected_report
        if not path.is_file():
            return None, HarnessErrorKind.REPORT_MISSING, f"官方汇总报告不存在: {path}", None
        try:
            report = self.parse_report_file(path)
        except HarnessError as error:
            # 损坏报告保留为证据，但绝不当成有效结果。
            return None, error.kind, error.detail, None
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return report, None, None, sha

    def _classify_execution(
        self,
        outcome: SubprocessOutcome,
        report: HarnessReport | None,
        report_error_kind: HarnessErrorKind | None,
        report_error_detail: str | None,
    ) -> tuple[HarnessErrorKind | None, str | None]:
        if outcome.timed_out:
            return (
                HarnessErrorKind.TIMEOUT,
                f"官方 harness 超过 {self._config.timeout_seconds}s 被终止；"
                f"stderr 尾部: {_tail(outcome.stderr)}",
            )
        if outcome.returncode != 0:
            combined = f"{outcome.stdout}\n{outcome.stderr}".lower()
            if any(marker in combined for marker in _DOCKER_UNAVAILABLE_MARKERS):
                return (
                    HarnessErrorKind.DOCKER_UNAVAILABLE,
                    f"官方 harness 退出码 {outcome.returncode} 且输出显示 Docker 不可用；"
                    f"stderr 尾部: {_tail(outcome.stderr)}",
                )
            return (
                HarnessErrorKind.PROCESS_FAILED,
                f"官方 harness 退出码 {outcome.returncode}；stderr 尾部: {_tail(outcome.stderr)}",
            )
        if report is None:
            return report_error_kind, report_error_detail
        return None, None

    def _platform_error_detail(self) -> str:
        if self._platform.is_windows:
            if not self._platform.has_resource_module:
                return (
                    "原生 Windows 无法执行官方 swebench 4.1.0："
                    "swebench.harness.prepare_images 顶层 import resource（Unix-only），"
                    "且官方评测依赖 Linux 容器环境。需要 WSL2/ Linux（platform.use_wsl=True）。"
                )
            return "Windows 上官方 harness 需要经 WSL2 执行（use_wsl=True 且 wsl.exe 可用）"
        return (
            f"官方 swebench 4.1.0 harness 仅支持 Linux/WSL2，当前平台 {self._platform.system!r} "
            "不在支持范围"
        )

    # -- 结论构造 ------------------------------------------------------------
    def _ordered_instance_ids(self, predictions: Sequence[_Prediction]) -> tuple[str, ...]:
        requested = list(self._config.instance_ids)
        submitted = [item.instance_id for item in predictions]
        extras = sorted(set(submitted) - set(requested))
        return tuple(requested + extras)

    def _fallback_verdicts(
        self,
        predictions: Sequence[_Prediction],
        *,
        command: HarnessCommand,
        identity: HarnessCacheIdentity,
        verdict: OfficialVerdict,
        error_kind: HarnessErrorKind | None,
        error_detail: str | None,
        runtime_seconds: float,
    ) -> tuple[HarnessResult, ...]:
        patch_by_id = {item.instance_id: item.model_patch for item in predictions}
        results: list[HarnessResult] = []
        for instance_id in self._ordered_instance_ids(predictions):
            item_detail: str | None
            if instance_id not in patch_by_id:
                item_verdict = OfficialVerdict.NOT_EVALUATED
                item_kind = error_kind
                item_detail = "该实例在 instance_ids 里但 predictions 未提交"
            elif patch_by_id[instance_id] == "":
                item_verdict = OfficialVerdict.EMPTY_PATCH
                item_kind = None
                item_detail = "model_patch 为空，官方 harness 跳过评测"
            else:
                item_verdict = verdict
                item_kind = error_kind
                item_detail = error_detail
            results.append(
                self._make_result(
                    instance_id=instance_id,
                    verdict=item_verdict,
                    command=command,
                    identity=identity,
                    report_path=None,
                    report_sha256=None,
                    error_kind=item_kind,
                    error_detail=item_detail,
                    runtime_seconds=runtime_seconds,
                )
            )
        return tuple(results)

    def _verdicts_from_report(
        self,
        report: HarnessReport,
        *,
        predictions: Sequence[_Prediction],
        command: HarnessCommand,
        identity: HarnessCacheIdentity,
        runtime_seconds: float,
    ) -> tuple[HarnessResult, ...]:
        results: list[HarnessResult] = []
        for instance_id in self._ordered_instance_ids(predictions):
            verdict = report.verdict_for(instance_id)
            error_kind: HarnessErrorKind | None = None
            error_detail: str | None = None
            if verdict is OfficialVerdict.UNRESOLVED:
                # 汇总报告不区分"补丁打不上"和"测试没过"；单实例报告能区分。
                applied = self._per_instance_patch_applied(command, instance_id)
                if applied is False:
                    verdict = OfficialVerdict.PATCH_APPLY_FAILED
                    error_detail = "官方单实例报告 patch_successfully_applied=false"
            if verdict is OfficialVerdict.ERROR:
                error_kind = HarnessErrorKind.INSTANCE_ERROR
                error_detail = "官方报告把该实例列入 error_ids（见 logs/run_evaluation 证据）"
            if verdict is OfficialVerdict.EMPTY_PATCH:
                error_detail = "model_patch 为空，官方 harness 跳过评测"
            if verdict is OfficialVerdict.MISSING:
                error_kind = HarnessErrorKind.REPORT_MISSING
                error_detail = "官方报告列入 submitted_ids 但既无完成记录也无错误归类"
            if verdict is OfficialVerdict.NOT_EVALUATED:
                error_detail = "官方报告列入 incomplete_ids（未提交预测）"
            results.append(
                self._make_result(
                    instance_id=instance_id,
                    verdict=verdict,
                    command=command,
                    identity=identity,
                    report_path=report.source_path,
                    report_sha256=report.source_sha256,
                    error_kind=error_kind,
                    error_detail=error_detail,
                    runtime_seconds=runtime_seconds,
                )
            )
        return tuple(results)

    def _per_instance_patch_applied(self, command: HarnessCommand, instance_id: str) -> bool | None:
        path = command.per_instance_report(
            instance_id, self._config.model_slug, self._config.run_id
        )
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        entry = payload.get(instance_id)
        if not isinstance(entry, dict):
            return None
        applied = entry.get("patch_successfully_applied")
        if isinstance(applied, bool):
            return applied
        return None

    def _make_result(
        self,
        *,
        instance_id: str,
        verdict: OfficialVerdict,
        command: HarnessCommand,
        identity: HarnessCacheIdentity,
        report_path: Path | None,
        report_sha256: str | None,
        error_kind: HarnessErrorKind | None,
        error_detail: str | None,
        runtime_seconds: float,
    ) -> HarnessResult:
        return HarnessResult(
            verdict=verdict,
            instance_id=instance_id,
            resolved=verdict.resolved_value,
            report_path=report_path,
            report_sha256=report_sha256,
            error_kind=error_kind,
            error_detail=error_detail,
            runtime_seconds=runtime_seconds,
            command=command,
            cache_identity=identity,
        )

    def _execution_result(
        self,
        *,
        command: HarnessCommand,
        identity: HarnessCacheIdentity,
        decision: HarnessRunDecision,
        exit_code: int | None,
        timed_out: bool,
        runtime_seconds: float,
        report: HarnessReport | None,
        verdicts: tuple[HarnessResult, ...],
        error_kind: HarnessErrorKind | None,
        error_detail: str | None,
        reused: bool,
        reused_reason: str | None,
        warnings: tuple[str, ...],
        stdout_tail: str = "",
        stderr_tail: str = "",
    ) -> HarnessExecutionResult:
        return HarnessExecutionResult(
            run_id=self._config.run_id,
            command=command,
            decision=decision,
            exit_code=exit_code,
            timed_out=timed_out,
            runtime_seconds=runtime_seconds,
            report=report,
            report_path=report.source_path if report is not None else None,
            report_sha256=report.source_sha256 if report is not None else None,
            verdicts=verdicts,
            error_kind=error_kind,
            error_detail=error_detail,
            reused=reused,
            reused_reason=reused_reason,
            warnings=warnings,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            platform=self._platform,
            cache_identity=identity,
        )

    # -- 证据落盘 ------------------------------------------------------------
    def _save_evidence(
        self,
        *,
        result: HarnessExecutionResult,
        identity: HarnessCacheIdentity,
        stdout: str,
        stderr: str,
    ) -> Path | None:
        if self._artifacts is not None:
            store = self._artifacts
            evidence_dir = store.path_for(f"{_ARTIFACT_DIRNAME}/result.json").parent
            store.write_json(
                f"{_ARTIFACT_DIRNAME}/command.json", result.command.model_dump(mode="json")
            )
            store.write_json(f"{_ARTIFACT_DIRNAME}/identity.json", identity.model_dump(mode="json"))
            store.write_text(f"{_ARTIFACT_DIRNAME}/stdout.log", stdout)
            store.write_text(f"{_ARTIFACT_DIRNAME}/stderr.log", stderr)
            if result.report_path is not None and result.report_path.is_file():
                store.write_bytes(
                    f"{_ARTIFACT_DIRNAME}/report.json", result.report_path.read_bytes()
                )
            # result.json 里记下 evidence_dir，使 load_saved_result 恢复出的对象与
            # 内存中的结果完全一致（包括该字段）。
            final = result.model_copy(update={"evidence_dir": evidence_dir})
            store.write_json(f"{_ARTIFACT_DIRNAME}/result.json", final.model_dump(mode="json"))
            return evidence_dir
        evidence_dir = self._config.report_dir.resolve() / _EVIDENCE_DIRNAME
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "command.json").write_text(
            json.dumps(result.command.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (evidence_dir / "identity.json").write_text(
            json.dumps(identity.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (evidence_dir / "stdout.log").write_text(stdout, encoding="utf-8", newline="\n")
        (evidence_dir / "stderr.log").write_text(stderr, encoding="utf-8", newline="\n")
        if result.report_path is not None and result.report_path.is_file():
            (evidence_dir / "report.json").write_bytes(result.report_path.read_bytes())
        final = result.model_copy(update={"evidence_dir": evidence_dir})
        (evidence_dir / "result.json").write_text(
            json.dumps(final.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return evidence_dir


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _injected_env() -> dict[str, str]:
    """只注入让子进程稳定使用 UTF-8 的变量。

    刻意不把 ``os.environ`` 整个塞进 ``HarnessCommand``：那会把 API key 之类写进
    command.json/artifact。真实执行器负责合并父进程环境。
    """
    return {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
    }


def _tail(text: str, limit: int = _OUTPUT_TAIL_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "…(截断)…\n" + text[-limit:]


def _read_dataset_instance_ids(path: Path) -> tuple[str, ...]:
    """读本地数据集快照的 instance_id（jsonl/json；json 支持 list 或 HF 形态）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HarnessInputError(
            HarnessErrorKind.DATASET_MISMATCH, f"读不到 dataset_snapshot: {error}"
        ) from error
    rows: list[object]
    if path.suffix.lower() == ".jsonl":
        rows = []
        for index, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise HarnessInputError(
                    HarnessErrorKind.DATASET_MISMATCH,
                    f"dataset_snapshot 第 {index + 1} 行不是合法 JSON: {error}",
                ) from error
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise HarnessInputError(
                HarnessErrorKind.DATASET_MISMATCH, f"dataset_snapshot 不是合法 JSON: {error}"
            ) from error
        if isinstance(payload, dict) and "instances" in payload:
            payload = payload["instances"]
        if not isinstance(payload, list):
            raise HarnessInputError(
                HarnessErrorKind.DATASET_MISMATCH, "dataset_snapshot JSON 顶层必须是数组"
            )
        rows = payload
    instance_ids: list[str] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not isinstance(row.get("instance_id"), str):
            raise HarnessInputError(
                HarnessErrorKind.DATASET_MISMATCH,
                f"dataset_snapshot 第 {index} 行缺少 instance_id",
            )
        instance_ids.append(row["instance_id"])
    return tuple(instance_ids)
