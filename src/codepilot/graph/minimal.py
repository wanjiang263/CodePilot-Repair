"""Single-patch LangGraph workflow for the Phase 5 M1 loop (P-20)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from codepilot.agents.coder import CodingAgent
from codepilot.agents.impact_analyzer import ImpactAnalyzer
from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.localizer import Localizer
from codepilot.agents.planner import Planner
from codepilot.artifacts import EXECUTION_JSON, PATCH_DIFF, VALIDATION_JSON, ArtifactStore
from codepilot.patch.diff import generate_git_diff
from codepilot.patch.validator import PatchValidator
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import RepositoryIndex
from codepilot.report import RunReport, write_run_report
from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason
from codepilot.schemas.retrieval import RetrievalResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.usage import Usage
from codepilot.tools.write_tools import validate_test_target

__all__ = ["MinimalDependencies", "build_minimal_graph", "run_minimal_graph"]


@dataclass(slots=True)
class MinimalDependencies:
    repository: str
    workspace: Path
    index: RepositoryIndex
    graph: RepoGraph
    issue_analyzer: IssueAnalyzer
    retriever: Callable[[str], RetrievalResult]
    localizer: Localizer
    impact_analyzer: ImpactAnalyzer
    planner: Planner
    coder: CodingAgent
    validator: PatchValidator
    sandbox: Sandbox
    environment: EnvironmentBuildResult
    env_spec: EnvSpec
    artifacts: ArtifactStore
    usage: Usage = field(default_factory=Usage)
    simulated_llm: bool = False


def build_minimal_graph(deps: MinimalDependencies) -> Any:
    # LangGraph's generic overloads do not currently express Pydantic state
    # nodes that return partial mapping updates.  Keep the runtime graph fully
    # typed at its boundary while avoiding false-positive add_node overload
    # errors from the third-party stubs.
    workflow: Any = StateGraph(RepairState)
    workflow.add_node("understand", _guard("understand", _understand(deps)))
    workflow.add_node("retrieve", _guard("retrieve", _retrieve(deps)))
    workflow.add_node("localize", _guard("localize", _localize(deps)))
    workflow.add_node("plan", _guard("plan", _plan(deps)))
    workflow.add_node("generate", _guard("generate", _generate(deps)))
    workflow.add_node("validate", _guard("validate", _validate(deps)))
    workflow.add_node("run_tests", _guard("run_tests", _run_tests(deps)))
    workflow.add_node("finalize", _finalize(deps))
    workflow.add_edge(START, "understand")
    _route(workflow, "understand", "retrieve")
    _route(workflow, "retrieve", "localize")
    _route(workflow, "localize", "plan")
    _route(workflow, "plan", "generate")
    _route(workflow, "generate", "validate")
    _route(workflow, "validate", "run_tests")
    workflow.add_edge("run_tests", "finalize")
    workflow.add_edge("finalize", END)
    return workflow.compile()


def run_minimal_graph(initial: RepairState, deps: MinimalDependencies) -> RepairState:
    result = build_minimal_graph(deps).invoke(initial.model_dump(mode="python"))
    return RepairState.model_validate(result)


def _understand(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        if not deps.environment.succeeded:
            return {
                "status": RunStatus.SANDBOX_ERROR,
                "stop_reason": StopReason.SANDBOX_FAILURE,
                "failure_reasons": [deps.environment.error or deps.environment.status.value],
                "events": _event(state, "understand", "environment_error"),
            }
        files = [item.path for item in deps.index.files()]
        analysis = deps.issue_analyzer.analyze(
            state.issue, repo_files=files, repo_root=deps.workspace
        )
        return {"issue_analysis": analysis, "events": _event(state, "understand", "ok")}

    return node


def _retrieve(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        result = deps.retriever(state.issue)
        return {"retrieval": result, "events": _event(state, "retrieve", "ok")}

    return node


def _localize(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        if state.issue_analysis is None or state.retrieval is None:
            raise ValueError("localize requires issue analysis and retrieval")
        result = deps.localizer.localize(state.issue_analysis, state.retrieval)
        return {"localization": result, "events": _event(state, "localize", "ok")}

    return node


def _plan(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        if state.issue_analysis is None or state.localization is None:
            raise ValueError("plan requires issue analysis and localization")
        impact = deps.impact_analyzer.analyze(state.localization)
        plan = deps.planner.plan(
            issue_text=state.issue,
            issue_analysis=state.issue_analysis,
            localization=state.localization,
            impact=impact,
            retrieval=state.retrieval,
        )
        return {
            "impact": impact,
            "plan": plan,
            "usage": deps.usage,
            "events": _event(state, "plan", "ok"),
        }

    return node


def _generate(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        if state.issue_analysis is None or state.plan is None:
            raise ValueError("generate requires issue analysis and plan")
        result = deps.coder.run(
            issue_text=state.issue,
            issue_analysis=state.issue_analysis,
            plan=state.plan,
            retrieval=state.retrieval,
        )
        patch = generate_git_diff(deps.workspace, state.base_commit)
        calls = [item.model_dump(mode="json") for item in result.tool_calls]
        update: dict[str, Any] = {
            "current_patch": patch.text,
            "tool_calls": calls,
            "usage": deps.usage,
            "events": _event(state, "generate", result.stop_reason),
        }
        if result.stop_reason in {"tool_limit", "token_budget"}:
            update.update(
                status=RunStatus.BUDGET_EXHAUSTED,
                stop_reason=StopReason.BUDGET_TOOL_CALLS,
                failure_reasons=[f"coding stopped: {result.stop_reason}"],
            )
        elif not patch.text and any(
            not item.ok and item.name != "finish" for item in result.tool_calls
        ):
            update.update(
                status=RunStatus.TOOL_ERROR,
                stop_reason=StopReason.TOOL_ERROR,
                failure_reasons=["all coding tool calls failed"],
            )
        return update

    return node


def _validate(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        tests = state.plan.tests_to_run if state.plan else []
        validation = deps.validator.validate(related_tests=tests, impact=state.impact)
        patch_hash = hashlib.sha256("".join(state.current_patch.split()).encode()).hexdigest()
        attempt = Attempt(
            iteration=1,
            patch_diff=state.current_patch,
            patch_hash=patch_hash,
            validation=validation,
        )
        update: dict[str, Any] = {
            "attempts": [attempt],
            "events": _event(state, "validate", "passed" if validation.passed else "rejected"),
        }
        if not validation.passed:
            update.update(
                status=RunStatus.PATCH_REJECTED,
                stop_reason=StopReason.PATCH_REJECTED,
                failure_reasons=list(validation.errors),
            )
        return update

    return node


def _run_tests(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        if state.plan is None:
            raise ValueError("run_tests requires a plan")
        targets = [
            validate_test_target(deps.validator.policy, item) for item in state.plan.tests_to_run
        ]
        execution = deps.sandbox.run_tests(
            workspace=deps.workspace,
            environment=deps.environment,
            env_spec=deps.env_spec,
            tests=targets,
            artifact_iteration=1,
        )
        attempts = list(state.attempts)
        if attempts:
            attempts[-1] = attempts[-1].model_copy(update={"execution": execution})
        if execution.status is ExecutionStatus.PASS:
            status = RunStatus.PROVISIONALLY_VERIFIED
            stop_reason = None
            failures: list[str] = []
        elif execution.is_environment_problem:
            status = RunStatus.SANDBOX_ERROR
            stop_reason = StopReason.SANDBOX_FAILURE
            failures = [execution.error_reason or execution.status.value]
        else:
            status = RunStatus.TESTS_FAILED
            stop_reason = StopReason.TESTS_FAILED
            failures = [execution.error_reason or execution.status.value]
        return {
            "execution": execution,
            "attempts": attempts,
            "test_runs": state.test_runs + 1,
            "status": status,
            "stop_reason": stop_reason,
            "failure_reasons": failures,
            "events": _event(state, "run_tests", execution.status.value),
        }

    return node


def _finalize(deps: MinimalDependencies) -> Callable[[RepairState], dict[str, Any]]:
    def node(state: RepairState) -> dict[str, Any]:
        patch = generate_git_diff(deps.workspace, state.base_commit)
        artifact_paths = {
            "patch": deps.artifacts.attempt_relpath(1, PATCH_DIFF),
            "validation": deps.artifacts.attempt_relpath(1, VALIDATION_JSON),
        }
        deps.artifacts.write_text(artifact_paths["patch"], patch.text)
        if state.current_attempt is not None:
            deps.artifacts.write_json(
                artifact_paths["validation"],
                state.current_attempt.validation.model_dump(mode="json"),
            )
        if state.execution is not None:
            artifact_paths["execution"] = deps.artifacts.attempt_relpath(1, EXECUTION_JSON)
            deps.artifacts.write_json(
                artifact_paths["execution"], state.execution.model_dump(mode="json")
            )
        final_events = _event(state, "finalize", state.status.value)
        interim = state.model_copy(
            update={"events": final_events, "report_artifacts": artifact_paths}
        )
        report = RunReport.from_state(
            interim,
            repository=deps.repository,
            git_diff=patch.text,
            modified_files=patch.files,
            simulated_llm=deps.simulated_llm,
            artifacts=artifact_paths,
        )
        report_paths = write_run_report(report, deps.artifacts, interim)
        return {
            "events": final_events,
            "report_artifacts": {**artifact_paths, **report_paths},
        }

    return node


def _guard(
    name: str, node: Callable[[RepairState], dict[str, Any]]
) -> Callable[[RepairState], dict[str, Any]]:
    def guarded(state: RepairState) -> dict[str, Any]:
        try:
            return node(state)
        except Exception as exc:  # explicit workflow boundary: error becomes auditable state
            return {
                "status": RunStatus.ERROR,
                "stop_reason": StopReason.WORKFLOW_ERROR,
                "failure_reasons": [f"{name}: {type(exc).__name__}: {exc}"],
                "events": _event(state, name, "error", str(exc)),
            }

    return guarded


def _route(workflow: Any, source: str, success: str) -> None:
    workflow.add_conditional_edges(
        source,
        lambda state: "continue" if state.status is RunStatus.RUNNING else "finalize",
        {"continue": success, "finalize": "finalize"},
    )


def _event(
    state: RepairState, node: str, status: str, error: str | None = None
) -> list[dict[str, Any]]:
    return [
        *state.events,
        {
            "seq": len(state.events) + 1,
            "node": node,
            "status": status,
            "error": error,
        },
    ]
