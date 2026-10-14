"""Actual JUnit + FakeLLM, Phase 6 handoff, and independently marked real Docker."""

from pathlib import Path

import pytest
from test_phase6_reproduction_e2e import (
    REPRO_NODE,
    REPRO_PATH,
    _assertion_failure,
    _Chain,
)
from test_phase6_reproduction_e2e import (
    docker_environment as docker_environment,
)

from codepilot.agents.diagnoser import DiagnosisAgent
from codepilot.artifacts import ArtifactStore
from codepilot.context.builder import ContextBudgetError
from codepilot.failure.classifier import FailureClassifier
from codepilot.failure.compressor import compress_failures
from codepilot.failure.parser import FailureParser
from codepilot.llm.errors import StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.fake import FakeSandbox
from codepilot.tools.base import WorkspacePathPolicy

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/failures"


def _diagnosis(file="test_case.py", line=2, text="assert 1 == 2"):
    return {
        "root_cause": "The expected value differs from the observed value (hypothesis).",
        "evidence": [{"kind": "code", "file": file, "line": line, "text": text}],
        "confidence": 0.8,
        "is_patch_caused": True,
    }


def test_actual_junit_to_validated_diagnosis(tmp_path):
    (tmp_path / "test_case.py").write_text((CORPUS / "sources/assertion/test_case.py").read_text())
    parser = FailureParser(repo_files={"test_case.py"})
    failures = parser.parse_xml(
        (CORPUS / "assertion.xml").read_text(), artifact_ref="assertion.xml"
    ).failures
    classifier = FailureClassifier(FakeLLM())
    failures = [
        f.model_copy(update={"category": classifier.classify(f).category}) for f in failures
    ]
    compressed = compress_failures(failures)
    llm = FakeLLM([_diagnosis()])
    agent = DiagnosisAgent(llm, WorkspacePathPolicy(tmp_path))
    result = agent.diagnose(failures, compressed=compressed)
    assert result.failure_category.value == "assertion"
    assert result.confidence == 0.8
    assert result.is_patch_caused is False
    assert agent.last_record.artifact_refs == ["assertion.xml"]
    assert agent.last_record.signatures == [compressed.groups[0].signature]
    assert llm.purposes() == ["diagnoser"]


def test_unknown_forgery_empty_budget_and_structured_errors(tmp_path):
    (tmp_path / "test_case.py").write_text((CORPUS / "sources/unknown/test_case.py").read_text())
    failures = (
        FailureParser(repo_files={"test_case.py"})
        .parse_xml((CORPUS / "unknown.xml").read_text())
        .failures
    )
    llm = FakeLLM([{"category": "unknown"}, _diagnosis(line=999)])
    assert FailureClassifier(llm).classify(failures[0]).source == "llm"
    agent = DiagnosisAgent(llm, WorkspacePathPolicy(tmp_path))
    result = agent.diagnose(failures)
    assert result.confidence == 0 and result.evidence == []
    assert len(agent.last_record.rejected_evidence) == 1
    assert llm.purposes() == ["failure_classifier", "diagnoser"]
    with pytest.raises(ValueError, match="failure"):
        agent.diagnose([])
    with pytest.raises(ContextBudgetError):
        agent.diagnose(failures, budget=1)
    assert len(llm.calls) == 2
    for bad in ("bad JSON", {}):
        agent = DiagnosisAgent(FakeLLM(default=bad), WorkspacePathPolicy(tmp_path))
        with pytest.raises(StructuredOutputError):
            agent.diagnose(failures)
        assert len(agent.last_record.contexts) == 3
        assert agent.last_record.error == "StructuredOutputError"
        assert all(c.used <= c.budget for c in agent.last_record.contexts)


def _handoff(chain):
    executions = []
    original = chain.sandbox.run_tests

    def capture(**kwargs):
        result = original(**kwargs)
        executions.append(result)
        return result

    chain.sandbox.run_tests = capture
    outcome = chain.run()
    execution = executions[-1]
    assert execution.failed_tests[0].failure_type == "AssertionError"
    parser = FailureParser(repo_files={"src/pkg/core.py", REPRO_PATH}, repo_roots=("/workspace",))
    failures = parser.parse_execution(execution).failures
    failures = [
        f.model_copy(update={"category": FailureClassifier().classify(f).category})
        for f in failures
    ]
    assert failures[0].category.value == "assertion"
    payload = _diagnosis("src/pkg/core.py", 2, "return value - 1")
    symbol = next(
        s for s in chain.index.symbol_objects() if s.qualified_name == "pkg.core.normalize"
    )
    payload["suspected_symbols"] = [symbol.id, "invented_symbol"]
    llm = FakeLLM([payload])
    agent = DiagnosisAgent(llm, chain.policy, index=chain.index)
    result = agent.diagnose(
        failures,
        issue_analysis=chain.analysis,
        localization=chain.localization,
        source_paths=["src/pkg/core.py"],
        reproduction_tests={REPRO_NODE},
    )
    assert result.evidence and result.confidence == 0.8
    assert result.suspected_symbols == [symbol.id]
    assert agent.last_record.rejected_symbols == ["invented_symbol"]
    assert chain.delivered_patch() == ""
    assert outcome.result.status.value == "reproduced"
    assert "Gold Patch" not in llm.prompt_texts()[0]
    return execution


def test_phase6_execution_interface(tmp_path, stores):
    chain = _Chain(tmp_path, stores, FakeSandbox([_assertion_failure(REPRO_NODE)]))
    _handoff(chain)
    failure = FailureParser().parse_xml((CORPUS / "did_not_raise.xml").read_text()).failures[0]
    assert failure.exception_type == "Failed"
    assert failure.message == "Failed: DID NOT RAISE ValueError"
    assert FailureClassifier().classify(failure).category.value == "expected_exception_missing"


@pytest.mark.docker
def test_real_docker_phase6_to_phase7(tmp_path, stores, docker_environment):
    artifacts = ArtifactStore(tmp_path / "docker-artifacts", "phase7-docker")
    sandbox = DockerSandbox(artifacts=artifacts, allowed_workspace_root=tmp_path)
    chain = _Chain(tmp_path, stores, sandbox, real_docker=True, environment=docker_environment)
    execution = _handoff(chain)
    assert execution.diagnostics["execution_backend"] == "docker"
    assert execution.diagnostics["container_verified"] is True
    xml_paths = list((tmp_path / "docker-artifacts").rglob("junit.xml"))
    assert xml_paths
    parsed = FailureParser(repo_files={REPRO_PATH}).parse_xml(
        xml_paths[0].read_text(encoding="utf-8")
    )
    assert parsed.failures[0].exception_type == "AssertionError"
