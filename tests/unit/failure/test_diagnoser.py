import pytest
from pydantic import ValidationError

from codepilot.agents.diagnoser import DiagnosisAgent, validate_evidence
from codepilot.artifacts import ArtifactStore
from codepilot.context.builder import ContextBudgetError, ContextBuilder
from codepilot.failure.compressor import compress_failures
from codepilot.failure.schemas import ParsedFailure
from codepilot.llm.errors import StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.patch.diff import generate_git_diff
from codepilot.repo.models import Symbol
from codepilot.schemas.diagnosis import Diagnosis, Evidence
from codepilot.schemas.enums import ExecutionStatus
from codepilot.tools.base import WorkspacePathPolicy
from support.gitfixture import init_repo


def draft(evidence):
    return Diagnosis(root_cause="hypothesis", confidence=0.8, evidence=evidence)


def test_evidence_proportion_and_line_matching(tmp_path):
    (tmp_path / "a.py").write_text("def first():\n    return 1\ndef second():\n    return 2\n")
    policy = WorkspacePathPolicy(tmp_path)
    good = Evidence(kind="code", file="a.py", line=2, text="return 1")
    bad = Evidence(kind="code", file="a.py", line=4, text="return 1")
    for evidence, expected in (([good], 0.8), ([good, bad], 0.4), ([bad], 0.0), ([], 0.0)):
        result, rejected = validate_evidence(draft(evidence), policy)
        assert result.confidence == expected
        assert len(rejected) == sum(item == bad for item in evidence)
    for file, line in (("missing.py", 1), ("a.py", 50), ("../a.py", 1), (".git/config", 1)):
        result, rejected = validate_evidence(
            draft([Evidence(kind="code", file=file, line=line, text="x")]), policy
        )
        assert result.confidence == 0 and len(rejected) == 1


def test_diagnosis_schema():
    for value in (
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"repair_strategy": "execute"},
    ):
        with pytest.raises(ValidationError):
            Diagnosis(root_cause="hypothesis", **value)
    with pytest.raises(ValidationError):
        Evidence(kind="code", file="a.py", line=0, text="")
    d = draft([Evidence(kind="code", file="a.py", line=1, text="return 1")])
    assert Diagnosis.model_validate_json(d.model_dump_json()) == d


def test_agent_validates_and_requires_failure(tmp_path):
    (tmp_path / "a.py").write_text("raise ValueError('bad')\n")
    model = FakeLLM(
        [draft([Evidence(kind="code", file="a.py", line=1, text="raise ValueError('bad')")])]
    )
    agent = DiagnosisAgent(model, WorkspacePathPolicy(tmp_path))
    result = agent.diagnose(
        [ParsedFailure(test_id="test_x", exception_type="ValueError", message="bad")],
        source_paths=["a.py"],
    )
    assert result.confidence == 0.8
    assert agent.last_record.contexts[-1].used <= 24000
    with pytest.raises(ValueError, match="failure"):
        agent.diagnose([])
    with pytest.raises(ValueError, match="environment"):
        agent.diagnose(
            [ParsedFailure(test_id="test_x", execution_status=ExecutionStatus.ENV_ERROR)]
        )


def test_symbol_and_traceback_evidence_must_match_same_range(tmp_path):
    (tmp_path / "a.py").write_text("def first():\n    return 1\ndef second():\n    return 1\n")
    symbol = Symbol(
        name="first",
        qualified_name="first",
        kind="function",
        id="s1",
        path="a.py",
        start_line=1,
        end_line=2,
    )
    wrong = Evidence(kind="code", file="a.py", line=4, text="return 1", symbol_id="s1")
    result, rejected = validate_evidence(
        draft([wrong]), WorkspacePathPolicy(tmp_path), symbols={"s1": symbol}
    )
    assert result.confidence == 0 and rejected[0].reason == "symbol_range_mismatch"
    trace = Evidence(kind="traceback", file="a.py", line=2, text="return 1")
    result, rejected = validate_evidence(draft([trace]), WorkspacePathPolicy(tmp_path), failures=[])
    assert result.confidence == 0 and rejected[0].reason == "unobserved_traceback_frame"


def test_symlink_escape_and_isolated_alias_are_rejected(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.py").write_text("secret = 1\n")
    (root / "gold.py").write_text("secret = 1\n")
    (root / "alias.py").symlink_to(tmp_path / "outside.py")
    (root / "alias2.py").symlink_to(root / "gold.py")
    for file in ("alias.py", "alias2.py", "gold.py"):
        result, rejected = validate_evidence(
            draft([Evidence(kind="code", file=file, line=1, text="secret = 1")]),
            WorkspacePathPolicy(root),
        )
        assert not result.evidence and rejected


def test_only_admitted_source_is_usable_and_no_stale_compression(tmp_path):
    (tmp_path / "a.py").write_text("return_value = 1\n")
    (tmp_path / "gold.py").write_text("GOLD_SENTINEL = 99\n")
    llm = FakeLLM([draft([Evidence(kind="code", file="a.py", line=1, text="return_value = 1")])])
    agent = DiagnosisAgent(llm, WorkspacePathPolicy(tmp_path))
    failure = ParsedFailure(test_id="test_x", message="bad")
    result = agent.diagnose([failure], source_paths=["gold.py"])
    assert result.confidence == 0
    assert "GOLD_SENTINEL" not in llm.prompt_texts()[0]
    with pytest.raises(ValueError, match="compressed"):
        agent.diagnose(
            [failure],
            compressed=compress_failures([failure.model_copy(update={"message": "stale"})]),
        )
    assert len(llm.calls) == 1


def test_patch_causation_requires_actual_changed_line(tmp_path):
    repo = init_repo(tmp_path / "repo", {"a.py": "def f():\n    return 1\n"})
    (repo.path / "a.py").write_text("def f():\n    return 2\n")
    patch = generate_git_diff(repo.path, repo.head).text
    llm = FakeLLM(
        [
            draft([Evidence(kind="code", file="a.py", line=2, text="return 2")]).model_copy(
                update={"is_patch_caused": True}
            )
        ]
    )
    agent = DiagnosisAgent(llm, WorkspacePathPolicy(repo.path))
    result = agent.diagnose(
        [ParsedFailure(test_id="test_x", message="bad")], source_paths=["a.py"], current_patch=patch
    )
    assert result.is_patch_caused
    with pytest.raises(ValueError, match="canonical"):
        agent.diagnose([ParsedFailure(test_id="test_x", message="bad")], current_patch="fabricated")


def test_deleted_file_diff_is_context_but_not_current_evidence(tmp_path):
    repo = init_repo(tmp_path / "repo", {"obsolete.py": "x = 1\n"})
    (repo.path / "obsolete.py").unlink()
    patch = generate_git_diff(repo.path, repo.head).text
    llm = FakeLLM(
        [
            draft([Evidence(kind="code", file="obsolete.py", line=1, text="x = 1")]).model_copy(
                update={"is_patch_caused": True}
            )
        ]
    )
    agent = DiagnosisAgent(llm, WorkspacePathPolicy(repo.path))
    result = agent.diagnose(
        [ParsedFailure(test_id="test_x", exception_type="ModuleNotFoundError")],
        current_patch=patch,
        base_commit=repo.head,
    )
    assert len(llm.calls) == 1
    assert result.evidence == [] and result.confidence == 0
    assert not result.is_patch_caused
    assert len(agent.last_record.rejected_evidence) == 1
    assert patch.strip() in llm.prompt_texts()[0]


def test_deleted_isolated_file_still_cannot_enter_diagnosis(tmp_path):
    repo = init_repo(tmp_path / "repo", {"gold.py": "answer = 42\n"})
    (repo.path / "gold.py").unlink()
    patch = generate_git_diff(repo.path, repo.head).text
    llm = FakeLLM([])
    agent = DiagnosisAgent(llm, WorkspacePathPolicy(repo.path))
    with pytest.raises(ValueError, match="gold_isolation"):
        agent.diagnose(
            [ParsedFailure(test_id="test_x")], current_patch=patch, base_commit=repo.head
        )
    assert llm.calls == []


def test_empty_response_and_budget_contract(tmp_path):
    failure = ParsedFailure(test_id="test_x", message="bad")
    for reply in ({}, "not json", {"root_cause": "  "}):
        agent = DiagnosisAgent(FakeLLM(default=reply), WorkspacePathPolicy(tmp_path))
        with pytest.raises(StructuredOutputError):
            agent.diagnose([failure])
    agent = DiagnosisAgent(
        FakeLLM(),
        WorkspacePathPolicy(tmp_path),
        context_builder=ContextBuilder(budgets={"diagnoser": 1}),
    )
    with pytest.raises(ContextBudgetError):
        agent.diagnose([failure])


def test_source_changed_during_model_call_is_rejected_and_recorded(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("return_value = 1\n")

    def changed(_request):
        source.write_text("return_value = 2\n")
        return draft([Evidence(kind="code", file="a.py", line=1, text="return_value = 2")])

    artifacts = ArtifactStore(tmp_path / "records", "diagnosis")
    agent = DiagnosisAgent(FakeLLM([changed]), WorkspacePathPolicy(tmp_path), artifacts=artifacts)
    result = agent.diagnose([ParsedFailure(test_id="test_x")], source_paths=["a.py"])
    assert result.confidence == 0
    assert agent.last_record.proposed_confidence == 0.8
    assert "source changed since context: a.py" in agent.last_record.assumptions
    record = artifacts.read_json("attempts/01/diagnosis.json")
    assert record["diagnosis"]["confidence"] == 0
    agent = DiagnosisAgent(
        FakeLLM(default="invalid"), WorkspacePathPolicy(tmp_path), artifacts=artifacts
    )
    with pytest.raises(StructuredOutputError):
        agent.diagnose([ParsedFailure(test_id="test_x")], artifact_iteration=2)
    assert artifacts.read_json("attempts/02/diagnosis.json")["error"] == "StructuredOutputError"
