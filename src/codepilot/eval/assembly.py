"""E-08 推理侧装配：把一个 InferenceTask 跑上**真实的 M2 图**（Phase 9 全套）。

Phase 10 有一条明确的红线：不许为了 benchmark 另写一套"简化修复 Agent"。这里因此
不实现任何修复逻辑，只做装配——节点、路由、预算、诊断、回归、复审全部来自
:mod:`codepilot.graph`，与 ``tests/graph/`` 里跑过真实 Docker 验收的是同一张图。

信息隔离靠**构造参数**而不是靠约定：:class:`InferenceAssembly` 需要的一切都不来自
``eval/``——它拿的是 :class:`~codepilot.eval.synthetic.InferenceTask`，类型上就接触
不到 gold patch、隐藏测试与目标清单。评测器用的
:class:`~codepilot.eval.synthetic.EvaluationTask` 走另一条路
(:mod:`codepilot.eval.evaluation`)，两边只在 runner 里汇合，且永远"推理先结束、
评测后开始"。

workspace 侧的隔离由三条共同保证，缺一条都不算隔离：

1. 仓库由 ``repo/`` 重新材料化，``eval/`` 从来不是那棵树的成员；
2. ``fetch`` 模式只取 base 的祖先，对象库里没有 base 之后的提交；
3. :class:`WorkspacePathPolicy` 的拒绝名单含 ``eval``，工具层读不到同名目录——
   即便某个仓库自己带了 ``eval/``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from codepilot.agents.coder import CodingAgent
from codepilot.agents.diagnoser import DiagnosisAgent
from codepilot.agents.impact_analyzer import ImpactAnalyzer
from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.localizer import Localizer
from codepilot.agents.planner import Planner
from codepilot.agents.reproducer import (
    BaseWorkspaceValidator,
    ReproductionAgent,
    WorkspaceManagerBaseProvider,
)
from codepilot.agents.reviewer import PatchReviewer
from codepilot.artifacts import LLM_REQUESTS_JSONL, ArtifactStore, RetrievalSnapshotStore
from codepilot.context.builder import ContextBuilder
from codepilot.eval.synthetic import INFERENCE_DENIED_DIRECTORIES
from codepilot.failure.parser import FailureParser
from codepilot.graph.builder import run_graph
from codepilot.graph.checkpoint import open_checkpointer, resume_run, run_with_checkpoint
from codepilot.graph.deps import AccountingSandbox, Deps
from codepilot.graph.regression import RegressionRunner
from codepilot.llm.base import LLMProvider
from codepilot.llm.calls import CallRecorder, LLMRunContext
from codepilot.llm.config import LLMConfig
from codepilot.llm.dispatch_budget import current_outstanding
from codepilot.llm.fake import FakeLLM, Reply
from codepilot.llm.registry import LLMMode, LLMRegistry
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.observability.events import EventSink, EventStoreProtocol
from codepilot.patch.validator import PatchValidator
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, WorkspaceManager, normalize_source
from codepilot.report import VerificationClosure, verification_level
from codepilot.retrieval.service import retrieve_symbols
from codepilot.sandbox.base import Sandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus, TaskMode
from codepilot.schemas.retrieval import FusionMethod
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage
from codepilot.storage.db import (
    create_engine_from_url,
    create_session_factory,
    init_db,
    session_scope,
)
from codepilot.storage.repository import Stores
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.registry import build_tool_registry
from codepilot.tools.write_tools import TestRunBudget

__all__ = [
    "WORKSPACE_IDENTITY",
    "InferenceAssembly",
    "InferenceOutcome",
    "ProviderBundle",
    "role_model_identity",
]

#: 现场身份记录。恢复进程靠它认回 workspace 与索引的 ``repository_id``。
WORKSPACE_IDENTITY = "workspace_identity.json"


class InferenceTaskLike(Protocol):
    """推理图需要的最小任务视图（Phase 11 起同时服务 Synthetic 与 SWE-bench）。

    这里刻意用结构化 Protocol 而不是 import 具体任务类型：SWE-bench 的任务模块
    (:mod:`codepilot.eval.swebench_task`) 必须可以独立于本模块存在，而本模块也
    绝不能反向依赖评测侧的 ``codepilot.eval.swebench``（那里有 gold patch 与
    隐藏测试清单）。两者只在这个五字段视图上相遇：

    * ``task_id`` / ``issue`` / ``base_commit`` 是任务身份与问题描述；
    * ``repository`` 既可以是本地路径（synthetic fixture 的 ``repo/``），也可以是
      远端 URL（SWE-bench 的 ``https://github.com/<owner>/<name>.git``）——
      :class:`~codepilot.repo.workspace.WorkspaceManager` 两种都支持；
    * ``env_spec`` 是环境规格；
    * ``mode`` 决定 ``RepairState.mode``（synthetic / swebench）。

    评测字段（gold patch、test patch、FAIL_TO_PASS…）**不在**这个视图里，因此
    装配层即使想泄漏也无从取值。
    """

    @property
    def task_id(self) -> str: ...

    @property
    def issue(self) -> str: ...

    @property
    def base_commit(self) -> str: ...

    @property
    def repository(self) -> Path | str: ...

    @property
    def env_spec(self) -> EnvSpec: ...

    @property
    def mode(self) -> str: ...


#: 图里会向模型要东西的角色。装配时一次备齐，缺一个就在构造期炸，而不是等某个
#: 节点跑到一半才报 KeyError。
ROLES: tuple[str, ...] = (
    "localizer",
    "planner",
    "coder",
    "reproducer",
    "diagnoser",
    "reviewer",
)

#: Agent 会对外宣称"这一步成功了"的终态，与 ``metrics.agent_claims_success`` 同源。
_CLAIM_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.PROVISIONALLY_VERIFIED}
)


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    """一次运行用到的全部 provider，外加"这是不是模拟"的诚实标记。

    FakeLLM 与真实 provider 实现同一个 :class:`LLMProvider` 接口——这正是 03 §23
    要求 provider 藏在接口后面的原因：换实现不该改变图的语义，也不该让"用了
    FakeLLM"这件事在报告里看不出来。所以 ``simulated`` 与 ``model_identity`` 会一路
    传到 ``results.jsonl``。
    """

    roles: Mapping[str, LLMProvider]
    usage: Usage
    simulated: bool
    model_identity: str
    registry: LLMRegistry | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        missing = [role for role in ROLES if role not in self.roles]
        if missing:
            raise ValueError(f"provider 缺少角色: {missing}")

    @classmethod
    def scripted(
        cls, queues: Mapping[str, Sequence[Reply]], *, model_identity: str = "FakeLLM"
    ) -> ProviderBundle:
        """按 ``purpose`` 分派脚本回复。

        分队列而不是一个大列表：一轮修复会产生 4~6 次调用，全局列表会把"分类器这次
        到底问没问模型"变成测试的隐式断言。队列耗尽时**抛错**，不重复上一次回复。
        """
        pending: dict[str, list[Reply]] = {key: list(value) for key, value in queues.items()}
        seen: list[str] = []

        def reply(request: Any) -> Reply:
            seen.append(request.purpose)
            queue = pending.get(request.purpose)
            if not queue:
                raise AssertionError(
                    f"purpose={request.purpose!r} 没有脚本回复了（已调用序列={seen}）"
                )
            return queue.pop(0)

        usage = Usage()
        shared = UsageTrackingProvider(FakeLLM(default=reply), usage=usage)
        return cls(
            roles=dict.fromkeys(ROLES, shared),
            usage=usage,
            simulated=True,
            model_identity=model_identity,
        )

    @classmethod
    def live(
        cls,
        config: LLMConfig,
        *,
        budget_currency: str | None = None,
        record_llm: bool = False,
        client_factory: Any = None,
        bedrock_client_factory: Any = None,
    ) -> ProviderBundle:
        """真实 provider。可能产生费用，因此必须由调用方显式授权后才构造。

        ``model_identity`` 取"角色 -> 模型 ID"的完整映射而不是配置文件名：换模型会
        换结果（02 §17.4），一份只写"configs/llm.yaml"的记录事后无法判断这批分数
        属于哪个模型。

        ``record_llm`` 来自 ``settings.record_llm``（02 §12）。这个开关以前只被 CLI
        打印出来、从没接到真实执行路径上，所以"我开了录制"与"磁盘上有录制"是两件事。
        录制落点要到每个 run 的 artifact 目录确定之后才绑定（见 :meth:`bind_run`）。
        """
        registry = LLMRegistry(
            config,
            mode=LLMMode.RECORD if record_llm else LLMMode.LIVE,
            budget_currency=budget_currency,
            client_factory=client_factory,
            bedrock_client_factory=bedrock_client_factory,
        )
        return cls(
            roles={role: registry.for_role(role) for role in ROLES},
            usage=registry.usage,
            simulated=False,
            model_identity=role_model_identity(config),
            registry=registry,
        )

    def bind_run(
        self,
        *,
        run_id: str,
        task_id: str,
        sink: EventSink,
        recorder: CallRecorder,
        context: LLMRunContext,
        calls_path: Path | None = None,
    ) -> None:
        """把 run 身份、事件流与逐请求日志接到每个角色的 provider 上。

        这一步是真实 GLM 实验暴露出的观测缺口本身：provider 在派发前就装配好了，
        那时还没有 run_id 与 artifact 目录，于是注册表拿着 ``sink=None`` 一路跑到结束，
        ``events.jsonl`` 里 16 条事件的 ``usage`` 全是 ``null``。
        """
        if self.registry is not None:
            self.registry.bind_run(
                run_id=run_id,
                task_id=task_id,
                sink=sink,
                recorder=recorder,
                context=context,
                calls_path=calls_path,
            )
            return
        for provider in self.roles.values():
            binder = getattr(provider, "bind_sink", None)
            if callable(binder):
                binder(sink, recorder=recorder)

    def close(self) -> None:
        if self.registry is not None:
            self.registry.close()


def role_model_identity(config: LLMConfig) -> str:
    """角色 → 官方模型 ID 的完整映射，作为一批运行的身份。

    单独成函数是因为 CLI 在**开跑之前**就要把这个字符串写进 manifest 与确认提示，
    而那时还没有 provider 对象；两处各自拼一遍会出现"报表里的身份和实际跑的不一样"。
    """
    mapping = {role: config.resolve_role(role).model for role in ROLES}
    return "/".join(f"{role}={mapping[role]}" for role in ROLES)


@dataclass(frozen=True, slots=True)
class InferenceOutcome:
    """一次推理运行的产出。**不含**任何评测结论。

    每一轮实际保存的 patch 都在这里：First Attempt Resolved 必须能拿到第一轮那份
    ``patch_diff`` 单独去评测，否则那个指标就只能靠最终结果推断，而推断值恒等于
    Resolved Rate，迭代修复的贡献再也测不出来。
    """

    run_id: str
    task_id: str
    seed: int
    base_commit: str
    repository_id: str
    status: str
    stop_reason: str | None
    final_patch: str
    final_patch_sha256: str
    first_attempt_patch: str | None
    first_attempt_patch_sha256: str | None
    iterations: int
    attempts_saved: int
    usage: Usage
    usage_uncertain: bool
    test_runs: int
    runtime_seconds: float
    localization_files: tuple[str, ...]
    localization_symbols: tuple[str, ...]
    agent_claims_success: bool
    empty_patch: bool
    invalid_patch: bool
    simulated_llm: bool
    sandbox_backend: str
    verification_mode: str
    artifact_dir: str
    failure_reasons: tuple[str, ...] = ()
    error_class: str | None = None
    """终态归因大类（``codepilot.errors.FailureClass`` 的值）。"""

    error_origin: str | None = None
    """责任侧：model / provider / tool / workspace / harness。"""

    target_test_source: str | None = None
    """目标测试来源：reproduction/planner/impact/baseline/related/none。"""

    target_tests: tuple[str, ...] = ()
    """实际交给 Sandbox 的目标测试；空元组只表示没有目标，不表示通过。"""

    plan_corrections: int = 0
    """Planner 被重新问了几次。与服务商 HTTP 重试分开计。"""

    plan_semantic_rejections: int = 0
    """被语义校验拒绝的计划次数。"""

    plan_schema_rejections: int = 0
    """未通过 JSON Schema / Pydantic 的次数。"""

    plan_tool_audit: dict[str, Any] = field(default_factory=dict)
    """Planner 工具审计摘要。``{}`` = **没有采集到**，不等于"模型没调用工具"。"""

    verification_level: str = ""
    """内部自验证级别（``m2_verified`` / ``sampled_verified`` / ``provisionally_verified``
    / ``unverified`` …；``""`` = 这条运行没有产出级别）。与官方判分各记各的。"""

    review_executed: bool = False
    """PatchReviewer 是否真的审过候选补丁。``False`` 只能读成"未执行"，
    **永远不等于通过**（Phase 11 §六）。"""

    coding_stop_reason: str = ""
    """编码阶段的局部停止原因。非空只说明"这一轮不能再改代码了"，不说明任务终止：
    补丁照样要过校验、过测试、过审查，报告里两套口径分开写。"""

    review_high_issues: int = 0
    """审查留下的 high 问题数。``review_executed=False`` 时这个 0 只能读成"没审"。"""

    post_review_repair: bool = False
    """是否发生过 Phase 9 政策允许的那一次"审查后修复"（复审因此必须重新挣）。"""

    unexecuted_stages: tuple[str, ...] = ()
    """闭环里**未执行**的阶段（含未执行原因）。空元组 = 全部执行过，不是"没采集"。"""

    review_bound_to_candidate: bool = False
    """审查结论是否绑定当前候选补丁的 SHA-256。审过 v1 不给 v2 背书。"""

    llm_requests: dict[str, Any] = field(default_factory=dict)
    """逐请求记录的聚合（按 role/model）。"""

    checkpointed: bool = False
    resumed_from_checkpoint: bool = False

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _verification_mode(state: RepairState) -> str:
    """评测覆盖到哪一层。抽样与全量不能混在一张表里读（03 §18）。"""
    if state.regression is None:
        return "limited" if state.status in _CLAIM_STATUSES else "none"
    return state.regression.mode.value


class InferenceAssembly:
    """从 :class:`InferenceTask` 装配并运行完整 M2 图。

    ``sandbox_factory`` / ``provider_bundle`` 由调用方注入，因此同一套装配既能接
    DockerSandbox 做真实验收，也能接 FakeSandbox 做离线工程测试——两种场合在报告里
    靠 ``sandbox_backend`` 与 ``simulated_llm`` 区分，不互相冒充。
    """

    def __init__(
        self,
        *,
        task: InferenceTaskLike,
        workspace_manager: WorkspaceManager,
        work_root: Path,
        sandbox_factory: Callable[[ArtifactStore], Sandbox],
        provider_bundle: ProviderBundle,
        budget: RepairBudget | None = None,
        with_reproducer: bool = False,
        allow_new_tests: bool = False,
        record_llm: bool = False,
        checkpoint: bool = False,
        env_spec_factory: Callable[[Path], EnvSpec] | None = None,
        enforce_base_only: bool = True,
        allow_build_network: bool = True,
        event_store: EventStoreProtocol | None = None,
        cancel_check: Callable[[], bool] | None = None,
        workspace_factory: Callable[[ArtifactStore], Workspace] | None = None,
    ) -> None:
        self._task = task
        self._manager = workspace_manager
        self._work_root = Path(work_root)
        self._sandbox_factory = sandbox_factory
        self._providers = provider_bundle
        self._budget = budget or RepairBudget()
        self._with_reproducer = with_reproducer
        self._allow_new_tests = allow_new_tests
        self._record_llm = record_llm
        self._checkpoint = checkpoint
        #: Web/user tasks clone an ordinary repository; the base-only invariant is
        #: specific to evaluation fixtures and SWE-bench mirrors.
        self._env_spec_factory = env_spec_factory
        self._enforce_base_only = enforce_base_only
        self._allow_build_network = allow_build_network
        self._event_store = event_store
        self._cancel_check = cancel_check
        #: Optional seam for environments where cloning is unavailable (for
        #: example the bundled offline demo on a host whose MSYS `sh` cannot be
        #: spawned from the worker). It must return a clean, self-contained git
        #: workspace; identity recording and resume verification stay the same.
        self._workspace_factory = workspace_factory
        #: 上一次 :meth:`run` 是否真的从节点级 checkpoint 续跑过。
        self.resumed_from_checkpoint = False
        self._recorder: CallRecorder | None = None

    # ------------------------------------------------------------------ 入口

    def run(self, *, run_id: str, seed: int) -> InferenceOutcome:
        """跑一次完整图。

        workspace 与 artifact 留在 run 目录下，**不**在这里删除：中断恢复要面对的是
        "旧现场"，删掉就等于每次重启都从头再花一遍钱。
        """
        started = time.time()
        artifacts = ArtifactStore(self._work_root, run_id)
        checkpoint_path = artifacts.run_dir / "checkpoints.sqlite"
        resuming = self._checkpoint and checkpoint_path.exists()
        if resuming:
            # 节点级续跑：现场必须原样接着用。``WorkspaceManager.create`` 拒绝写入
            # 非空目录，正是为了拦住"把旧现场当成新现场"，所以这里显式地承认它是旧的。
            workspace = self._reopen(artifacts)
        else:
            workspace = (
                self._workspace_factory(artifacts)
                if self._workspace_factory is not None
                else self._manager.create(
                    source=str(self._task.repository),
                    base_commit=self._task.base_commit,
                    target=artifacts.run_dir / "workspace",
                )
            )
            self._record_identity(artifacts, workspace)
        if self._enforce_base_only:
            self._assert_isolated(workspace)
        env_spec = self._resolve_env_spec(workspace.path)
        policy = WorkspacePathPolicy(
            workspace.path,
            deny_directories=WorkspacePathPolicy.DEFAULT_DENY_DIRECTORIES
            | INFERENCE_DENIED_DIRECTORIES,
            repro_write=self._with_reproducer,
            allow_new_tests=self._allow_new_tests,
            deny_existing_test_edits=os.environ.get("CODEPILOT_DENY_EXISTING_TEST_EDITS") == "1",
        )
        database = artifacts.run_dir / "index.sqlite3"
        engine = create_engine_from_url(f"sqlite:///{database.as_posix()}")
        init_db(engine)
        factory = create_session_factory(engine)
        try:
            if not (resuming and database.exists()):
                # 索引必须在跑图**之前**提交。和图共用一个 session 时，进程被硬杀
                # （真实中断就是这么死的）会让索引库停留在未提交状态，恢复进程拿到一个
                # 空索引，症状是"计划里的 symbol 不在索引里"——看起来像模型乱写。
                with session_scope(factory) as session:
                    Indexer().index(Stores(session), workspace)
            with session_scope(factory) as session:
                index = RepositoryIndex(Stores(session), workspace.repository_id)
                state = self._execute(
                    run_id=run_id,
                    workspace=workspace,
                    artifacts=artifacts,
                    policy=policy,
                    index=index,
                    env_spec=env_spec,
                    checkpoint_path=checkpoint_path if self._checkpoint else None,
                    resuming=resuming,
                )
        finally:
            engine.dispose()
        return self._outcome(
            run_id=run_id,
            seed=seed,
            state=state,
            artifacts=artifacts,
            runtime=time.time() - started,
        )

    def _record_identity(self, artifacts: ArtifactStore, workspace: Workspace) -> None:
        """把创建现场时算出的身份写进 run 目录，供恢复进程原样读回。"""
        artifacts.write_json(
            WORKSPACE_IDENTITY,
            {
                "workspace": str(workspace.path),
                "source": str(workspace.source.normalized),
                "base_commit": workspace.base_commit,
                "repository_id": workspace.repository_id,
                "task_id": self._task.task_id,
            },
        )

    def _reopen(self, artifacts: ArtifactStore) -> Workspace:
        """按 run 目录留下的身份记录重认旧现场；身份缺失或不匹配就拒绝续跑。

        刻意**不**在这里重新推导 ``repository_id``：索引库是按创建现场时算出的 id 存的，
        恢复进程若用另一套推导规则算出不同的 id，就会拿到一个"零符号"的索引，
        症状是计划被语义校验拒绝（symbol 不在索引里），看起来像模型的问题。
        """
        from codepilot.repo.gitcmd import GitError, run_git

        identity_path = artifacts.run_dir / WORKSPACE_IDENTITY
        if not identity_path.is_file():
            raise RuntimeError(
                f"旧现场缺少 {WORKSPACE_IDENTITY}：无法确认 workspace 与索引的身份，"
                "拒绝在身份不明的目录上继续跑"
            )
        recorded = json.loads(identity_path.read_text(encoding="utf-8"))
        path = Path(str(recorded.get("workspace") or (artifacts.run_dir / "workspace")))
        if not path.is_dir():
            raise RuntimeError(f"旧现场的 workspace 不存在：{path}")
        try:
            head = run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()
        except (GitError, OSError) as exc:
            raise RuntimeError(f"无法重认旧现场 {path}: {exc}") from exc
        base = str(recorded.get("base_commit") or "")
        if head != base or base != self._task.base_commit:
            raise RuntimeError(
                f"旧现场 base commit 不一致（workspace HEAD={head}, 记录={base}, "
                f"本次={self._task.base_commit}）；节点级恢复拒绝跨 base 复用"
            )
        return Workspace(
            path=path,
            source=normalize_source(str(recorded.get("source") or self._task.repository)),
            base_commit=base,
            repository_id=str(recorded.get("repository_id") or ""),
        )

    def _resolve_env_spec(self, workspace: Path) -> EnvSpec:
        if self._env_spec_factory is None:
            return self._task.env_spec
        return self._env_spec_factory(workspace)

    # ------------------------------------------------------------------ 隔离

    @staticmethod
    def _assert_isolated(workspace: Workspace) -> None:
        """运行前确认推理现场干净。

        这三条不是"顺便看看"：任何一条破了，后面所有数字都失去意义，而失败症状是
        事后极难归因的"成功率莫名偏高"。宁可在这里拒绝开跑。
        """
        if (workspace.path / "eval").exists():
            raise RuntimeError(f"推理 workspace 内含评测目录，拒绝开跑: {workspace.path / 'eval'}")
        if not workspace.self_contained:
            raise RuntimeError(
                "推理 workspace 的 git 元数据不在子树内（worktree 模式与缓存共享对象库，"
                f"看得到 base 之后的提交）: {workspace.path}"
            )
        from codepilot.repo.gitcmd import run_git

        revisions = run_git(["rev-list", "--all", "--count"], cwd=workspace.path).stdout.strip()
        if revisions != "1":
            raise RuntimeError(
                f"推理仓库含 {revisions} 个可达提交，应当只有 base 一个: {workspace.path}"
            )

    # ------------------------------------------------------------------ 执行

    def _execute(
        self,
        *,
        run_id: str,
        workspace: Workspace,
        artifacts: ArtifactStore,
        policy: WorkspacePathPolicy,
        index: RepositoryIndex,
        env_spec: EnvSpec,
        checkpoint_path: Path | None = None,
        resuming: bool = False,
    ) -> RepairState:
        task = self._task
        graph = RepoGraph(index)
        symbols = list(index.symbol_objects())
        files = list(index.files())
        provider = self._providers.roles
        events = EventSink(
            run_id=run_id,
            task_id=task.task_id,
            jsonl_path=artifacts.run_dir / "events.jsonl",
            store=self._event_store,
        )
        # 逐请求观测要在**任何模型调用之前**接好：装配期没有 run_id，只有现在能绑。
        # 首次真实 GLM 运行没做这一步，于是 events.jsonl 的 usage 全为 null、
        # by_model 只有一个聚合项、llm_calls.jsonl 根本不存在。
        context = LLMRunContext(run_id=run_id, task_id=task.task_id)
        recorder = CallRecorder(path=artifacts.run_dir / LLM_REQUESTS_JSONL, context=context)
        self._recorder = recorder
        self._providers.bind_run(
            run_id=run_id,
            task_id=task.task_id,
            sink=events,
            recorder=recorder,
            context=context,
            calls_path=(artifacts.run_dir / "llm_calls.jsonl" if self._record_llm else None),
        )
        raw_sandbox = self._sandbox_factory(artifacts)
        sandbox = (
            raw_sandbox
            if isinstance(raw_sandbox, AccountingSandbox)
            else AccountingSandbox(raw_sandbox)
        )
        environment = sandbox.build_env(
            repository=str(task.repository),
            base_commit=workspace.base_commit,
            workspace=workspace.path,
            env_spec=env_spec,
            allow_network=self._allow_build_network,
        )
        tools = build_tool_registry(
            policy=policy,
            index=index,
            graph=graph,
            base_commit=workspace.base_commit,
            sandbox=sandbox,
            environment=environment,
            env_spec=env_spec,
            test_budget=TestRunBudget(self._budget.max_test_runs),
        )
        deps = Deps(
            run_id=run_id,
            task_id=task.task_id,
            repository=str(task.repository),
            workspace=workspace.path,
            base_commit=workspace.base_commit,
            index=index,
            repo_graph=graph,
            # 解析器必须知道真实文件清单，否则 traceback 里没有一帧会被判成"仓库内
            # 帧"，失败签名全退化成 no_repo_frame，重复失败与振荡检测就成了在比空壳。
            failure_parser=FailureParser(
                repo_files={item.path for item in files}, repo_roots=("/workspace",)
            ),
            retriever=lambda text: retrieve_symbols(
                workspace.path,
                symbols,
                text,
                channel="bm25",
                fusion_method=FusionMethod.WEIGHTED,
                channel_weights={"bm25": 1.0},
                graph=graph,
            ),
            issue_analyzer=IssueAnalyzer(),
            localizer=Localizer(
                provider["localizer"], index, workspace.path, RetrievalSnapshotStore(artifacts)
            ),
            impact_analyzer=ImpactAnalyzer(index, graph),
            planner=Planner(
                provider["planner"],
                tools,
                policy,
                index,
                events=events,
                artifacts=artifacts,
                llm_context=context,
            ),
            coder=CodingAgent(
                provider["coder"],
                tools,
                max_tool_calls=self._budget.max_tool_calls_per_iteration,
                max_total_tokens=self._budget.max_total_tokens,
            ),
            diagnoser=DiagnosisAgent(
                provider["diagnoser"], policy, index=index, artifacts=artifacts
            ),
            sandbox=sandbox,
            environment=environment,
            env_spec=env_spec,
            validator=PatchValidator(policy, workspace.base_commit, index=index),
            tools=tools,
            context_builder=ContextBuilder(workspace_root=workspace.path),
            artifacts=artifacts,
            events=events,
            workspace_manager=self._manager,
            usage=self._providers.usage,
            simulated_llm=self._providers.simulated,
            # 闭环预留要按 Reviewer 实际绑定模型的单价估算；这份配置就是那条链
            # （角色 → 别名 → 单价/汇率/币种）。脚本化与 Fake 批次没有注册表，
            # 传 None 让预留退回账本单价口径，零成本运行照旧放行。
            llm_config=self._providers.registry.config if self._providers.registry else None,
            # 逐请求预留的"已承诺未落账"部分必须进预算准入：state.usage 只在响应回来
            # 之后累加，飞行中的那笔钱否则是隐形的，预留就会在一个并不存在的余额上
            # 再扣一次。未启用进程级账本（离线批次）时这个读数恒为 None，行为不变。
            outstanding_cost=current_outstanding,
            cancel_check=self._cancel_check,
            regression_runner=RegressionRunner(),
            reviewer=PatchReviewer(provider["reviewer"]),
            reproducer=(
                ReproductionAgent(
                    provider["reproducer"],
                    policy=policy,
                    validator=BaseWorkspaceValidator(
                        sandbox=sandbox,
                        env_spec=env_spec,
                        environment=environment,
                        provider=WorkspaceManagerBaseProvider(
                            self._manager,
                            source=str(task.repository),
                            base_commit=workspace.base_commit,
                            root=artifacts.run_dir / "reproduction-base",
                        ),
                    ),
                    artifacts=artifacts,
                )
                if self._with_reproducer
                else None
            ),
        )
        deps.llm_context = context
        initial = RepairState(
            task_id=task.task_id,
            run_id=run_id,
            repository_id=workspace.repository_id,
            base_commit=workspace.base_commit,
            issue=task.issue,
            mode=TaskMode(task.mode),
            started_at=datetime.now(UTC),
            budget=self._budget,
        )
        try:
            if checkpoint_path is None:
                return run_graph(initial, deps)
            return asyncio.run(self._run_checkpointed(initial, deps, checkpoint_path, resuming))
        finally:
            deps.close()

    async def _run_checkpointed(
        self,
        initial: RepairState,
        deps: Deps,
        checkpoint_path: Path,
        resuming: bool,
    ) -> RepairState:
        """节点级 checkpoint 执行入口。复用 Phase 9 的 checkpointer 与恢复核对。

        恢复走 :func:`resume_run`（它会核对 workspace/base/patch/artifact 证据，
        核对不过就安全终止而不是"将就着跑完"）。不宣称 exactly-once：崩溃落在
        "外部副作用已做完、checkpoint 还没写"的窗口里时，那一轮会重做并重新计费。
        """
        async with await open_checkpointer(checkpoint_path, initial.run_id) as handle:
            if resuming:
                self.resumed_from_checkpoint = True
                return await resume_run(deps, handle, initial.run_id)
            return await run_with_checkpoint(initial, deps, handle)

    def _outcome(
        self,
        *,
        run_id: str,
        seed: int,
        state: RepairState,
        artifacts: ArtifactStore,
        runtime: float,
    ) -> InferenceOutcome:
        final = state.current_patch
        first = state.attempts[0].patch_diff if state.attempts else None
        localization = state.localization
        backend = "unknown"
        for attempt in reversed(state.attempts):
            if attempt.execution is not None:
                backend = str(attempt.execution.diagnostics.get("execution_backend") or "unknown")
                break
        if state.execution is not None:
            backend = str(state.execution.diagnostics.get("execution_backend") or backend)
        # 闭环逐阶段事实只算一次：``review_executed`` 与报告里的 closure 必须同源，
        # 两处各判一遍迟早会出现"报告说没审、结果行说审过了"。
        closure = VerificationClosure.from_state(state)
        return InferenceOutcome(
            run_id=run_id,
            task_id=state.task_id,
            seed=seed,
            base_commit=state.base_commit,
            repository_id=state.repository_id,
            status=state.status.value,
            stop_reason=state.stop_reason.value if state.stop_reason else None,
            final_patch=final,
            final_patch_sha256=InferenceOutcome.digest(final),
            first_attempt_patch=first,
            first_attempt_patch_sha256=(
                InferenceOutcome.digest(first) if first is not None else None
            ),
            iterations=len(state.attempts),
            attempts_saved=sum(1 for item in state.attempts if item.patch_diff.strip()),
            usage=state.usage.model_copy(deep=True),
            usage_uncertain=state.usage_uncertain,
            test_runs=state.test_runs,
            runtime_seconds=runtime,
            localization_files=tuple(
                item.path for item in (localization.files if localization else ())
            ),
            localization_symbols=tuple(
                item.qualified_name for item in (localization.symbols if localization else ())
            ),
            agent_claims_success=state.status in _CLAIM_STATUSES,
            empty_patch=not final.strip(),
            invalid_patch=bool(
                state.current_attempt and not state.current_attempt.validation.passed
            ),
            simulated_llm=self._providers.simulated,
            sandbox_backend=backend,
            verification_mode=_verification_mode(state),
            verification_level=verification_level(state),
            review_executed=closure.review_executed,
            coding_stop_reason=closure.coding_stop_reason or "",
            review_high_issues=closure.review_high_issues,
            post_review_repair=closure.post_review_repair,
            unexecuted_stages=tuple(closure.unexecuted_stages),
            review_bound_to_candidate=closure.review_bound_to_candidate,
            artifact_dir=str(artifacts.run_dir),
            failure_reasons=tuple(state.failure_reasons),
            error_class=state.error_class,
            error_origin=state.error_origin,
            target_test_source=(
                state.current_attempt.target_test_source
                if state.current_attempt is not None
                else None
            ),
            target_tests=(
                tuple(state.current_attempt.target_tests)
                if state.current_attempt is not None
                else ()
            ),
            plan_corrections=state.plan_corrections,
            plan_semantic_rejections=state.plan_semantic_rejections,
            plan_schema_rejections=state.plan_schema_rejections,
            plan_tool_audit=(state.plan_tools.summary() if state.plan_tools is not None else {}),
            llm_requests=self._recorder.summary() if self._recorder is not None else {},
            checkpointed=self._checkpoint,
            resumed_from_checkpoint=self.resumed_from_checkpoint,
        )
