"""JUnit XML 是测试结果的唯一主要事实来源。"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from codepilot.schemas.enums import TestOutcome
from codepilot.schemas.execution import TestCaseResult

__all__ = [
    "JUnitParseError",
    "JUnitReport",
    "parse_junit",
    "parse_junit_text",
]

#: traceback 末行的 ``<file>:<line>: <ExceptionName>`` 是 pytest 写在 JUnit 结构化
#: 文本里的异常类型，比在 stdout 里搜索可靠得多（Phase 6 D-03 的有效性判定依赖它）。
_TRACEBACK_TYPE = re.compile(r"^[^\n]*\.\w+:\d+: (?P<exc>[A-Za-z_][A-Za-z0-9_.]*)\s*$")
_MESSAGE_TYPE = re.compile(
    r"^(?P<exc>[A-Z][A-Za-z0-9_]*(?:Error|Exception|Failed|Exit|Interrupt|Warning)):"
)


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
        failure_type=(
            None if detail is None else _failure_type(detail, message=message, traceback=traceback)
        ),
    )


def _failure_type(
    detail: ElementTree.Element, *, message: str | None, traceback: str | None
) -> str | None:
    """按「``type`` 属性 → traceback 末行 → message 前缀」提取结构化失败类型。"""
    declared = (detail.get("type") or "").strip()
    if declared and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", declared):
        return declared.rsplit(".", 1)[-1]
    if traceback:
        for line in reversed([item.strip() for item in traceback.splitlines() if item.strip()]):
            if (match := _TRACEBACK_TYPE.match(line)) is not None:
                return match.group("exc").rsplit(".", 1)[-1]
    if message:
        match = _MESSAGE_TYPE.match(message.strip())
        if match is not None:
            return match.group("exc")
    return None


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
