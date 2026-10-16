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

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codepilot.agents.coder import CodingAgent
from codepilot.agents.diagnoser import DiagnosisAgent
from codepilot.agents.impact_analyzer import ImpactAnalyzer
from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.localizer import Localizer
from codepilot.agents.planner import Planner
from codepilot.agents.reviewer import PatchReviewer
from codepilot.artifacts import ArtifactStore, RetrievalSnapshotStore
from codepilot.context.builder import ContextBuilder
from codepilot.eval.synthetic import INFERENCE_DENIED_DIRECTORIES, InferenceTask
from codepilot.failure.parser import FailureParser
from codepilot.graph.builder import run_graph
from codepilot.graph.deps import AccountingSandbox, Deps
from codepilot.graph.regression import RegressionRunner
from codepilot.llm.base import LLMProvider
from codepilot.llm.config import LLMConfig
from codepilot.llm.fake import FakeLLM, Reply
from codepilot.llm.registry import LLMRegistry
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.observability.events import EventSink
from codepilot.patch.validator import PatchValidator
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, WorkspaceManager
from codepilot.retrieval.service import retrieve_symbols
from codepilot.sandbox.base import Sandbox
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

__all__ = ["InferenceAssembly", "InferenceOutcome", "ProviderBundle"]

#: 图里会向模型要东西的角色。装配时一次备齐，缺一个就在构造期炸，而不是等某个
#: 节点跑到一半才报 KeyError。
ROLES: tuple[str, ...] = ("localizer", "planner", "coder", "diagnoser", "reviewer")

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
    def live(cls, config: LLMConfig, *, budget_currency: str = "CNY") -> ProviderBundle:
        """真实 provider。可能产生费用，因此必须由调用方显式授权后才构造。

        ``model_identity`` 取"角色 -> 模型 ID"的完整映射而不是配置文件名：换模型会
        换结果（02 §17.4），一份只写"configs/llm.yaml"的记录事后无法判断这批分数
        属于哪个模型。
        """
        registry = LLMRegistry(config, budget_currency=budget_currency)
        mapping = {role: config.resolve_role(role).model for role in ROLES}
        identity = "/".join(f"{role}={mapping[role]}" for role in ROLES)
        return cls(
            roles={role: registry.for_role(role) for role in ROLES},
            usage=registry.usage,
            simulated=False,
            model_identity=identity,
            registry=registry,
        )

    def close(self) -> None:
        if self.registry is not None:
            self.registry.close()


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
        task: InferenceTask,
        workspace_manager: WorkspaceManager,
        work_root: Path,
        sandbox_factory: Callable[[ArtifactStore], Sandbox],
        provider_bundle: ProviderBundle,
        budget: RepairBudget | None = None,
        with_reproducer: bool = False,
        allow_new_tests: bool = False,
    ) -> None:
        self._task = task
        self._manager = workspace_manager
        self._work_root = Path(work_root)
        self._sandbox_factory = sandbox_factory
        self._providers = provider_bundle
        self._budget = budget or RepairBudget()
        self._with_reproducer = with_reproducer
        self._allow_new_tests = allow_new_tests

    # ------------------------------------------------------------------ 入口

    def run(self, *, run_id: str, seed: int) -> InferenceOutcome:
        """跑一次完整图。

        workspace 与 artifact 留在 run 目录下，**不**在这里删除：中断恢复要面对的是
        "旧现场"，删掉就等于每次重启都从头再花一遍钱。
        """
        started = time.time()
        artifacts = ArtifactStore(self._work_root, run_id)
        workspace = self._manager.create(
            source=str(self._task.repository),
            base_commit=self._task.base_commit,
            target=artifacts.run_dir / "workspace",
        )
        self._assert_isolated(workspace)
        policy = WorkspacePathPolicy(
            workspace.path,
            deny_directories=WorkspacePathPolicy.DEFAULT_DENY_DIRECTORIES
            | INFERENCE_DENIED_DIRECTORIES,
            repro_write=self._with_reproducer,
            allow_new_tests=self._allow_new_tests,
        )
        database = artifacts.run_dir / "index.sqlite3"
        engine = create_engine_from_url(f"sqlite:///{database.as_posix()}")
        init_db(engine)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as session:
                stores = Stores(session)
                Indexer().index(stores, workspace)
                index = RepositoryIndex(stores, workspace.repository_id)
                state = self._execute(
                    run_id=run_id,
                    workspace=workspace,
                    artifacts=artifacts,
                    policy=policy,
                    index=index,
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
    ) -> RepairState:
        task = self._task
        graph = RepoGraph(index)
        symbols = list(index.symbol_objects())
        files = list(index.files())
        provider = self._providers.roles
        events = EventSink(
            run_id=run_id, task_id=task.task_id, jsonl_path=artifacts.run_dir / "events.jsonl"
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
            env_spec=task.env_spec,
            allow_network=True,
        )
        tools = build_tool_registry(
            policy=policy,
            index=index,
            graph=graph,
            base_commit=workspace.base_commit,
            sandbox=sandbox,
            environment=environment,
            env_spec=task.env_spec,
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
            planner=Planner(provider["planner"], tools, policy, index),
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
            env_spec=task.env_spec,
            validator=PatchValidator(policy, workspace.base_commit, index=index),
            tools=tools,
            context_builder=ContextBuilder(workspace_root=workspace.path),
            artifacts=artifacts,
            events=events,
            workspace_manager=self._manager,
            usage=self._providers.usage,
            simulated_llm=self._providers.simulated,
            regression_runner=RegressionRunner(),
            reviewer=PatchReviewer(provider["reviewer"]),
        )
        initial = RepairState(
            task_id=task.task_id,
            run_id=run_id,
            repository_id=workspace.repository_id,
            base_commit=workspace.base_commit,
            issue=task.issue,
            mode=TaskMode.SYNTHETIC,
            started_at=datetime.now(UTC),
            budget=self._budget,
        )
        try:
            return run_graph(initial, deps)
        finally:
            deps.close()

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
            artifact_dir=str(artifacts.run_dir),
            failure_reasons=tuple(state.failure_reasons),
        )
