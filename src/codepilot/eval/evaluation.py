"""评测侧执行器：在真实沙箱里判定"某个补丁是否解决了这个 fixture"（E-02 / E-08 共用）。

同一套判合同时服务两件事，而它们**必须**共用：

* ``scripts/validate_fixtures.py`` 证明 fixture 有效（base 失败、gold 通过、不引入
  新回归）；
* ``eval/runner.py`` 用它判定模型补丁。

判据分开的后果是"fixture 按 A 规则被判为有效，分数按 B 规则计算"，那种
Resolved Rate 没有含义。两条路径都走 :meth:`GoldEvaluator.evaluate`，只是喂进去的
补丁不同（gold / 模型最终 / 模型第一轮）。

三条口径写进类型与注释，因为它们最容易写错、又最难在事后发现：

1. **ENV_ERROR / SANDBOX_ERROR 既不是"任务无效"也不是"通过"**。它是"这次没测出来"，
   结论必须是 ``None`` 并单独统计。当成失败会低估模型，当成通过会高估模型，当成
   无效会让整批 fixture 从分母里消失。
2. **回归只与 base 的实测结论比**。base 上本来就失败的测试不计入新增回归，否则
   任何有历史欠账的仓库永远过不了校验（03 §18 同一条规则）。
3. **声明了但 junit 里不存在的测试算失败**，不算"没测到所以通过"。测试名打错、
   路径写错最常见的表现就是"一个都没跑"，那必须是响的。

base 侧结论来自 :meth:`GoldEvaluator.baseline` 的**全量**运行（``base + test.patch``），
因此校验一个 fixture 只需要两次容器运行，评测一个补丁只需第三次。逐测试结论按
``fixture_version`` 缓存复用——它是 fixture 的属性，与补丁、seed、模型都无关。
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from codepilot.artifacts import ArtifactStore
from codepilot.eval.synthetic import SyntheticFixture
from codepilot.patch.unified_diff import apply_unified_diff
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import Sandbox
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult
from codepilot.tools.base import WorkspacePathPolicy

__all__ = [
    "EvaluationVerdict",
    "FixtureBaseline",
    "FixtureValidation",
    "GoldEvaluator",
    "PatchEvaluation",
    "ValidationStatus",
    "newly_failing",
    "validate_fixture",
]

#: 评测侧 workspace 只需要挡住 ``.git``：补丁不该改版本元数据。
#: ``allow_new_tests=True`` 是**可信构造参数**——test.patch 的本意就是往仓库里
#: 添加隐藏测试，而这个 policy 对象从不交给 Coding Agent。
_EVAL_DENY_DIRECTORIES = (".git",)


class EvaluationVerdict(StrEnum):
    """一次补丁评测的结论。"""

    # 名字带 Test/Eval 前缀的类会被 pytest 当成测试类收集；枚举显式关掉。
    __test__: ClassVar[bool] = False

    RESOLVED = "RESOLVED"
    NOT_RESOLVED = "NOT_RESOLVED"
    PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    ENV_ERROR = "ENV_ERROR"
    SANDBOX_ERROR = "SANDBOX_ERROR"

    @property
    def resolved(self) -> bool | None:
        """三态结论：``True`` / ``False`` / ``None``（没能判定）。"""
        if self is EvaluationVerdict.RESOLVED:
            return True
        if self is EvaluationVerdict.NOT_RESOLVED:
            return False
        return None

    @property
    def is_environment_failure(self) -> bool:
        return self in (EvaluationVerdict.ENV_ERROR, EvaluationVerdict.SANDBOX_ERROR)


class ValidationStatus(StrEnum):
    """E-02 要求的九种 fixture 状态。互斥，且 ENV_ERROR 不等于任务无效。"""

    __test__: ClassVar[bool] = False

    VALID = "VALID"
    INVALID_FIXTURE = "INVALID_FIXTURE"
    GOLD_PATCH_INVALID = "GOLD_PATCH_INVALID"
    BASE_NOT_FAILING = "BASE_NOT_FAILING"
    GOLD_NOT_PASSING = "GOLD_NOT_PASSING"
    REGRESSION_INTRODUCED = "REGRESSION_INTRODUCED"
    ENV_ERROR = "ENV_ERROR"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    COLLECTION_ERROR = "COLLECTION_ERROR"

    @property
    def usable(self) -> bool:
        """只有 VALID 的 fixture 才能进 benchmark 分母。"""
        return self is ValidationStatus.VALID

    @property
    def is_environment_failure(self) -> bool:
        """环境类失败：既不能算任务无效，也不能算测试通过。"""
        return self in (ValidationStatus.ENV_ERROR, ValidationStatus.SANDBOX_ERROR)


def _outcome_map(result: ExecutionResult) -> dict[str, TestOutcome]:
    return {case.node_id: case.outcome for case in result.tests}


def newly_failing(
    base: Mapping[str, TestOutcome], candidate: Mapping[str, TestOutcome]
) -> list[str]:
    """base 上 ``passed``、补丁后 ``failed``/``error`` 的测试。

    ``skipped`` 两侧都不参与：因平台条件被跳过的测试不构成回归，把它算进去会
    让"同一份 fixture 在不同镜像上有效性不同"。base 上本来就失败的测试同样不算
    （03 §18）。
    """
    return sorted(
        node
        for node, outcome in candidate.items()
        if base.get(node) is TestOutcome.PASSED
        and outcome in (TestOutcome.FAILED, TestOutcome.ERROR)
    )


class TestOutcomeRecord(BaseModel):
    """一个声明测试的实测结果。``present=False`` 表示 junit 里根本没有它。"""

    node_id: str
    outcome: str
    present: bool


class PatchEvaluation(BaseModel):
    """一个补丁在一个 fixture 上的评测结论。**评测侧对象**，绝不交给推理侧。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    base_commit: str
    fixture_version: str
    patch_source: str
    """``gold`` / ``model_final`` / ``model_first_attempt``。"""

    patch_sha256: str
    verdict: EvaluationVerdict
    resolved: bool | None
    environment: str
    """沙箱后端 + 镜像身份。ENV_ERROR 归因时要知道是**哪个**环境坏了。"""

    target: list[TestOutcomeRecord] = Field(default_factory=list)
    regression: list[TestOutcomeRecord] = Field(default_factory=list)
    missing_tests: list[str] = Field(default_factory=list)
    new_failures: list[str] = Field(default_factory=list)
    execution_status: str | None = None
    error_reason: str | None = None
    duration_seconds: float = 0.0
    tests_observed: int = 0
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    sandbox_backend: str = "unknown"

    @property
    def failure_details(self) -> dict[str, str]:
        """没通过的测试及原因，供 summary 与失败案例分析直接引用。"""
        return {
            item.node_id: ("not collected" if not item.present else item.outcome)
            for item in (*self.target, *self.regression)
            if item.outcome != TestOutcome.PASSED.value
        }


class FixtureBaseline(BaseModel):
    """``base + test.patch`` 的逐测试实测结论，按 ``fixture_version`` 复用。"""

    fixture_version: str
    task_id: str
    base_commit: str
    environment: str
    sandbox_backend: str
    execution_status: str
    outcomes: dict[str, str] = Field(default_factory=dict)
    tests_observed: int = 0
    complete: bool = True
    error_reason: str | None = None
    duration_seconds: float = 0.0
    artifact_refs: dict[str, str] = Field(default_factory=dict)

    def outcome_map(self) -> dict[str, TestOutcome]:
        return {node: TestOutcome(value) for node, value in self.outcomes.items()}

    def record(self, node: str) -> TestOutcomeRecord:
        value = self.outcomes.get(node)
        return TestOutcomeRecord(
            node_id=node,
            outcome=value if value is not None else "missing",
            present=value is not None,
        )

    @property
    def usable(self) -> bool:
        """只有真跑完的基线才能支撑"base 上目标测试确实失败"这个结论。"""
        return self.complete and self.execution_status not in (
            ExecutionStatus.ENV_ERROR.value,
            ExecutionStatus.SANDBOX_ERROR.value,
            ExecutionStatus.COLLECTION_ERROR.value,
        )


class FixtureValidation(BaseModel):
    """E-02 对单个 fixture 的结论。每一项都必须指回一次真实执行。"""

    task_id: str
    base_commit: str
    fixture_version: str
    category: str
    difficulty: str
    multi_file: bool
    has_traceback: bool
    environment: str
    status: ValidationStatus
    reason: str | None = None
    target_count: int = 0
    regression_count: int = 0
    base_target_passed: int = 0
    gold_target_passed: int = 0
    gold_result: PatchEvaluation | None = None
    baseline: FixtureBaseline | None = None
    artifact_refs: dict[str, str] = Field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status.usable


class GoldEvaluator:
    """评测侧唯一的数据入口。

    推理模块拿不到它的实例：构造需要 :class:`SyntheticFixture`（含 ``evaluation``
    视图），而推理侧只有 :class:`~codepilot.eval.synthetic.InferenceTask`。
    """

    def __init__(
        self,
        *,
        manager: WorkspaceManager,
        sandbox_factory: Callable[[ArtifactStore], Sandbox],
        work_root: Path,
    ) -> None:
        """
        ``sandbox_factory`` 按 run 造一个沙箱：每个 run 需要自己的 ArtifactStore，
        共用一个会把不同任务的 junit 写进同一个目录。
        """
        self._manager = manager
        self._sandbox_factory = sandbox_factory
        self._work_root = Path(work_root)
        self._baseline_cache: dict[str, FixtureBaseline] = {}

    # ------------------------------------------------------------------ 仓库

    def materialized_repository(self, fixture: SyntheticFixture) -> Path:
        """把 ``repo/`` 落成只含 base commit 的 git 仓库，按 fixture_version 缓存。

        缓存命中时仍然核对一次 HEAD SHA：一个上次中断留下的半成品目录看起来"已经
        材料化过"，但它的对象库里可能根本没有 base commit，那会让后面每一次评测
        都建立在一个不对的仓库上。
        """
        from codepilot.repo.gitcmd import GitError, run_git

        version = fixture.evaluation.fixture_version
        target = self._work_root / "repositories" / _digest_name(version)
        if target.is_dir():
            try:
                head = run_git(["rev-parse", "HEAD"], cwd=target, check=False).stdout.strip()
            except GitError:
                head = ""
            if head == fixture.metadata.base_commit:
                return target
            shutil.rmtree(target, ignore_errors=True)
        fixture.materialize(target)
        return target

    # ------------------------------------------------------------------ 入口

    def baseline(self, fixture: SyntheticFixture, *, run_id: str) -> FixtureBaseline:
        """``base + test.patch`` 的全量测试结论；同一 fixture 只真跑一次。"""
        version = fixture.evaluation.fixture_version
        cached = self._baseline_cache.get(version)
        if cached is not None:
            return cached
        outcome = self._execute(
            fixture,
            # 基线**必须**带上 test.patch：隐藏评测测试只存在于 test_patch 里，
            # 不带它跑的"base 全量"根本不包含目标测试，于是"目标测试在 base 上
            # 找不到"会被读成 COLLECTION_ERROR，而真实原因只是测试没被加进来。
            patches=(fixture.evaluation.test_patch,),
            tests=(),
            run_id=f"{run_id}-baseline",
        )
        baseline = self._build_baseline(fixture, outcome.result)
        if baseline.usable:
            # 不可用的基线**不**进缓存：那是一次没测出来的运行，缓存它等于把
            # ENV_ERROR 固化成这个 fixture 的"事实"，之后每次复用都不会重测。
            self._baseline_cache[version] = baseline
        return baseline

    def evaluate(
        self,
        fixture: SyntheticFixture,
        patch: str,
        *,
        patch_source: str,
        run_id: str,
        baseline: FixtureBaseline | None = None,
        full_suite: bool = False,
    ) -> PatchEvaluation:
        """在隔离 workspace 里应用 ``test.patch`` + ``patch`` 并执行测试。

        ``full_suite=True`` 跑整个测试套件而不是只跑声明的目标测试。这条只给
        fixture 校验用：docs/04 要求"其他测试不变"，而"其他"恰恰是声明清单之外
        的那部分——只跑声明清单就永远发现不了 gold 顺手改坏了别的测试。
        benchmark 判定用默认的定向运行，与 SWE-bench 的 f2p/p2p 口径一致。
        """
        evaluation = fixture.evaluation
        reference = baseline if baseline is not None else self.baseline(fixture, run_id=run_id)
        selected = (
            [] if full_suite else [*evaluation.tests.fail_to_pass, *evaluation.tests.pass_to_pass]
        )
        outcome = self._execute(
            fixture,
            patches=(evaluation.test_patch, patch) if patch else (evaluation.test_patch,),
            tests=selected,
            run_id=f"{run_id}-{patch_source}",
        )
        return self._classify(
            fixture,
            result=outcome.result,
            patch=patch,
            patch_source=patch_source,
            baseline=reference,
            backend=outcome.backend,
            environment=outcome.environment,
        )

    # ------------------------------------------------------------------ 执行

    def _execute(
        self,
        fixture: SyntheticFixture,
        *,
        patches: Sequence[str],
        tests: Sequence[str],
        run_id: str,
    ) -> _RunOutcome:
        repository = self.materialized_repository(fixture)
        run_dir = self._work_root / "runs" / run_id
        artifacts = ArtifactStore(self._work_root, run_id)
        workspace = None
        try:
            workspace = self._manager.create(
                source=str(repository),
                base_commit=fixture.metadata.base_commit,
                target=run_dir / "workspace",
            )
            policy = WorkspacePathPolicy(
                workspace.path, deny_directories=_EVAL_DENY_DIRECTORIES, allow_new_tests=True
            )
            for patch in patches:
                applied = apply_unified_diff(patch, policy)
                if not applied.applied:
                    return _RunOutcome(
                        ExecutionResult(
                            status=ExecutionStatus.COLLECTION_ERROR,
                            error_reason=f"PATCH_APPLY_FAILED: {applied.error}",
                            diagnostics={"execution_backend": "patch_apply"},
                        ),
                        "patch_apply",
                        "patch_apply:none:none",
                    )
            sandbox = self._sandbox_factory(artifacts)
            environment = sandbox.build_env(
                repository=str(repository),
                base_commit=workspace.base_commit,
                workspace=workspace.path,
                env_spec=fixture.env_spec,
                allow_network=True,
            )
            backend = str(getattr(sandbox, "backend", _guess_backend(sandbox)))
            if not environment.succeeded:
                return _RunOutcome(
                    ExecutionResult(
                        status=environment.status,
                        error_reason=environment.error,
                        diagnostics={
                            "execution_backend": backend,
                            "environment_cache_key": environment.cache_key,
                            "environment_image_ref": environment.image_ref or "",
                            "environment_image_id": environment.image_id or "",
                        },
                    ),
                    backend,
                    _environment_identity(backend, environment.cache_key, environment.image_id),
                )
            result = sandbox.run_tests(
                workspace=workspace.path,
                environment=environment,
                env_spec=fixture.env_spec,
                tests=tests,
            )
            merged = dict(result.diagnostics)
            merged.setdefault("execution_backend", backend)
            merged.setdefault("environment_cache_key", environment.cache_key)
            merged.setdefault("environment_image_id", environment.image_id or "")
            merged.setdefault("environment_image_ref", environment.image_ref or "")
            identity = _environment_identity(
                backend,
                environment.cache_key,
                environment.image_id or merged.get("environment_image_id"),
            )
            return _RunOutcome(result.model_copy(update={"diagnostics": merged}), backend, identity)
        finally:
            if workspace is not None:
                self._manager.remove(workspace)
            shutil.rmtree(run_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 判定

    def _build_baseline(
        self, fixture: SyntheticFixture, result: ExecutionResult
    ) -> FixtureBaseline:
        outcomes = _outcome_map(result)
        backend = str(result.diagnostics.get("execution_backend") or "unknown")
        return FixtureBaseline(
            fixture_version=fixture.evaluation.fixture_version,
            task_id=fixture.task_id,
            base_commit=fixture.metadata.base_commit,
            environment=_environment_from_diagnostics(result, backend),
            sandbox_backend=backend,
            execution_status=result.status.value,
            outcomes={node: outcome.value for node, outcome in outcomes.items()},
            tests_observed=len(outcomes),
            complete=result.status
            not in (
                ExecutionStatus.ENV_ERROR,
                ExecutionStatus.SANDBOX_ERROR,
                ExecutionStatus.COLLECTION_ERROR,
            ),
            error_reason=result.error_reason,
            duration_seconds=result.duration_seconds,
            artifact_refs={"baseline_execution": result.artifact_dir}
            if result.artifact_dir
            else {},
        )

    def _classify(
        self,
        fixture: SyntheticFixture,
        *,
        result: ExecutionResult,
        patch: str,
        patch_source: str,
        baseline: FixtureBaseline,
        backend: str,
        environment: str,
    ) -> PatchEvaluation:
        evaluation = fixture.evaluation
        shared: dict[str, Any] = {
            "task_id": fixture.task_id,
            "base_commit": fixture.metadata.base_commit,
            "fixture_version": evaluation.fixture_version,
            "patch_source": patch_source,
            "patch_sha256": _sha(patch),
            "environment": environment,
            "sandbox_backend": backend,
            "execution_status": result.status.value,
            "error_reason": result.error_reason,
            "duration_seconds": result.duration_seconds,
            "artifact_refs": {"patch_execution": result.artifact_dir}
            if result.artifact_dir
            else {},
        }

        if result.status is ExecutionStatus.ENV_ERROR:
            return PatchEvaluation(
                **shared, verdict=EvaluationVerdict.ENV_ERROR, resolved=None, tests_observed=0
            )
        if result.status is ExecutionStatus.SANDBOX_ERROR:
            return PatchEvaluation(
                **shared, verdict=EvaluationVerdict.SANDBOX_ERROR, resolved=None, tests_observed=0
            )
        if (result.error_reason or "").startswith("PATCH_APPLY_FAILED"):
            return PatchEvaluation(
                **shared,
                verdict=EvaluationVerdict.PATCH_APPLY_FAILED,
                resolved=None,
                tests_observed=0,
            )

        outcomes = _outcome_map(result)
        target = [
            TestOutcomeRecord(
                node_id=node,
                outcome=outcomes[node].value if node in outcomes else "missing",
                present=node in outcomes,
            )
            for node in evaluation.tests.fail_to_pass
        ]
        regression = [
            TestOutcomeRecord(
                node_id=node,
                outcome=outcomes[node].value if node in outcomes else "missing",
                present=node in outcomes,
            )
            for node in evaluation.tests.pass_to_pass
        ]
        declared = (*target, *regression)
        missing = [item.node_id for item in declared if not item.present]
        if declared and len(missing) == len(declared):
            # 一个声明的测试都没跑起来：这不是"补丁没修好"，是评测根本没发生。
            return PatchEvaluation(
                **shared,
                verdict=EvaluationVerdict.COLLECTION_ERROR,
                resolved=None,
                target=target,
                regression=regression,
                missing_tests=missing,
                tests_observed=len(outcomes),
            )

        passed = not missing and all(item.outcome == TestOutcome.PASSED.value for item in declared)
        regressions = newly_failing(baseline.outcome_map(), outcomes)
        verdict = EvaluationVerdict.RESOLVED if passed else EvaluationVerdict.NOT_RESOLVED
        return PatchEvaluation(
            **shared,
            verdict=verdict,
            resolved=verdict.resolved,
            target=target,
            regression=regression,
            missing_tests=missing,
            new_failures=regressions,
            tests_observed=len(outcomes),
        )


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """一次内部执行的结果加它的身份。"""

    result: ExecutionResult
    backend: str
    environment: str


# --------------------------------------------------------------------- fixture 校验


def validate_fixture(
    evaluator: GoldEvaluator, fixture: SyntheticFixture, *, run_id: str
) -> FixtureValidation:
    """E-02：证明"base 上目标测试失败、gold 之后通过、没有引入新回归"。

    顺序是有意的：**先**取基线。反过来（先跑 gold）的话，一个"base 本来就能过"
    的无效任务会被 gold 的成功掩盖成 VALID，而那正是让 Resolved Rate 虚高的那类
    fixture。
    """
    evaluation = fixture.evaluation
    metadata = fixture.metadata
    identity: dict[str, Any] = {
        "task_id": fixture.task_id,
        "base_commit": metadata.base_commit,
        "fixture_version": evaluation.fixture_version,
        "category": metadata.category.value,
        "difficulty": metadata.difficulty,
        "multi_file": metadata.multi_file,
        "has_traceback": metadata.has_traceback,
        "target_count": len(evaluation.tests.fail_to_pass),
        "regression_count": len(evaluation.tests.pass_to_pass),
    }

    baseline = evaluator.baseline(fixture, run_id=run_id)
    if not baseline.usable:
        status = {
            ExecutionStatus.SANDBOX_ERROR.value: ValidationStatus.SANDBOX_ERROR,
            ExecutionStatus.ENV_ERROR.value: ValidationStatus.ENV_ERROR,
        }.get(baseline.execution_status, ValidationStatus.COLLECTION_ERROR)
        return FixtureValidation(
            **identity,
            environment=baseline.environment,
            status=status,
            reason=f"base 运行未能完成：{baseline.error_reason or baseline.execution_status}",
            baseline=baseline,
            artifact_refs=baseline.artifact_refs,
        )

    # 目标测试在 base 上必须**失败**。全都通过 = 这个任务在 base 上就没有缺陷。
    base_records = [baseline.record(node) for node in evaluation.tests.fail_to_pass]
    unexpectedly_passing = [
        item.node_id for item in base_records if item.outcome == TestOutcome.PASSED.value
    ]
    absent_on_base = [item.node_id for item in base_records if not item.present]
    if absent_on_base and len(absent_on_base) == len(base_records):
        return FixtureValidation(
            **identity,
            environment=baseline.environment,
            status=ValidationStatus.COLLECTION_ERROR,
            reason=f"base 运行里找不到目标测试: {absent_on_base[:3]}",
            baseline=baseline,
            artifact_refs=baseline.artifact_refs,
        )
    if unexpectedly_passing:
        return FixtureValidation(
            **identity,
            environment=baseline.environment,
            status=ValidationStatus.BASE_NOT_FAILING,
            reason=f"目标测试在 base 上已经通过: {unexpectedly_passing[:3]}",
            base_target_passed=len(unexpectedly_passing),
            baseline=baseline,
            artifact_refs=baseline.artifact_refs,
        )
    # PASS_TO_PASS 必须在 base 上就是通过的，否则"保持通过"无从谈起。
    p2p_broken = [
        node for node in evaluation.tests.pass_to_pass if baseline.record(node).outcome != "passed"
    ]
    if p2p_broken:
        return FixtureValidation(
            **identity,
            environment=baseline.environment,
            status=ValidationStatus.INVALID_FIXTURE,
            reason=f"PASS_TO_PASS 在 base 上不通过: {p2p_broken[:3]}",
            baseline=baseline,
            artifact_refs=baseline.artifact_refs,
        )

    gold = evaluator.evaluate(
        fixture,
        evaluation.gold_patch,
        patch_source="gold",
        run_id=run_id,
        baseline=baseline,
        full_suite=True,
    )
    refs = {**baseline.artifact_refs, **gold.artifact_refs}
    common: dict[str, Any] = {
        "environment": gold.environment,
        "gold_result": gold,
        "baseline": baseline,
        "artifact_refs": refs,
        "base_target_passed": 0,
        "gold_target_passed": sum(
            1 for item in gold.target if item.outcome == TestOutcome.PASSED.value
        ),
    }
    if gold.verdict is EvaluationVerdict.SANDBOX_ERROR:
        return FixtureValidation(
            **identity,
            status=ValidationStatus.SANDBOX_ERROR,
            reason="gold 运行是沙箱错误",
            **common,
        )
    if gold.verdict is EvaluationVerdict.ENV_ERROR:
        return FixtureValidation(
            **identity,
            status=ValidationStatus.ENV_ERROR,
            reason="gold 运行是环境错误（不计为任务无效）",
            **common,
        )
    if gold.verdict is EvaluationVerdict.PATCH_APPLY_FAILED:
        return FixtureValidation(
            **identity,
            status=ValidationStatus.GOLD_PATCH_INVALID,
            reason=f"gold.patch 应用失败: {gold.error_reason}",
            **common,
        )
    if gold.verdict is EvaluationVerdict.COLLECTION_ERROR:
        return FixtureValidation(
            **identity,
            status=ValidationStatus.COLLECTION_ERROR,
            reason=f"gold 运行找不到目标测试: {gold.missing_tests[:3]}",
            **common,
        )
    if gold.verdict is not EvaluationVerdict.RESOLVED:
        return FixtureValidation(
            **identity,
            status=ValidationStatus.GOLD_NOT_PASSING,
            reason=f"gold 之后仍未通过: {gold.failure_details or gold.missing_tests}",
            **common,
        )
    if gold.new_failures:
        return FixtureValidation(
            **identity,
            status=ValidationStatus.REGRESSION_INTRODUCED,
            reason=f"gold 引入了相对 base 的新失败: {gold.new_failures[:5]}",
            **common,
        )
    return FixtureValidation(**identity, status=ValidationStatus.VALID, reason=None, **common)


# --------------------------------------------------------------------- 辅助


def _environment_identity(backend: str, cache_key: str, image_id: object) -> str:
    return f"{backend}:{(cache_key or 'no-key')[:16]}:{str(image_id or 'no-image')[:19]}"


def _environment_from_diagnostics(result: ExecutionResult, backend: str) -> str:
    diagnostics = result.diagnostics
    return _environment_identity(
        backend,
        str(diagnostics.get("environment_cache_key") or ""),
        diagnostics.get("environment_image_id") or "",
    )


def _guess_backend(sandbox: object) -> str:
    name = type(sandbox).__name__
    if "Docker" in name:
        return "docker"
    if "Fake" in name:
        return "fake"
    return name.lower()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_name(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
