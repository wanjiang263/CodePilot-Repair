from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.sandbox.junit import JUnitParseError, parse_junit
from codepilot.schemas.enums import TestOutcome

FIXTURES = Path(__file__).parents[2] / "fixtures" / "junit"


@pytest.mark.parametrize(
    ("name", "node_id", "outcome"),
    [
        ("passed.xml", "tests/test_math.py::test_add", TestOutcome.PASSED),
        (
            "failed.xml",
            "tests/test_auth.py::TestLogin::test_bad_password",
            TestOutcome.FAILED,
        ),
        ("error.xml", "tests/test_import.py::test_import", TestOutcome.ERROR),
        ("skipped.xml", "tests/test_platform.py::test_linux_only", TestOutcome.SKIPPED),
    ],
)
def test_junit_golden_outcomes(name: str, node_id: str, outcome: TestOutcome) -> None:
    report = parse_junit(FIXTURES / name)
    assert report.tests[0].node_id == node_id
    assert report.tests[0].outcome is outcome


def test_failure_message_traceback_and_duration_are_preserved() -> None:
    case = parse_junit(FIXTURES / "failed.xml").tests[0]
    assert case.duration == 0.2
    assert case.message == "assert 401 == 200"
    assert case.traceback == "Traceback line 1\nAssertionError"


def test_multiple_test_suites_are_flattened() -> None:
    report = parse_junit(FIXTURES / "multiple-suites.xml")
    assert [case.outcome for case in report.tests] == [TestOutcome.PASSED, TestOutcome.FAILED]
    assert report.tests[1].duration == 0.0
    assert report.declared_tests == 2
    assert report.suite_failures == 1


def test_incomplete_but_well_formed_xml_has_explicit_defaults() -> None:
    case = parse_junit(FIXTURES / "incomplete.xml").tests[0]
    assert case.node_id == "<unknown>"
    assert case.duration == 0.0


def test_malformed_xml_is_not_inferred_from_stdout() -> None:
    with pytest.raises(JUnitParseError, match="损坏"):
        parse_junit(FIXTURES / "malformed.xml")


def test_classname_resolves_to_the_real_workspace_file(tmp_path: Path) -> None:
    (tmp_path / "testing/python").mkdir(parents=True)
    (tmp_path / "testing/python/metafunc.py").write_text("", encoding="utf-8")
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0">'
        '<testcase classname="testing.python.metafunc" name="test_example" time="0"/>'
        "</testsuite></testsuites>"
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(xml, encoding="utf-8")

    report = parse_junit(junit, workspace=tmp_path)

    assert report.tests[0].node_id == "testing/python/metafunc.py::test_example"


def test_classname_falls_back_without_a_workspace(tmp_path: Path) -> None:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0">'
        '<testcase classname="testing.python.metafunc" name="test_example" time="0"/>'
        "</testsuite></testsuites>"
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(xml, encoding="utf-8")

    report = parse_junit(junit)

    assert report.tests[0].node_id == "testing.py::python::metafunc::test_example"
