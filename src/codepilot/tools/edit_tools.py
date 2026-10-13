"""Controlled source editing tools."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from codepilot.patch.search_replace import apply_search_replace
from codepilot.tools.base import Tool, ToolPermission, ToolResponse, WorkspacePathPolicy

__all__ = ["CreateFileInput", "EditFileInput", "create_file_tool", "edit_file_tool"]


class EditFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    search: str = Field(min_length=1)
    replace: str


class CreateFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str


def edit_file_tool(policy: WorkspacePathPolicy) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = EditFileInput.model_validate(raw)
        path = policy.resolve(args.path, must_exist=True, for_write=True)
        policy.verify_identity(path)
        result = apply_search_replace(path, args.search, args.replace)
        if not result.applied:
            return ToolResponse.failure(
                "edit_file",
                result.status.value,
                "search block was not applied safely",
                matches=[item.model_dump(mode="json") for item in result.matches],
                suggestions=[item.model_dump(mode="json") for item in result.suggestions],
            )
        return ToolResponse.success(
            "edit_file", path=policy.relative(path), match_status=result.status.value
        )

    return Tool(
        name="edit_file",
        description="Atomically replace one unique search block in an existing file.",
        permission=ToolPermission.WRITE,
        input_model=EditFileInput,
        handler=handle,
    )


def create_file_tool(policy: WorkspacePathPolicy) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = CreateFileInput.model_validate(raw)
        path = policy.resolve(args.path, must_exist=False, for_write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        _exclusive_write(path, args.content)
        return ToolResponse.success("create_file", path=path.relative_to(policy.root).as_posix())

    return Tool(
        name="create_file",
        description="Create a new repository file without overwriting an existing path.",
        permission=ToolPermission.WRITE,
        input_model=CreateFileInput,
        handler=handle,
    )


def _exclusive_write(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
