"""Phase26 invariants. Authored under static-only authorization; NOT EXECUTED."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from codepilot.agents.exploration import ExplorationProgress
from codepilot.api.runtime_view import activity_view
from codepilot.observability.events import EventSink
from codepilot.observability.lifecycle import Lifecycle
from codepilot.rsi import archive_candidate, load_selection, replay, select, validate_snapshot
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.plan import ContractItem, RepairPlan
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.edit_tools import create_file_tool, edit_file_tool
from codepilot.tools.query_cache import QueryCache
from codepilot.tools.read_tools import read_file_tool, search_code_tool
from codepilot.tools.registry import ToolRegistry, ToolRole


def registry(root: Path) -> ToolRegistry:
    policy = WorkspacePathPolicy(root)
    result = ToolRegistry(
        [
            read_file_tool(policy),
            search_code_tool(policy),
            edit_file_tool(policy),
            create_file_tool(policy),
        ]
    )
    result.query_cache = QueryCache(policy)
    return result


def test_read_content_identity_detects_external_same_length_change(tmp_path: Path) -> None:
    path = tmp_path / "core.py"
    path.write_text("old\n", encoding="utf-8")
    tools = registry(tmp_path)
    args = {"path": "core.py"}
    assert not tools.execute("read_file", args).data["cache"]["hit"]
    assert tools.execute("read_file", args).data["cache"]["hit"]
    path.write_text("new\n", encoding="utf-8")
    result = tools.execute("read_file", args)
    assert "new" in result.data["content"]
    assert not result.data["cache"]["hit"]


def test_new_file_invalidates_cached_negative_and_write_invalidates_reads(tmp_path: Path) -> None:
    tools = registry(tmp_path)
    (tmp_path / "src").mkdir()
    args = {"pattern": "new_evidence", "path_glob": "src/**"}
    assert tools.execute("search_code", args).data["matches"] == []
    assert tools.execute("search_code", args).data["cache"]["hit"]
    (tmp_path / "src/new.py").write_text("new_evidence = 1", encoding="utf-8")
    found = tools.execute("search_code", args)
    assert found.data["matches"] and not found.data["cache"]["hit"]
    tools.execute("read_file", {"path": "src/new.py"})
    edited = tools.execute(
        "edit_file",
        {"path": "src/new.py", "search": "new_evidence = 1", "replace": "new_evidence = 2"},
    )
    assert edited.ok
    assert not tools.execute("read_file", {"path": "src/new.py"}).data["cache"]["hit"]


def test_query_permissions_empty_missing_and_truncated_are_distinct(tmp_path: Path) -> None:
    (tmp_path / "empty.py").write_text("", encoding="utf-8")
    (tmp_path / "many.py").write_text("match\n" * 60, encoding="utf-8")
    tools = registry(tmp_path)
    assert tools.execute("read_file", {"path": "empty.py"}).data["cache"]["result_kind"] == "empty"
    assert not tools.execute("read_file", {"path": "absent.py"}).ok
    tools.execute("read_file", {"path": "empty.py"}, role=ToolRole.PLANNER)
    assert not tools.execute("read_file", {"path": "empty.py"}, role=ToolRole.CODER).data["cache"][
        "hit"
    ]
    truncated = tools.execute("search_code", {"pattern": "match"})
    assert truncated.data["cache"]["result_kind"] == "truncated"
    assert not tools.execute("search_code", {"pattern": "match"}).data["cache"]["hit"]
    assert not tools.execute("read_file", {"path": "../outside"}).ok


def test_failed_cache_keeps_original_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.tools.read_tools as read_tools

    (tmp_path / "core.py").write_text("needle", encoding="utf-8")
    tools = registry(tmp_path)
    monkeypatch.setattr(read_tools.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        tools.query_cache, "execute", lambda *a: (_ for _ in ()).throw(ValueError("broken cache"))
    )
    result = tools.execute("search_code", {"pattern": "needle"})
    assert result.ok and result.data["engine"] == "python_fallback"
    assert result.data["matches"]


def test_exploration_is_advisory_and_hypothesis_can_reset_low_yield() -> None:
    progress = ExplorationProgress(relevant_files={"core.py"})
    for _ in range(5):
        facts = progress.observe(
            "search_code", {"pattern": "old"}, {"ok": True, "data": {"matches": []}}, ""
        )
    assert facts["feedback"] and not facts["has_modification"]
    changed = progress.observe(
        "read_file",
        {"path": "core.py"},
        {"ok": True, "data": {"path": "core.py", "content": "cause"}},
        "Hypothesis: wrong default",
    )
    assert changed["hypothesis_changed"] and changed["new_relevant_evidence"] == 1
    assert changed["low_yield_streak"] == 0


def test_legacy_plan_contract_defaults_unknown_and_claims_need_sources() -> None:
    plan = RepairPlan(root_cause_hypothesis="unknown", alternatives=[])
    assert all(item["confidence"] == "unknown" for item in plan.contract.model_dump().values())
    with pytest.raises(ValidationError):
        ContractItem(text="must return tuple", confidence="explicit")


def test_observer_cannot_mutate_state_and_failure_does_not_block_next(tmp_path: Path) -> None:
    class Broken:
        name = "broken"
        version = "1"

        def observe(self, hook: str, state: Any, facts: Any) -> dict[str, Any]:
            state["nested"]["value"] = "corrupted"
            return {}

    sink = EventSink(run_id="run", task_id="task", jsonl_path=tmp_path / "events.jsonl")
    from codepilot.observability.lifecycle import ResourceObserver

    lifecycle = Lifecycle(sink, observers=(Broken(), ResourceObserver()))
    original = {"nested": {"value": "unchanged"}}
    lifecycle.bind(original, "policy")
    suggestions = lifecycle.emit("before_model")
    assert suggestions[0]["resource"] == "model"
    assert original["nested"]["value"] == "unchanged"
    assert "observer_error" in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_offline_candidate_never_promotes_and_selection_is_explicit(tmp_path: Path) -> None:
    proposal = {
        "parent": None,
        "hypothesis": "reduce repeated reads",
        "input_evidence": ["public-issue"],
        "candidate_changes": "query dedup",
        "code_revision": "test-only",
    }
    archive_candidate(tmp_path, "parent", proposal)
    archive_candidate(tmp_path, "child", {**proposal, "parent": "parent"})
    assert not (tmp_path / "active.json").exists()
    with pytest.raises(ValueError, match="allow-pending"):
        select(tmp_path, "child")
    select(tmp_path, "child", allow_pending=True)
    selected = load_selection(tmp_path)
    assert selected["parent"] == "parent" and selected["status"] == "pending"
    select(tmp_path, selected["parent"], allow_pending=True)
    assert load_selection(tmp_path)["version"] == "parent"
    with pytest.raises(FileExistsError):
        archive_candidate(tmp_path, "parent", proposal)


def test_l3_cannot_be_created_without_comparison_or_with_hidden_evidence() -> None:
    entry = {
        "id": "meta",
        "level": "L3",
        "source": ["public"],
        "source_run": "run",
        "applies_when": "x",
        "invalid_when": "y",
        "action": "z",
        "version": "1",
        "status": "pending",
        "evaluation_exposure": "public_only",
    }
    with pytest.raises(ValueError, match="comparison"):
        validate_snapshot({"schema_version": 1, "experiences": [entry]})
    with pytest.raises(ValueError, match="hidden"):
        validate_snapshot(
            {
                "schema_version": 1,
                "experiences": [{**entry, "level": "L2", "evaluation_exposure": "hidden"}],
            }
        )


def test_replay_only_reads_events_and_distinguishes_decision_from_effect(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {"extra": {"hook": "exploration", "facts": {"repeated_query": True, "feedback": "old"}}}
        )
        + "\ninvalid\n",
        encoding="utf-8",
    )
    original = path.read_bytes()
    assert replay(path)[0]["mode"] == "trace"
    decision = replay(path, decisions=True)
    assert decision[0]["mode"] == "deterministic_decision"
    assert decision[0]["effect_validation"] == "NOT_RUN"
    assert path.read_bytes() == original


def test_runtime_queue_execution_and_external_wait_are_not_a_percentage() -> None:
    events = [
        {
            "extra": {
                "hook": "stage_start",
                "facts": {
                    "runtime": {
                        "stage": "generate_patch",
                        "stage_started_at": "2026-10-28T00:00:00+00:00",
                    }
                },
            }
        },
        {"extra": {"hook": "before_model"}},
    ]
    view = activity_view(events, now=datetime(2026, 10, 28, 0, 0, 10, tzinfo=UTC), terminal=False)
    assert view["activity"] == "external_wait" and view["stage_elapsed_seconds"] == 10
    assert "percent" not in view


def test_runner_timeout_resolves_before_budget_clamp(tmp_path: Path) -> None:
    from codepilot.artifacts import ArtifactStore
    from codepilot.sandbox.env_spec import EnvSpec
    from codepilot.sandbox.official import OfficialInstanceSandbox
    from codepilot.sandbox.runners import RunnerConfig

    sandbox = object.__new__(OfficialInstanceSandbox)
    sandbox.instance_id = "owner__repo-1"
    sandbox.runner = SimpleNamespace(
        config=RunnerConfig(
            kind="pytest", test_command="pytest --junitxml=/out/junit.xml", timeout_seconds=600
        )
    )
    sandbox.runners = None
    sandbox.artifacts = ArtifactStore(tmp_path, "run")
    spec = EnvSpec(timeout_seconds=300)
    effective = sandbox.effective_env_spec(spec)
    assert effective.timeout_seconds == 600
    config = json.loads(
        (sandbox.artifacts.run_dir / "runner-effective-config.json").read_text(encoding="utf-8")
    )
    assert config["timeout_source"] == "runner_override"


def test_snapshot_is_fixed_across_new_process_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.graph.harness as harness
    from codepilot.artifacts import ArtifactStore
    from codepilot.schemas.state import RepairState

    def deps() -> Any:
        artifacts = ArtifactStore(tmp_path, "run")
        events = EventSink(
            run_id="run", task_id="task", jsonl_path=artifacts.run_dir / "events.jsonl"
        )
        return SimpleNamespace(
            artifacts=artifacts,
            lifecycle=Lifecycle(events),
            harness_started=False,
            coder=SimpleNamespace(system_prompt="coder-v1"),
            planner=SimpleNamespace(system_prompt="planner-v1"),
            reviewer=SimpleNamespace(system_prompt="reviewer-v1"),
            repository="repo",
            env_spec=EnvSpec(),
            clock=lambda: datetime.now(UTC),
            experience_snapshot=[],
            tools=SimpleNamespace(query_cache=None),
        )

    monkeypatch.setattr(
        harness, "load_selection", lambda: {"schema_version": 1, "version": "v1", "experiences": []}
    )
    initial = RepairState(
        task_id="task",
        run_id="run",
        repository_id="repo",
        base_commit="abc",
        issue="public issue",
        started_at=datetime.now(UTC),
    )
    first = harness.begin(initial, deps(), "plan")
    monkeypatch.setattr(
        harness,
        "load_selection",
        lambda: (_ for _ in ()).throw(AssertionError("must use saved snapshot")),
    )
    resumed_deps = deps()
    resumed_deps.coder.system_prompt = "changed-on-disk"
    resumed = harness.begin(first, resumed_deps, "generate_patch")
    assert resumed.policy_snapshot == first.policy_snapshot
    assert resumed_deps.coder.system_prompt == "coder-v1"
    path = resumed_deps.artifacts.run_dir / "harness/snapshot.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        harness.begin(first, deps(), "generate_patch")


def test_timeout_summary_retains_missing_scope_and_stale_candidate_cannot_pass() -> None:
    from codepilot.graph.evidence import failure_summary, verification_view
    from codepilot.graph.stop_rules import patch_hash
    from codepilot.schemas.enums import ExecutionStatus
    from codepilot.schemas.execution import ExecutionResult
    from codepilot.schemas.state import Attempt, RepairState
    from codepilot.schemas.validation import ValidationResult

    state = RepairState(
        task_id="task",
        run_id="run",
        repository_id="repo",
        base_commit="abc",
        issue="public",
        started_at=datetime.now(UTC),
        current_patch="candidate",
    )
    state.attempts.append(
        Attempt(
            iteration=1,
            patch_hash=patch_hash("candidate"),
            validation=ValidationResult(passed=True),
            target_tests=["tests/test_core.py::test_a"],
            execution=ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                diagnostics={"requested_missing": ["tests/test_core.py::test_a"]},
                artifact_dir="attempts/1",
            ),
        )
    )
    summary = failure_summary(state)
    assert summary["category"] == "timeout" and summary["scope"]["missing"] == 1
    assert summary["candidate_hash"] == patch_hash("candidate")
    state.current_patch = "different candidate"
    assert failure_summary(state) == {}
    assert verification_view(state)["execution"] == "NOT_EXECUTED"


def test_coder_cancel_stops_next_tool_and_retains_first_write(tmp_path: Path) -> None:
    from codepilot.agents.coder import CodingAgent
    from codepilot.llm.fake import FakeLLM
    from codepilot.llm.types import LLMResponse, ToolCall
    from codepilot.schemas.issue import IssueAnalysis

    response = LLMResponse(
        content="Hypothesis: fix public behavior",
        tool_calls=[
            ToolCall(
                id="one", name="create_file", arguments={"path": "first.py", "content": "first"}
            ),
            ToolCall(
                id="two", name="create_file", arguments={"path": "second.py", "content": "second"}
            ),
        ],
    )
    provider = FakeLLM([response])
    coder = CodingAgent(provider, registry(tmp_path))
    coder.cancel_check = lambda: (tmp_path / "first.py").exists()
    result = coder.run(
        issue_text="public issue",
        issue_analysis=IssueAnalysis(summary="public issue"),
        plan=RepairPlan(root_cause_hypothesis="fix default", alternatives=[]),
    )
    assert result.stop_reason == "cancelled" and result.llm_calls == 1
    assert (tmp_path / "first.py").read_text(encoding="utf-8") == "first"
    assert not (tmp_path / "second.py").exists()
    assert len(result.tool_calls) == 1


def test_tool_timeout_callback_is_applied_before_any_sandbox_side_effect(tmp_path: Path) -> None:
    from codepilot.sandbox.env_spec import EnvSpec
    from codepilot.schemas.enums import ExecutionStatus
    from codepilot.schemas.execution import ExecutionResult
    from codepilot.tools.write_tools import TestRunBudget, run_tests_tool

    (tmp_path / "test_core.py").write_text("def test_core(): pass", encoding="utf-8")
    calls: list[int] = []

    class Sandbox:
        def run_tests(self, **kwargs: Any) -> ExecutionResult:
            calls.append(kwargs["env_spec"].timeout_seconds)
            return ExecutionResult(status=ExecutionStatus.TIMEOUT)

    budget = TestRunBudget(3, before_run=lambda: EnvSpec(timeout_seconds=90))
    tool = run_tests_tool(
        WorkspacePathPolicy(tmp_path),
        Sandbox(),
        SimpleNamespace(),
        EnvSpec(timeout_seconds=600),
        budget,
    )
    assert tool.execute({"test_ids": ["test_core.py"]}).ok
    assert calls == [90] and budget.used == 1
    budget.before_run = lambda: None
    assert not tool.execute({"test_ids": ["test_core.py"]}).ok
    assert calls == [90] and budget.used == 1
