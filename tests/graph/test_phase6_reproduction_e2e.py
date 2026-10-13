"""Phase 6 端到端：Issue → IssueAnalyzer → Localization → ReproductionAgent →
base commit → Sandbox → JUnit → ReproductionResult。

两组后端各测一条完整链路：

* **FakeLLM + FakeSandbox**：状态转移、重试、预算、错误分流、降级、确定性、Report
  兼容。FakeSandbox 的每个结果都带 ``execution_backend=fake``，不会与真实执行混淆。
* **FakeLLM + 真实 DockerSandbox**（``@pytest.mark.docker``）：容器里真正跑出
  AssertionError 的复现，以及一个"复现不出来"的真实无效场景。

Phase 5 的单轮修复主图没有被改动：复现能力是独立入口，未来 REPRODUCE 节点直接消费
:class:`ReproductionResult`。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.localizer import Localizer
from codepilot.agents.reproducer import (
    BaseWorkspaceValidator,
    ReproductionAgent,
    WorkspaceManagerBaseProvider,
)
from codepilot.artifacts import ArtifactStore, RetrievalSnapshotStore
from codepilot.llm.fake import FakeLLM
from codepilot.patch.diff import generate_git_diff
from codepilot.patch.validator import PatchValidator
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, WorkspaceManager, normalize_source
from codepilot.report import RunReport
from codepilot.retrieval.service import retrieve_symbols
from codepilot.sandbox.base import Sandbox
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus, RunStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.reproduction import (
    MAX_REPRO_ATTEMPTS,
    REPRO_TEST_DIRECTORY,
    ReproductionFailureKind,
    ReproductionResult,
    ReproductionStatus,
)
from codepilot.schemas.retrieval import FusionMethod
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.usage import Usage
from codepilot.schemas.validation import ValidationResult
from codepilot.storage.repository import Stores
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.registry import build_tool_registry
from codepilot.tools.write_tools import TestRunBudget as _TestRunBudget
from support.gitfixture import init_repo

DEFECTIVE = "def normalize(value: int) -> int:\n    return value - 1\n"
EXISTING_TEST = "from pkg.core import normalize\n\n\ndef test_normalize() -> None:\n    assert normalize(3) == 3\n"
FIXED = "def normalize(value: int) -> int:\n    return value\n"
ISSUE = "normalize(3) returns 2; the public contract is that it returns the value unchanged."
REPRO_FILE = "test_repro_normalize.py"
REPRO_PATH = f"{REPRO_TEST_DIRECTORY}/{REPRO_FILE}"
REPRO_CONTENT = (
    "from pkg.core import normalize\n\n\ndef test_normalize_returns_the_input() -> None:\n"
    "    assert normalize(3) == 3\n"
)
REPRO_NODE = f"{REPRO_PATH}::test_normalize_returns_the_input"
FALLBACK = ("tests/test_core.py",)


def _env_spec() -> EnvSpec:
    return EnvSpec(
        install=("python -m pip install pytest",),
        env={"PYTHONPATH": "/workspace/src"},
    )


def _fixture(tmp_path: Path) -> tuple[Path, str]:
    """一个真实 git 仓库：源码里有已知缺陷，既有测试因此本来就是红的。"""
    repo = init_repo(
        tmp_path / "original",
        {
            "src/pkg/core.py": DEFECTIVE,
            "tests/test_core.py": EXISTING_TEST,
            "pyproject.toml": "[project]\nname='fixture'\nversion='0.0.1'\n",
        },
    )
    return repo.path, repo.head


def _assertion_failure(node_id: str) -> ExecutionResult:
    return scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id=node_id,
                outcome=TestOutcome.FAILED,
                duration=0.1,
                failure_type="AssertionError",
                message="assert 2 == 3",
                traceback="src/pkg/core.py:2: AssertionError",
            )
        ],
        exit_code=1,
    )


def _passed(node_id: str) -> ExecutionResult:
    return scripted_result(
        ExecutionStatus.PASS,
        tests=[TestCaseResult(node_id=node_id, outcome=TestOutcome.PASSED, duration=0.1)],
    )


def _draft(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "filename": REPRO_FILE,
        "content": REPRO_CONTENT,
        "expected_failure": "AssertionError：normalize(3) 返回 2 而不是 3",
    }
    payload.update(overrides)
    return payload


def _script(index: RepositoryIndex, *, drafts: int = 1) -> list[Any]:
    """Localizer 的两段决策 + ReproductionAgent 的 ``drafts`` 份候选测试。"""
    core = next(item for item in index.files() if item.path == "src/pkg/core.py")
    symbol = next(
        item for item in index.symbol_objects() if item.qualified_name == "pkg.core.normalize"
    )
    return [
        {"files": [{"candidate_id": core.id, "reason": "BM25 top file"}]},
        {
            "symbols": [
                {
                    "candidate_id": symbol.id,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "reason": "the off-by-one body",
                }
            ]
        },
        *[_draft() for _ in range(drafts)],
    ]


class _Chain:
    """装配一条完整的复现链路，并把各环节暴露给断言。"""

    def __init__(
        self,
        tmp_path: Path,
        stores: Stores,
        sandbox: Sandbox,
        *,
        real_docker: bool = False,
        replies: list[Any] | None = None,
        max_retries: int = 3,
        drafts: int = 1,
    ) -> None:
        self.original, self.base = _fixture(tmp_path)
        self.tmp_path = tmp_path
        work_root = tmp_path / "work"
        work_root.mkdir(parents=True, exist_ok=True)

        self.workspace = WorkspaceManager(cache_root=tmp_path / "cache").create(
            source=str(self.original), base_commit=self.base, target=work_root / "repair"
        )
        Indexer().index(stores, self.workspace)
        self.index = RepositoryIndex(stores, self.workspace.repository_id)
        graph = RepoGraph(self.index)
        self.analysis = IssueAnalyzer().analyze(
            ISSUE,
            repo_files=[item.path for item in self.index.files()],
            repo_root=self.workspace.path,
        )
        retrieval = retrieve_symbols(
            self.workspace.path,
            self.index.symbol_objects(),
            ISSUE,
            channel="bm25",
            fusion_method=FusionMethod.WEIGHTED,
            channel_weights={"bm25": 1.0},
            graph=graph,
        )
        self.artifacts = ArtifactStore(tmp_path / "artifacts", "phase6-e2e")
        self.llm = FakeLLM(replies if replies is not None else _script(self.index, drafts=drafts))
        self.localization = Localizer(
            self.llm, self.index, self.workspace.path, RetrievalSnapshotStore(self.artifacts)
        ).localize(self.analysis, retrieval)

        self.sandbox = sandbox
        self.validator = BaseWorkspaceValidator(
            sandbox=sandbox,
            env_spec=_env_spec(),
            provider=WorkspaceManagerBaseProvider(
                WorkspaceManager(cache_root=tmp_path / "base-cache"),
                source=str(self.original),
                base_commit=self.base,
                root=work_root / "base-workspaces",
            ),
            allow_network=real_docker,
        )
        self.policy = WorkspacePathPolicy(self.workspace.path, repro_write=True)
        self.max_retries = max_retries
        self.graph = graph
        self.usage = Usage()

    def agent(self, llm: FakeLLM | None = None) -> ReproductionAgent:
        return ReproductionAgent(
            llm or self.llm,
            policy=self.policy,
            validator=self.validator,
            artifacts=self.artifacts,
            max_retries=self.max_retries,
            usage=self.usage,
        )

    def run(self, **kwargs: Any) -> Any:
        defaults: dict[str, Any] = {
            "issue_text": ISSUE,
            "analysis": self.analysis,
            "localization": self.localization,
            "reference_tests": ["tests/test_core.py"],
            "fallback_tests": FALLBACK,
        }
        defaults.update(kwargs)
        return self.agent().reproduce(**defaults)

    def delivered_patch(self) -> str:
        return generate_git_diff(
            self.workspace.path, self.base, policy=WorkspacePathPolicy(self.workspace.path)
        ).text


def test_fake_llm_and_fake_sandbox_reproduce_the_issue_offline(
    tmp_path: Path, stores: Stores
) -> None:
    sandbox = FakeSandbox([_assertion_failure(REPRO_NODE)])
    chain = _Chain(tmp_path, stores, sandbox)

    outcome = chain.run()

    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert outcome.result.kind is ReproductionFailureKind.VALID_ASSERTION
    assert outcome.result.attempts == 1 and outcome.degraded is False
    assert outcome.target_tests == (REPRO_NODE,)
    assert (chain.workspace.path / REPRO_PATH).read_text(encoding="utf-8") == REPRO_CONTENT
    # 复现测试确实进了沙箱，而且跑的是显式指定的 NodeID。
    assert sandbox.run_calls[0][1] == (REPRO_NODE,)
    assert sandbox.build_calls[0][1] == chain.base
    # 证据可追溯到真实的 JUnit 结局，并且被标成模拟。
    evidence = chain.artifacts.read_json("reproduction/attempt-01-outcome.json")
    assert evidence["execution"]["status"] == "FAIL"
    assert evidence["execution"]["tests"][0]["failure_type"] == "AssertionError"
    assert evidence["execution"]["diagnostics"]["execution_backend"] == "fake"
    # 交付补丁里没有复现目录；原始用户仓库分毫未动。
    assert chain.delivered_patch() == ""
    assert not (chain.original / REPRO_TEST_DIRECTORY).exists()
    assert (chain.original / "src/pkg/core.py").read_text(encoding="utf-8") == DEFECTIVE
    assert chain.llm.remaining == 0


def test_first_attempt_passes_then_the_retry_reproduces(tmp_path: Path, stores: Stores) -> None:
    sandbox = FakeSandbox([_passed(REPRO_NODE), _assertion_failure(REPRO_NODE)])
    chain = _Chain(tmp_path, stores, sandbox, drafts=2)

    outcome = chain.run()

    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert outcome.result.attempts == 2
    assert [item.kind for item in outcome.result.history] == [
        ReproductionFailureKind.INVALID_PASS,
        ReproductionFailureKind.VALID_ASSERTION,
    ]
    # 第二轮的 prompt 带上了第一轮的真实失败证据（脱敏裁剪后）。
    second_prompt = chain.llm.prompt_texts()[-1]
    assert "上一轮的真实失败证据" in second_prompt and "invalid_pass" in second_prompt
    # 每轮都在独立的 base workspace 里跑。
    assert sandbox.run_calls[0][0] != sandbox.run_calls[1][0]


def test_reproducible_chain_is_deterministic_for_the_same_script(
    tmp_path: Path, stores: Stores
) -> None:
    first = _Chain(tmp_path / "first", stores, FakeSandbox([_assertion_failure(REPRO_NODE)])).run()
    second = _Chain(
        tmp_path / "second", stores, FakeSandbox([_assertion_failure(REPRO_NODE)])
    ).run()
    assert first.result.model_dump(mode="json") == second.result.model_dump(mode="json")
    assert first.target_tests == second.target_tests


def test_retries_exhaust_and_degrade_to_the_existing_repo_tests(
    tmp_path: Path, stores: Stores
) -> None:
    sandbox = FakeSandbox([_passed(REPRO_NODE)] * MAX_REPRO_ATTEMPTS)
    chain = _Chain(tmp_path, stores, sandbox, drafts=MAX_REPRO_ATTEMPTS)

    outcome = chain.run()

    assert outcome.result.status is ReproductionStatus.FAILED
    assert outcome.result.attempts == MAX_REPRO_ATTEMPTS
    assert outcome.degraded is True
    assert outcome.target_tests == FALLBACK
    assert outcome.result.test_path is None
    assert not (chain.workspace.path / REPRO_TEST_DIRECTORY).exists()
    assert len(chain.llm.calls) == 2 + MAX_REPRO_ATTEMPTS  # 两段定位 + 每轮一次生成
    assert len(sandbox.run_calls) == MAX_REPRO_ATTEMPTS


def test_structured_output_failure_is_counted_and_never_fakes_success(
    tmp_path: Path, stores: Stores
) -> None:
    sandbox = FakeSandbox([_assertion_failure(REPRO_NODE)])
    chain = _Chain(tmp_path, stores, sandbox, max_retries=1)
    broken = FakeLLM(
        [
            "这不是 JSON",
            "还不是 JSON",
            "仍然不是 JSON",
            {
                "filename": REPRO_FILE,
                "content": REPRO_CONTENT,
                "expected_failure": "AssertionError",
            },
        ]
    )
    outcome = chain.agent(broken).reproduce(
        issue_text=ISSUE,
        analysis=chain.analysis,
        localization=chain.localization,
        fallback_tests=FALLBACK,
    )
    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert outcome.result.history[0].kind is ReproductionFailureKind.LLM_OUTPUT_ERROR
    assert outcome.result.attempts == 2
    assert len(broken.calls) == 4  # 3 次结构化纠正 + 1 次成功生成
    assert len(sandbox.run_calls) == 1


def test_environment_failure_is_not_mistaken_for_a_reproduction_failure(
    tmp_path: Path, stores: Stores
) -> None:
    sandbox = FakeSandbox([scripted_result(ExecutionStatus.ENV_ERROR)])
    chain = _Chain(tmp_path, stores, sandbox)

    outcome = chain.run()

    assert outcome.result.status is ReproductionStatus.ENV_ERROR
    assert outcome.result.kind is ReproductionFailureKind.ENV_ERROR
    assert outcome.result.attempts == 1
    assert len(chain.llm.calls) == 3  # 两段定位 + 一次复现生成，没有多烧预算
    assert outcome.target_tests == FALLBACK


def test_pseudo_reproduction_is_refused_before_the_sandbox_runs(
    tmp_path: Path, stores: Stores
) -> None:
    sandbox = FakeSandbox([_assertion_failure(REPRO_NODE)])
    chain = _Chain(tmp_path, stores, sandbox, max_retries=1)
    scripted = FakeLLM(
        [
            {
                "filename": REPRO_FILE,
                "content": "def test_normalize_returns_the_input() -> None:\n    assert False\n",
                "expected_failure": "无条件失败",
            },
            {
                "filename": REPRO_FILE,
                "content": REPRO_CONTENT,
                "expected_failure": "AssertionError",
            },
        ]
    )
    outcome = chain.agent(scripted).reproduce(
        issue_text=ISSUE,
        analysis=chain.analysis,
        localization=chain.localization,
        fallback_tests=FALLBACK,
    )
    assert outcome.result.history[0].kind is ReproductionFailureKind.INVALID_PSEUDO_REPRODUCTION
    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert len(sandbox.run_calls) == 1


def test_reproduction_result_is_consumable_by_the_future_reproduce_node(
    tmp_path: Path, stores: Stores
) -> None:
    """与 RepairState / RunReport 的兼容面：未来的图节点可以直接写入这个字段。"""
    sandbox = FakeSandbox([_assertion_failure(REPRO_NODE)])
    chain = _Chain(tmp_path, stores, sandbox)
    outcome = chain.run()
    assert isinstance(outcome.result, ReproductionResult)

    state = RepairState(
        task_id="phase6/e2e",
        run_id="phase6-e2e",
        repository_id=chain.workspace.repository_id,
        base_commit=chain.base,
        issue=ISSUE,
        started_at=datetime(2026, 10, 13, tzinfo=UTC),
        issue_analysis=chain.analysis,
        localization=chain.localization,
        reproduction=outcome.result,
        attempts=[
            Attempt(
                iteration=1,
                validation=ValidationResult(
                    passed=True, files_changed=1, lines_added=1, lines_deleted=1
                ),
                execution=_passed(REPRO_NODE),
            )
        ],
        status=RunStatus.PROVISIONALLY_VERIFIED,
    )
    restored = RepairState.model_validate_json(state.model_dump_json())
    assert restored.reproduction == outcome.result
    assert restored.reproduction is not None and restored.reproduction.test_path == REPRO_PATH

    patch = generate_git_diff(
        chain.workspace.path, chain.base, policy=WorkspacePathPolicy(chain.workspace.path)
    )
    report = RunReport.from_state(
        restored,
        repository=str(chain.original),
        git_diff=patch.text,
        modified_files=patch.files,
        simulated_llm=True,
    )
    assert REPRO_TEST_DIRECTORY not in report.git_diff
    assert report.modified_files == []
    assert report.simulated_sandbox is True
    rendered = report.render_markdown()
    assert "FakeLLM=True" in rendered
    # 复现证据仍然可以从 state 里被报告引用。
    assert restored.reproduction is not None
    assert "AssertionError" in restored.reproduction.fail_reason


def test_phase5_single_patch_loop_still_works_with_the_reproduction_primitives(
    tmp_path: Path, stores: Stores
) -> None:
    """回归护栏：复现能力不能改动 Phase 5 的交付补丁语义与主图行为。"""
    from codepilot.agents.coder import CodingAgent
    from codepilot.agents.impact_analyzer import ImpactAnalyzer
    from codepilot.agents.planner import Planner
    from codepilot.graph.minimal import MinimalDependencies, run_minimal_graph
    from codepilot.llm.tracking import UsageTrackingProvider

    original, base = _fixture(tmp_path)
    workspace_root = tmp_path / "repair"
    shutil.copytree(original, workspace_root)
    workspace = Workspace(
        path=workspace_root,
        source=normalize_source(str(original)),
        base_commit=base,
        repository_id="phase6_compat_repo",
    )
    Indexer().index(stores, workspace)
    index = RepositoryIndex(stores, workspace.repository_id)
    graph = RepoGraph(index)
    usage = Usage()
    llm = UsageTrackingProvider(FakeLLM(_patch_only_script(index)), usage=usage)
    artifacts = ArtifactStore(tmp_path / "artifacts", "phase5-compat")
    sandbox = FakeSandbox([_passed("tests/test_core.py::test_normalize")])
    env_spec = _env_spec()
    environment = sandbox.build_env(
        repository=str(original),
        base_commit=base,
        workspace=workspace_root,
        env_spec=env_spec,
    )
    policy = WorkspacePathPolicy(workspace_root)
    registry = build_tool_registry(
        policy=policy,
        index=index,
        graph=graph,
        base_commit=base,
        sandbox=sandbox,
        environment=environment,
        env_spec=env_spec,
        test_budget=_TestRunBudget(6),
    )
    retrieval = retrieve_symbols(
        workspace_root,
        index.symbol_objects(),
        ISSUE,
        channel="bm25",
        fusion_method=FusionMethod.WEIGHTED,
        channel_weights={"bm25": 1.0},
        graph=graph,
    )

    deps = MinimalDependencies(
        repository=str(original),
        workspace=workspace_root,
        index=index,
        graph=graph,
        issue_analyzer=IssueAnalyzer(),
        retriever=lambda issue: retrieval,
        localizer=Localizer(llm, index, workspace_root, RetrievalSnapshotStore(artifacts)),
        impact_analyzer=ImpactAnalyzer(index, graph),
        planner=Planner(llm, registry, policy, index),
        coder=CodingAgent(llm, registry),
        validator=PatchValidator(policy, base, index=index),
        sandbox=sandbox,
        environment=environment,
        env_spec=env_spec,
        artifacts=artifacts,
        usage=usage,
        simulated_llm=True,
    )
    state = RepairState(
        task_id="phase6/compat",
        run_id="phase5-compat",
        repository_id=workspace.repository_id,
        base_commit=base,
        issue=ISSUE,
        started_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    result = run_minimal_graph(state, deps)

    assert result.status is RunStatus.PROVISIONALLY_VERIFIED, result.model_dump_json(indent=2)
    assert (workspace_root / "src/pkg/core.py").read_text(encoding="utf-8") == FIXED
    assert result.current_patch and REPRO_TEST_DIRECTORY not in result.current_patch
    assert result.reproduction is None  # Phase 5 主图没有被改写
    assert (original / "src/pkg/core.py").read_text(encoding="utf-8") == DEFECTIVE


def _patch_only_script(index: RepositoryIndex) -> list[Any]:
    """Phase 5 主图需要的 6 次调用：定位两段 + 计划 + 读文件 + 编辑 + finish。"""
    core = next(item for item in index.files() if item.path == "src/pkg/core.py")
    symbol = next(
        item for item in index.symbol_objects() if item.qualified_name == "pkg.core.normalize"
    )
    return [
        {"files": [{"candidate_id": core.id, "reason": "top file"}]},
        {
            "symbols": [
                {
                    "candidate_id": symbol.id,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "reason": "target symbol",
                }
            ]
        },
        {
            "root_cause_hypothesis": "normalize subtracts one",
            "evidence": ["src/pkg/core.py returns value - 1"],
            "changes": [
                {
                    "file": "src/pkg/core.py",
                    "symbol": "pkg.core.normalize",
                    "action": "modify",
                    "description": "return the input unchanged",
                }
            ],
            "tests_to_run": ["tests/test_core.py"],
            "risk": "low",
            "alternatives": ["adjust the arithmetic only"],
        },
        {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}},
        {
            "tool": "edit_file",
            "arguments": {
                "path": "src/pkg/core.py",
                "search": "    return value - 1",
                "replace": "    return value",
            },
        },
        {"tool": "finish", "arguments": {"summary": "patched normalize"}},
    ]


@pytest.mark.docker
def test_real_docker_reproduces_the_defect_on_the_base_commit(
    tmp_path: Path, stores: Stores
) -> None:
    from codepilot.repo.gitcmd import run_git

    sandbox = DockerSandbox(
        artifacts=ArtifactStore(tmp_path / "docker-artifacts", "phase6-docker"),
        allowed_workspace_root=tmp_path,
    )
    chain = _Chain(tmp_path, stores, sandbox, real_docker=True)

    outcome = chain.run()

    assert outcome.result.status is ReproductionStatus.REPRODUCED, outcome.result.model_dump_json(
        indent=2
    )
    attempt = outcome.result.history[-1]
    assert attempt.kind in {
        ReproductionFailureKind.VALID_ASSERTION,
        ReproductionFailureKind.VALID_ISSUE_EXCEPTION,
    }
    assert attempt.failure_type == "AssertionError"
    assert attempt.node_id == REPRO_NODE
    assert attempt.exit_code == 1
    evidence = chain.artifacts.read_json("reproduction/attempt-01-outcome.json")
    assert evidence["execution"]["diagnostics"]["execution_backend"] == "docker"
    assert evidence["execution"]["diagnostics"]["container_verified"] is True
    assert (chain.workspace.path / REPRO_PATH).is_file()

    patch = generate_git_diff(
        chain.workspace.path, chain.base, policy=WorkspacePathPolicy(chain.workspace.path)
    )
    assert patch.text == ""
    assert patch.excluded_files == [REPRO_PATH]
    assert run_git(["status", "--porcelain"], cwd=chain.original).stdout == ""
    assert not (chain.original / REPRO_TEST_DIRECTORY).exists()


@pytest.mark.docker
def test_real_docker_reports_an_unreproducible_issue_and_degrades(
    tmp_path: Path, stores: Stores
) -> None:
    """真实容器里跑一个与 Issue 无关、在 base 上就通过的测试：必须判无效并降级。"""
    from codepilot.repo.gitcmd import run_git

    sandbox = DockerSandbox(
        artifacts=ArtifactStore(tmp_path / "docker-artifacts", "phase6-docker-invalid"),
        allowed_workspace_root=tmp_path,
    )
    chain = _Chain(tmp_path, stores, sandbox, real_docker=True, max_retries=0)
    scripted = FakeLLM(
        [
            {
                "filename": "test_repro_environment_probe.py",
                "content": "def test_arithmetic_never_fails() -> None:\n    assert 1 + 1 == 2\n",
                "expected_failure": "不该出现失败：这个测试与 Issue 无关",
            },
        ]
    )
    outcome = chain.agent(scripted).reproduce(
        issue_text=ISSUE,
        analysis=chain.analysis,
        localization=chain.localization,
        fallback_tests=FALLBACK,
    )

    assert outcome.result.status is ReproductionStatus.FAILED, outcome.result.model_dump_json(
        indent=2
    )
    assert outcome.result.attempts == 1
    assert outcome.result.kind is ReproductionFailureKind.INVALID_PASS
    assert outcome.degraded is True
    assert outcome.target_tests == FALLBACK
    assert not (chain.workspace.path / REPRO_TEST_DIRECTORY).exists()
    assert run_git(["status", "--porcelain"], cwd=chain.original).stdout == ""
