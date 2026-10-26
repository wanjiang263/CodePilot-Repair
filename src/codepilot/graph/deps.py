"""节点依赖容器（02 §15.2、G-02）。

节点一律通过 ``Deps`` 拿到 LLM / Sandbox / 索引 / 事件流，**不读任何模块级全局**：
一次 benchmark 会在同一个进程里并发跑多个 run_id，共享可变对象会让两个任务的
预算与用量互相污染，而且这种 bug 只在并发下出现，事后基本查不出来。

因此 ``Deps`` 有两条硬约束：

1. **身份绑定**：构造时绑定 ``run_id`` / ``task_id`` / ``base_commit``，恢复入口
   :meth:`Deps.bind` 逐条核对。拿 A 任务的 Deps 跑 B 任务是编程错误，让它立刻
   抛出来，不要静默地写出混合状态的 checkpoint。
2. **生命周期可控**：:meth:`Deps.close` 释放 EventSink / LLMRegistry；
   **不删 Workspace、不删 Artifact**——它们是中断恢复的证据。

测试执行次数的累计走 :class:`AccountingSandbox`：复现验证、目标测试、以及 Coding
Agent 的 ``run_tests`` 工具都经同一层。包装而不是在各调用点手工 ``+= 1``，是因为
漏掉工具调用那一条路径就等于漏掉了最烧预算的那一部分。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from codepilot.agents.coder import CodingAgent
from codepilot.agents.diagnoser import DiagnosisAgent
from codepilot.agents.impact_analyzer import ImpactAnalyzer
from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.localizer import Localizer
from codepilot.agents.planner import Planner
from codepilot.agents.reproducer import ReproductionAgent
from codepilot.artifacts import ArtifactStore
from codepilot.clock import utcnow
from codepilot.context.builder import ContextBuilder
from codepilot.failure.classifier import FailureClassifier
from codepilot.failure.parser import FailureParser
from codepilot.graph.budget import BudgetManager
from codepilot.graph.stop_rules import StopRules
from codepilot.llm.calls import LLMRunContext
from codepilot.llm.config import LLMConfig
from codepilot.llm.registry import LLMRegistry
from codepilot.observability.events import EventSink
from codepilot.patch.validator import PatchValidator
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import RepositoryIndex
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.execution import ExecutionResult
from codepilot.schemas.regression import RegressionResult
from codepilot.schemas.retrieval import RetrievalResult
from codepilot.schemas.review import ReviewResult
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage
from codepilot.tools.registry import ToolRegistry

__all__ = [
    "AccountingSandbox",
    "Deps",
    "DepsConfigurationError",
    "PatchReviewer",
    "RegressionRunner",
    "Retriever",
]

#: 03 §5 的检索边界：图节点只需要"给 Issue 文本，拿回候选"。
Retriever = Callable[[str], RetrievalResult]

#: 节点进入前的时间来源；测试注入假时钟来触发 ``budget_time``，而不必真等 30 分钟。
Clock = Callable[[], datetime]


@runtime_checkable
class RegressionRunner(Protocol):
    """Phase 9（G-26～G-29）回归实现的注入契约。"""

    def run(self, state: RepairState, deps: Deps) -> RegressionResult: ...


@runtime_checkable
class PatchReviewer(Protocol):
    """Phase 9（G-31）的接缝。本轮没有实现，节点据此记录 DEFERRED。"""

    def review(self, state: RepairState, deps: Deps) -> ReviewResult: ...


class DepsConfigurationError(ValueError):
    """Deps 与 State 身份不匹配，或缺少某条路径必需的依赖。"""


@dataclass(slots=True)
class AccountingSandbox:
    """记录 ``run_tests`` 调用次数的薄包装。"""

    inner: Sandbox
    runs: int = 0
    run_log: list[tuple[str, int]] = field(default_factory=list)

    def build_env(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: EnvSpec,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult:
        return self.inner.build_env(
            repository=repository,
            base_commit=base_commit,
            workspace=workspace,
            env_spec=env_spec,
            allow_network=allow_network,
        )

    def run_tests(
        self,
        *,
        workspace: Path,
        environment: EnvironmentBuildResult,
        env_spec: EnvSpec,
        tests: Sequence[str] = (),
        artifact_iteration: int = 1,
    ) -> ExecutionResult:
        self.runs += 1
        self.run_log.append((str(Path(workspace)), len(tuple(tests))))
        return self.inner.run_tests(
            workspace=workspace,
            environment=environment,
            env_spec=env_spec,
            tests=tests,
            artifact_iteration=artifact_iteration,
        )

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:
        return self.inner.cleanup(older_than_seconds=older_than_seconds)

    def seed(self, already_used: int) -> None:
        """中断恢复时把已计费次数补回计数器，避免重新清零。"""
        self.runs = already_used


@dataclass(slots=True)
class Deps:
    """一次运行的全部注入依赖；字段按消费方分组。

    节点只读这些字段，不允许在节点里替换实现，否则工具权限会随节点扩大
    （03 §5 的硬约束）。
    """

    # ---- 身份：与 RepairState 一一对应 -------------------------------------
    run_id: str
    task_id: str
    repository: str
    workspace: Path
    base_commit: str

    # ---- Repository Intelligence ------------------------------------------
    index: RepositoryIndex
    repo_graph: RepoGraph
    retriever: Retriever

    # ---- Agents（复用 Phase 3/5/6/7 的既有实现）---------------------------
    issue_analyzer: IssueAnalyzer
    localizer: Localizer
    impact_analyzer: ImpactAnalyzer
    planner: Planner
    coder: CodingAgent
    diagnoser: DiagnosisAgent

    # ---- 执行环境与补丁（Phase 4/5）---------------------------------------
    sandbox: Sandbox
    environment: EnvironmentBuildResult
    env_spec: EnvSpec
    validator: PatchValidator
    tools: ToolRegistry
    context_builder: ContextBuilder

    # ---- 记账与可观测 ------------------------------------------------------
    artifacts: ArtifactStore
    events: EventSink

    # ---- 带默认值的可选依赖 ------------------------------------------------
    failure_parser: FailureParser = field(default_factory=FailureParser)
    failure_classifier: FailureClassifier = field(default_factory=FailureClassifier)
    workspace_manager: WorkspaceManager | None = None
    reproducer: ReproductionAgent | None = None
    usage: Usage = field(default_factory=Usage)
    clock: Clock = utcnow
    #: 允许注入自定义判定器；缺省时在 ``__post_init__`` 里绑定 ``clock``。
    budget_manager: BudgetManager | None = None
    stop_rules: StopRules | None = None

    #: Phase 9 可注入实现；旧调用方未配置时仍记录 DEFERRED，不伪造 PASS。
    regression_runner: RegressionRunner | None = None
    reviewer: PatchReviewer | None = None
    patch_reviewer: PatchReviewer | None = None

    simulated_llm: bool = False
    llm_registry: LLMRegistry | None = None
    llm_config: LLMConfig | None = None
    """生效的 LLM 配置（模型别名、单价、汇率、角色映射）。

    它存在的唯一理由是**闭环预留必须按 Reviewer 实际绑定模型的单价估算**：注册表
    按角色构造 provider，角色 → 别名 → 单价这条链就在 ``llm_config`` 里。刻意只传
    配置、不传注册表本身：注册表跨越一个批次的所有 run 共享，``Deps.close()`` 关掉
    它会让后面的任务拿到已关闭的 HTTP 客户端。
    """
    #: 派发层**已预扣但尚未结算**的费用读数 ``(金额, 币种)``；无活动账本时返回 ``None``。
    outstanding_cost: Callable[[], tuple[float, str] | None] = lambda: None
    """逐请求预留（:mod:`codepilot.llm.dispatch_budget`）的"已承诺未落账"部分。

    ``state.usage`` 只在响应回来后累加，所以一次请求在飞行中（或被冻结成
    ``unknown_frozen``）时，那笔钱既不在账本里、也确实已经花出去了。预算准入必须
    看见它，否则"已花"是一个偏小的数，预留就是在一段并不存在的 headroom 上又扣一次。
    默认 ``lambda: None``：离线/测试路径没有进程级账本，读数就是"没有预扣"。
    """

    #: 恢复流程核对外部证据后置为 False，用于禁止复用已记录的测试结论。
    evidence_trusted: bool = True
    discover_missing_targets: bool = False

    #: 协作式取消信号。只允许在节点边界读取；``None`` 表示不启用（CLI/评测
    #: 行为不变）。它不删除 Workspace、不杀容器，只阻止下一次派发。
    cancel_check: Callable[[], bool] | None = None

    #: ``__post_init__`` 归一化后的计数视图。
    accounting: AccountingSandbox = field(init=False)
    _budget: BudgetManager | None = field(init=False, default=None, repr=False)
    _stop_rules: StopRules | None = field(init=False, default=None, repr=False)
    closed: bool = field(init=False, default=False)

    #: 逐请求 LLM 记录里的"当前在哪个节点、第几轮"。装配时绑定 run/task 身份，
    #: 节点进入时由 :func:`codepilot.graph.builder._guard` 更新。
    llm_context: LLMRunContext = field(default_factory=LLMRunContext, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.patch_reviewer is not None:
            self.reviewer = self.patch_reviewer
        if not self.run_id:
            raise DepsConfigurationError("Deps 必须绑定 run_id")
        if not self.task_id:
            raise DepsConfigurationError("Deps 必须绑定 task_id")
        self.llm_context.run_id = self.run_id
        self.llm_context.task_id = self.task_id
        if isinstance(self.sandbox, AccountingSandbox):
            self.accounting = self.sandbox
        else:
            self.accounting = AccountingSandbox(self.sandbox)
            self.sandbox = self.accounting
        if self.budget_manager is not None:
            self._budget = self.budget_manager
        if self.stop_rules is not None:
            self._stop_rules = self.stop_rules

    @property
    def budget(self) -> BudgetManager:
        """预算判定器，与 ``deps.clock`` 共用同一个时间来源。

        测试注入假时钟触发 ``budget_time`` 时，如果判定器仍用真实 ``utcnow``，
        节点入口检查就永远看不到超时——所以时钟必须从 deps 传进来。
        """
        if self._budget is None:
            self._budget = BudgetManager(clock=self.clock)
        return self._budget

    @property
    def rules(self) -> StopRules:
        """停止规则判定器，必须复用 :attr:`budget`。

        它不能是模块级单例：单例里的 ``BudgetManager`` 会在构造时绑定真实
        ``utcnow``，于是 ``check_budget`` 用宿主机时间判预算，而节点入口检查用
        注入时钟判预算——同一个 run 里两套时间口径，实测会让 ``budget_time``
        在第一次修复轮就误报耗尽。单例还会让并行 run 共享判定状态。
        """
        if self._stop_rules is None:
            self._stop_rules = StopRules(budget=self.budget)
        return self._stop_rules

    # ------------------------------------------------------------------ 身份

    def bind(self, state: RepairState) -> None:
        """把外部副作用计数器对齐到 ``state``（中断恢复的必经入口）。

        ``state`` 是 checkpoint 里的权威账本：进程重启后内存中的 ``usage`` 与
        Sandbox 计数都是 0，不对齐就会把已经花掉的 token、费用和测试次数当作
        没花，进而绕过预算——这正是 02 §15.3 不允许的行为。
        """
        if state.run_id != self.run_id:
            raise DepsConfigurationError(
                f"Deps 绑定 run_id={self.run_id!r}，但状态属于 {state.run_id!r}"
            )
        if state.task_id != self.task_id:
            raise DepsConfigurationError(
                f"Deps 绑定 task_id={self.task_id!r}，但状态属于 {state.task_id!r}"
            )
        if state.base_commit != self.base_commit:
            raise DepsConfigurationError(
                f"Deps 绑定 base_commit={self.base_commit!r}，但状态属于 {state.base_commit!r}"
            )
        self.restore_usage(state.usage)
        self.accounting.seed(state.test_runs)

    def restore_usage(self, usage: Usage) -> None:
        """原地覆盖用量字段，保持 :class:`Usage` 对象身份不变。

        Coding Agent / Planner 持有同一个 ``Usage`` 引用；换成新对象会让后续
        LLM 调用记到 state 已经不再持有的对象上，恢复之后费用就悄悄丢了。
        逐字段覆盖而不是只覆盖四个总数：``by_role`` / ``by_model`` / 未知费用计数
        漏掉一项，恢复后的分角色账目就会与总账对不上（G-13 的记账自检）。
        """
        self.usage.reset_to(usage)

    @property
    def test_runs(self) -> int:
        return self.accounting.runs

    def require_workspace(self) -> Path:
        path = Path(self.workspace)
        if not path.is_dir():
            raise DepsConfigurationError(f"Workspace 不存在：{path}")
        return path

    def bind_llm_context(self, node: str, iteration: int) -> None:
        """记录"接下来这些 LLM 请求属于哪个节点、第几轮修复"。

        放在节点入口而不是让每个 Agent 自己上报：Agent 拿不到 state，而漏报的
        表现是记录里 role 对了、轮次全 0，事后完全看不出漏在哪。
        """
        self.llm_context.node = node
        self.llm_context.iteration = iteration

    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        """释放可关闭资源；**不删除 Workspace / Artifact**（恢复证据）。"""
        if self.closed:
            return
        self.closed = True
        self.events.close()
        if self.llm_registry is not None:
            self.llm_registry.close()

    def __enter__(self) -> Deps:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
