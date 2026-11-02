"""Real Docker execution, scripted LLM only; original fixture never modified."""

import shutil
from pathlib import Path

import harness as harness_module
import pytest
from harness import FINISH, build_harness, diagnosis, edit

from codepilot.agents.reviewer import PatchReviewer
from codepilot.artifacts import ArtifactStore
from codepilot.graph.regression import RegressionRunner
from codepilot.llm.fake import FakeLLM
from codepilot.repo.gitcmd import run_git
from codepilot.sandbox.docker import DockerSandbox
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus


@pytest.mark.docker
@pytest.mark.parametrize("case", ["normal", "regression_break", "baseline_failures", "flaky_suite"])
def test_real_regression(tmp_path: Path, monkeypatch, case):
    fixture = "regression_break" if case == "normal" else case
    source = Path(__file__).parents[1] / "fixtures/repos" / fixture

    def base_repo(root, *, include_tests: bool = True):
        original = root / "original"
        shutil.copytree(source, original)
        run_git(["init", "-q"], cwd=original)
        run_git(["config", "user.email", "test@example.invalid"], cwd=original)
        run_git(["config", "user.name", "Test"], cwd=original)
        run_git(["add", "."], cwd=original)
        run_git(["commit", "-qm", "fixture base"], cwd=original)
        return original, run_git(["rev-parse", "HEAD"], cwd=original).stdout.strip()

    monkeypatch.setattr(harness_module, "_base_repo", base_repo)
    artifacts = ArtifactStore(tmp_path / "docker", case)
    sandbox = DockerSandbox(artifacts=artifacts, allowed_workspace_root=tmp_path)
    # Controlled test input: fail on the first post-patch affected execution,
    # then pass on retries. Every result is still produced by pytest in Docker.
    original_run = sandbox.run_tests
    calls = []

    def controlled_run(**kwargs):
        calls.append(tuple(kwargs.get("tests", ())))
        if case == "flaky_suite":
            spec = kwargs["env_spec"]
            flag = "fail" if len(calls) == 4 else "pass"
            kwargs["env_spec"] = spec.model_copy(update={"env": {**spec.env, "PHASE9_FLAKY": flag}})
        return original_run(**kwargs)

    monkeypatch.setattr(sandbox, "run_tests", controlled_run)
    edits = [edit("src/pkg/core.py", "return value - 1", "return value")]
    if case == "normal":
        edits.append(
            {
                "tool": "create_file",
                "arguments": {
                    "path": "tests/test_added.py",
                    "content": "from pkg.core import normalize\n\ndef test_added():\n    assert normalize(4) == 4\n",
                },
            }
        )
    if case == "regression_break":
        edits.append(edit("src/pkg/core.py", "return value * 2", "return value * 3"))
    harness = build_harness(
        tmp_path,
        results=[],
        baseline_results=[],
        sandbox_override=sandbox,
        coder_rounds=[[*edits, FINISH]],
        diagnoses=[diagnosis("give_up", line=5, text="return value * 3")],
        budget=RepairBudget(max_test_runs=16),
    )
    if case == "normal":
        harness.deps.validator.policy.allow_new_tests = True  # trusted task configuration
    analyzer = harness.deps.impact_analyzer.analyze

    def analyze(*args, **kwargs):
        result = analyzer(*args, **kwargs)
        return result.model_copy(
            update={"related_tests": ["tests/test_core.py", "tests/test_other.py"]}
        )

    monkeypatch.setattr(harness.deps.impact_analyzer, "analyze", analyze)
    harness.deps.regression_runner = RegressionRunner()
    reviewer_llm = FakeLLM([{"approved": True, "summary": "fixture review"}])
    harness.deps.reviewer = PatchReviewer(reviewer_llm)
    state = harness.run()
    assert state.regression is not None, state.failure_reasons
    if case == "regression_break":
        assert state.regression.new_failures == ["tests/test_other.py::test_other"]
        assert state.status is not RunStatus.SUCCEEDED
        assert state.review is None
        assert state.attempts[-1].diagnosis is not None
        # A real Docker failure supplies the identity; only its numeric message
        # is perturbed for the controlled collision negative.
        import re
        from dataclasses import replace

        from codepilot.failure.signature import failure_signature
        from codepilot.graph.progress import round_evidence

        failure = harness.deps.failure_parser.parse_execution(
            state.regression.executions["L2"]
        ).failures[0]
        changed = failure.model_copy(update={"message": re.sub(r"\d+", "99", failure.message)})
        assert changed.message != failure.message
        assert failure_signature(changed) == failure_signature(failure)
        evidence = round_evidence(state.attempts[-1])
        assert evidence is not None
        assert evidence.same_failures_as(evidence)
        conflicting = replace(
            evidence,
            failure_details=frozenset(
                (node, kind, message + " conflicting evidence")
                for node, kind, message in evidence.failure_details
            ),
        )
        assert evidence.signature_collision_with(conflicting)
        assert not evidence.same_failures_as(conflicting)
        harness.deps.artifacts.write_json(
            "collision-samples.json",
            {
                "source": "real Docker regression with controlled message perturbation",
                "pairs": 2,
                "false_merges": 0,
                "missed_repeats": 0,
                "repeated_positive": 1,
                "collision_suspected": 1,
                "real_model_quality_evaluated": False,
            },
        )
    else:
        assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
        assert state.regression.complete
        assert not state.regression.new_failures
        if case == "baseline_failures":
            assert (
                "tests/test_other.py::test_existing_failure" in state.regression.existing_failures
            )
        if case == "flaky_suite":
            node = "tests/test_other.py::test_controlled_flaky"
            assert node in state.regression.flaky
            assert len(state.regression.retries[node]) == 2
    assert state.test_runs == len(calls)
    assert all(
        e.diagnostics["execution_backend"] == "docker" for e in state.regression.executions.values()
    )
    assert run_git(["status", "--porcelain"], cwd=harness.original).stdout == ""

    if case == "normal":
        import json

        report = json.loads(
            (harness.deps.artifacts.run_dir / "report.json").read_text(encoding="utf-8")
        )
        assert "tests/test_added.py" in report["added_tests"]
        assert all("test_added" not in node for node in state.regression.executed_tests)
        assert "tests/test_added.py" in (
            harness.deps.artifacts.run_dir / "patch_final.diff"
        ).read_text(encoding="utf-8")
