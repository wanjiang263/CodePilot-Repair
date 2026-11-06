"""Phase 26 风险 C：时间估算的适用性与派发前后的边界（真实执行）。

重点不是"估算函数算得对不对"，而是 ``AccountingSandbox.run_tests`` 这条链：

- 旧候选（不同 identity）的慢样本不能影响新候选，也不能拦下派发；
- 余量不足时按 runner 上限受限派发，而不是直接拒绝；
- 资源拿到之前就停，不算一次测试运行；
- 范围不是精确节点集时如实记成 aggregate，不标 coverage；
- Review/closure 预留只扣一次。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from codepilot.graph.deps import AccountingSandbox
from codepilot.graph.validation_time import timing_identity
from codepilot.observability.dispatch import (
    Deadline,
    DispatchScope,
    DispatchStoppedError,
    dispatch_scope,
)
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.execution import ExecutionResult


def _identity(base: str, spec: EnvSpec, selectors: list[str]) -> str:
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS, image_id="image", cache_key="env"
    )
    return timing_identity(
        "",
        base,
        {**environment.model_dump(mode="json"), "runner_identity": "types.SimpleNamespace:pytest"},
        spec.model_dump(mode="json"),
        selectors,
    )


def _sandbox(seen: list[int], *, base: str = "") -> AccountingSandbox:
    def run_tests(**kwargs):
        seen.append(kwargs["env_spec"].timeout_seconds)
        # Windows 的单调时钟分辨率约 15.6ms，瞬间返回会被记成 0 秒并当作无效样本丢掉。
        time.sleep(0.05)
        return ExecutionResult(status=ExecutionStatus.PASS)

    sandbox = AccountingSandbox(SimpleNamespace(run_tests=run_tests))
    sandbox.base_commit = base
    return sandbox


def test_a_changed_candidate_ignores_old_slow_samples_and_still_dispatches(tmp_path):
    spec = EnvSpec(timeout_seconds=300)
    selectors = ["test.py::test_one"]
    old_identity = _identity("old-base", spec, selectors)
    seen: list[int] = []
    sandbox = _sandbox(seen, base="new-base")
    sandbox.timing_facts = [
        {"identity": old_identity, "seconds": seconds, "complete": True, "status": "pass"}
        for seconds in (260, 270, 280)
    ]

    result = sandbox.run_tests(
        workspace=tmp_path,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_id="image", cache_key="env"
        ),
        env_spec=spec,
        tests=selectors,
    )

    allocation = result.diagnostics["time_allocation"]
    assert seen == [300]
    assert allocation["source"] == "conservative_default"
    assert allocation["complete_samples"] == 0
    assert allocation["margin_insufficient"] is False
    assert sandbox.runs == 1


def test_insufficient_margin_dispatches_with_the_runner_cap_not_a_refusal(tmp_path):
    spec = EnvSpec(timeout_seconds=300)
    selectors = ["test.py::test_one"]
    seen: list[int] = []
    sandbox = _sandbox(seen)
    identity = _identity("", spec, selectors)
    sandbox.timing_facts = [
        {"identity": identity, "seconds": 250, "complete": True, "status": "pass"},
        {"identity": identity, "seconds": 320, "status": "timeout"},
    ]

    result = sandbox.run_tests(
        workspace=tmp_path,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_id="image", cache_key="env"
        ),
        env_spec=spec,
        tests=selectors,
    )

    allocation = result.diagnostics["time_allocation"]
    assert seen == [300]
    assert allocation["suggested_seconds"] > 300
    assert allocation["final_timeout_seconds"] == 300
    assert allocation["margin_insufficient"] is True
    assert allocation["deadline_available_seconds"] == 300


def test_a_run_stopped_before_dispatch_does_not_count_or_record(tmp_path):
    spec = EnvSpec(timeout_seconds=300)
    seen: list[int] = []
    sandbox = _sandbox(seen)

    with (
        dispatch_scope(DispatchScope(Deadline(time.monotonic() + 0.2), reserve=0)),
        pytest.raises(DispatchStoppedError),
    ):
        sandbox.run_tests(
            workspace=tmp_path,
            environment=EnvironmentBuildResult(
                status=ExecutionStatus.PASS, image_id="image", cache_key="env"
            ),
            env_spec=spec,
            tests=["test.py::test_one"],
        )

    assert seen == []
    assert sandbox.runs == 0
    assert sandbox.timing_facts == []
    assert sandbox.run_log == []


def test_incomplete_scope_is_recorded_as_aggregate_evidence_only(tmp_path):
    spec = EnvSpec(timeout_seconds=300)
    selectors = ["test.py"]
    seen: list[int] = []
    sandbox = _sandbox(seen)
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS, image_id="image", cache_key="env"
    )

    first = sandbox.run_tests(
        workspace=tmp_path, environment=environment, env_spec=spec, tests=selectors
    )
    fact = first.diagnostics["timing_fact"]
    assert fact["complete"] is False
    assert fact["process_completed"] is True
    assert "aggregate" in fact["scope_note"]

    second = sandbox.run_tests(
        workspace=tmp_path, environment=environment, env_spec=spec, tests=selectors
    )
    allocation = second.diagnostics["time_allocation"]
    assert allocation["source"] == "conservative_default+exact_scope_aggregate" or (
        "exact_scope_aggregate" in allocation["source"]
    )
    assert allocation["coverage_reduced"] is False
    assert allocation["complete_samples"] == 0
    assert allocation["aggregate_samples"] == 1


def test_closure_reservation_is_deducted_once_from_the_remaining_window():
    with dispatch_scope(DispatchScope(Deadline(30, clock=lambda: 0), reserve=10)):
        from codepilot.observability.dispatch import dispatch_timeout

        # 30 秒窗口扣一次 10 秒预留 = 20；扣两次会得到 10。
        assert dispatch_timeout(300) == 20
        # 给 runner 的上限仍然生效。
        assert dispatch_timeout(5) == 5
