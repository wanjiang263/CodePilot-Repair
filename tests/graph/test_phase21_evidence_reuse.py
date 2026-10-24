"""Phase 21: evidence is bound to the candidate patch, not the attempt number."""

from __future__ import annotations

from pathlib import Path

from harness import FINISH, build_harness, edit, passed_result

from codepilot.graph.operations import execute_once
from codepilot.patch.diff import generate_git_diff
from codepilot.schemas.execution import ExecutionResult


def test_identical_patch_reuses_across_iterations_and_changed_patch_does_not(
    tmp_path: Path,
) -> None:
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
    )
    state = harness.run()
    workspace = harness.deps.require_workspace()
    calls: list[int] = []

    def action() -> ExecutionResult:
        calls.append(1)
        return passed_result()

    first = execute_once(state, harness.deps, "phase21-probe", ExecutionResult, action)
    assert len(calls) == 1

    # A later attempt with the byte-identical candidate resolves to the same
    # operation identity, so the completed evidence is reused instead of
    # spending a second container run.
    later = state.model_copy(update={"iteration": state.iteration + 1})
    second = execute_once(later, harness.deps, "phase21-probe", ExecutionResult, action)
    assert len(calls) == 1
    assert first == second

    # Changing the workspace changes the patch identity. The old evidence must
    # not be returned for the new candidate, even under the same key.
    (workspace / "src" / "pkg" / "extra.py").write_text("VALUE = 2\n", encoding="utf-8")
    diff = generate_git_diff(workspace, state.base_commit, policy=harness.deps.validator.policy)
    changed = state.model_copy(
        update={"iteration": state.iteration + 2, "current_patch": diff.text}
    )
    third = execute_once(changed, harness.deps, "phase21-probe", ExecutionResult, action)
    assert len(calls) == 2
    assert third == first
