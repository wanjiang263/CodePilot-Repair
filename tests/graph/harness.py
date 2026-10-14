"""Phase 8 图测试的共享装配。

刻意放在 ``tests/graph/harness.py``（不是 ``test_*.py``）：pytest 不会收集它，
但同目录的测试可以直接 ``from harness import ...``（tests/graph 无 ``__init__.py``，
rootdir 注入后同目录可导入，与 Phase 6/7 的 E2E 用法一致）。

装配用的是**真实**组件：真实 git 夹具仓库、真实 ``WorkspaceManager`` 隔离
Workspace、真实 Tree-sitter 索引、真实 PatchValidator、真实 Phase 7 解析/签名/
压缩/Diagnosis 链路。只有两处是替身，并且在报告里明确标出边界：

* ``FakeLLM``：按 purpose 分派脚本回复（``simulated_llm=True``）；
* ``FakeSandbox`` / ``DockerSandbox``：脚本化结果或真实容器。

LLM 脚本按 **purpose 分队列**而不是一个全局列表：一轮修复会产生 4~6 次调用，
具体次数取决于分类器是否需要问模型、Planner 是否走工具，全局列表会把这些
实现细节变成测试的隐式断言，改一处就全线崩，而且崩出来的信息毫无指导性。
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codepilot.agents.coder import CodingAgent
from codepilot.agents.diagnoser import DiagnosisAgent
from codepilot.agents.impact_analyzer import ImpactAnalyzer
from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.localizer import Localizer
from codepilot.agents.planner import Planner
from codepilot.artifacts import ArtifactStore
from codepilot.context.builder import ContextBuilder
from codepilot.failure.parser import FailureParser
from codepilot.graph.deps import Deps
from codepilot.llm.fake import FakeLLM, Reply
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.observability.events import EventSink
from codepilot.patch.validator import PatchValidator
from codepilot.repo.gitcmd import run_git
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import WorkspaceManager, normalize_source
from codepilot.retrieval.service import retrieve_symbols
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.retrieval import FusionMethod
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.registry import build_tool_registry
from codepilot.tools.write_tools import TestRunBudget

DEFECTIVE = "def normalize(value: int) -> int:\n    return value - 1\n"
CORRECT = "def normalize(value: int) -> int:\n    return value\n"
EXISTING_TEST = (
    "from pkg.core import normalize\n\n\ndef test_normalize() -> None:\n    assert normalize(3) == 3\n"
)
ISSUE = "normalize(3) returns 2; the contract is that it returns the value unchanged."
TARGET = "tests/test_core.py"

__all__ = [
    "DEFECTIVE",
    "Harness",
    "ISSUE",
    "PurposeScript",
    "TARGET",
    "assert_original_clean",
    "assertion_failure",
    "build_harness",
    "passed_result",
]


def _base_repo(root: Path) -> tuple[Path, str]:
    original = root / "original"
    (original / "src/pkg").mkdir(parents=True)
    (original / "tests").mkdir(parents=True)
    (original / "src/pkg/core.py").write_text(DEFECTIVE, encoding="utf-8")
    (original / "tests/test_core.py").write_text(EXISTING_TEST, encoding="utf-8")
    (original / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0.0.1'\n", encoding="utf-8"
    )
    run_git(["init", "-q"], cwd=original)
    run_git(["config", "user.email", "tests@example.invalid"], cwd=original)
    run_git(["config", "user.name", "Tests"], cwd=original)
    run_git(["add", "."], cwd=original)
    run_git(["commit", "-qm", "base"], cwd=original)
    return original, run_git(["rev-parse", "HEAD"], cwd=original).stdout.strip()


def assertion_failure(node_id: str = TARGET, *, message: str = "assert 2 == 3") -> ExecutionResult:
    """一条真实的 pytest 断言失败记录，够 Phase 7 的解析器产出结构化失败。"""
    return scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id=node_id,
                outcome=TestOutcome.FAILED,
                duration=0.1,
                failure_type="AssertionError",
                message=message,
                traceback=f"src/pkg/core.py:2: AssertionError: {message}",
            )
        ],
        exit_code=1,
    )


def multiple_failures(count: int, *, prefix: str) -> ExecutionResult:
    """N 条互不相同的断言失败，用于"失败数量不变/变多"两种进度场景。"""
    cases = [
        TestCaseResult(
            node_id=f"{TARGET}::test_variant_{index}",
            outcome=TestOutcome.FAILED,
            duration=0.1,
            failure_type="AssertionError",
            message=f"{prefix}{index} == {index + 1}",
            traceback=f"src/pkg/core.py:{index + 1}: AssertionError",
        )
        for index in range(1, count + 1)
    ]
    return scripted_result(ExecutionStatus.FAIL, tests=cases, exit_code=1)


def passed_result(node_id: str = TARGET) -> ExecutionResult:
    return scripted_result(
        ExecutionStatus.PASS, tests=[TestCaseResult(node_id=node_id, outcome=TestOutcome.PASSED, duration=0.1)]
    )


class PurposeScript:
    """按 ``request.purpose`` 分派的 LLM 脚本。

    队列耗尽时**抛错**而不是重复最后一次回复：静默复用会让"图确实又调了一次模型"
    这种 bug 伪装成通过。
    """

    def __init__(self) -> None:
        self.queues: dict[str, list[Any]] = defaultdict(list)
        self.calls: list[str] = []

    def add(self, purpose: str, *replies: Reply) -> PurposeScript:
        self.queues[purpose].extend(replies)
        return self

    def remaining(self, purpose: str) -> int:
        return len(self.queues[purpose])

    def reply(self, request: Any) -> Reply:
        purpose = request.purpose
        self.calls.append(purpose)
        queue = self.queues.get(purpose)
        if not queue:
            raise AssertionError(
                f"purpose={purpose!r} 没有脚本回复了（已调用 purpose 序列={self.calls}）。"
                "补足脚本，或让测试显式断言这一步不该发生。"
            )
        reply = queue.pop(0)
        return reply(request) if callable(reply) and not isinstance(reply, (str, dict)) else reply


def edit(path: str, search: str, replace: str) -> dict[str, Any]:
    return {"tool": "edit_file", "arguments": {"path": path, "search": search, "replace": replace}}


FINISH: dict[str, Any] = {"tool": "finish", "arguments": {"summary": "done"}}


def _localizer_replies(script: PurposeScript, target_symbol: Any, target_file: Any) -> None:
    """定位脚本必须用**检索候选的 id**。

    ``Localizer`` 的 candidate_id 是 ``SkeletonBuilder`` 给的 FileNode/Symbol id，
    直接取 ``index.files()[0]`` 会拿到 ``pyproject.toml``，然后被"未知候选"拒掉。
    """
    script.add(
        "localizer_files",
        {"files": [{"candidate_id": target_file.id, "reason": "the module named in the issue"}]},
    )
    script.add(
        "localizer_symbols",
        {
            "symbols": [
                {
                    "candidate_id": target_symbol.id,
                    "start_line": target_symbol.start_line,
                    "end_line": target_symbol.end_line,
                    "reason": "the normalize function itself",
                }
            ]
        },
    )


def _planner_reply(script: PurposeScript, tests: Sequence[str]) -> None:
    script.add(
        "planner",
        {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}},
        {
            "root_cause_hypothesis": "normalize subtracts one unexpectedly",
            "evidence": ["src/pkg/core.py returns value - 1"],
            "changes": [
                {
                    "file": "src/pkg/core.py",
                    "symbol": "pkg.core.normalize",
                    "action": "modify",
                    "description": "return the input unchanged",
                }
            ],
            "tests_to_run": list(tests),
            "risk": "low",
            "alternatives": ["guard the subtraction instead of removing it"],
        },
    )


def diagnosis(
    strategy: str = "refine",
    *,
    is_patch_caused: bool = False,
    suspected: Sequence[str] = (),
    file: str = "src/pkg/core.py",
    line: int = 2,
    text: str = "return value - 1",
) -> dict[str, Any]:
    return {
        "root_cause": "The arithmetic in normalize is off by one and the tests still fail.",
        "evidence": [{"kind": "code", "file": file, "line": line, "text": text}],
        "confidence": 0.8,
        "is_patch_caused": is_patch_caused,
        "repair_strategy": strategy,
        "suspected_symbols": list(suspected),
    }


@dataclass
class Harness:
    """一次 Phase 8 图运行的全部现场。"""

    deps: Deps
    initial: RepairState
    original: Path
    workspace: Path
    base_commit: str
    llm: FakeLLM
    sandbox: FakeSandbox | Any
    events_path: Path
    artifacts_root: Path
    checkpoint_path: Path | None = None
    clock: list[datetime] = field(default_factory=list)

    @property
    def usage(self) -> Usage:
        return self.deps.usage

    def source(self, relative: str = "src/pkg/core.py") -> str:
        return (self.workspace / relative).read_text(encoding="utf-8")

    def original_source(self, relative: str = "src/pkg/core.py") -> str:
        return (self.original / relative).read_text(encoding="utf-8")

    def run(self, *, checkpointer: Any | None = None) -> RepairState:
        from codepilot.graph.builder import run_graph

        if checkpointer is None:
            return run_graph(self.initial, self.deps)
        return run_graph(self.initial, self.deps, checkpointer=checkpointer)

    def close(self) -> None:
        self.deps.close()


def assert_original_clean(harness: Harness) -> None:
    """原始仓库必须分毫不差（03 §28：never modify the user's working tree）。"""
    assert run_git(["status", "--porcelain"], cwd=harness.original).stdout == ""


def build_harness(
    tmp_path: Path,
    *,
    results: Sequence[ExecutionResult],
    coder_rounds: Sequence[Sequence[Reply]],
    diagnoses: Sequence[Reply] = (),
    budget: RepairBudget | None = None,
    tests: Sequence[str] = (TARGET,),
    baseline_results: Sequence[ExecutionResult] | None = None,
    build_status: ExecutionStatus = ExecutionStatus.PASS,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_per_call: float = 0.0,
    now: datetime | None = None,
    offsets: Sequence[float] = (),
    plan_tests: bool = True,
) -> Harness:
    """装配一次可运行的图。

    ``offsets`` 注入**假时钟**：``clock()`` 依次返回 ``now + offsets``，用于触发
    ``budget_time`` 而不必真等 30 分钟。队列耗尽后一直返回最后一个值。
    """
    original, base = _base_repo(tmp_path)
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    workspace = manager.create(source=str(original), base_commit=base, target=tmp_path / "workspace")
    ws_path = workspace.path

    from codepilot.storage.repository import Stores

    factory = _session_factory(tmp_path)
    session = factory()
    stores = Stores(session)
    Indexer().index(stores, workspace)
    index = RepositoryIndex(stores, workspace.repository_id)
    graph = RepoGraph(index)
    symbols = list(index.symbol_objects())
    files = list(index.files())

    script = PurposeScript()
    target_symbol = next(item for item in symbols if item.qualified_name == "pkg.core.normalize")
    target_file = next(item for item in files if item.path == "src/pkg/core.py")
    _localizer_replies(script, target_symbol, target_file)
    if plan_tests:
        _planner_reply(script, tests)
    for round_replies in coder_rounds:
        script.add("coder", *round_replies)
    for item in diagnoses:
        script.add("diagnoser", item)

    llm = FakeLLM(
        default=script.reply,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_per_call=cost_per_call,
    )
    usage = Usage()
    provider = UsageTrackingProvider(llm, usage=usage)

    artifacts_root = tmp_path / "artifacts"
    run_id = "phase8-run"
    artifacts = ArtifactStore(artifacts_root, run_id)
    events_path = artifacts_root / "runs" / run_id / "events.jsonl"
    events = EventSink(run_id=run_id, task_id="fixture/normalize", jsonl_path=events_path)

    # build_env 一定先在 base commit 上跑**两次**全量测试建基线（03 §14），所以
    # 基线结果由夹具显式提供并排在前面，测试脚本里的 ``results`` 只写目标测试。
    # 不自动补的话，FakeSandbox 会在基线那一步就拒绝伪造结果——这正是它应有的行为。
    scripted = list(baseline_results if baseline_results is not None else [passed_result(), passed_result()])
    scripted.extend(results)
    sandbox = FakeSandbox(scripted, build_status=build_status)
    env_spec = EnvSpec(install=("python -m pip install pytest",), env={"PYTHONPATH": "/workspace/src"})
    environment = sandbox.build_env(
        repository=str(original), base_commit=base, workspace=ws_path, env_spec=env_spec
    )
    policy = WorkspacePathPolicy(ws_path)
    test_budget = TestRunBudget(budget.max_test_runs if budget else 12)
    registry = build_tool_registry(
        policy=policy,
        index=index,
        graph=graph,
        base_commit=base,
        sandbox=sandbox,
        environment=environment,
        env_spec=env_spec,
        test_budget=test_budget,
    )
    effective_budget = budget or RepairBudget()
    planner = Planner(provider, registry, policy, index)
    coder = CodingAgent(
        provider,
        registry,
        max_tool_calls=effective_budget.max_tool_calls_per_iteration,
        max_total_tokens=effective_budget.max_total_tokens,
    )
    localizer = Localizer(provider, index, ws_path, _snapshots(artifacts))
    diagnoser = DiagnosisAgent(provider, policy, index=index, artifacts=artifacts)

    started = now or datetime(2026, 10, 14, tzinfo=UTC)
    ticks = list(offsets) or [0.0]

    def clock() -> datetime:
        index_ = min(clock.step, len(ticks) - 1)
        clock.step += 1
        return started + timedelta(seconds=ticks[index_])

    clock.step = 0

    deps = Deps(
        run_id=run_id,
        task_id="fixture/normalize",
        repository=str(original),
        workspace=ws_path,
        base_commit=base,
        index=index,
        repo_graph=graph,
        # 解析器必须知道真实的仓库文件清单，否则 traceback 里没有一帧会被判成
        # "仓库内帧"，FailureSignature 全都会退化成 no_repo_frame，重复失败与
        # 振荡检测就变成在比一堆相同的空壳。
        failure_parser=FailureParser(
            repo_files={item.path for item in files},
            repo_roots=("/workspace",),
        ),
        retriever=lambda issue: retrieve_symbols(
            ws_path,
            symbols,
            issue,
            channel="bm25",
            fusion_method=FusionMethod.WEIGHTED,
            channel_weights={"bm25": 1.0},
            graph=graph,
        ),
        issue_analyzer=IssueAnalyzer(),
        localizer=localizer,
        impact_analyzer=ImpactAnalyzer(index, graph),
        planner=planner,
        coder=coder,
        diagnoser=diagnoser,
        sandbox=sandbox,
        environment=environment,
        env_spec=env_spec,
        validator=PatchValidator(policy, base, index=index),
        tools=registry,
        context_builder=ContextBuilder(workspace_root=ws_path),
        artifacts=artifacts,
        events=events,
        workspace_manager=manager,
        usage=usage,
        clock=clock,
        simulated_llm=True,
    )
    initial = RepairState(
        task_id="fixture/normalize",
        run_id=run_id,
        repository_id=workspace.repository_id,
        base_commit=base,
        issue=ISSUE,
        started_at=started,
        budget=effective_budget,
    )
    return Harness(
        deps=deps,
        initial=initial,
        original=original,
        workspace=ws_path,
        base_commit=base,
        llm=llm,
        sandbox=sandbox,
        events_path=events_path,
        artifacts_root=artifacts_root,
        clock=[started],
    )


# ------------------------------------------------------------------ 索引夹具


def _session_factory(tmp_path: Path) -> Any:
    """每个 tmp_path 一个独立 SQLite 引擎。

    索引用文件库而不是 ``sqlite://`` 内存库：恢复测试要在**子进程**里重建同一个
    索引，内存库换个进程就是空库。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from codepilot.storage.db import create_session_factory, init_db

    key = tmp_path.resolve()
    if key in _ENGINES:
        return _ENGINES[key]
    database = tmp_path / "index.sqlite3"
    engine = create_engine(
        f"sqlite:///{database.as_posix()}",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    init_db(engine)
    factory = create_session_factory(engine)
    _ENGINES[key] = factory
    return factory


_ENGINES: dict[Path, Any] = {}


def _snapshots(artifacts: ArtifactStore) -> Any:
    from codepilot.artifacts import RetrievalSnapshotStore

    return RetrievalSnapshotStore(artifacts)
