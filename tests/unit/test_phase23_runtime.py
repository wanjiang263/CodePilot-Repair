from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from codepilot.eval.candidate import prepare_candidate, verify_candidate
from codepilot.graph.stage_recovery import import_stage
from codepilot.graph.target_tests import discover_target_files
from codepilot.schemas.enums import RunStatus
from codepilot.schemas.run_config import InferenceRunConfig, StageRecovery
from codepilot.schemas.state import RepairState
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.edit_tools import edit_file_tool
from support.gitfixture import init_repo


def test_typed_policy_blocks_test_write_before_mutation_and_allows_source(tmp_path):
    repo = init_repo(
        tmp_path / "repo", {"pkg/core.py": "value = 1\n", "tests/test_core.py": "assert True\n"}
    )
    config = InferenceRunConfig()
    policy = WorkspacePathPolicy(
        repo.path, deny_existing_test_edits=config.deny_existing_test_edits
    )
    tool = edit_file_tool(policy)
    before = (repo.path / "tests/test_core.py").read_bytes()
    result = tool.execute({"path": "tests/test_core.py", "search": "True", "replace": "False"})
    assert not result.ok
    assert "read-only" in result.error.message
    assert (repo.path / "tests/test_core.py").read_bytes() == before
    assert tool.execute({"path": "pkg/core.py", "search": "value = 1", "replace": "value = 2"}).ok
    with pytest.raises(ValidationError):
        InferenceRunConfig(unrecognized_policy=True)


def test_legal_candidate_is_official_eligible_even_with_internal_failure(tmp_path):
    project = tmp_path
    run = project / "run"
    repo = init_repo(run / "workspace", {"pkg/core.py": "value = 1\n"})
    (repo.path / "pkg/core.py").write_text("value = 2\n", encoding="utf8")
    state = RepairState(
        task_id="owner__repo-1",
        run_id="run",
        repository_id="repo",
        base_commit=repo.head,
        issue="fix core",
        status=RunStatus.FAILED,
        started_at=datetime.now(UTC),
    )
    from codepilot.patch.diff import generate_git_diff

    state = state.model_copy(update={"current_patch": generate_git_diff(repo.path, repo.head).text})
    (run / "state_final.json").write_text(state.model_dump_json(), encoding="utf8")
    ledger = project / "ledger.json"
    ledger.write_text(
        json.dumps({"requests": [{"status": "settled", "cost_cny": 0.01}]}), encoding="utf8"
    )
    receipt = prepare_candidate(
        project=project,
        run=run,
        instance=state.task_id,
        base=repo.head,
        source_ledger=ledger,
        revision="rev",
    )
    assert receipt
    data = json.loads(receipt.read_text(encoding="utf8"))
    assert data["internal_status"] == "failed"
    prediction, _ = verify_candidate(project, data)
    record = json.loads(prediction.read_text(encoding="utf8"))
    record["model_patch"] += "\n"
    prediction.write_text(json.dumps(record), encoding="utf8")
    with pytest.raises(ValueError, match="changed"):
        verify_candidate(project, data)


def test_existing_test_candidate_never_enters_official(tmp_path):
    run = tmp_path / "run"
    repo = init_repo(run / "workspace", {"tests/test_core.py": "assert 1 == 2\n"})
    (repo.path / "tests/test_core.py").write_text("assert 1 == 1\n", encoding="utf8")
    (run / "state_final.json").write_text(
        json.dumps({"task_id": "owner__repo-1", "base_commit": repo.head, "status": "failed"}),
        encoding="utf8",
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"requests":[{}]}', encoding="utf8")
    assert (
        prepare_candidate(
            project=tmp_path,
            run=run,
            instance="owner__repo-1",
            base=repo.head,
            source_ledger=ledger,
            revision="r",
        )
        is None
    )


def test_recovery_rejects_changed_state_before_applying_patch(tmp_path):
    source = tmp_path / "old/state.json"
    source.parent.mkdir()
    source.write_text("{}", encoding="utf8")
    recovery = StageRecovery(
        source_state=source,
        source_state_sha256="0" * 64,
        source_run_id="old",
        entry="review",
        historical_ledger_hashes={},
        reason="fix omitted review context",
    )
    initial = RepairState(
        task_id="task",
        run_id="new",
        repository_id="repo",
        base_commit="a" * 40,
        issue="x",
        started_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="state changed"):
        import_stage(initial, recovery, SimpleNamespace())


def test_target_discovery_uses_module_evidence_and_never_arbitrary_test():
    patch = "diff --git a/pkg/core.py b/pkg/core.py\n--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1 +1 @@\n-old\n+new\n"
    index = SimpleNamespace(
        files=lambda: [
            SimpleNamespace(path="tests/test_unrelated.py"),
            SimpleNamespace(path="tests/test_core.py"),
        ],
        symbol_objects=lambda: [],
    )
    graph = SimpleNamespace(related_tests=lambda name: [])
    assert discover_target_files(patch, index, graph) == ("tests/test_core.py",)


def test_explicit_stage_import_keeps_candidate_but_discards_blind_review(tmp_path):
    import hashlib

    from codepilot.artifacts import ArtifactStore
    from codepilot.graph.stop_rules import patch_hash
    from codepilot.patch.diff import generate_git_diff
    from codepilot.patch.validator import PatchValidator
    from codepilot.schemas.run_config import StageRecovery
    from codepilot.schemas.state import Attempt
    from codepilot.schemas.validation import ValidationResult

    repo = init_repo(tmp_path / "repo", {"pkg/core.py": "value = 1\n"})
    (repo.path / "pkg/core.py").write_text("value = 2\n", encoding="utf8")
    patch = generate_git_diff(repo.path, repo.head).text
    repo.git("checkout", "--", "pkg/core.py")
    initial = RepairState(
        task_id="task",
        run_id="new",
        repository_id="repo",
        base_commit=repo.head,
        issue="x",
        started_at=datetime.now(UTC),
    )
    old = initial.model_copy(
        update={
            "run_id": "old",
            "current_patch": patch,
            "attempts": [
                Attempt(
                    iteration=1,
                    patch_diff=patch,
                    patch_hash=patch_hash(patch),
                    validation=ValidationResult(passed=True),
                )
            ],
        }
    )
    source = tmp_path / "old/state_final.json"
    source.parent.mkdir()
    raw = old.model_dump(mode="json")
    raw["attempts"][0]["review"] = {"approved": True, "unverifiable_old_input": True}
    source.write_text(json.dumps(raw), encoding="utf8")
    old_bytes = source.read_bytes()
    recovery = StageRecovery(
        source_state=source,
        source_state_sha256=hashlib.sha256(old_bytes).hexdigest(),
        source_run_id="old",
        entry="review",
        candidate_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        historical_ledger_hashes={},
        reason="replace missing review evidence",
    )
    policy = WorkspacePathPolicy(repo.path, deny_existing_test_edits=True)
    deps = SimpleNamespace(
        validator=PatchValidator(policy, repo.head),
        artifacts=ArtifactStore(tmp_path / "artifacts", "new"),
    )
    recovered = import_stage(initial, recovery, deps)
    assert recovered.current_patch == patch
    assert recovered.current_attempt.review is None
    assert recovered.review is None
    assert recovered.usage.cost == 0
    assert recovered.run_id == "new"
    assert source.read_bytes() == old_bytes
    assert (repo.path / "pkg/core.py").read_text() == "value = 2\n"


def test_settled_cache_hits_do_not_charge_and_changed_inputs_use_live(tmp_path):
    from codepilot.llm.fake import FakeLLM
    from codepilot.llm.record import RecordingLLM
    from codepilot.llm.settled_cache import SettledResponseCache
    from codepilot.llm.types import LLMRequest, LLMResponse, Message

    archive = tmp_path / "old-calls.jsonl"
    old = RecordingLLM(
        FakeLLM(default=LLMResponse(content="old settled", cost=0.2, model_alias="model")), archive
    )
    request = LLMRequest(messages=[Message.user("same input")], purpose="reproducer")
    old.complete(request)
    live = FakeLLM(default=LLMResponse(content="new live", model_alias="model"))
    cache = SettledResponseCache(
        live, archive=archive, alias="model", evidence=tmp_path / "reuse.jsonl"
    )
    reused = cache.complete(request)
    assert reused.content == "old settled" and reused.cost == 0
    assert live.calls == []
    changed = cache.complete(
        LLMRequest(messages=[Message.user("changed input")], purpose="reproducer")
    )
    assert changed.content == "new live"
    assert len(live.calls) == 1
    assert json.loads((tmp_path / "reuse.jsonl").read_text())["new_cost_cny"] == 0


def test_typed_model_selection_reaches_actual_batch_transport_without_http():
    from pathlib import Path

    from pydantic import SecretStr

    from codepilot.eval.assembly import ProviderBundle
    from codepilot.llm.config import load_llm_config
    from codepilot.llm.registry import transport_provider
    from codepilot.schemas.run_config import InferenceRunConfig

    original = load_llm_config(Path(__file__).resolve().parents[2] / "configs/phase22/llm.yaml")
    runtime = InferenceRunConfig()
    selected = original.for_model(runtime.model_alias, require_temperature=True)
    selected.providers["bigmodel"] = selected.providers["bigmodel"].model_copy(
        update={"api_key_value": SecretStr("unit-test-only-not-a-credential")}
    )
    bundle = ProviderBundle.live(selected, budget_currency="CNY")
    try:
        for provider in bundle.roles.values():
            actual = transport_provider(provider)
            assert actual.provider.name == "bigmodel"
            assert actual.spec.alias == runtime.model_alias
            assert actual.spec.model == "glm-5.3-flash"
            assert actual.spec.supports_temperature is True
            from codepilot.llm.capabilities import build_request_body
            from codepilot.llm.types import LLMRequest, Message

            request = LLMRequest(
                messages=[Message.user("contract")], temperature=runtime.temperature
            )
            body = build_request_body(request, actual.spec, request.messages)
            assert body["model"] == "glm-5.3-flash"
            assert body["temperature"] == 0.0
            assert body["max_tokens"] == 4096
            assert actual.policy.max_attempts == 1
            assert actual._client.timeout.as_dict() == {
                "connect": 10.0,
                "read": 120.0,
                "write": 30.0,
                "pool": 10.0,
            }
            assert actual._client.trust_env is False
        assert original.roles["reviewer"] != selected.roles["reviewer"]
    finally:
        bundle.close()


def test_repeated_test_write_switches_to_readonly_source_relocalization(tmp_path):
    from codepilot.agents.coder import CodingAgent
    from codepilot.llm.fake import FakeLLM
    from codepilot.schemas.issue import IssueAnalysis
    from codepilot.schemas.plan import PlannedChange, RepairPlan
    from codepilot.tools.read_tools import read_file_tool
    from codepilot.tools.registry import ToolRegistry

    repo = init_repo(
        tmp_path / "repo", {"pkg/core.py": "value = 1\n", "tests/test_core.py": "assert True\n"}
    )
    policy = WorkspacePathPolicy(repo.path, deny_existing_test_edits=True)
    bad = {
        "tool": "edit_file",
        "arguments": {"path": "tests/test_core.py", "search": "True", "replace": "False"},
    }
    llm = FakeLLM(
        [
            bad,
            bad,
            {"tool": "read_file", "arguments": {"path": "pkg/core.py"}},
            {
                "tool": "edit_file",
                "arguments": {"path": "pkg/core.py", "search": "value = 1", "replace": "value = 2"},
            },
            {},
        ]
    )
    coder = CodingAgent(llm, ToolRegistry([edit_file_tool(policy), read_file_tool(policy)]))
    coder.execution_feedback = "Previous test-only candidate was rejected. Inspect pkg/core.py."
    plan = RepairPlan(
        root_cause_hypothesis="fix implementation",
        changes=[PlannedChange(file="pkg/core.py", description="fix value")],
        alternatives=[],
    )
    result = coder.run(issue_text="fix", issue_analysis=IssueAnalysis(summary="fix"), plan=plan)
    assert result.finished
    assert (repo.path / "tests/test_core.py").read_text() == "assert True\n"
    assert (repo.path / "pkg/core.py").read_text() == "value = 2\n"
    assert llm.calls[2].purpose == "coder_relocalize"
    assert all(
        spec.name not in {"edit_file", "create_file", "run_tests"} for spec in llm.calls[2].tools
    )
    assert "Previous test-only candidate" in llm.calls[0].messages[0].content
    assert len(result.tool_calls) == 4


def test_target_evidence_cache_requires_same_frozen_image(monkeypatch):
    from codepilot.graph import nodes
    from codepilot.schemas.enums import ExecutionStatus, TestOutcome
    from codepilot.schemas.execution import ExecutionResult, TestCaseResult

    result = ExecutionResult(
        status=ExecutionStatus.PASS,
        tests=[
            TestCaseResult(node_id="tests/test_x.py::test_x", outcome=TestOutcome.PASSED, duration=0)
        ],
        diagnostics={"environment_image_id": "image-old"},
    )
    earlier = SimpleNamespace(patch_hash="same", execution=result)
    state = SimpleNamespace(
        current_attempt=SimpleNamespace(patch_hash="same"), attempts=[earlier, object()]
    )
    deps = SimpleNamespace(environment=SimpleNamespace(image_id="image-new"))
    monkeypatch.setattr(nodes, "_artifact_evidence_valid", lambda *_: True)
    assert nodes._prior_execution_for_patch(state, deps, ["tests/test_x.py::test_x"]) is None
    deps.environment.image_id = "image-old"
    assert nodes._prior_execution_for_patch(state, deps, ["tests/test_x.py::test_x"]) == (
        earlier,
        result,
    )
    earlier.execution = result.model_copy(update={"diagnostics": {}})
    assert nodes._prior_execution_for_patch(state, deps, ["tests/test_x.py::test_x"]) is None
