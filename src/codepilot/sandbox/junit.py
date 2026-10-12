"""JUnit XML 是测试结果的唯一主要事实来源。"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from codepilot.schemas.enums import TestOutcome
from codepilot.schemas.execution import TestCaseResult

__all__ = ["JUnitParseError", "JUnitReport", "parse_junit", "parse_junit_text"]


class JUnitParseError(ValueError):
    """JUnit 缺失、损坏或不是受支持的根元素。"""


class JUnitReport(BaseModel):
    tests: list[TestCaseResult] = Field(default_factory=list)
    suite_errors: int = 0
    suite_failures: int = 0
    suite_skipped: int = 0
    declared_tests: int = 0


def parse_junit(path: Path) -> JUnitReport:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise JUnitParseError(f"无法读取 JUnit XML: {path}") from exc
    return parse_junit_text(text)


def parse_junit_text(text: str) -> JUnitReport:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise JUnitParseError(f"JUnit XML 损坏: {exc}") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise JUnitParseError(f"不支持的 JUnit 根元素: {root.tag!r}")

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    cases: list[TestCaseResult] = []
    for element in root.iter("testcase"):
        cases.append(_parse_case(element))
    return JUnitReport(
        tests=cases,
        suite_errors=sum(_integer(suite.get("errors")) for suite in suites),
        suite_failures=sum(_integer(suite.get("failures")) for suite in suites),
        suite_skipped=sum(_integer(suite.get("skipped")) for suite in suites),
        declared_tests=sum(_integer(suite.get("tests")) for suite in suites),
    )


def _parse_case(element: ElementTree.Element) -> TestCaseResult:
    outcome = TestOutcome.PASSED
    detail: ElementTree.Element | None = None
    if (failure := element.find("failure")) is not None:
        outcome = TestOutcome.FAILED
        detail = failure
    elif (error := element.find("error")) is not None:
        outcome = TestOutcome.ERROR
        detail = error
    elif (skipped := element.find("skipped")) is not None:
        outcome = TestOutcome.SKIPPED
        detail = skipped

    message = detail.get("message") if detail is not None else None
    traceback = (detail.text or "").strip() or None if detail is not None else None
    try:
        duration = float(element.get("time", "0") or "0")
    except ValueError:
        duration = 0.0
    return TestCaseResult(
        node_id=_node_id(element),
        outcome=outcome,
        duration=max(duration, 0.0),
        message=message,
        traceback=traceback,
    )


def _node_id(element: ElementTree.Element) -> str:
    name = element.get("name", "<unknown>")
    file_name = element.get("file")
    class_name = element.get("classname", "")
    if file_name:
        suffix = []
        if class_name:
            class_leaf = class_name.rsplit(".", 1)[-1]
            if not class_leaf.startswith("test"):
                suffix.append(class_leaf)
        suffix.append(name)
        return "::".join([file_name.replace("\\", "/"), *suffix])
    if not class_name:
        return name

    parts = class_name.replace("/", ".").split(".")
    module_index = next(
        (index for index in range(len(parts) - 1, -1, -1) if parts[index].startswith("test")),
        len(parts) - 1,
    )
    module = "/".join(parts[: module_index + 1]) + ".py"
    return "::".join([module, *parts[module_index + 1 :], name])


def _integer(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0
