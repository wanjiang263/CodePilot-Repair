"""JUnit/ExecutionResult adapters; stdout is never a failure oracle."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from xml.etree import ElementTree

from codepilot.failure.schemas import ExecutionFailure, FailureBatch, ParsedFailure
from codepilot.failure.traceback import parse_traceback
from codepilot.sandbox.junit import JUnitParseError, failure_type_from_traceback, parse_junit_text
from codepilot.schemas.enums import ExecutionStatus, FailureCategory, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult


class FailureParser:
    def __init__(self, *, repo_files: Collection[str] = (), repo_roots: Sequence[str] = ()) -> None:
        self.repo_files = frozenset(path.replace("\\", "/") for path in repo_files)
        self.repo_roots = tuple(repo_roots)

    def parse_case(self, case: TestCaseResult, *, artifact_ref: str = "") -> ParsedFailure:
        if case.outcome not in (TestOutcome.FAILED, TestOutcome.ERROR):
            raise ValueError("Only failed/error cases are failure evidence")
        frames = parse_traceback(
            case.traceback or "", repo_files=self.repo_files, repo_roots=self.repo_roots
        )
        phase = "call"
        message = case.message or ""
        exception_type = case.failure_type or failure_type_from_traceback(case.traceback or "")
        if case.outcome == TestOutcome.ERROR:
            phase = "unknown"
            if message.startswith("collection failure"):
                phase = "collection"
            elif message.startswith("failed on setup"):
                phase = "setup"
            elif message.startswith("failed on teardown"):
                phase = "teardown"
        return ParsedFailure(
            test_id=case.node_id,
            exception_type=exception_type,
            message=message,
            frames=frames,
            deepest_repo_frame=next((f for f in reversed(frames) if f.in_repo), None),
            traceback=case.traceback or "",
            phase=phase,
            type_source="failure_type"
            if case.failure_type
            else ("traceback" if exception_type else "none"),
            artifact_ref=artifact_ref,
        )

    def parse_xml(self, text: str, *, artifact_ref: str = "") -> FailureBatch:
        report = parse_junit_text(text)
        if (report.suite_errors or report.suite_failures) and not any(
            case.outcome in (TestOutcome.FAILED, TestOutcome.ERROR) for case in report.tests
        ):
            raise JUnitParseError("JUnit declares failures without usable failure records")
        elements = list(ElementTree.fromstring(text).iter("testcase"))
        failures = []
        for case, element in zip(report.tests, elements, strict=True):
            if case.outcome not in (TestOutcome.FAILED, TestOutcome.ERROR):
                continue
            failure = self.parse_case(case, artifact_ref=artifact_ref)
            detail = element.find("failure")
            if detail is None:
                detail = element.find("error")
            if detail is not None and detail.get("type") and case.failure_type:
                failure = failure.model_copy(update={"type_source": "junit_type"})
            elif case.failure_type:
                failure = failure.model_copy(
                    update={
                        "type_source": "traceback"
                        if failure_type_from_traceback(case.traceback or "")
                        else "message"
                    }
                )
            failures.append(failure)
        return FailureBatch(failures=failures, artifact_ref=artifact_ref)

    def parse_execution(self, result: ExecutionResult) -> FailureBatch:
        abnormal = result.status in (
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.ENV_ERROR,
            ExecutionStatus.SANDBOX_ERROR,
        ) or (result.status == ExecutionStatus.COLLECTION_ERROR and not result.failed_tests)
        execution = None
        if abnormal:
            execution = ExecutionFailure(
                status=result.status,
                reason=result.error_reason,
                artifact_ref=result.artifact_dir,
                exit_code=result.exit_code,
                duration_seconds=result.duration_seconds,
                diagnostics=result.diagnostics,
                category=FailureCategory.TIMEOUT
                if result.status == ExecutionStatus.TIMEOUT
                else (
                    FailureCategory.COLLECTION
                    if result.status == ExecutionStatus.COLLECTION_ERROR
                    else FailureCategory.ENVIRONMENT
                ),
            )
        return FailureBatch(
            failures=[
                self.parse_case(c, artifact_ref=result.artifact_dir).model_copy(
                    update={"execution_status": result.status}
                )
                for c in result.failed_tests
            ],
            execution_failure=execution,
            artifact_ref=result.artifact_dir,
        )
