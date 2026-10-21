"""E-02：fixture 校验的判据与状态区分（离线，用规则化 FakeSandbox）。

真实 Docker 验收在 ``scripts/validate_fixtures.py`` 与
``tests/eval/test_fixtures_docker.py``。这里守的是**判定逻辑**：九种状态各自要
由什么样的执行结果得出，尤其是"没测出来"绝不能被读成"通过"或"无效"。

FakeSandbox 在这里是合适用具：它按脚本返回 ``ExecutionResult``，所以能把
ENV_ERROR / 收集失败 / 局部通过这些**真实里很难稳定复现**的形态摆出来。它不能
替代的命题是"这个 fixture 真的有效"——那条只能由容器执行回答。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codepilot.artifacts import ArtifactStore
from codepilot.eval.evaluation import (
    EvaluationVerdict,
    GoldEvaluator,
    ValidationStatus,
    newly_failing,
    validate_fixture,
)
from codepilot.eval.synthetic import load_fixture
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox
from codepilot.sandbox.docker import DockerSandbox, execution_container_config
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from support.synth import demo_spec, write_fixture

TARGET = "tests/test_hidden.py::test_normalize_keeps_value"
PASSED_TO_PASS = "tests/test_public.py::test_documented_base_behaviour"
OTHER = "tests/test_public.py::test_other"


def _case(node: str, outcome: TestOutcome) -> TestCaseResult:
    return TestCaseResult(node_id=node, outcome=outcome, duration=0.01)


def _result(
    *cases: TestCaseResult, status: ExecutionStatus = ExecutionStatus.PASS
) -> ExecutionResult:
    return scripted_result(
        status, tests=list(cases), exit_code=0 if status is ExecutionStatus.PASS else 1
    )


def _evaluator(
    tmp_path: Path, results: list[ExecutionResult], **kwargs: Any
) -> tuple[FakeSandbox, GoldEvaluator]:
    """共享一个 FakeSandbox：``_execute`` 每次都向工厂要新的，但脚本必须全局排队。"""
    sandbox = FakeSandbox(results, **kwargs)

    def factory(artifacts: ArtifactStore) -> Sandbox:
        del artifacts
        return sandbox

    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=tmp_path / "cache"),
        sandbox_factory=factory,
        work_root=tmp_path / "work",
    )
    return sandbox, evaluator


def _fixture(tmp_path: Path, **spec_kwargs: Any) -> Any:
    return load_fixture(write_fixture(tmp_path, demo_spec(**spec_kwargs), name="eval"))


# ------------------------------------------------------------------ 判定逻辑


def test_valid_fixture_reports_valid(tmp_path: Path) -> None:
    sandbox, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(_case(TARGET, TestOutcome.PASSED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.VALID
    assert validation.usable
    assert validation.target_count == 1
    assert validation.gold_target_passed == 1
    # base 与 gold 各一次全量运行；基线不重复跑。
    assert len(sandbox.run_calls) == 2
    assert validation.baseline is not None
    assert validation.baseline.sandbox_backend == "fake"


def test_base_not_failing_is_its_own_status(tmp_path: Path) -> None:
    """目标测试在 base 上就通过 → 这个任务根本没有缺陷。

    把它算进 benchmark 会直接抬高 Resolved Rate：模型什么都不做也算"解决"。
    """
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.PASSED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.BASE_NOT_FAILING
    assert not validation.usable
    assert TARGET in (validation.reason or "")


def test_gold_not_passing_is_its_own_status(tmp_path: Path) -> None:
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.GOLD_NOT_PASSING


def test_regression_outside_declared_tests_is_detected(tmp_path: Path) -> None:
    """gold 把**没写进 PASS_TO_PASS** 的测试改坏了，仍然是回归。

    这条要求 full-suite 的 gold 运行：只看声明清单的话，"其他测试不变"这个
    条件根本没有被检查过。
    """
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(
                _case(TARGET, TestOutcome.FAILED),
                _case(PASSED_TO_PASS, TestOutcome.PASSED),
                _case(OTHER, TestOutcome.PASSED),
            ),
            _result(
                _case(TARGET, TestOutcome.PASSED),
                _case(PASSED_TO_PASS, TestOutcome.PASSED),
                _case(OTHER, TestOutcome.FAILED),
            ),
        ],
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.REGRESSION_INTRODUCED
    assert validation.gold_result is not None
    assert OTHER in validation.gold_result.new_failures


def test_collection_error_when_target_never_ran(tmp_path: Path) -> None:
    """junit 里完全没有声明的目标测试，是"评测没发生"，不是"补丁失败"。"""
    _, evaluator = _evaluator(
        tmp_path,
        [_result(_case(PASSED_TO_PASS, TestOutcome.PASSED))],
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.COLLECTION_ERROR
    assert TARGET in (validation.reason or "")


def test_pass_to_pass_broken_on_base_is_invalid_fixture(tmp_path: Path) -> None:
    _, evaluator = _evaluator(
        tmp_path,
        [_result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.FAILED))],
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.INVALID_FIXTURE


def test_env_error_is_neither_invalid_nor_passing(tmp_path: Path) -> None:
    """ENV_ERROR 单独成状态，并且不进入"已验证"缓存。"""
    _, evaluator = _evaluator(
        tmp_path,
        [_result()],
        build_status=ExecutionStatus.ENV_ERROR,
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.ENV_ERROR
    assert not validation.usable
    assert validation.status.is_environment_failure
    assert validation.gold_result is None


def test_sandbox_error_is_its_own_status(tmp_path: Path) -> None:
    _, evaluator = _evaluator(
        tmp_path,
        [_result()],
        build_status=ExecutionStatus.SANDBOX_ERROR,
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.SANDBOX_ERROR
    assert validation.status.is_environment_failure


def test_env_error_on_gold_run_does_not_become_pass(tmp_path: Path) -> None:
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(status=ExecutionStatus.ENV_ERROR),
        ],
    )
    validation = validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    assert validation.status is ValidationStatus.ENV_ERROR
    assert validation.gold_result is not None
    assert validation.gold_result.resolved is None


def test_gold_patch_that_does_not_apply_is_gold_patch_invalid(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    # 手工破坏 gold.patch 的上下文，让 git apply 拒绝它。
    (fixture.directory / "eval" / "gold.patch").write_text(
        fixture.evaluation.gold_patch.replace(
            "def normalize(value: int) -> int:", "def nonexistent_signature(x):"
        ),
        encoding="utf-8",
        newline="\n",
    )
    reloaded = load_fixture(fixture.directory)
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(status=ExecutionStatus.COLLECTION_ERROR),
        ],
    )
    validation = validate_fixture(evaluator, reloaded, run_id="run")
    assert validation.status is ValidationStatus.GOLD_PATCH_INVALID


def test_baseline_is_reused_across_evaluations(tmp_path: Path) -> None:
    """同一 fixture 的基线只跑一次；第二次评测复用缓存。"""
    sandbox, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(_case(TARGET, TestOutcome.PASSED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    fixture = _fixture(tmp_path)
    baseline = evaluator.baseline(fixture, run_id="a")
    evaluator.evaluate(
        fixture, fixture.evaluation.gold_patch, patch_source="model", run_id="a", baseline=baseline
    )
    evaluator.baseline(fixture, run_id="b")
    assert len(sandbox.run_calls) == 2, "基线应按 fixture_version 复用"
    assert sandbox.run_calls[0][1] == (), "基线跑全量套件"
    assert set(sandbox.run_calls[1][1]) == {TARGET, PASSED_TO_PASS}


def test_model_patch_evaluation_is_targeted(tmp_path: Path) -> None:
    """benchmark 判定只跑声明的 f2p/p2p，与 SWE-bench 口径一致。"""
    sandbox, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(_case(TARGET, TestOutcome.PASSED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    fixture = _fixture(tmp_path)
    record = evaluator.evaluate(
        fixture, fixture.evaluation.gold_patch, patch_source="model", run_id="run"
    )
    assert record.verdict is EvaluationVerdict.RESOLVED
    assert record.resolved is True
    assert set(sandbox.run_calls[1][1]) == {TARGET, PASSED_TO_PASS}


def test_evaluation_records_provenance(tmp_path: Path) -> None:
    """环境身份与后端必须留在记录里，否则事后无法区分"真跑过"与"伪造过"。"""
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(_case(TARGET, TestOutcome.PASSED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    fixture = _fixture(tmp_path)
    record = evaluator.evaluate(
        fixture, fixture.evaluation.gold_patch, patch_source="model", run_id="run"
    )
    assert record.sandbox_backend == "fake"
    assert record.environment.startswith("fake:")
    assert record.patch_sha256
    assert record.fixture_version == fixture.evaluation.fixture_version


# ------------------------------------------------------------------ 纯函数口径


def test_newly_failing_ignores_tests_already_broken_on_base() -> None:
    base = {"a": TestOutcome.FAILED, "b": TestOutcome.PASSED, "c": TestOutcome.SKIPPED}
    after = {"a": TestOutcome.FAILED, "b": TestOutcome.FAILED, "c": TestOutcome.SKIPPED}
    assert newly_failing(base, after) == ["b"]


def test_newly_failing_ignores_tests_absent_on_base() -> None:
    """base 上不存在（隐藏测试）不构成"回归"，它只是新加的测试。"""
    assert (
        newly_failing({"b": TestOutcome.PASSED}, {"b": TestOutcome.PASSED, "n": TestOutcome.FAILED})
        == []
    )


def test_newly_failing_treats_error_as_failure() -> None:
    assert newly_failing({"b": TestOutcome.PASSED}, {"b": TestOutcome.ERROR}) == ["b"]


def test_verdict_resolved_is_three_valued() -> None:
    assert EvaluationVerdict.RESOLVED.resolved is True
    assert EvaluationVerdict.NOT_RESOLVED.resolved is False
    for verdict in (
        EvaluationVerdict.ENV_ERROR,
        EvaluationVerdict.SANDBOX_ERROR,
        EvaluationVerdict.COLLECTION_ERROR,
        EvaluationVerdict.PATCH_APPLY_FAILED,
    ):
        assert verdict.resolved is None
        assert verdict.is_environment_failure is (
            verdict in (EvaluationVerdict.ENV_ERROR, EvaluationVerdict.SANDBOX_ERROR)
        )


def test_evaluation_workspace_still_has_no_eval_directory(tmp_path: Path) -> None:
    """评测侧用同一个 workspace 机制，但补丁是从**外部**应用的。

    这条守住一个具体的实现诱惑：把 ``eval/`` 复制进 workspace 再 ``git apply``
    会省一步跨目录操作，而那样 agent 跑的任何一条命令都可能读到它。
    """
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    repository = evaluator.materialized_repository(_fixture(tmp_path))
    assert not (repository / "eval").exists()
    assert (repository / "tests" / "test_hidden.py").exists() is False


def test_docker_and_fake_backends_are_distinguishable() -> None:
    """报告必须能分清真实容器与脚本化结果，不能靠"看起来一样"。"""
    sandbox = FakeSandbox([scripted_result(ExecutionStatus.PASS)])
    result = sandbox.run_tests(
        workspace=Path(),
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="x", cache_key="k"
        ),
        env_spec=EnvSpec(),
    )
    assert result.diagnostics["execution_backend"] == "fake"
    # DockerSandbox 的容器配置是纯函数，可以在没有 daemon 的机器上核对它确实
    # 断网并打上归属标签——评测结论的可归因性依赖这两条。
    config = execution_container_config(
        image_ref="img",
        workspace_volume="codepilot-token-ws",
        output_volume="codepilot-token-out",
        command="pytest",
        environment={},
    )
    assert config["network_mode"] == "none"
    assert config["labels"]["codepilot"] == "1"
    # 卷的 key 是卷名而不是宿主路径：daemon 看不见调用方文件系统时，宿主路径只会
    # 让容器静默地拿到空目录（Phase 11R 首次真实实验就是这么停的）。
    assert all(not Path(name).is_absolute() for name in config["volumes"])
    assert DockerSandbox is not None


def test_cleanup_removes_evaluation_workspaces(tmp_path: Path) -> None:
    """临时 workspace 与 artifact 不留下残骸（E-02 第 13 条）。"""
    _, evaluator = _evaluator(
        tmp_path,
        [
            _result(_case(TARGET, TestOutcome.FAILED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
            _result(_case(TARGET, TestOutcome.PASSED), _case(PASSED_TO_PASS, TestOutcome.PASSED)),
        ],
    )
    validate_fixture(evaluator, _fixture(tmp_path), run_id="run")
    runs = tmp_path / "work" / "runs"
    remaining = (
        sorted(path.name for path in runs.iterdir() if path.is_dir()) if runs.is_dir() else []
    )
    assert remaining == [], f"评测临时 run 目录未清理: {remaining}"


@pytest.mark.parametrize(
    "outcome,status",
    [
        (ValidationStatus.ENV_ERROR, False),
        (ValidationStatus.SANDBOX_ERROR, False),
        (ValidationStatus.INVALID_FIXTURE, False),
        (ValidationStatus.VALID, True),
    ],
)
def test_only_valid_is_usable(outcome: ValidationStatus, status: bool) -> None:
    assert outcome.usable is status
