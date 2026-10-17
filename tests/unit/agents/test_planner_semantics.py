"""Planner 计划语义校验（Harness Hardening 修复一 / 修复二）。

这些用例的存在理由是一条真实事故：GLM 往 ``tests_to_run`` 里写了
``planned:tests/test_ledger.py::test_new``，旧校验器把它当成"待创建文件"去解析，
撞上已存在的 ``tests/test_ledger.py`` 抛 ``PathAccessError("already_exists")``，
整次真实实验（7 次 HTTP 200、¥0.1334616）就以 ``workflow_error`` 收场，并且被记成
"模型没修好"。

脱敏后的复现材料来自那次真实输出（``artifacts/phase10/glm-live-m2-cda534ca9b4b``
的 ``error_details`` 与 run 现场），只保留字段形态，不含密钥与源码正文。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codepilot.agents.planner import Planner
from codepilot.errors import ErrorOrigin, FailureClass, PlanSemanticError, classify_exception
from codepilot.llm.errors import StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.llm.structured import STRUCTURED_MAX_ATTEMPTS
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.schemas.enums import RiskLevel
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import (
    FileDecision,
    LocalizationResult,
    SymbolDecision,
)
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.retrieval import RankedFile, RankedSymbol
from codepilot.storage.repository import Stores
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.read_tools import find_symbol_tool, read_file_tool
from codepilot.tools.registry import ToolRegistry

SOURCE = '''"""Ledger under test."""


class Ledger:
    total = 0

    def add(self, amount):
        self.total += amount
        return self
'''

TESTS = """from src.budget.ledger import Ledger


def test_add_positive():
    assert Ledger().add(2).total == 2


def test_add_negative():
    assert Ledger().add(-1).total == -1
"""

#: 首次真实 GLM 返回的那一条：形状合法、语义上是"我打算写一个还不存在的测试"。
GLM_LIVE_PLANNED_TARGET = "planned:tests/test_ledger.py::test_new"

VALID_TARGET = "tests/test_ledger.py::test_add_positive"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "budget").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "private-eval").mkdir(parents=True)
    (root / "src" / "budget" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "budget" / "ledger.py").write_text(SOURCE, encoding="utf-8")
    (root / "tests" / "test_ledger.py").write_text(TESTS, encoding="utf-8")
    (root / "private-eval" / "hidden_answers.py").write_text("ANSWER = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def built(repo: Path, stores: Stores) -> tuple[RepositoryIndex, LocalizationResult]:
    workspace = Workspace(
        path=repo,
        source=normalize_source(str(repo)),
        base_commit="0" * 40,
        repository_id="glm_live_repo",
    )
    Indexer().index(stores, workspace)
    index = RepositoryIndex(stores, workspace.repository_id)
    RepoGraph(index)
    symbol = next(
        item for item in index.symbol_objects() if item.qualified_name.endswith("Ledger.add")
    )
    localization = LocalizationResult(
        files=[RankedFile(path=symbol.path, score=1.0, top_symbols=[symbol.qualified_name])],
        symbols=[
            RankedSymbol(
                symbol_id=symbol.id,
                qualified_name=symbol.qualified_name,
                file=symbol.path,
                score=1.0,
                kind=symbol.kind,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                reason="issue names Ledger.add",
            )
        ],
        file_decisions=[
            FileDecision(candidate_id="f1", path=symbol.path, reason="named in the issue")
        ],
        symbol_decisions=[
            SymbolDecision(
                symbol_id=symbol.id,
                file=symbol.path,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                file_line_count=len((repo / symbol.path).read_text(encoding="utf-8").splitlines()),
                reason="exact indexed symbol",
            )
        ],
        retrieval_snapshot_id="retrieval-sha256-" + "0" * 64,
    )
    return index, localization


def _impact() -> ImpactAnalysis:
    return ImpactAnalysis(
        changed_symbols=["budget.ledger.Ledger.add"],
        related_tests=[VALID_TARGET],
        risk=RiskLevel.LOW,
        risk_estimated=False,
    )


def _plan_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "root_cause_hypothesis": "Ledger.add accepts a non-positive amount",
        "evidence": ["src/budget/ledger.py"],
        "changes": [
            {
                "file": "src/budget/ledger.py",
                "action": "modify",
                "description": "reject amounts that are not positive",
            }
        ],
        "tests_to_run": [VALID_TARGET],
        "risk": "low",
        "alternatives": ["raise instead of returning unchanged"],
    }
    payload.update(overrides)
    return payload


def _policy(repo: Path, *, allow_new_tests: bool = False) -> WorkspacePathPolicy:
    return WorkspacePathPolicy(repo, allow_new_tests=allow_new_tests)


def _planner(
    repo: Path,
    built: tuple[RepositoryIndex, LocalizationResult],
    replies: list[Any],
    *,
    allow_new_tests: bool = False,
    **kwargs: Any,
) -> tuple[Planner, FakeLLM]:
    index, _ = built
    policy = _policy(repo, allow_new_tests=allow_new_tests)
    registry = ToolRegistry([read_file_tool(policy), find_symbol_tool(index)])
    llm = FakeLLM(replies)
    return Planner(llm, registry, policy, index, **kwargs), llm


def _call(planner: Planner, built: Any, issue_text: str = "non-positive amounts are accepted"):
    _, localization = built
    return planner.plan(
        issue_text=issue_text,
        issue_analysis=IssueAnalysis(summary=issue_text),
        localization=localization,
        impact=_impact(),
    )


def _validate(repo: Path, built: Any, payload: dict[str, Any]) -> list[str]:
    """直接跑一次语义校验，返回字段错误码。"""
    index, localization = built
    planner = Planner(FakeLLM([]), ToolRegistry([]), _policy(repo), index)
    plan = RepairPlan.model_validate(payload)
    with pytest.raises(PlanSemanticError) as raised:
        planner.validate_plan(plan, localization, _impact())
    return [item.code for item in raised.value.field_errors]


# ------------------------------------------------------------------ 合法边界


def test_existing_test_path_and_nodeid_are_accepted(repo: Path, built: Any) -> None:
    planner, _ = _planner(repo, built, [_plan_payload()])
    assert _call(planner, built).tests_to_run == [VALID_TARGET]

    second, _ = _planner(repo, built, [_plan_payload(tests_to_run=["tests/test_ledger.py"])])
    assert _call(second, built).tests_to_run == ["tests/test_ledger.py"]


def test_file_exists_but_function_does_not_is_rejected(repo: Path, built: Any) -> None:
    """同一个"我打算写"的意图，只是没写前缀——也不能当成可执行目标。"""
    assert _validate(
        repo, built, _plan_payload(tests_to_run=["tests/test_ledger.py::test_new"])
    ) == ["test_case_not_found"]


def test_parametrized_suffix_is_not_static_checked(repo: Path, built: Any) -> None:
    """参数化 id 在收集期生成，静态核对不了；误判会拒掉合法计划。"""
    (repo / "tests" / "test_param.py").write_text(
        "import pytest\n\n\n@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_add(n):\n    assert n\n",
        encoding="utf-8",
    )
    planner, _ = _planner(
        repo, built, [_plan_payload(tests_to_run=["tests/test_param.py::test_add[1]"])]
    )
    assert _call(planner, built).tests_to_run == ["tests/test_param.py::test_add[1]"]


# ------------------------------------------------------------------ planned: 标记


def test_planned_marker_never_touches_file_creation(repo: Path, built: Any) -> None:
    """修复一的验收点：既不执行创建校验，也不抛 PathAccessError。"""
    codes = _validate(repo, built, _plan_payload(tests_to_run=[GLM_LIVE_PLANNED_TARGET]))
    assert codes == ["intent_marker_in_tests_to_run"]
    assert "already_exists" not in codes


def test_planned_marker_towards_missing_file_is_also_a_marker_error(repo: Path, built: Any) -> None:
    codes = _validate(
        repo, built, _plan_payload(tests_to_run=["planned:tests/test_brand_new.py::test_new"])
    )
    assert codes == ["intent_marker_in_tests_to_run"]


def test_marker_stripping_is_not_a_fix(repo: Path, built: Any) -> None:
    """把前缀删掉就放行等于承认模型可以自造待创建目标。"""
    assert _validate(
        repo, built, _plan_payload(tests_to_run=["tests/test_ledger.py::test_new"])
    ) == ["test_case_not_found"]


def test_correction_loop_accepts_a_fixed_plan(repo: Path, built: Any) -> None:
    """真实失败场景的正解：第一次给 ``planned:``，纠正后给出既有测试。"""
    planner, llm = _planner(
        repo,
        built,
        [
            _plan_payload(tests_to_run=[GLM_LIVE_PLANNED_TARGET]),
            _plan_payload(tests_to_run=[VALID_TARGET]),
        ],
    )
    plan = _call(planner, built)
    assert plan.tests_to_run == [VALID_TARGET]
    assert planner.last_plan_corrections == 1
    assert planner.last_semantic_rejections == 1
    assert llm.remaining == 0, "纠正一次就够，不该把队列用光"

    prompts = llm.prompt_texts()
    assert len(prompts) == 2
    correction = prompts[1]
    assert "intent_marker_in_tests_to_run" in correction
    assert "planned_new_tests" in correction
    assert "已存在" in correction, "纠正反馈要讲清 tests_to_run 与新增测试的区别"


def test_system_prompt_states_the_test_contract(repo: Path, built: Any) -> None:
    """模型必须一开始就知道两个字段各是什么意思，否则纠正环只是在补课时。"""
    planner, llm = _planner(repo, built, [_plan_payload()])
    _call(planner, built)
    prompt = llm.prompt_texts()[0]
    assert "planned_new_tests" in prompt
    assert "already exist" in prompt
    assert "cannot grant yourself permission" in prompt


# ------------------------------------------------------------------ 新增测试的授权


def test_new_test_without_trusted_authorization_is_rejected(repo: Path, built: Any) -> None:
    codes = _validate(
        repo,
        built,
        _plan_payload(
            tests_to_run=[VALID_TARGET], planned_new_tests=["tests/test_extra.py::test_new"]
        ),
    )
    assert codes == ["unauthorized_new_test"]


def test_new_test_with_trusted_authorization_is_accepted(repo: Path, built: Any) -> None:
    planner, _ = _planner(
        repo,
        built,
        [_plan_payload(planned_new_tests=["tests/test_extra.py::test_new"])],
        allow_new_tests=True,
    )
    plan = _call(planner, built)
    assert plan.planned_new_tests == ["tests/test_extra.py::test_new"]
    # 计划新增的测试**不会**变成可执行目标。
    assert plan.tests_to_run == [VALID_TARGET]


def test_new_test_alone_cannot_be_the_only_evidence(repo: Path, built: Any) -> None:
    codes = _validate(
        repo,
        built,
        _plan_payload(tests_to_run=[], planned_new_tests=["tests/test_extra.py::test_new"]),
    )
    assert codes == ["unauthorized_new_test", "new_tests_are_not_sole_evidence"]


def test_existing_test_file_is_protected_even_when_authorized(repo: Path, built: Any) -> None:
    """可信授权只允许**新文件**；已有测试文件仍是受保护的。"""
    index, localization = built
    policy = _policy(repo, allow_new_tests=True)
    planner = Planner(FakeLLM([]), ToolRegistry([]), policy, index)
    plan = RepairPlan.model_validate(
        _plan_payload(tests_to_run=[VALID_TARGET], planned_new_tests=["tests/test_ledger.py::x"])
    )
    with pytest.raises(PlanSemanticError) as raised:
        planner.validate_plan(plan, localization, _impact())
    assert [item.code for item in raised.value.field_errors] == ["existing_test_is_protected"]


# ------------------------------------------------------------------ 越界与越权


def test_path_traversal_is_rejected(repo: Path, built: Any) -> None:
    assert _validate(repo, built, _plan_payload(tests_to_run=["../outside/test_x.py"])) == [
        "path_escape"
    ]


def test_symlink_escape_is_rejected(repo: Path, built: Any) -> None:
    outside = repo.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "test_leak.py").write_text("def test_leak():\n    assert True\n", encoding="utf-8")
    try:
        (repo / "tests" / "test_leak.py").symlink_to(outside / "test_leak.py")
    except (OSError, NotImplementedError):  # pragma: no cover - 无符号链接权限的环境
        pytest.skip("该环境不允许创建符号链接")
    codes = _validate(repo, built, _plan_payload(tests_to_run=["tests/test_leak.py"]))
    assert codes == ["path_escape"]


def test_hidden_evaluation_path_is_rejected(repo: Path, built: Any) -> None:
    codes = _validate(repo, built, _plan_payload(tests_to_run=["private-eval/hidden_answers.py"]))
    assert codes == ["forbidden_path"]


def test_duplicate_and_non_test_targets_are_rejected(repo: Path, built: Any) -> None:
    codes = _validate(
        repo,
        built,
        _plan_payload(tests_to_run=[VALID_TARGET, VALID_TARGET, "src/budget/ledger.py"]),
    )
    assert codes == ["duplicate_target", "invalid_test_target"]


def test_risk_cannot_understate_the_impact_analysis(repo: Path, built: Any) -> None:
    index, localization = built
    planner = Planner(FakeLLM([]), ToolRegistry([]), _policy(repo), index)
    plan = RepairPlan.model_validate(_plan_payload(risk="low"))
    with pytest.raises(PlanSemanticError) as raised:
        planner.validate_plan(
            plan, localization, _impact().model_copy(update={"risk": RiskLevel.HIGH})
        )
    assert [item.code for item in raised.value.field_errors] == ["risk_understated"]


# ------------------------------------------------------------------ 归因与预算


def test_correction_budget_exhaustion_is_a_model_failure_not_a_crash(
    repo: Path, built: Any
) -> None:
    planner, llm = _planner(
        repo,
        built,
        [_plan_payload(tests_to_run=[GLM_LIVE_PLANNED_TARGET]) for _ in range(10)],
    )
    with pytest.raises(StructuredOutputError) as raised:
        _call(planner, built)

    failure = classify_exception(raised.value, node="plan")
    assert failure.failure_class is FailureClass.PLAN_SEMANTIC_INVALID
    assert failure.origin is ErrorOrigin.MODEL
    assert not failure.is_harness_defect
    # 封顶 = Schema 环 3 次 + 语义纠正 2 次 = 5 次模型请求；多一次都没有。
    assert STRUCTURED_MAX_ATTEMPTS + planner.max_semantic_repairs == 5
    assert len(llm.calls) == 5
    assert planner.last_semantic_rejections == 5


def test_model_that_never_fixes_stops_without_fake_fallback(repo: Path, built: Any) -> None:
    """纠正不动就终止当前任务，绝不静默换个"能过"的模型。"""
    planner, llm = _planner(
        repo,
        built,
        [_plan_payload(tests_to_run=[GLM_LIVE_PLANNED_TARGET]) for _ in range(10)],
        max_semantic_repairs=1,
    )
    with pytest.raises(StructuredOutputError):
        _call(planner, built)
    # 收缩纠正预算后请求数跟着减少：预算是真的在起作用，不是摆设。
    assert len(llm.calls) == STRUCTURED_MAX_ATTEMPTS + 1
    assert llm.remaining == 10 - (STRUCTURED_MAX_ATTEMPTS + 1)


def test_no_path_access_error_escapes_planning(repo: Path, built: Any) -> None:
    """本轮的核心断言：Planner 不再把 PathAccessError 抛到节点边界。"""
    from codepilot.tools.base import PathAccessError

    planner, _ = _planner(
        repo,
        built,
        [
            _plan_payload(tests_to_run=[GLM_LIVE_PLANNED_TARGET]),
            _plan_payload(tests_to_run=[VALID_TARGET]),
        ],
    )
    try:
        plan = _call(planner, built)
    except PathAccessError as exc:  # pragma: no cover - 修复前正是这条路径
        pytest.fail(f"PathAccessError 仍然逃出 Planner：{exc}")
    assert plan.tests_to_run == [VALID_TARGET]


# ------------------------------------------------------- 变更条目与计划新增测试


def test_add_action_on_existing_file_is_rejected(repo: Path, built: Any) -> None:
    """``action=add`` 只能用于还不存在的文件；这是旧 ``planned:`` 误判的来源之一。"""
    assert _validate(
        repo,
        built,
        _plan_payload(
            changes=[
                {
                    "file": "src/budget/ledger.py",
                    "action": "add",
                    "description": "rewrite the module",
                }
            ]
        ),
    ) == ["file_exists"]


def test_add_action_outside_the_workspace_never_reaches_validation(repo: Path, built: Any) -> None:
    """越界路径在 Pydantic 层就被拒，不需要等到语义校验。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RepairPlan.model_validate(
            _plan_payload(
                changes=[{"file": "../outside/new.py", "action": "add", "description": "x"}]
            )
        )


def test_add_action_on_a_hidden_path_is_denied_not_created(repo: Path, built: Any) -> None:
    codes = _validate(
        repo,
        built,
        _plan_payload(
            changes=[
                {
                    "file": "private-eval/answers_new.py",
                    "action": "add",
                    "description": "peek at the answers",
                }
            ]
        ),
    )
    assert codes == ["forbidden_path"]


def test_add_action_can_plan_a_new_source_file(repo: Path, built: Any) -> None:
    """新增**源码**文件不受测试授权约束：计划合法，交付仍由工具层与 diff 层把关。"""
    planner, _ = _planner(
        repo,
        built,
        [
            _plan_payload(
                changes=[
                    {
                        "file": "src/budget/validation.py",
                        "action": "add",
                        "description": "new amount validator",
                    }
                ]
            )
        ],
    )
    assert _call(planner, built).changes[0].action.value == "add"


def test_planned_new_tests_rejects_non_test_and_escaping_paths(repo: Path, built: Any) -> None:
    """计划新增的测试同样受 workspace 边界与 denylist 约束。"""
    codes = _validate(
        repo,
        built,
        _plan_payload(
            planned_new_tests=[
                "src/budget/checks.py::test_new",
                "private-eval/test_new.py::test_new",
            ]
        ),
    )
    assert codes == ["not_a_test_path", "forbidden_path"]


def test_planned_new_tests_rejects_escaping_relative_paths(repo: Path, built: Any) -> None:
    codes = _validate(
        repo,
        built,
        _plan_payload(planned_new_tests=["../outside/test_x.py::test_new"]),
    )
    assert codes == ["path_escape"]


def test_planned_new_tests_rejects_a_redundant_marker(repo: Path, built: Any) -> None:
    codes = _validate(
        repo,
        built,
        _plan_payload(planned_new_tests=["planned:tests/test_extra.py::test_new"]),
    )
    assert codes == ["intent_marker_in_planned_new_tests"]


def test_marker_variants_are_all_recognised(repo: Path, built: Any) -> None:
    for marker in ("planned:", "planned：", "todo:", "new:", "to_add:"):
        codes = _validate(
            repo, built, _plan_payload(tests_to_run=[f"{marker}tests/test_ledger.py::test_x"])
        )
        assert codes == ["intent_marker_in_tests_to_run"], marker


def test_unindexed_change_file_and_symbol_mismatch_are_reported_together(
    repo: Path, built: Any
) -> None:
    codes = _validate(
        repo,
        built,
        _plan_payload(
            changes=[
                {
                    "file": "tests/test_ledger.py",
                    "symbol": "budget.ledger.Ledger.add",
                    "action": "modify",
                    "description": "touch a protected test",
                }
            ]
        ),
    )
    assert "not_localized" in codes
    assert "symbol_mismatch" in codes
