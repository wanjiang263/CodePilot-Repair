"""E-12 / E-13：Gold Sentinel、运行时装配隔离与 Import Graph 的工程测试。

这里验的是"推理侧在结构上有没有可能看见答案"，不是模型能力。三条主线：

1. **Import Graph**：推理侧入口（assembly / graph / agents / context / tools）的依赖
   闭包里不得出现评测模块。正控制用真实的 ``src/codepilot`` 图；负控制把违规
   import 真的写进临时包的 AST（直接、间接、相对 import、``__init__`` 再导出、
   ``importlib.import_module`` / ``__import__`` 字面量动态入口各一份），确认能抓到。
2. **运行时装配**：用真实 ``InferenceAssembly`` + 真实 M2 图 + FakeLLM 脚本 +
   规则化 FakeSandbox 跑完一轮（不调真实 LLM、不碰 Docker），再把 Gold Sentinel
   植入评测侧对象与 ``eval/`` 文件，逐面确认 Prompt、InferenceTask、State、Deps、
   ToolRegistry、Artifact、Checkpoint、恢复路径都读不到它。
3. **正向可读性**：Base 源码、Base 已有测试、Repository Index、``get_git_diff``
   仍然可访问。"一律拒绝读文件"也能让上面所有负控制通过，所以必须同时断言这些
   正向路径可用。

宿主限制说明：本会话的 Python 子进程拉起 msys ``sh.exe`` 会被拒
（``NtCreateDirectoryObject ... 0xC0000022``），因此从 Python 里执行
``WorkspaceManager.create`` 的 ``git clone`` 必失败。这里用
:class:`_CopyWorkspaceManager` 做**等价材料化**（把已经 materialize 的自包含仓库
复制成 workspace，``repository_id`` 仍按真实 ``(normalized source, base_commit)``
计算），``InferenceAssembly._assert_isolated`` 照常执行。这是宿主限制下的等价做法，
不是隔离绕过；真实运行仍走 ``WorkspaceManager``。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import make_bench_scripts as gen  # noqa: E402

from codepilot.artifacts import ArtifactStore  # noqa: E402
from codepilot.eval.assembly import InferenceAssembly, ProviderBundle  # noqa: E402
from codepilot.eval.evaluation import GoldEvaluator  # noqa: E402
from codepilot.eval.swebench_isolation import (  # noqa: E402
    DYNAMIC_IMPORT_MARKER,
    GOLD_SENTINEL_PATTERN,
    GOLD_SENTINEL_PREFIX,
    GoldSentinel,
    IsolationViolation,
    assert_inference_import_closure,
    audit_artifact_tree,
    audit_checkpoint,
    audit_git_history,
    audit_inference_request,
    audit_tool_registry,
    audit_workspace,
    build_import_graph,
    dynamic_import_modules,
    find_sentinels,
    import_closure,
    redact_sentinel,
)
from codepilot.eval.synthetic import (  # noqa: E402
    INFERENCE_DENIED_DIRECTORIES,
    EvaluationTask,
    load_fixture,
)
from codepilot.eval.synthetic_build import build_fixture  # noqa: E402
from codepilot.graph.checkpoint import (  # noqa: E402
    checkpoint_state,
    open_checkpointer,
    read_manifest,
)
from codepilot.repo.gitcmd import run_git  # noqa: E402
from codepilot.repo.index import Indexer, RepositoryIndex  # noqa: E402
from codepilot.repo.workspace import (  # noqa: E402
    Workspace,
    normalize_source,
    repository_id,
)
from codepilot.report import RunReport  # noqa: E402
from codepilot.sandbox.base import EnvironmentBuildResult  # noqa: E402
from codepilot.schemas.enums import ExecutionStatus, TestOutcome  # noqa: E402
from codepilot.schemas.execution import ExecutionResult, TestCaseResult  # noqa: E402
from codepilot.schemas.state import RepairState  # noqa: E402
from codepilot.storage.db import (  # noqa: E402
    create_engine_from_url,
    create_session_factory,
    init_db,
)
from codepilot.storage.repository import Stores  # noqa: E402
from codepilot.tools.base import Tool, ToolPermission, WorkspacePathPolicy  # noqa: E402
from codepilot.tools.read_tools import read_file_tool, search_code_tool  # noqa: E402
from codepilot.tools.registry import ToolRegistry  # noqa: E402
from codepilot.tools.write_tools import get_git_diff_tool  # noqa: E402
from support.synth import demo_spec  # noqa: E402

PACKAGE_ROOT = ROOT / "src" / "codepilot"

#: 推理侧入口集合。覆盖契约第 6 节点名的三类（assembly / graph / agents），另加
#: context 与 tools：它们直接消费 Issue 文本与仓库内容，是"答案入口"的另一半。
INFERENCE_ENTRIES: tuple[str, ...] = (
    "codepilot.eval.assembly",
    "codepilot.graph.builder",
    "codepilot.agents.planner",
    "codepilot.agents.coder",
    "codepilot.agents.diagnoser",
    "codepilot.agents.reviewer",
    "codepilot.agents.localizer",
    "codepilot.context.builder",
    "codepilot.tools.registry",
)

#: 推理闭包里**不得出现**的模块。按点分层级匹配：``codepilot.eval.swebench`` 命中
#: 加载器本身与 ``.xxx`` 子模块，但不会误伤 ``codepilot.eval.swebench_task``
#: （Owner A 的推理安全任务类型模块，Lead 明确要求归允许项）。
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "codepilot.eval.swebench",
    "codepilot.eval.swebench_prediction",
    "codepilot.eval.swebench_harness",
    "codepilot.eval.swebench_isolation",
    "codepilot.eval.evaluation",
    "codepilot.sandbox.swebench_env",
)

#: 允许出现在推理闭包里的 eval 模块：Phase 10 既有类型模块（Lead 明确允许）。
ALLOWED_EVAL_MODULES: tuple[str, ...] = (
    "codepilot.eval.synthetic",
    "codepilot.eval.swebench_task",
)

EVAL_FIELD_NAMES = frozenset(
    {"gold_patch", "test_patch", "fail_to_pass", "pass_to_pass", "gold_files", "tests"}
)


# ============================================================ 宿主限制下的材料化


class _CopyWorkspaceManager:
    """测试用等价材料化器：用文件复制代替 ``git clone``。

    宿主沙箱禁止 Python 子进程创建 msys 命名对象，``git clone`` 会拉起 ``sh.exe``
    并失败（``NtCreateObject ... 0xC0000022``）。这里复制已经 materialize 好的
    自包含仓库（仅 base commit、``.git`` 在子树内），``repository_id`` 仍按真实
    的 ``(normalized source, base_commit)`` 计算，因此索引 id 与真实
    ``WorkspaceManager`` 完全一致。``InferenceAssembly._assert_isolated`` 照常检查
    "自包含 + 单一可达提交"，隔离语义不变。
    """

    def create(
        self,
        *,
        source: object,
        base_commit: str,
        target: Path,
        mode: str = "fetch",
    ) -> Workspace:
        del mode
        src = Path(str(source))
        target = Path(target)
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(f"workspace 已存在且非空: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, target)
        normalized = normalize_source(str(src))
        return Workspace(
            path=target,
            source=normalized,
            base_commit=base_commit,
            repository_id=repository_id(normalized, base_commit),
        )


class _RuleSandbox:
    """按规则返回结果的沙箱。

    第一次目标测试返回 FAIL（驱动 diagnose → 第二轮 coder），之后返回 PASS（驱动
    regression → review → finalize）。这样一轮跑图会真实经过
    planner/coder/diagnoser/reviewer 四个角色的 LLM 调用，运行时隔离检查才覆盖到
    "所有模型输入"而不只是第一轮。基线（``tests=()``）返回全部可见测试 PASS，
    否则 ``_authorized_targets`` 会因为"基线里没有这个测试"而拒绝执行目标测试。
    """

    backend = "fake"

    def __init__(self, node_ids: list[str]) -> None:
        self._nodes = node_ids
        self.run_calls: list[tuple[str, ...]] = []
        self._target_calls = 0

    def build_env(self, **kwargs: Any) -> EnvironmentBuildResult:
        del kwargs
        return EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="fake://env",
            image_id="fake-image",
            cache_key="fake-cache",
        )

    def run_tests(self, **kwargs: Any) -> ExecutionResult:
        selected = tuple(kwargs.get("tests") or ())
        self.run_calls.append(selected)
        nodes = selected or tuple(self._nodes)
        failing = False
        if selected:
            self._target_calls += 1
            failing = self._target_calls == 1
        cases = [
            TestCaseResult(
                node_id=node,
                outcome=TestOutcome.FAILED if failing else TestOutcome.PASSED,
                duration=0.01,
            )
            for node in nodes
        ]
        return ExecutionResult(
            status=ExecutionStatus.FAIL if failing else ExecutionStatus.PASS,
            exit_code=1 if failing else 0,
            tests=cases,
            tests_passed=sum(case.outcome is TestOutcome.PASSED for case in cases),
            tests_failed=sum(case.outcome is TestOutcome.FAILED for case in cases),
            duration_seconds=0.01,
            diagnostics={"execution_backend": "fake", "container_verified": False},
        )

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:
        del older_than_seconds
        return []


def _build_index(fixture: Any, repository: Path, work: Path) -> RepositoryIndex:
    """与 ``scripts/make_bench_scripts._index`` 等价，但用复制式 materializer。"""
    work.mkdir(parents=True, exist_ok=True)
    engine = create_engine_from_url(f"sqlite:///{(work / 'index.sqlite3').as_posix()}")
    init_db(engine)
    session = create_session_factory(engine)()
    try:
        stores = Stores(session)
        workspace = _CopyWorkspaceManager().create(
            source=str(repository),
            base_commit=fixture.metadata.base_commit,
            target=work / "workspace",
        )
        Indexer().index(stores, workspace)
        session.commit()
        return RepositoryIndex(stores, workspace.repository_id)
    finally:
        session.close()
        engine.dispose()


async def _read_checkpoint_state(checkpoint_path: Path, run_id: str) -> RepairState | None:
    handle = await open_checkpointer(checkpoint_path, run_id)
    try:
        return await checkpoint_state(handle.saver, run_id)
    finally:
        await handle.close()


@dataclass(frozen=True, slots=True)
class _RuntimeRun:
    """一次真实离线装配的现场快照，供多个测试共享（跑图只做一次）。"""

    root: Path
    sentinel: GoldSentinel
    planted_evaluation: EvaluationTask
    fixture_directory: Path
    task: Any
    outcome: Any
    deps: Any
    llm: Any
    run_dir: Path
    checkpoint_path: Path
    state: RepairState
    report: RunReport
    repository: Path

    @property
    def prompts(self) -> list[str]:
        return list(self.llm.prompt_texts())


@pytest.fixture(scope="module")
def runtime_run(tmp_path_factory: pytest.TempPathFactory) -> _RuntimeRun:
    """跑一轮真实 M2 图（FakeLLM + 规则化 FakeSandbox），并把 sentinel 只放评测侧。

    评测侧植入三处：评测对象的副本、``eval/gold.patch``、``eval/test.patch``。
    推理侧拿到的始终是 ``fixture.inference(...)`` 与由 ``repo/`` 材料化的仓库，
    结构上读不到这些文件——这正是要断言的事。
    """
    root = tmp_path_factory.mktemp("isolation-runtime")
    sentinel = GoldSentinel.generate(run_id="iso-runtime", instance_id="demo-off-by-one")
    fixture_directory = build_fixture(
        demo_spec(task_id="demo-off-by-one"),
        root / "suite",
        scratch_root=root / "scratch",
    )
    fixture = load_fixture(fixture_directory)

    # 评测侧：对象副本 + 文件。两者都必须真的带 token，否则后面的"读不到"是空跑。
    planted = sentinel.plant_in_evaluation_task(fixture.evaluation)
    assert isinstance(planted, EvaluationTask)
    assert sentinel.token in planted.model_dump_json()
    assert sentinel.token not in fixture.evaluation.model_dump_json(), (
        "植入必须是副本，不能污染原对象"
    )
    for name in ("gold.patch", "test.patch"):
        sentinel.plant_in_file(fixture_directory / "eval" / name)

    evaluator = GoldEvaluator(
        manager=_CopyWorkspaceManager(),  # type: ignore[arg-type]
        sandbox_factory=lambda artifacts: None,  # type: ignore[arg-type,return-value]
        work_root=root / "materialize",
    )
    repository = evaluator.materialized_repository(fixture)
    index = _build_index(fixture, repository, root / "index")
    script = gen._build_script(fixture, "correct", index)
    bundle = ProviderBundle.scripted(script)
    node_ids = sorted(
        path.relative_to(repository).as_posix() for path in (repository / "tests").glob("test_*.py")
    )
    sandboxes: list[_RuleSandbox] = []

    def sandbox_factory(_artifacts: ArtifactStore) -> _RuleSandbox:
        sandbox = _RuleSandbox(node_ids)
        sandboxes.append(sandbox)
        return sandbox

    assembly_module = sys.modules["codepilot.eval.assembly"]
    captured: list[Any] = []
    real_deps = assembly_module.Deps

    def capture_deps(*args: Any, **kwargs: Any) -> Any:
        deps = real_deps(*args, **kwargs)
        captured.append(deps)
        return deps

    work_root = root / "work"
    run_id = "iso-runtime-run"
    with mock.patch.object(assembly_module, "Deps", capture_deps):
        assembly = InferenceAssembly(
            task=fixture.inference(repository),
            workspace_manager=_CopyWorkspaceManager(),  # type: ignore[arg-type]
            work_root=work_root,
            sandbox_factory=sandbox_factory,
            provider_bundle=bundle,
            checkpoint=True,
        )
        outcome = assembly.run(run_id=run_id, seed=0)

    assert captured, "Deps 没有被捕获：装配路径没有经过 InferenceAssembly"
    deps = captured[-1]
    run_dir = Path(outcome.artifact_dir)
    checkpoint_path = run_dir / "checkpoints.sqlite"
    state = asyncio.run(_read_checkpoint_state(checkpoint_path, run_id))
    assert state is not None, "checkpoint 里应当能读回最终 State（恢复路径的前提）"
    report = RunReport.from_state(
        state,
        repository=str(fixture.inference(repository).repository),
        git_diff=state.current_patch,
        modified_files=[],
        simulated_llm=True,
    )
    llm = bundle.roles["planner"].inner
    return _RuntimeRun(
        root=root,
        sentinel=sentinel,
        planted_evaluation=planted,
        fixture_directory=fixture_directory,
        task=fixture.inference(repository),
        outcome=outcome,
        deps=deps,
        llm=llm,
        run_dir=run_dir,
        checkpoint_path=checkpoint_path,
        state=state,
        report=report,
        repository=repository,
    )


# =============================================================== Import Graph


def _write_package(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    return root


def test_import_graph_resolves_relative_reexport_and_dynamic_entries(tmp_path: Path) -> None:
    """静态/相对 import、包 ``__init__`` 再导出、字面量动态 import 都能解析出来。"""
    package = _write_package(
        tmp_path / "pkg",
        {
            "__init__.py": "",
            "app.py": (
                "from .helpers import util\n"
                "import pkg.sub.mod\n"
                "import importlib\n"
                "importlib.import_module('pkg.dynamic_target')\n"
            ),
            "helpers.py": "def util() -> int:\n    return 1\n",
            "sub/__init__.py": "from pkg.eval import golden\n",
            "sub/mod.py": "from ..helpers import util\n",
            "eval/__init__.py": "",
            "eval/golden.py": "GOLD = 1\n",
            "dynamic_target.py": "",
        },
    )
    graph = build_import_graph(package)

    assert "pkg.helpers" in graph["pkg.app"], "普通相对 import 必须被解析"
    assert "pkg.sub.mod" in graph["pkg.app"], "普通绝对 import 必须被解析"
    assert "pkg.dynamic_target" in graph["pkg.app"], "importlib.import_module 的字面量参数必须解析"
    assert "pkg.helpers" in graph["pkg.sub.mod"], "``from ..helpers`` 必须按层级解析"
    assert "pkg.eval.golden" in graph["pkg.sub"], "包 __init__ 的再导出必须建模成包节点依赖"

    closure = import_closure(graph, ["pkg.app"])
    assert {"pkg.sub", "pkg.eval.golden"} <= closure, (
        "闭包必须把祖先包节点拉进来：import 任何子模块都会执行包的 __init__"
    )
    with pytest.raises(IsolationViolation, match="import_closure_violation"):
        assert_inference_import_closure(graph, forbidden_prefixes=["pkg.eval"], entries=["pkg.app"])


def test_import_graph_catches_injected_violating_import(tmp_path: Path) -> None:
    """负控制：真的把违规 import 写进 AST（直接 + 间接两条链），必须被抓到。"""
    direct = _write_package(
        tmp_path / "direct" / "pkg",
        {
            "__init__.py": "",
            "inference.py": "from pkg.eval.swebench import SWEBenchEvaluationTask\n",
            "eval/__init__.py": "",
            "eval/swebench.py": "class SWEBenchEvaluationTask:\n    pass\n",
        },
    )
    graph = build_import_graph(direct)
    with pytest.raises(IsolationViolation, match=r"pkg\.eval\.swebench"):
        assert_inference_import_closure(
            graph,
            forbidden_prefixes=["pkg.eval.swebench", "pkg.eval.evaluation"],
            entries=["pkg.inference"],
        )

    indirect = _write_package(
        tmp_path / "indirect" / "pkg",
        {
            "__init__.py": "",
            "inference.py": "from .helper import load_gold\n",
            "helper.py": "from .eval.evaluation import EvaluationTask\n",
            "eval/__init__.py": "",
            "eval/evaluation.py": "class EvaluationTask:\n    pass\n",
        },
    )
    graph = build_import_graph(indirect)
    with pytest.raises(IsolationViolation, match=r"pkg\.eval\.evaluation"):
        assert_inference_import_closure(
            graph, forbidden_prefixes=["pkg.eval.evaluation"], entries=["pkg.inference"]
        )


def test_import_graph_catches_dynamic_imports_and_flags_opaque_ones(tmp_path: Path) -> None:
    """动态 import 入口：字面量按真实依赖检查；非字面量单独记录并支持 strict 模式。"""
    for statement in (
        "import importlib\nimportlib.import_module('pkg.eval.swebench')\n",
        "import importlib\nimportlib.import_module(name='pkg.eval.swebench')\n",
        "__import__('pkg.eval.swebench')\n",
    ):
        package = _write_package(
            tmp_path / f"dyn-{abs(hash(statement))}" / "pkg",
            {
                "__init__.py": "",
                "inference.py": statement,
                "eval/__init__.py": "",
                "eval/swebench.py": "",
            },
        )
        graph = build_import_graph(package)
        with pytest.raises(IsolationViolation, match=r"pkg\.eval\.swebench"):
            assert_inference_import_closure(
                graph, forbidden_prefixes=["pkg.eval.swebench"], entries=["pkg.inference"]
            )

    opaque = _write_package(
        tmp_path / "opaque" / "pkg",
        {
            "__init__.py": "",
            "inference.py": (
                "import importlib\n\n\n"
                "def resolve(name: str) -> object:\n"
                "    return importlib.import_module(name)\n"
            ),
            "eval/__init__.py": "",
            "eval/swebench.py": "",
        },
    )
    graph = build_import_graph(opaque)
    assert DYNAMIC_IMPORT_MARKER in graph["pkg.inference"]
    assert dynamic_import_modules(graph, ["pkg.inference"]) == ("pkg.inference",)
    # 默认不把"无法静态解析"当已证明违规（代码库里有合法的按注解取模块名写法），
    # 但 strict_dynamic=True 时必须 fail closed。
    assert_inference_import_closure(
        graph, forbidden_prefixes=["pkg.eval.swebench"], entries=["pkg.inference"]
    )
    with pytest.raises(IsolationViolation, match="import_closure_dynamic"):
        assert_inference_import_closure(
            graph,
            forbidden_prefixes=["pkg.eval.swebench"],
            entries=["pkg.inference"],
            strict_dynamic=True,
        )


def test_swebench_task_is_allowed_but_loader_is_not() -> None:
    """``codepilot.eval.swebench_task`` 是推理安全模块；加载器/评测模块不是。

    这条同时锁住禁止前缀的匹配规则：点分层级匹配不会把 ``swebench_task`` 误伤。
    Owner A 的模块落地后，这里从"注入边"改为对**真实图**断言。
    """
    graph = build_import_graph(PACKAGE_ROOT)
    assert "codepilot.eval.swebench_task" in graph, (
        "Owner A 的推理安全任务模块应当已落地；缺失说明包结构变了，需要重新确认契约"
    )
    # 它自身也不得引入任何被判违规的模块（"推理安全模块"的定义）。
    assert_inference_import_closure(
        graph,
        forbidden_prefixes=FORBIDDEN_PREFIXES,
        entries=("codepilot.eval.swebench_task",),
    )
    # 把它挂到推理入口闭包里：允许；同一前缀下的加载器则必须被抓。
    injected = {name: set(deps) for name, deps in graph.items()}
    injected["codepilot.eval.assembly"].add("codepilot.eval.swebench_task")
    assert_inference_import_closure(
        injected,
        forbidden_prefixes=FORBIDDEN_PREFIXES,
        entries=("codepilot.eval.assembly",),
    )
    injected["codepilot.eval.assembly"].add("codepilot.eval.swebench")
    with pytest.raises(IsolationViolation, match=r"codepilot\.eval\.swebench"):
        assert_inference_import_closure(
            injected,
            forbidden_prefixes=FORBIDDEN_PREFIXES,
            entries=("codepilot.eval.assembly",),
        )


def test_real_inference_closure_has_no_evaluation_modules() -> None:
    """正控制：真实的推理入口闭包里没有被禁止的评测模块。"""
    graph = build_import_graph(PACKAGE_ROOT)
    missing = [entry for entry in INFERENCE_ENTRIES if entry not in graph]
    assert not missing, f"推理入口模块不在 src/codepilot 里: {missing}"

    assert_inference_import_closure(
        graph, forbidden_prefixes=FORBIDDEN_PREFIXES, entries=INFERENCE_ENTRIES
    )
    closure = import_closure(graph, INFERENCE_ENTRIES)
    violations = sorted(
        module
        for module in closure
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_PREFIXES)
    )
    assert violations == [], f"推理闭包里出现评测模块: {violations}"
    assert "codepilot.eval.synthetic" in closure, "合成类型模块是 Phase 10 既有类型来源，允许出现"

    # 非字面量动态 import 目前只有 checkpoint 的"按注解取模块名"这一处合法用法；
    # 断言它被记录而不是被静默忽略（未来有人新增时这份清单会变，测试会提醒复核）。
    dynamic = dynamic_import_modules(graph, INFERENCE_ENTRIES)
    assert "codepilot.graph.checkpoint" in dynamic, (
        "无法静态解析的动态 import 必须被记录，否则'没抓到'会伪装成'没有'"
    )
    with pytest.raises(IsolationViolation, match="import_closure_dynamic"):
        assert_inference_import_closure(
            graph,
            forbidden_prefixes=FORBIDDEN_PREFIXES,
            entries=INFERENCE_ENTRIES,
            strict_dynamic=True,
        )

    # 注入违规边（不改生产文件）→ 同一个断言必须失败。这是"检查器真的在查"的证据。
    injected = {name: set(deps) for name, deps in graph.items()}
    injected["codepilot.eval.assembly"].add("codepilot.eval.swebench")
    with pytest.raises(IsolationViolation, match="import_closure_violation"):
        assert_inference_import_closure(
            injected, forbidden_prefixes=FORBIDDEN_PREFIXES, entries=INFERENCE_ENTRIES
        )


# ============================================================ Gold Sentinel 本体


def test_gold_sentinel_is_unique_redacted_and_scope_limited(tmp_path: Path) -> None:
    first = GoldSentinel.generate(run_id="run-a", instance_id="inst-1")
    second = GoldSentinel.generate(run_id="run-a", instance_id="inst-1")
    assert first != second, "同身份生成两次也必须不同：token 的唯一性来自随机数"
    for sentinel in (first, second):
        assert sentinel.token.startswith(GOLD_SENTINEL_PREFIX)
        assert GOLD_SENTINEL_PATTERN.fullmatch(sentinel.token) is not None
        assert sentinel.token not in repr(sentinel), "repr 不得泄漏 token"
        assert sentinel.token not in str(sentinel)
        assert redact_sentinel(sentinel.token) != sentinel.token
    assert find_sentinels(f"x {first.token} y {first.token}") == (first.token,)

    with pytest.raises(IsolationViolation, match="gold_sentinel_in_inference_side"):
        first.assert_absent(f"leaked: {first.token}", where="unit")
    first.assert_absent("clean text", where="unit")

    # 只能植入评测侧对象：推理对象没有任何评测字段，误用必须立刻失败。
    fixture = load_fixture(
        build_fixture(
            demo_spec(task_id="sentinel-scope"),
            tmp_path / "suite",
            scratch_root=tmp_path / "scratch",
        )
    )
    inference = fixture.inference(fixture.directory / "repo")
    with pytest.raises(IsolationViolation, match="sentinel_scope"):
        first.plant_in_evaluation_task(inference)
    planted = first.plant_in_evaluation_task(fixture.evaluation)
    assert first.token in planted.model_dump_json()
    assert first.token not in fixture.evaluation.model_dump_json()
    planted_file = first.plant_in_file(tmp_path / "eval" / "gold.patch")
    assert first.token in planted_file.read_text(encoding="utf-8")

    # 异常与事件都必须脱敏：它们会被写进日志与 artifact，原文写进去本身就是泄漏。
    with pytest.raises(IsolationViolation) as caught:
        first.assert_absent(first.token, where="unit")
    assert first.token not in str(caught.value)
    assert first.token not in json.dumps(caught.value.event(), ensure_ascii=False)
    assert redact_sentinel(first.token) in caught.value.detail


def test_audit_inference_request_detects_prompt_leaks() -> None:
    sentinel = GoldSentinel.generate()
    clean = "Fix normalize() so that it returns its argument. Existing tests are in tests/."
    audit_inference_request(clean)  # 不抛即通过

    with pytest.raises(IsolationViolation, match="gold_sentinel_in_inference_side"):
        audit_inference_request(f"here is the answer: {sentinel.token}", where="planner")
    for leaked in (
        "FAIL_TO_PASS: ['tests/test_hidden.py::test_x']",
        "PASS_TO_PASS = []",
        "apply eval/gold.patch first",
        "test_patch: diff --git a/tests/test_hidden.py b/tests/test_hidden.py",
        "codepilot.eval.swebench.SWEBenchEvaluationTask(...)",
    ):
        with pytest.raises(IsolationViolation, match="evaluation_content_in_inference_side"):
            audit_inference_request(leaked, where="coder")


# ============================================================= 运行时装配隔离


def test_runtime_fixture_really_planted_the_sentinel(runtime_run: _RuntimeRun) -> None:
    """先证明哨兵真的在评测侧：否则后面每一条"读不到"都可以是空跑。"""
    assert runtime_run.sentinel.token in runtime_run.planted_evaluation.model_dump_json()
    for name in ("gold.patch", "test.patch"):
        text = (runtime_run.fixture_directory / "eval" / name).read_text(encoding="utf-8")
        assert runtime_run.sentinel.token in text, f"评测侧 {name} 应含 sentinel"
    # 推理侧材料化仓库里没有 eval/，也读不到 token。
    assert not (runtime_run.repository / "eval").exists()
    assert runtime_run.sentinel.token not in json.dumps(
        [str(path) for path in runtime_run.repository.rglob("*")]
    )


def test_runtime_prompts_and_inference_task_are_sentinel_free(runtime_run: _RuntimeRun) -> None:
    sentinel = runtime_run.sentinel
    task = runtime_run.task

    # 1) InferenceTask：字段集合与序列化都不含评测字段/哨兵。
    assert not EVAL_FIELD_NAMES & set(type(task).model_fields)
    assert not hasattr(task, "gold_patch")
    sentinel.assert_absent(task.model_dump_json(), where="InferenceTask")
    audit_inference_request(task.issue, where="InferenceTask.issue")

    # 2) Prompt：一轮图真实经过了 planner/coder/diagnoser/reviewer 四个角色的模型输入。
    prompts = runtime_run.prompts
    assert prompts, "FakeLLM 没有记录任何 prompt，运行时检查会变成空跑"
    purposes = runtime_run.llm.purposes()
    assert {
        "localizer_files",
        "localizer_symbols",
        "planner",
        "coder",
        "diagnoser",
        "reviewer",
    } <= set(purposes), f"本轮没有覆盖到全部角色输入: {sorted(set(purposes))}"
    for index, prompt in enumerate(prompts):
        audit_inference_request(prompt, where=f"llm_request[{index}]")
        sentinel.assert_absent(prompt, where=f"llm_request[{index}]")


def test_runtime_state_deps_and_report_are_sentinel_free(runtime_run: _RuntimeRun) -> None:
    sentinel = runtime_run.sentinel
    state = runtime_run.state

    # State（含恢复路径回读的那一份）
    sentinel.assert_absent(state.model_dump_json(), where="RepairState@checkpoint")
    audit_inference_request(state.model_dump_json(), where="RepairState@checkpoint")
    # Deps 是节点拿到的一切依赖；它的 repr 里不得出现哨兵。
    sentinel.assert_absent(repr(runtime_run.deps), where="Deps")
    sentinel.assert_absent(repr(runtime_run.deps.context_builder), where="ContextBuilder")
    # Report 是最终产物，会被人复制走排查；同样不得夹带。
    sentinel.assert_absent(runtime_run.report.model_dump_json(), where="RunReport")
    audit_inference_request(runtime_run.report.model_dump_json(), where="RunReport")

    # 工具注册表本身（名称/描述/schema）不得夹带；并实际尝试读评测目录。
    audit_tool_registry(runtime_run.deps.tools, sentinel=sentinel)
    assert runtime_run.deps.tools.names(), "工具注册表为空，审计无从谈起"


def test_runtime_workspace_git_artifacts_and_checkpoint_are_clean(
    runtime_run: _RuntimeRun,
) -> None:
    sentinel = runtime_run.sentinel
    workspace = Path(runtime_run.deps.workspace)

    audit_workspace(workspace, sentinel=sentinel)
    audit_git_history(workspace, base_commit=runtime_run.outcome.base_commit)
    assert not (workspace / "eval").exists()
    assert not (workspace / "evaluation").exists()

    audit_artifact_tree(runtime_run.run_dir, sentinel=sentinel)
    audit_checkpoint(runtime_run.run_dir, sentinel=sentinel)
    audit_checkpoint(runtime_run.checkpoint_path, sentinel=sentinel)

    # 恢复路径：checkpoint manifest + workspace 身份记录 + 回读 state 都不得含哨兵。
    manifest = read_manifest(runtime_run.checkpoint_path)
    assert manifest is not None, "带 checkpoint 的运行必须写出 manifest"
    sentinel.assert_absent(json.dumps(manifest, ensure_ascii=False), where="checkpoint_manifest")
    identity = json.loads(
        (runtime_run.run_dir / "workspace_identity.json").read_text(encoding="utf-8")
    )
    sentinel.assert_absent(json.dumps(identity, ensure_ascii=False), where="workspace_identity")
    sentinel.assert_absent(
        (runtime_run.run_dir / "report.json").read_text(encoding="utf-8"), where="report.json"
    )
    sentinel.assert_absent(
        (runtime_run.run_dir / "state_final.json").read_text(encoding="utf-8"),
        where="state_final.json",
    )


def test_runtime_tools_cannot_read_evaluation_content_but_can_read_base(
    runtime_run: _RuntimeRun, tmp_path: Path
) -> None:
    """工具层：评测目录读不到，但 Base 源码/已有测试/索引/get_git_diff 都能用。

    这是"不能靠一律拒绝所有文件读取通过"的那条正向断言。
    """
    sentinel = runtime_run.sentinel
    workspace = tmp_path / "tool-workspace"
    shutil.copytree(runtime_run.repository, workspace)
    # 评测目录在工作区之外（父级），同时工作区内也放一个同名目录：
    # 两个方向都必须被拒——`..` 逃逸与 deny 名单。
    evaluation_dir = tmp_path / "evaluation"
    evaluation_dir.mkdir()
    sentinel.plant_in_file(evaluation_dir / "test.patch")
    (workspace / "eval").mkdir()
    sentinel.plant_in_file(workspace / "eval" / "gold.patch")

    policy = WorkspacePathPolicy(
        workspace,
        deny_directories=WorkspacePathPolicy.DEFAULT_DENY_DIRECTORIES
        | INFERENCE_DENIED_DIRECTORIES,
    )
    base_commit = run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    registry = ToolRegistry(
        [
            read_file_tool(policy),
            search_code_tool(policy),
            get_git_diff_tool(policy, base_commit),
        ]
    )

    # ---- 正向：Base 源码、Base 已有测试、git diff 都可读
    source = registry.execute("read_file", {"path": "src/pkg/core.py"})
    assert source.ok, source.model_dump_json()
    assert "normalize" in str(source.data.get("content"))
    base_test = registry.execute("read_file", {"path": "tests/test_public.py"})
    assert base_test.ok, "Base 已有测试必须保持可读（契约第 6 节明确允许）"
    search = registry.execute("search_code", {"pattern": "normalize"})
    assert search.ok and search.data.get("matches"), "Repository 检索不得因为隔离而失效"
    diff = registry.execute("get_git_diff", {})
    assert diff.ok, diff.model_dump_json()

    # ---- 负向：评测目录、父级目录、绝对路径、反斜杠形态全部拒绝
    for path in (
        "eval/gold.patch",
        "eval/test.patch",
        "../evaluation/test.patch",
        "..\\evaluation\\test.patch",
        "../../evaluation/gold.patch",
        str(evaluation_dir / "test.patch"),
    ):
        response = registry.execute("read_file", {"path": path})
        assert not response.ok, (
            f"read_file 不应读到评测内容: {path} -> {response.model_dump_json()}"
        )
        sentinel.assert_absent(response.model_dump_json(), where=f"tool_response:{path}")
    searched = registry.execute(
        "search_code",
        {"pattern": sentinel.token, "path_glob": "eval/**"},
    )
    assert not searched.data.get("matches"), "搜索不得命中 workspace 内的评测目录"
    sentinel.assert_absent(searched.model_dump_json(), where="search_code")

    # ---- 审计本身：干净的注册表通过；泄漏路径与多出来的工具必须被抓到
    audit_tool_registry(registry, sentinel=sentinel)

    # 泄漏路径 1：拒绝名单被清空，且评测文件内容带 sentinel → 要么被判"读到了评测
    # 内容"，要么（更早）被判"响应里带 sentinel"，两者都说明审计不是空跑。
    leaky = ToolRegistry(
        [read_file_tool(WorkspacePathPolicy(workspace, deny_directories=(), deny_files=()))]
    )
    with pytest.raises(IsolationViolation) as caught:
        audit_tool_registry(leaky, sentinel=sentinel)
    assert caught.value.kind in {"tool_reaches_evaluation", "gold_sentinel_in_inference_side"}

    # 泄漏路径 2：评测文件内容干净（不带 sentinel）时，必须由"读到了评测目录"这一条抓住。
    plain_workspace = tmp_path / "plain-workspace"
    shutil.copytree(runtime_run.repository, plain_workspace)
    (plain_workspace / "eval").mkdir()
    (plain_workspace / "eval" / "gold.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    plain_policy = WorkspacePathPolicy(plain_workspace, deny_directories=(), deny_files=())
    with pytest.raises(IsolationViolation, match="tool_reaches_evaluation"):
        audit_tool_registry(ToolRegistry([read_file_tool(plain_policy)]), sentinel=sentinel)

    extra = ToolRegistry(
        [
            read_file_tool(policy),
            Tool(
                name="read_gold_patch",
                description="read the evaluation answer",
                permission=ToolPermission.READ,
                input_model=read_file_tool(policy).input_model,
                handler=lambda raw: source,
            ),
        ]
    )
    with pytest.raises(IsolationViolation, match="unexpected_tool_registered"):
        audit_tool_registry(extra, sentinel=sentinel)


# ============================================== Workspace / Artifact / Checkpoint 负控制


def test_audit_workspace_rejects_sentinel_links_and_path_escapes(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def normalize(v):\n    return v\n", encoding="utf-8"
    )
    audit_workspace(workspace, sentinel=sentinel)

    # 1) 内容里出现哨兵
    (workspace / "src" / "leak.txt").write_text(sentinel.token, encoding="utf-8")
    with pytest.raises(IsolationViolation, match="gold_sentinel_in_workspace"):
        audit_workspace(workspace, sentinel=sentinel)
    (workspace / "src" / "leak.txt").unlink()

    # 2) 评测目录 / 评测文件名
    (workspace / "eval").mkdir()
    with pytest.raises(IsolationViolation, match="evaluation_directory_in_workspace"):
        audit_workspace(workspace, sentinel=sentinel)
    shutil.rmtree(workspace / "eval")
    (workspace / "gold.patch").write_text("x", encoding="utf-8")
    with pytest.raises(IsolationViolation, match="evaluation_file_in_workspace"):
        audit_workspace(workspace, sentinel=sentinel)
    (workspace / "gold.patch").unlink()

    # 3) 目录链接逃逸（Windows 用 junction；不需要管理员权限）
    outside = tmp_path / "evaluation"
    outside.mkdir()
    sentinel.plant_in_file(outside / "test.patch")
    link = workspace / "linked-eval"
    _make_directory_link(link, outside)
    with pytest.raises(IsolationViolation, match="workspace_link"):
        audit_workspace(workspace, sentinel=sentinel)
    _remove_directory_link(link)

    # 4) 路径本身带 .. 或 UNC
    with pytest.raises(IsolationViolation, match="workspace_path_escape"):
        audit_workspace(Path(str(tmp_path) + "\\..\\workspace"), sentinel=sentinel)
    with pytest.raises(IsolationViolation, match="workspace_unc_path"):
        audit_workspace(Path(r"\\server\share\workspace"), sentinel=sentinel)


def test_audit_git_history_rejects_future_commits_remotes_and_tags(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    repository = tmp_path / "repo"
    repository.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b.invalid",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b.invalid",
    }
    run_git(["init", "-q"], cwd=repository)
    (repository / "core.py").write_text("def normalize(v):\n    return v - 1\n", encoding="utf-8")
    run_git(["add", "-A"], cwd=repository, env=env)
    run_git(["commit", "-qm", "base"], cwd=repository, env=env)
    base = run_git(["rev-parse", "HEAD"], cwd=repository, env=env).stdout.strip()
    audit_git_history(repository, base_commit=base)

    # 未来提交（分支/对象都可达）
    (repository / "core.py").write_text(
        f"def normalize(v):\n    return v  # {sentinel.token}\n", encoding="utf-8"
    )
    run_git(["add", "-A"], cwd=repository, env=env)
    run_git(["commit", "-qm", "gold fix"], cwd=repository, env=env)
    with pytest.raises(IsolationViolation, match=r"git_(ref_outside_base|future_commit_object)"):
        audit_git_history(repository, base_commit=base)

    # 把分支退回去也删掉引用：对象还在对象库里，git show 仍读得到 → 必须被抓到。
    run_git(["reset", "--hard", base], cwd=repository, env=env)
    run_git(["update-ref", "-d", "refs/heads/master"], cwd=repository, env=env, check=False)
    run_git(["update-ref", "-d", "refs/heads/main"], cwd=repository, env=env, check=False)
    with pytest.raises(IsolationViolation, match=r"git_(future_commit_object|reflog_outside_base)"):
        audit_git_history(repository, base_commit=base)

    # remote / tag
    run_git(["remote", "add", "origin", "https://example.invalid/x.git"], cwd=repository, env=env)
    with pytest.raises(IsolationViolation, match="git_remote_present"):
        audit_git_history(repository, base_commit=base)
    run_git(["remote", "remove", "origin"], cwd=repository, env=env)
    run_git(["tag", "v-gold", base], cwd=repository, env=env)
    with pytest.raises(IsolationViolation, match="git_tag_present"):
        audit_git_history(repository, base_commit=base)


def test_audit_artifact_and_checkpoint_catch_planted_sentinel(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    (artifact / "events.jsonl").write_text('{"node": "plan"}\n', encoding="utf-8")
    audit_artifact_tree(artifact, sentinel=sentinel)

    (artifact / "state_final.json").write_text(
        f'{{"issue": "{sentinel.token}"}}\n', encoding="utf-8"
    )
    with pytest.raises(IsolationViolation, match="gold_sentinel_in_artifact"):
        audit_artifact_tree(artifact, sentinel=sentinel)
    (artifact / "state_final.json").unlink()

    (artifact / "gold.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    with pytest.raises(IsolationViolation, match="evaluation_file_in_artifact"):
        audit_artifact_tree(artifact, sentinel=sentinel)
    (artifact / "gold.patch").unlink()

    sentinel.plant_in_file(artifact / "llm_requests.jsonl")
    with pytest.raises(IsolationViolation, match="gold_sentinel_in_artifact"):
        audit_artifact_tree(artifact, sentinel=sentinel)

    # checkpoint：真实 SQLite 复制一份再追加 token（msgpack 里字符串就是 UTF-8 字节，
    # 原始字节扫描不依赖 schema 版本，也不怕 WAL）。恢复路径读回的就是这份文件。
    checkpoint = tmp_path / "checkpoints.sqlite"
    checkpoint.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    audit_checkpoint(checkpoint, sentinel=sentinel)
    checkpoint.write_bytes(checkpoint.read_bytes() + sentinel.token.encode("utf-8"))
    with pytest.raises(IsolationViolation, match="gold_sentinel_in_checkpoint"):
        audit_checkpoint(checkpoint, sentinel=sentinel)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "checkpoint_manifest.json").write_text(
        json.dumps({"run_id": "r", "note": sentinel.token}), encoding="utf-8"
    )
    with pytest.raises(IsolationViolation, match="gold_sentinel_in_checkpoint"):
        audit_checkpoint(run_dir, sentinel=sentinel)

    with pytest.raises(IsolationViolation, match="checkpoint_missing"):
        audit_checkpoint(tmp_path / "does-not-exist.sqlite", sentinel=sentinel)


# ------------------------------------------------------------------- 链接辅助


def _make_directory_link(link: Path, target: Path) -> None:
    """在 Windows 上用 junction、其他平台用 symlink（两者都算重解析点）。"""
    import subprocess

    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"mklink /J 失败: {completed.stdout} {completed.stderr}")
        return
    link.symlink_to(target, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()
