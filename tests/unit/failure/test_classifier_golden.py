import json
from collections import Counter
from pathlib import Path

from codepilot.failure.classifier import FailureClassifier
from codepilot.failure.parser import FailureParser
from codepilot.failure.signature import failure_signature
from codepilot.llm.fake import FakeLLM
from codepilot.schemas.execution import ExecutionResult

CORPUS = Path(__file__).resolve().parents[2] / "fixtures/failures"


def test_real_pytest_corpus_golden():
    expected = json.loads((CORPUS / "expected.json").read_text(encoding="utf-8"))
    classifier = FailureClassifier(repo_modules={"pkg"})
    parser = FailureParser(repo_files={"test_case.py", "pkg.py"}, repo_roots=("/fixture",))
    counts = Counter()
    signatures = {}
    assert len(expected) == 16
    for name, golden in expected.items():
        failures = parser.parse_xml(
            (CORPUS / f"{name}.xml").read_text(encoding="utf-8"), artifact_ref=f"{name}.xml"
        ).failures
        assert [f.message for f in failures] == golden["messages"]
        for failure in failures:
            assert failure.exception_type == golden["exception_type"]
            assert failure.deepest_repo_frame.file == "test_case.py"
            assert failure.deepest_repo_frame.line == golden["line"]
            result = classifier.classify(failure)
            assert result.category.value == golden["category"]
            counts[result.category.value] += 1
            if result.category.value != "unknown":
                llm = FakeLLM()
                assert (
                    FailureClassifier(llm, repo_modules={"pkg"}).classify(failure).category
                    == result.category
                )
                assert llm.calls == []
            signatures[name] = failure_signature(failure)
    assert classifier.total == 17
    assert classifier.rule_hits == 16
    assert classifier.rule_hit_rate == 16 / 17
    assert counts["unknown"] == 1
    assert signatures["volatile_a"] == signatures["volatile_b"]
    assert counts == {
        "assertion": 3,
        "name_attribute": 1,
        "value_key_index": 4,
        "collection": 1,
        "environment": 1,
        "expected_exception_missing": 1,
        "fixture": 2,
        "import": 1,
        "syntax": 1,
        "type": 1,
        "unknown": 1,
    }


def test_real_timeout_execution_has_no_fabricated_test():
    execution = ExecutionResult.model_validate_json(
        (CORPUS / "timeout.execution.json").read_text(encoding="utf-8")
    )
    batch = FailureParser().parse_execution(execution)
    assert not batch.failures
    assert batch.execution_failure.category.value == "timeout"
    assert execution.diagnostics["execution_backend"] == "controlled_local_pytest"
