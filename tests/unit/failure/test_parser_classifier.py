import pytest
from pydantic import ValidationError

from codepilot.failure.classifier import FailureClassifier
from codepilot.failure.parser import FailureParser
from codepilot.failure.schemas import ParsedFailure
from codepilot.failure.traceback import parse_traceback
from codepilot.llm.errors import StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.sandbox.junit import JUnitParseError
from codepilot.schemas.enums import ExecutionStatus, FailureCategory, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult


def test_structured_type_wins_and_does_not_call_llm():
    parser = FailureParser(repo_files={"test_a.py"})
    result = parser.parse_xml(
        '<testsuite><testcase classname="test_a" name="test_x"><failure type="TypeError" message="AssertionError misleading">test_a.py:3: AssertionError</failure></testcase></testsuite>'
    )
    llm = FakeLLM()
    classified = FailureClassifier(llm).classify(result.failures[0])
    assert classified.category == FailureCategory.TYPE
    assert not llm.calls
    assert result.failures[0].test_id == "test_a.py::test_x"


def test_traceback_identity_and_chain():
    frames = parse_traceback(
        'File "/workspace/pkg/a.py", line 3, in first\n    fail()\nFile "C:\\repo\\pkg\\a.py", line 5, in second\n    other()\nFile "/lib/third.py", line 8, in outer',
        repo_files={"pkg/a.py"},
        repo_roots=("/workspace", "C:/repo"),
    )
    assert [f.in_repo for f in frames] == [True, True, False]
    assert frames[1].file == "pkg/a.py"
    assert frames[0].code == "fail()"


def test_invalid_xml_and_execution_errors():
    parser = FailureParser()
    with pytest.raises(JUnitParseError):
        parser.parse_xml("<testsuite>")
    with pytest.raises(JUnitParseError):
        parser.parse_xml('<testsuite errors="1" tests="1"/>')
    for status in (
        ExecutionStatus.TIMEOUT,
        ExecutionStatus.ENV_ERROR,
        ExecutionStatus.SANDBOX_ERROR,
    ):
        batch = parser.parse_execution(
            ExecutionResult(status=status, error_reason="controlled", artifact_dir="run/1")
        )
        assert batch.failures == []
        assert batch.execution_failure.status == status
        assert batch.execution_failure.artifact_ref == "run/1"


def test_unknown_fallback_validation_and_statistics():
    failure = ParsedFailure(test_id="test_x", message="opaque")
    llm = FakeLLM([{"category": "value_key_index"}])
    classifier = FailureClassifier(llm)
    assert classifier.classify(failure).source == "llm"
    assert classifier.total == 1 and classifier.rule_hits == 0
    assert classifier.rule_hit_rate == 0
    assert FailureClassifier().classify(failure).source == "unavailable"
    for reply in ({"category": "invented"}, {"category": "type", "frames": []}, "bad JSON"):
        with pytest.raises(StructuredOutputError):
            FailureClassifier(FakeLLM(default=reply)).classify(failure)


def test_frame_invariants_and_roundtrip():
    frames = parse_traceback('File "pkg/a.py", line 1, in f', repo_files={"pkg/a.py"})
    failure = ParsedFailure(test_id="x", frames=frames, deepest_repo_frame=frames[0])
    assert ParsedFailure.model_validate_json(failure.model_dump_json()) == failure
    with pytest.raises(ValidationError):
        ParsedFailure(test_id="x", deepest_repo_frame=frames[0])
    assert (
        parse_traceback('File "../pkg/a.py", line 1, in f', repo_files={"pkg/a.py"})[0].in_repo
        is False
    )
    assert parse_traceback('File "x.py", line 0, in f') == []


def test_pytest_specific_and_module_inventory():
    classifier = FailureClassifier(repo_modules={"pkg"})
    cases = [
        (
            "Failed",
            "Failed: DID NOT RAISE <class 'ValueError'>",
            "call",
            "expected_exception_missing",
        ),
        ("AssertionError", "Failed: DID NOT RAISE", "call", "assertion"),
        ("ModuleNotFoundError", "No module named 'pkg.child'", "call", "import"),
        ("ModuleNotFoundError", "No module named 'absent_dependency'", "call", "environment"),
        ("RuntimeError", "failed on setup", "setup", "fixture"),
        (None, "collection failure", "collection", "collection"),
    ]
    for exc, message, phase, category in cases:
        result = classifier.classify(
            ParsedFailure(test_id="x", exception_type=exc, message=message, phase=phase)
        )
        assert result.category.value == category


def test_multisuite_skipped_parametrized_and_last_exception():
    result = FailureParser().parse_xml(
        """<testsuites><testsuite><testcase classname="test_a" name="test_x[一]"><failure type="ValueError" message="bad"/></testcase><testcase name="skip"><skipped/></testcase></testsuite><testsuite><testcase classname="test_b" name="test_x[一]"><error message="failed on teardown with ValueError">x.py:3: ValueError</error></testcase></testsuite></testsuites>"""
    )
    assert [f.test_id for f in result.failures] == [
        "test_a.py::test_x[一]",
        "test_b.py::test_x[一]",
    ]
    assert result.failures[1].phase == "teardown"
    result = FailureClassifier().classify(
        ParsedFailure(test_id="x", traceback="ValueError: first\nTypeError: last")
    )
    assert result.category == FailureCategory.TYPE


def test_environment_status_overrides_partial_failure_records():
    execution = ExecutionResult(
        status=ExecutionStatus.SANDBOX_ERROR,
        error_reason="OOM_KILLED",
        diagnostics={"oom_killed": True},
        tests=[
            TestCaseResult(
                node_id="test_x",
                duration=0,
                outcome=TestOutcome.FAILED,
                failure_type="AssertionError",
            )
        ],
    )
    batch = FailureParser().parse_execution(execution)
    assert batch.execution_failure.diagnostics["oom_killed"] is True
    llm = FakeLLM()
    assert (
        FailureClassifier(llm).classify(batch.failures[0]).category == FailureCategory.ENVIRONMENT
    )
    assert llm.calls == []


def test_authoritative_enum_exact_and_terminal_failed_type():
    assert {c.value for c in FailureCategory} == {
        "syntax",
        "import",
        "name_attribute",
        "type",
        "value_key_index",
        "assertion",
        "expected_exception_missing",
        "timeout",
        "collection",
        "fixture",
        "environment",
        "unknown",
    }
    failure = (
        FailureParser()
        .parse_xml(
            '<testsuite><testcase name="x"><failure message="Failed: DID NOT RAISE ValueError"/></testcase></testsuite>'
        )
        .failures[0]
    )
    assert failure.exception_type == "Failed"
    assert failure.type_source == "message"
    timeout = failure.model_copy(update={"message": "Failed: Timeout >1.0s"})
    assert FailureClassifier().classify(timeout).category == FailureCategory.TIMEOUT


def test_module_origin_requires_an_explicit_inventory():
    f = ParsedFailure(
        test_id="test_x", exception_type="ModuleNotFoundError", message="No module named 'pkg'"
    )
    assert FailureClassifier().classify(f).category == FailureCategory.UNKNOWN
    assert FailureClassifier(repo_modules=[]).classify(f).category == FailureCategory.ENVIRONMENT
    assert FailureClassifier(repo_modules=["pkg"]).classify(f).category == FailureCategory.IMPORT


def test_call_failure_and_teardown_error_are_both_retained():
    batch = FailureParser().parse_xml(
        """<testsuite><testcase classname="test_a" name="test_x"><failure type="AssertionError" message="assert failed">test_a.py:1: AssertionError</failure><error type="ValueError" message="failed on teardown with ValueError">test_a.py:2: ValueError</error></testcase></testsuite>"""
    )
    assert len(batch.failures) == 2
    assert [f.test_id for f in batch.failures] == ["test_a.py::test_x"] * 2
    assert [FailureClassifier().classify(f).category.value for f in batch.failures] == [
        "assertion",
        "fixture",
    ]
    f = (
        FailureParser()
        .parse_xml(
            '<testsuite><testcase name="x"><failure type="invalid type" message="bad">a.py:1: TypeError</failure></testcase></testsuite>'
        )
        .failures[0]
    )
    assert f.type_source == "traceback"
