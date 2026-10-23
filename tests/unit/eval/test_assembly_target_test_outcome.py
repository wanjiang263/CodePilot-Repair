"""A3: ``InferenceOutcome`` must expose the Attempt's target-test facts verbatim."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from codepilot.artifacts import ArtifactStore
from codepilot.eval.assembly import InferenceAssembly, ProviderBundle
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.validation import ValidationResult

PATCH = (
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "--- a/src/pkg/core.py\n"
    "+++ b/src/pkg/core.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


def _outcome(tmp_path: Path, state: RepairState) -> Any:
    assembly = InferenceAssembly(
        task=SimpleNamespace(task_id=state.task_id),  # type: ignore[arg-type]
        workspace_manager=None,  # type: ignore[arg-type]
        work_root=tmp_path / "work",
        sandbox_factory=lambda artifacts: None,  # type: ignore[arg-type]
        provider_bundle=ProviderBundle.scripted({}),
    )
    artifacts = ArtifactStore(tmp_path / "work", state.run_id)
    return assembly._outcome(
        run_id=state.run_id,
        seed=1,
        state=state,
        artifacts=artifacts,
        runtime=0.1,
    )


def _state(*, attempts: list[Attempt]) -> RepairState:
    return RepairState(
        task_id="task-a3",
        run_id="run-a3",
        repository_id="repo_deadbeef",
        base_commit="0" * 40,
        issue="target test wiring",
        current_patch=PATCH if attempts else "",
        attempts=attempts,
        started_at=datetime.now(UTC),
    )


def test_outcome_without_targets_reports_none_and_empty(tmp_path: Path) -> None:
    outcome = _outcome(tmp_path, _state(attempts=[]))

    assert outcome.target_test_source is None
    assert outcome.target_tests == ()


def test_outcome_carries_attempt_target_source_and_tests(tmp_path: Path) -> None:
    attempt = Attempt(
        iteration=1,
        patch_diff=PATCH,
        validation=ValidationResult(passed=True),
        target_test_source="impact",
        target_tests=["tests/test_core.py", "tests/unit/test_helper.py::test_coerce"],
    )
    state = _state(attempts=[attempt])

    outcome = _outcome(tmp_path, state)

    assert state.current_attempt is attempt
    assert outcome.target_test_source == state.current_attempt.target_test_source == "impact"
    assert outcome.target_tests == tuple(state.current_attempt.target_tests)
