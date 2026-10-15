"""G-02：Deps 注入容器。

这里不重复 Phase 5 已经证明过的"用 Fake 依赖能跑图"（那条在
``tests/graph/test_path_first_pass.py`` 里用真实组件跑过），只测容器自己的约定：

* 身份绑定：拿 A 任务的 Deps 跑 B 任务必须抛错，而不是静默写出混合状态的 checkpoint；
* 计数器对齐：恢复时从 state 恢复 usage 与 test_runs，不能清零；
* 对象身份：``Usage`` 必须原地更新，因为 Coding Agent 持有同一个引用；
* Sandbox 计数：所有执行路径都经同一个计数器，包括 Coding Agent 的工具；
* 生命周期：``close()`` 释放 sink，但**不删 Workspace**（恢复证据）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codepilot.artifacts import ArtifactStore
from codepilot.graph.budget import BudgetManager
from codepilot.graph.deps import AccountingSandbox, Deps, DepsConfigurationError
from codepilot.observability.events import EventSink
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage


class StubSandbox:
    """记录调用次数但不提供任何结果语义的 Sandbox。"""

    def __init__(self) -> None:
        self.runs = 0

    def build_env(self, **kwargs: Any):
        return scripted_result(ExecutionStatus.PASS)

    def run_tests(self, **kwargs: Any):
        self.runs += 1
        return scripted_result(ExecutionStatus.PASS)

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:
        return []


def _state(
    run_id: str = "run-a", task_id: str = "task-a", base_commit: str = "abc123"
) -> RepairState:
    return RepairState(
        task_id=task_id,
        run_id=run_id,
        repository_id="repo",
        base_commit=base_commit,
        issue="x",
        started_at=datetime(2026, 10, 14, tzinfo=UTC),
    )


def _deps(tmp_path: Path, **overrides: Any) -> Deps:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    artifacts = ArtifactStore(tmp_path / "artifacts", overrides.get("run_id", "run-a"))
    events = EventSink(
        run_id=overrides.get("run_id", "run-a"),
        task_id="task-a",
        jsonl_path=artifacts.run_dir / "events.jsonl",
    )
    fields: dict[str, Any] = {
        "run_id": "run-a",
        "task_id": "task-a",
        "repository": str(tmp_path / "original"),
        "workspace": workspace,
        "base_commit": "abc123",
        "index": SimpleNamespace(),
        "repo_graph": SimpleNamespace(),
        "retriever": lambda issue: SimpleNamespace(),
        "issue_analyzer": SimpleNamespace(),
        "localizer": SimpleNamespace(),
        "impact_analyzer": SimpleNamespace(),
        "planner": SimpleNamespace(),
        "coder": SimpleNamespace(),
        "diagnoser": SimpleNamespace(),
        "sandbox": StubSandbox(),
        "environment": SimpleNamespace(succeeded=True),
        "env_spec": EnvSpec(),
        "validator": SimpleNamespace(),
        "tools": SimpleNamespace(),
        "context_builder": SimpleNamespace(),
        "artifacts": artifacts,
        "events": events,
    }
    fields.update(overrides)
    return Deps(**fields)


# ------------------------------------------------------------------ 身份绑定


def test_binding_rejects_a_foreign_run_id(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    with pytest.raises(DepsConfigurationError, match="run_id"):
        deps.bind(_state(run_id="run-b"))


def test_binding_rejects_a_foreign_task_id(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    with pytest.raises(DepsConfigurationError, match="task_id"):
        deps.bind(_state(task_id="task-b"))


def test_binding_rejects_a_foreign_base_commit(tmp_path: Path) -> None:
    """base commit 不一致意味着 Workspace 里是另一个世界的代码。"""
    deps = _deps(tmp_path)
    with pytest.raises(DepsConfigurationError, match="base_commit"):
        deps.bind(_state(base_commit="dead123"))


def test_binding_accepts_the_matching_state(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    deps.bind(_state())


def test_missing_run_id_is_rejected_at_construction(tmp_path: Path) -> None:
    """空 run_id 在 Deps 构造期就拒绝。

    ``ArtifactStore`` 也会拒绝空 run_id，但它抛的是普通 ``ValueError``，测它等于
    什么都没测到 Deps 的约定，所以这里用占位对象绕过 Artifact 层。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    with pytest.raises(DepsConfigurationError, match="run_id"):
        Deps(
            run_id="",
            task_id="task-a",
            repository="r",
            workspace=workspace,
            base_commit="abc123",
            index=SimpleNamespace(),
            repo_graph=SimpleNamespace(),
            retriever=lambda issue: SimpleNamespace(),
            issue_analyzer=SimpleNamespace(),
            localizer=SimpleNamespace(),
            impact_analyzer=SimpleNamespace(),
            planner=SimpleNamespace(),
            coder=SimpleNamespace(),
            diagnoser=SimpleNamespace(),
            sandbox=StubSandbox(),
            environment=SimpleNamespace(),
            env_spec=EnvSpec(),
            validator=SimpleNamespace(),
            tools=SimpleNamespace(),
            context_builder=SimpleNamespace(),
            artifacts=SimpleNamespace(),
            events=SimpleNamespace(),
        )


# ------------------------------------------------------------------ 计数与恢复


def test_bind_restores_usage_in_place(tmp_path: Path) -> None:
    """恢复后必须原地覆盖，而不是换成 state 里那个对象。

    Coding Agent / Planner 构造时持有 ``deps.usage`` 的引用；换对象会让后续调用
    记到一个 state 不再持有的 Usage 上，费用就在报告里消失了。
    """
    deps = _deps(tmp_path)
    original = deps.usage
    restored = Usage()
    restored.add_call(alias="coder", input_tokens=5_000, output_tokens=1_200, cost=3.5)
    assert restored.llm_calls == 1
    deps.bind(_state().model_copy(update={"usage": restored}))
    assert deps.usage is original
    assert original.input_tokens == 5_000
    assert original.output_tokens == 1_200
    assert original.cost == pytest.approx(3.5)
    assert original.llm_calls == 1
    assert set(original.by_model) == {"coder"}


def test_bind_seeds_the_sandbox_counter_so_test_runs_are_not_restarted(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    state = _state().model_copy(update={"test_runs": 9})
    deps.bind(state)
    assert deps.test_runs == 9
    # 恢复后新跑一次是第 10 次，不是从 1 重新数。
    deps.sandbox.run_tests(
        workspace=deps.workspace, environment=deps.environment, env_spec=deps.env_spec
    )
    assert deps.test_runs == 10


def test_every_run_tests_path_is_counted_including_tool_calls(tmp_path: Path) -> None:
    """计数发生在 Sandbox 层而不是节点层：复现校验与 run_tests 工具都跑不掉。"""
    deps = _deps(tmp_path)
    for _ in range(3):
        deps.sandbox.run_tests(
            workspace=deps.workspace, environment=deps.environment, env_spec=deps.env_spec
        )
    assert deps.test_runs == 3
    assert deps.accounting.run_log == [(str(deps.workspace), 0)] * 3


def test_two_runs_do_not_share_counters(tmp_path: Path) -> None:
    first = _deps(tmp_path / "a", run_id="run-a")
    second = _deps(tmp_path / "b", run_id="run-b")
    first.sandbox.run_tests(
        workspace=first.workspace, environment=first.environment, env_spec=first.env_spec
    )
    assert first.test_runs == 1
    assert second.test_runs == 0
    assert first.usage is not second.usage
    assert first.artifacts.run_id != second.artifacts.run_id


# ------------------------------------------------------------------ 时钟一致性


def test_budget_property_shares_the_injected_clock(tmp_path: Path) -> None:
    """``deps.budget`` 与 ``deps.rules`` 必须共用 deps.clock。

    否则节点入口检查用假时钟、停止判定用宿主机时间，同一个 run 里两套口径。
    """
    calls: list[datetime] = []

    def clock() -> datetime:
        moment = datetime(2026, 10, 14, tzinfo=UTC)
        calls.append(moment)
        return moment

    deps = _deps(tmp_path, clock=clock)
    assert deps.budget.elapsed_seconds(_state()) == 0.0
    assert calls, "the injected clock was never consulted"
    assert deps.rules.budget is deps.budget


def test_injected_stop_rules_are_kept(tmp_path: Path) -> None:
    from codepilot.graph.stop_rules import StopRules

    injected = StopRules(budget=BudgetManager())
    deps = _deps(tmp_path, stop_rules=injected)
    assert deps.rules is injected


# ------------------------------------------------------------------ 生命周期


def test_close_is_idempotent_and_stops_writing_events(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    deps.close()
    deps.close()
    assert deps.closed is True
    with pytest.raises(RuntimeError):
        deps.events.emit(node="x", type="node_end")


def test_close_keeps_the_workspace_and_artifacts(tmp_path: Path) -> None:
    """close 不删 Workspace：删了就没有可验证的恢复证据了（G-13 的前提）。"""
    deps = _deps(tmp_path)
    artifacts_dir = deps.artifacts.run_dir
    deps.close()
    assert deps.workspace.is_dir()
    assert artifacts_dir.is_dir()


def test_context_manager_closes_on_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), _deps(tmp_path):
        raise RuntimeError("boom")
    # 退出后仍可读取现场，只是不能再写事件。
    assert (tmp_path / "artifacts" / "runs" / "run-a").is_dir()


def test_accounting_sandbox_wraps_only_the_protocol(tmp_path: Path) -> None:
    inner = FakeSandbox([scripted_result(ExecutionStatus.FAIL)])
    wrapped = AccountingSandbox(inner)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    environment = FakeSandbox([scripted_result(ExecutionStatus.PASS)]).build_env(
        repository="r", base_commit="c", workspace=workspace, env_spec=EnvSpec()
    )
    result = wrapped.run_tests(workspace=workspace, environment=environment, env_spec=EnvSpec())
    assert result.status is ExecutionStatus.FAIL
    assert wrapped.runs == 1
    assert wrapped.cleanup(older_than_seconds=1) == []


def test_require_workspace_reports_a_missing_directory(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    deps.workspace = tmp_path / "does-not-exist"
    with pytest.raises(DepsConfigurationError, match="Workspace"):
        deps.require_workspace()
