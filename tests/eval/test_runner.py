"""E-08 / E-09：runner 与报告的离线集成测试。

工程测试，不是效果验收。这里用 FakeLLM 与 FakeSandbox：FakeSandbox 按规则返回
``ExecutionResult``，所以能稳定摆出成功、失败与环境异常三种结局，而这些结局在真实
容器里要靠构造缺陷才能复现。跑通的是**链路**（派发 → 推理 → 独立评测 → 指标 → 产物），
不是模型能力。

刻意覆盖三条最容易出事的路径：

* 预算截停之后不再派发，但已完成结果与失败任务都保留；
* 续跑不重复执行、不重复计费，且证据被改动时拒绝复用；
* 分母与实际记录条数一致，环境失败仍计入总体分母。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codepilot.artifacts import ArtifactStore
from codepilot.eval.assembly import InferenceOutcome, ProviderBundle
from codepilot.eval.evaluation import GoldEvaluator
from codepilot.eval.report import (
    MANIFEST_JSON,
    METRICS_JSON,
    RESULTS_JSONL,
    SUMMARY_MD,
    build_metrics,
    evidence_classification,
    read_results,
    write_benchmark_report,
)
from codepilot.eval.runner import (
    BenchmarkConfig,
    BenchmarkRunner,
    CostLedger,
    ExecutionState,
    JournalCorruptionError,
    ResultJournal,
    RunIdentity,
    TaskRecord,
    code_identity,
    load_bench_config,
    provider_config_hash,
)
from codepilot.eval.synthetic import load_fixture
from codepilot.eval.synthetic_build import build_fixture
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.usage import Usage
from support.synth import demo_spec

TARGET_NODE = "tests/test_hidden.py::test_normalize_keeps_value"
P2P_NODE = "tests/test_public.py::test_documented_base_behaviour"


def _fixtures(tmp_path: Path, count: int = 3) -> list[Any]:
    out: list[Any] = []
    for index in range(count):
        directory = build_fixture(
            demo_spec(task_id=f"demo-task-{index}"),
            tmp_path / "suite",
            scratch_root=tmp_path / "scratch" / str(index),
        )
        out.append(load_fixture(directory))
    return out


def _outcome(
    *,
    run_id: str,
    task_id: str,
    seed: int,
    patch: str,
    cost: float | None = 0.5,
    llm_calls: int = 4,
    empty: bool = False,
    iterations: int = 2,
    localization: tuple[str, ...] = (),
) -> InferenceOutcome:
    usage = Usage(input_tokens=100, output_tokens=50, cost=cost or 0.0, llm_calls=llm_calls)
    if cost is None:
        usage.cost = 0.0
    return InferenceOutcome(
        run_id=run_id,
        task_id=task_id,
        seed=seed,
        base_commit="0" * 40,
        repository_id="repo_demo",
        status="succeeded" if not empty else "failed",
        stop_reason=None if not empty else "budget_iterations",
        final_patch="" if empty else patch,
        final_patch_sha256="-" if empty else str(len(patch)),
        first_attempt_patch="" if empty else patch,
        first_attempt_patch_sha256=None if empty else str(len(patch)),
        iterations=iterations,
        attempts_saved=0 if empty else 1,
        usage=usage,
        usage_uncertain=cost is None,
        test_runs=1,
        runtime_seconds=1.5,
        localization_files=localization,
        localization_symbols=(),
        agent_claims_success=not empty,
        empty_patch=empty,
        invalid_patch=False,
        simulated_llm=True,
        sandbox_backend="fake",
        verification_mode="full",
        artifact_dir=str(Path("artifacts") / run_id),
    )


class _StubAssembly:
    """替身装配：只负责产出预设的 InferenceOutcome，不跑图。

    图本身在 Phase 8/9 已有真实 Docker 验收；这里要测的是 runner 的编排、预算与
    记账，用假装配才能把"第 3 个任务费用未知""预算刚好截在第 5 个"这类边界摆准。
    """

    def __init__(self, outcome: InferenceOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    def run(self, *, run_id: str, seed: int) -> InferenceOutcome:
        self.calls += 1
        import dataclasses

        return dataclasses.replace(self._outcome, run_id=run_id, seed=seed)


class _RuleSandbox:
    """按规则返回结果的沙箱。

    不用 :class:`FakeSandbox` 的脚本列表：一次 (task, seed) 评测要 build_env + 基线
    + 最终补丁 + 第一轮补丁，固定长度的脚本会在第 4 次调用后抛"脚本已用尽"，于是
    "续跑不重跑"这类测试会被误判成 evaluation_error。规则化返回才能让失败归因于
    被测性质本身。
    """

    def __init__(self, *, fail_target: bool = False, broken: bool = False) -> None:
        self._fail_target = fail_target
        self._broken = broken
        self.run_count = 0
        self.backend = "fake"

    def build_env(self, **kwargs: Any) -> EnvironmentBuildResult:
        if self._broken:
            return EnvironmentBuildResult(
                status=ExecutionStatus.ENV_ERROR, cache_key="k", error="scripted env failure"
            )
        return EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="fake://env",
            image_id="fake-image",
            cache_key="k",
        )

    def run_tests(self, **kwargs: Any) -> ExecutionResult:
        self.run_count += 1
        if self._broken:
            return scripted_result(ExecutionStatus.ENV_ERROR)
        cases = [
            TestCaseResult(
                node_id=TARGET_NODE,
                outcome=TestOutcome.FAILED if self._fail_target else TestOutcome.PASSED,
                duration=0.01,
            ),
            TestCaseResult(node_id=P2P_NODE, outcome=TestOutcome.PASSED, duration=0.01),
        ]
        return scripted_result(ExecutionStatus.PASS, tests=cases)


def _sandbox(*, fail_target: bool = False, broken: bool = False) -> _RuleSandbox:
    return _RuleSandbox(fail_target=fail_target, broken=broken)


def _runner(
    tmp_path: Path,
    *,
    fixtures: list[Any],
    seeds: tuple[int, ...] = (0, 1, 2),
    sandbox: FakeSandbox | None = None,
    outcome_factory: Any = None,
    assembly_factory: Any = None,
    max_total_cost: float | None = None,
    reservation: float = 0.0,
    journal_dir: Path | None = None,
    **extra: Any,
) -> BenchmarkRunner:
    active = sandbox if sandbox is not None else _sandbox()
    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=tmp_path / "cache"),
        sandbox_factory=lambda artifacts: active,
        work_root=tmp_path / "evalwork",
    )
    built: dict[str, _StubAssembly] = {}

    def factory(fixture: Any, seed: int) -> Any:
        if assembly_factory is not None:
            return assembly_factory(fixture, seed)
        key = f"{fixture.task_id}-{seed}"
        if key not in built:
            patch = "" if getattr(fixture, "_no_patch", False) else fixture.evaluation.gold_patch
            outcome = (outcome_factory or _outcome)(
                run_id="stub", task_id=fixture.task_id, seed=seed, patch=patch
            )
            built[key] = _StubAssembly(outcome)
        return built[key]

    config = BenchmarkConfig(
        seeds=seeds,
        max_total_cost=max_total_cost,
        task_reservation=reservation,
        out_dir=tmp_path / "out",
        **extra,
    )
    return BenchmarkRunner(
        config=config,
        fixtures=fixtures,
        assembly_factory=factory,
        evaluator=evaluator,
        journal_directory=journal_dir,
    )


# ------------------------------------------------------------------ 身份与配置


def test_identity_is_reproducible_for_same_settings() -> None:
    first = RunIdentity("synthetic", "t", 1, "cfg", "abc", "fv")
    second = RunIdentity("synthetic", "t", 1, "cfg", "abc", "fv")
    assert first.key == second.key
    changed = RunIdentity("synthetic", "t", 1, "cfg", "abc", "fv2")
    assert changed.key != first.key, "fixture 版本变了必须换身份，否则续跑会复用旧结果"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seeds": ()},
        {"seeds": (-1,)},
        {"max_in_flight": 0},
        {"max_total_cost": -1.0},
        {"task_reservation": -0.5},
    ],
)
def test_config_rejects_impossible_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BenchmarkConfig(**kwargs)


def test_batch_ceiling_without_reservation_is_refused() -> None:
    """设了整批上限却没预留额，等于承诺了一个没人执行的约束。"""
    with pytest.raises(ValueError, match="预留"):
        BenchmarkConfig(max_total_cost=10.0)


def test_provider_config_hash_ignores_key_values() -> None:
    """配置指纹要稳定，且不因密钥轮换而变。"""
    a = provider_config_hash({"providers": {"deepseek": {"api_key_env": "DEEPSEEK_API_KEY"}}})
    b = provider_config_hash({"providers": {"deepseek": {"api_key_env": "DEEPSEEK_API_KEY"}}})
    assert a == b
    assert provider_config_hash({"providers": {"deepseek": {"api_key_env": "OTHER"}}}) != a


def test_bench_config_loads_repo_defaults() -> None:
    config = load_bench_config(Path("configs/full.yaml"), seeds_override=(0, 1, 2))
    assert config.suite == "synthetic"
    assert config.per_task_budget.max_iterations == 5
    assert config.budget_currency == "CNY"


def test_bench_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("suite: synthetic\nseeds: [0]\ntypo_budget: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未知配置项"):
        load_bench_config(path)


def test_bench_config_reservation_falls_back_conservatively(tmp_path: Path) -> None:
    """只有整批上限时给一个非零预留额，宁可少派也不超发。"""
    path = tmp_path / "c.yaml"
    path.write_text("limits:\n  max_total_cost: 8.0\n", encoding="utf-8")
    config = load_bench_config(path)
    assert config.task_reservation == pytest.approx(1.0)


def test_code_identity_marks_dirty_tree() -> None:
    identity = code_identity()
    assert identity["commit"]
    assert isinstance(identity["dirty"], bool)


# ------------------------------------------------------------------ 费用账本


def test_ledger_blocks_dispatch_at_the_ceiling() -> None:
    ledger = CostLedger(max_total_cost=1.0, reservation=0.4)
    # 1.0 / 0.4 ⇒ 只占得下 2 次（0.4、0.8；第 3 次要 1.2 就超了）
    assert [ledger.reserve() for _ in range(4)] == [True, True, False, False]
    assert ledger.blocked == 2


def test_unknown_cost_does_not_release_reservation() -> None:
    """费用未知 ≠ 0。按 0 记账会让预算被系统性高估，上限形同虚设。"""
    ledger = CostLedger(max_total_cost=1.0, reservation=0.5)
    assert ledger.reserve()
    ledger.settle(None)
    assert ledger.unknown_cost_tasks == 1
    assert ledger.committed == pytest.approx(0.5), "未知成本必须继续占着预留额度"
    assert ledger.reserve() is True
    ledger.settle(None)
    assert ledger.reserve() is False


def test_ledger_is_thread_safe_under_concurrent_reservation() -> None:
    import threading

    ledger = CostLedger(max_total_cost=2.0, reservation=0.5)
    granted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        ok = ledger.reserve()
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for item in threads:
        item.start()
    for item in threads:
        item.join()
    assert sum(granted) == 4, "并发下超发一次，预算就已失效"


def test_ledger_without_ceiling_never_blocks() -> None:
    ledger = CostLedger(max_total_cost=None, reservation=0.0)
    assert all(ledger.reserve() for _ in range(5))
    assert ledger.blocked == 0


# ------------------------------------------------------------------ 续跑 journal


def test_journal_roundtrip_and_reuse(tmp_path: Path) -> None:
    journal = ResultJournal(tmp_path / "journal.sqlite")
    record = _record(task_id="t0", seed=1, resolved=True)
    journal.commit(record)
    loaded = journal.lookup(record.identity)
    assert loaded is not None
    assert loaded.as_dict() == record.as_dict()


def test_journal_refuses_tampered_evidence(tmp_path: Path) -> None:
    """证据被改动时必须拒绝复用，而不是"当作没跑过"重跑一次。"""
    journal = ResultJournal(tmp_path / "journal.sqlite")
    record = _record(task_id="t0", seed=0, resolved=True)
    journal.commit(record)
    with journal._connect() as db:  # 故意破坏，模拟磁盘被改动
        db.execute(
            "UPDATE results SET payload = ? WHERE identity = ?",
            (json.dumps({**record.as_dict(), "resolved": False}), record.identity),
        )
    with pytest.raises(JournalCorruptionError):
        journal.lookup(record.identity)
    with pytest.raises(JournalCorruptionError):
        journal.all()


def _record(*, task_id: str, seed: int, resolved: bool | None) -> TaskRecord:
    identity = RunIdentity("synthetic", task_id, seed, "cfg", "commit", "fv")
    return TaskRecord(
        task_id=task_id,
        seed=seed,
        category="incorrect_return",
        difficulty="easy",
        multi_file=False,
        has_traceback=False,
        model_identity="FakeLLM",
        config_hash="cfg",
        code_commit="commit",
        fixture_version="fv",
        base_commit="0" * 40,
        run_id="r",
        identity=identity.key,
        execution_state=ExecutionState.COMPLETED.value,
        evaluation_status="RESOLVED" if resolved else "NOT_RESOLVED",
        resolved=resolved,
        first_attempt_resolved=resolved,
        agent_claims_success=bool(resolved),
        empty_patch=False,
        invalid_patch=False,
        patch_sha256="p",
        first_attempt_patch_sha256="p",
        iterations=1,
        stop_reason=None,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        llm_calls=1,
        cost=0.1,
        cost_currency="CNY",
        runtime_seconds=1.0,
        verification_mode="full",
        simulated_llm=True,
        sandbox_backend="fake",
        real_docker_evaluation=False,
        usage_uncertain=False,
        finished_at="now",
    )


def test_resume_does_not_re_run_or_double_count(tmp_path: Path) -> None:
    """续跑不重跑、不重复计费、不重复计成功数、不覆盖既有有效结果。"""
    fixtures = _fixtures(tmp_path, 2)
    journal_dir = tmp_path / "run"
    first = _runner(tmp_path / "a", fixtures=fixtures, seeds=(0,), journal_dir=journal_dir)
    run_one = first.run()
    assert run_one.counts()[ExecutionState.COMPLETED.value] == 2
    spent_after_first = first._ledger.spent

    second_runner = _runner(tmp_path / "b", fixtures=fixtures, seeds=(0,), journal_dir=journal_dir)
    run_two = second_runner.run()
    assert len(run_two.records) == 2, "复用后观测数不变，说明没有重跑"
    assert run_two.counts()[ExecutionState.COMPLETED.value] == 2
    assert second_runner._ledger.spent == 0.0
    assert spent_after_first > 0
    resolved = sum(1 for item in run_two.records if item.resolved)
    assert resolved == sum(1 for item in run_one.records if item.resolved)


def test_resume_after_new_seed_only_runs_the_new_one(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 1)
    journal_dir = tmp_path / "run2"
    _runner(tmp_path / "a", fixtures=fixtures, seeds=(0,), journal_dir=journal_dir).run()
    second = _runner(tmp_path / "b", fixtures=fixtures, seeds=(0, 1), journal_dir=journal_dir)
    result = second.run()
    assert len(result.records) == 2
    states = {item.seed: item.execution_state for item in result.records}
    assert states[0] == ExecutionState.COMPLETED.value
    assert states[1] == ExecutionState.COMPLETED.value


def test_two_fresh_runs_get_disjoint_inference_run_dirs(tmp_path: Path) -> None:
    """第二次全新运行不得复用上一次的推理 run 目录名。

    真实症状：推理 workspace 在运行结束后刻意保留（中断现场要能核查），而
    WorkspaceManager 拒绝写入非空目录。run_id 只由 task+seed+identity 决定时，
    第二次跑同一批 fixture 会一头撞进上一次的残留，9/9 任务全部变成
    inference_error——复跑是评测基础设施的底线，不是锦上添花。
    """
    import dataclasses

    fixtures = _fixtures(tmp_path, 2)
    claimed: set[str] = set()

    class _ClaimingAssembly:
        def __init__(self, outcome: InferenceOutcome) -> None:
            self._outcome = outcome

        def run(self, *, run_id: str, seed: int) -> InferenceOutcome:
            if run_id in claimed:
                raise RuntimeError(f"workspace 目标目录已存在且非空: {run_id}")
            claimed.add(run_id)
            return dataclasses.replace(self._outcome, run_id=run_id, seed=seed)

    def factory(fixture: Any, seed: int) -> _ClaimingAssembly:
        return _ClaimingAssembly(
            _outcome(
                run_id="stub",
                task_id=fixture.task_id,
                seed=seed,
                patch=fixture.evaluation.gold_patch,
            )
        )

    for attempt in range(2):
        result = _runner(
            tmp_path / f"run{attempt}",
            fixtures=fixtures,
            seeds=(0, 1, 2),
            assembly_factory=factory,
        ).run()
        states = [item.execution_state for item in result.records]
        assert states == [ExecutionState.COMPLETED.value] * 6, result.records
    assert len(claimed) == 12


# ------------------------------------------------------------------ 编排与产物
def test_full_pipeline_writes_four_artifacts(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 3)
    runner = _runner(tmp_path, fixtures=fixtures, seeds=(0, 1, 2), journal_dir=tmp_path / "run")
    result = runner.run()
    paths = write_benchmark_report(result, runner.run_dir)
    for path in (paths.results, paths.metrics, paths.summary, paths.manifest):
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").strip(), f"{path.name} 是空的"
    assert paths.results.read_text(encoding="utf-8").strip().count("\n") == 8
    lines = paths.results.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 9 == len(result.records)
    for line in lines:
        json.loads(line)


def test_localization_predictions_reach_recall_metrics(tmp_path: Path) -> None:
    """推理侧的定位预测要进到 Recall@K 的分母里。

    分母 0（``value=None``）与"算出来是 0%"是两件事：前者说明评测压根没看到候选，
    后者说明模型没找准。少了这条链路，File/Function Recall@K 会永远停在 None，
    而报告看上去"只是没算好"。
    """
    import dataclasses

    fixtures = _fixtures(tmp_path, 1)
    gold = tuple(sorted(fixtures[0].evaluation.gold_files))
    assert gold, "demo fixture 应当有 gold 修改的文件"

    class _Recording:
        def __init__(self, outcome: InferenceOutcome) -> None:
            self._outcome = outcome

        def run(self, *, run_id: str, seed: int) -> InferenceOutcome:
            return dataclasses.replace(
                self._outcome,
                run_id=run_id,
                seed=seed,
                localization_files=gold,
            )

    def factory(fixture: Any, seed: int) -> _Recording:
        return _Recording(
            _outcome(
                run_id="stub",
                task_id=fixture.task_id,
                seed=seed,
                patch=fixture.evaluation.gold_patch,
            )
        )

    result = _runner(tmp_path, fixtures=fixtures, seeds=(0,), assembly_factory=factory).run()
    record = result.records[0]
    assert record.localization_files == gold, "定位预测没落到逐任务记录里"
    sample = result.metric_inputs()[0].localization
    assert sample is not None and sample.gold_files == frozenset(gold), "评测侧的 gold 没接上"

    metrics = build_metrics(result)
    recall = metrics["overall"]["localization"]["k1"]["file_recall"]
    assert recall["denominator"] == 1, recall
    assert recall["value"] == 1.0, recall
    function_recall = metrics["overall"]["localization"]["k1"]["function_recall"]
    assert function_recall["denominator"] == 0 and function_recall["value"] is None, (
        "符号级 gold 评测侧不提供，必须如实显示为无证据，而不是 0%"
    )


def test_record_count_matches_denominator(tmp_path: Path) -> None:
    """任务数 × seed 数 = 记录数 = 指标分母。三者不一致就是漏了或重了。"""
    fixtures = _fixtures(tmp_path, 3)
    runner = _runner(tmp_path, fixtures=fixtures, seeds=(0, 1, 2), journal_dir=tmp_path / "run")
    result = runner.run()
    metrics = build_metrics(result)
    assert len(result.records) == 9 == result.declared_observations
    assert metrics["overall"]["repair"]["total_tasks"] == 9
    assert metrics["recorded_observations"] == 9
    assert metrics["integrity"] == "full"


def test_env_failure_stays_in_the_denominator(tmp_path: Path) -> None:
    """环境失败既不算通过也不算无效：它留在总体分母里并单独计数。"""
    fixtures = _fixtures(tmp_path, 1)
    runner = _runner(
        tmp_path,
        fixtures=fixtures,
        seeds=(0,),
        sandbox=_sandbox(broken=True),
        journal_dir=tmp_path / "run",
    )
    result = runner.run()
    metrics = build_metrics(result)
    assert metrics["overall"]["repair"]["resolved_rate"]["denominator"] == 1
    assert metrics["overall"]["repair"]["resolved_rate"]["numerator"] == 0
    assert metrics["overall"]["repair"]["undetermined_tasks"] == 1
    assert result.records[0].resolved is None


def test_failure_and_success_mix_is_counted_exactly(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 2)
    runner = _runner(
        tmp_path,
        fixtures=fixtures,
        seeds=(0, 1),
        sandbox=_sandbox(fail_target=True),
        journal_dir=tmp_path / "run",
    )
    result = runner.run()
    metrics = build_metrics(result)
    # 补丁就是 gold，但沙箱被脚本设成"目标测试失败"：4 个观测全判未解决。
    assert metrics["overall"]["repair"]["resolved_rate"] == {
        "numerator": 0,
        "denominator": 4,
        "value": 0.0,
    }


def test_budget_stop_keeps_completed_and_records_blocked(tmp_path: Path) -> None:
    """预算达到后停止派发；已完成结果保留，被挡的显式记录而不是消失。"""
    fixtures = _fixtures(tmp_path, 4)
    runner = _runner(
        tmp_path,
        fixtures=fixtures,
        seeds=(0,),
        max_total_cost=1.0,
        reservation=0.6,  # 每个任务实际花 0.5，预留 0.6 ⇒ 只能派发 1 个
        journal_dir=tmp_path / "run",
    )
    result = runner.run()
    counts = result.counts()
    assert counts.get(ExecutionState.BUDGET_BLOCKED.value, 0) >= 1
    assert len(result.records) == 4, "被挡掉的任务也要有记录，否则分母会悄悄缩小"
    assert metrics_partial(result)
    assert result.ledger["blocked_dispatches"] >= 1


def metrics_partial(result: Any) -> bool:
    return result.integrity.startswith("partial")


def test_inference_error_does_not_abort_the_batch(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 3)

    def factory(_fixture: Any, _seed: Any) -> Any:
        def run(*, run_id: str, seed: int) -> Any:
            if "task-1" in run_id:
                raise RuntimeError("boom")
            return _outcome(run_id=run_id, task_id="x", seed=seed, patch="p")

        return type("A", (), {"run": staticmethod(run)})()

    config = BenchmarkConfig(seeds=(0,), out_dir=tmp_path / "out")
    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=tmp_path / "cache"),
        sandbox_factory=lambda artifacts: _sandbox(),
        work_root=tmp_path / "w",
    )
    runner = BenchmarkRunner(
        config=config,
        fixtures=fixtures,
        assembly_factory=factory,
        evaluator=evaluator,
        journal_directory=tmp_path / "run",
    )
    result = runner.run()
    states = {item.execution_state for item in result.records}
    assert ExecutionState.INFERENCE_ERROR.value in states
    assert ExecutionState.COMPLETED.value in states
    assert all(
        item.error_details for item in result.records if item.execution_state == "inference_error"
    )


# ------------------------------------------------------------------ 隔离


def test_assembly_factory_only_builds_inference_views(tmp_path: Path) -> None:
    """生产装配工厂造出来的是 :class:`InferenceTask`，不是评测视图。

    这条测的是 :func:`codepilot.eval.runner.assembly_factory` 本身：它把 fixture 翻译成
    推理侧对象。用 stub 工厂测这件事等于什么都没测，所以这里走真实工厂 + 真实
    材料化，只把沙箱换成规则化的假沙箱。
    """
    from codepilot.eval.assembly import InferenceAssembly
    from codepilot.eval.runner import assembly_factory
    from codepilot.schemas.budget import RepairBudget

    fixtures = _fixtures(tmp_path, 1)
    sandbox = _sandbox()
    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=tmp_path / "cache"),
        sandbox_factory=lambda artifacts: sandbox,
        work_root=tmp_path / "w",
    )
    providers = lambda fixture, seed: ProviderBundle.scripted(  # noqa: E731 - 测试内一次用
        {role: [] for role in ("localizer", "planner", "coder", "diagnoser", "reviewer")}
    )
    factory = assembly_factory(
        work_root=tmp_path / "work",
        cache_root=tmp_path / "ws-cache",
        sandbox_factory=lambda artifacts: sandbox,
        providers=providers,
        evaluator=evaluator,
        budget=RepairBudget(),
    )
    assembly = factory(fixtures[0], 0)
    assert isinstance(assembly, InferenceAssembly)
    task = assembly._task
    payload = task.model_dump_json()
    assert "gold_patch" not in payload
    assert "fail_to_pass" not in payload
    assert "test_patch" not in payload
    assert not hasattr(task, "gold_patch")
    # 材料化出来的推理仓库里根本没有 eval/，工具再怎么找也找不到。
    assert not (Path(task.repository) / "eval").exists()


def test_records_declare_themselves_as_simulated(tmp_path: Path) -> None:
    """FakeLLM 跑出来的每一行都必须自带 simulated_llm=True。

    报告标题会由它决定"工程 smoke"还是"真实模型"；标记丢了，一次脚本跑通就会被
    读成模型能力结论。
    """
    fixtures = _fixtures(tmp_path, 1)
    result = _runner(tmp_path, fixtures=fixtures, seeds=(0,), journal_dir=tmp_path / "run").run()
    assert all(item.simulated_llm for item in result.records)
    assert not any(item.real_docker_evaluation for item in result.records)
    evidence = evidence_classification(result)
    assert evidence["engine"] == "FakeLLM 工程 smoke"
    assert evidence["claims_model_capability"] is False
    assert evidence["real_docker_evaluation"] is False


def test_results_jsonl_refuses_partial_writes(tmp_path: Path) -> None:
    """空行意味着文件被截断过，不能拿它复算指标。"""
    path = tmp_path / RESULTS_JSONL
    good = _record(task_id="a", seed=0, resolved=True)
    path.write_text(json.dumps(good.as_dict()) + "\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="部分写入"):
        read_results(path)
    path.write_text(json.dumps(good.as_dict()) + "\n", encoding="utf-8")
    assert read_results(path)[0].as_dict() == good.as_dict()


def test_summary_reports_denominators_not_just_percentages(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 2)
    result = _runner(tmp_path, fixtures=fixtures, seeds=(0, 1), journal_dir=tmp_path / "run").run()
    metrics = build_metrics(result)
    text = write_benchmark_report(result, tmp_path / "run").summary.read_text("utf-8")
    assert "Resolved Rate" in text
    assert "4/4" in text
    assert "Repair Gain" in text
    assert "First Attempt Resolved" in text
    assert metrics["per_seed"], "02 §17.5 要每 seed 的均值 ± 标准差"
    assert "stddev" in text


def test_manifest_binds_code_config_and_data(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 1)
    runner = _runner(tmp_path, fixtures=fixtures, seeds=(0,), journal_dir=tmp_path / "run")
    result = runner.run()
    paths = write_benchmark_report(result, runner.run_dir)
    manifest = json.loads(paths.manifest.read_text("utf-8"))
    assert manifest["run"]["config_hash"] == runner._config.config_hash
    assert manifest["run"]["code"]["commit"]
    assert manifest["data_source"]["fixture_versions"]
    assert set(manifest["artifact_files"]) == {
        RESULTS_JSONL,
        METRICS_JSON,
        SUMMARY_MD,
        MANIFEST_JSON,
    }


def test_seeds_are_reported_independently_and_then_aggregated(tmp_path: Path) -> None:
    """摊平成 3N 个任务得到的是"一次大运行"，不是三次独立重复实验。"""
    fixtures = _fixtures(tmp_path, 2)
    result = _runner(
        tmp_path, fixtures=fixtures, seeds=(0, 1, 2), journal_dir=tmp_path / "run"
    ).run()
    metrics = build_metrics(result)
    assert sorted(metrics["per_seed"]) == ["0", "1", "2"]
    for seed in ("0", "1", "2"):
        assert metrics["per_seed"][seed]["repair"]["total_tasks"] == 2
    assert metrics["seed_aggregate"]["resolved_rate"]["seeds"] == 3


def test_providers_bundle_rejects_missing_roles() -> None:
    bundle = ProviderBundle.scripted({"planner": [], "coder": []})
    with pytest.raises(ValueError, match="缺少角色"):
        ProviderBundle(roles={"planner": None}, usage=Usage(), simulated=True, model_identity="x")  # type: ignore[dict-item]
    assert set(bundle.roles) == {
        "localizer",
        "planner",
        "coder",
        "diagnoser",
        "reviewer",
    }


def test_environment_build_failure_is_not_reported_as_resolved(tmp_path: Path) -> None:
    """ENV_ERROR 既不算通过也不算无效：结论必须是"没测出来"。"""
    fixtures = _fixtures(tmp_path, 1)
    sandbox = _sandbox(broken=True)
    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=tmp_path / "cache"),
        sandbox_factory=lambda artifacts: sandbox,
        work_root=tmp_path / "w",
    )
    record = evaluator.evaluate(
        fixtures[0], fixtures[0].evaluation.gold_patch, patch_source="gold", run_id="r"
    )
    assert record.resolved is None
    assert record.verdict.value == "ENV_ERROR"
    build = EnvironmentBuildResult(status=ExecutionStatus.ENV_ERROR, cache_key="k")
    assert build.succeeded is False


@pytest.mark.parametrize("backend", ["docker", "fake"])
def test_sandbox_backend_is_recorded_per_row(backend: str, tmp_path: Path) -> None:
    """报告要能分清真实容器与脚本化结果，不能靠"看起来一样"。"""
    fixtures = _fixtures(tmp_path, 1)
    sandbox = (
        FakeSandbox([], build_status=ExecutionStatus.PASS) if backend == "fake" else _sandbox()
    )
    result = _runner(
        tmp_path,
        fixtures=fixtures,
        seeds=(0,),
        sandbox=sandbox,
        journal_dir=tmp_path / f"r-{backend}",
    ).run()
    assert result.records[0].sandbox_backend in {"fake", "docker"}


def test_artifact_store_writes_are_contained(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "art", "run1")
    with pytest.raises(ValueError, match="越界"):
        store.write_json("../escape.json", {})
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path / "art", "../run")
