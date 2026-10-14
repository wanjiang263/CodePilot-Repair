"""Failure facts. Paths are display identities, never read capabilities."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codepilot.schemas.common import TraceFrame
from codepilot.schemas.enums import ExecutionStatus, FailureCategory

__all__ = ["ExecutionFailure", "FailureBatch", "ParsedFailure", "TraceFrame"]


class ParsedFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    test_id: str = Field(min_length=1)
    exception_type: str | None = None
    message: str = ""
    frames: list[TraceFrame] = Field(default_factory=list)
    deepest_repo_frame: TraceFrame | None = None
    category: FailureCategory = FailureCategory.UNKNOWN
    traceback: str = ""
    type_source: Literal["junit_type", "failure_type", "traceback", "message", "none"] = "none"
    phase: Literal["call", "collection", "setup", "teardown", "unknown"] = "call"
    artifact_ref: str = ""
    execution_status: ExecutionStatus | None = None

    @model_validator(mode="after")
    def check_frames(self) -> ParsedFailure:
        for frame in self.frames:
            path = PurePosixPath(frame.file.replace("\\", "/"))
            if frame.in_repo and (
                path.is_absolute() or PureWindowsPath(frame.file).anchor or ".." in path.parts
            ):
                raise ValueError("repository frame identity must be repository-relative")
        deepest = next((f for f in reversed(self.frames) if f.in_repo), None)
        if self.deepest_repo_frame != deepest:
            raise ValueError("deepest_repo_frame must equal the last observed repository frame")
        return self


class ExecutionFailure(BaseModel):
    status: ExecutionStatus
    reason: str | None = None
    artifact_ref: str = ""
    category: FailureCategory
    exit_code: int | None = None
    duration_seconds: float = 0.0
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class FailureBatch(BaseModel):
    failures: list[ParsedFailure] = Field(default_factory=list)
    execution_failure: ExecutionFailure | None = None
    artifact_ref: str = ""
