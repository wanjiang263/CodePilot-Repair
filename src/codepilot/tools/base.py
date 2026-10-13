"""工具公共契约与唯一的 workspace 路径安全实现（P-06）。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codepilot.llm.types import ToolSpec

__all__ = [
    "PathAccessError",
    "Tool",
    "ToolError",
    "ToolPermission",
    "ToolResponse",
    "WorkspacePathPolicy",
]


class ToolPermission(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class ToolError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    ok: bool
    tool: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None

    @classmethod
    def success(cls, tool: str, **data: Any) -> ToolResponse:
        return cls(ok=True, tool=tool, data=data)

    @classmethod
    def failure(cls, tool: str, code: str, message: str, **details: Any) -> ToolResponse:
        return cls(
            ok=False,
            tool=tool,
            error=ToolError(code=code, message=message, details=details),
        )


class PathAccessError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkspacePathPolicy:
    """Realpath-based boundary check shared by every file-facing tool.

    ``deny_directories`` is an explicit, auditable configuration. No fuzzy name
    matching is used, so ordinary fixtures and public tests are not blocked.
    """

    DEFAULT_DENY_DIRECTORIES = frozenset(
        {".git", ".codepilot", "private-eval", "hidden-answers", "artifacts/runs"}
    )
    DEFAULT_DENY_FILES: frozenset[str] = frozenset()

    def __init__(
        self,
        workspace: Path,
        *,
        deny_directories: Iterable[str] = DEFAULT_DENY_DIRECTORIES,
        deny_files: Iterable[str] = DEFAULT_DENY_FILES,
    ) -> None:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.root = root
        self.deny_directories = frozenset(self._normalize_rule(item) for item in deny_directories)
        self.deny_files = frozenset(self._normalize_rule(item) for item in deny_files)

    @staticmethod
    def _normalize_rule(value: str) -> str:
        return value.strip("/\\").replace("\\", "/")

    def resolve(
        self,
        relative: str,
        *,
        must_exist: bool,
        expect_file: bool = True,
        for_write: bool = False,
    ) -> Path:
        del for_write  # permission is enforced by ToolRegistry; retained for explicit call sites.
        if not isinstance(relative, str) or not relative.strip():
            raise PathAccessError("invalid_path", "path must be a non-empty string")
        windows = PureWindowsPath(relative)
        normalized = relative.replace("\\", "/")
        posix = PurePosixPath(normalized)
        if (
            windows.is_absolute()
            or bool(windows.anchor)
            or posix.is_absolute()
            or ".." in posix.parts
        ):
            raise PathAccessError("path_escape", "absolute, UNC and parent paths are forbidden")
        lexical = Path(*posix.parts)
        self._check_denylist(posix)

        candidate = self.root / lexical
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise PathAccessError("not_found", f"path does not exist: {normalized}") from exc
        if not resolved.is_relative_to(self.root):
            raise PathAccessError("path_escape", "resolved path escapes the workspace")

        # A missing leaf may still have a symlink parent; resolve(strict=False) above
        # follows it. Recheck the existing parent identity immediately before use.
        parent_candidate = candidate.parent
        while not parent_candidate.exists() and parent_candidate != self.root:
            parent_candidate = parent_candidate.parent
        parent = parent_candidate.resolve(strict=True)
        if not parent.is_relative_to(self.root):
            raise PathAccessError("path_escape", "resolved parent escapes the workspace")
        if must_exist and expect_file and not resolved.is_file():
            raise PathAccessError("not_file", f"path is not a file: {normalized}")
        if not must_exist and candidate.exists():
            raise PathAccessError("already_exists", f"path already exists: {normalized}")
        return resolved

    def relative(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise PathAccessError("path_escape", "path escapes workspace")
        return resolved.relative_to(self.root).as_posix()

    def _check_denylist(self, path: PurePosixPath) -> None:
        text = path.as_posix()
        prefixes = [
            PurePosixPath(*path.parts[:index]).as_posix() for index in range(1, len(path.parts) + 1)
        ]
        if any(prefix in self.deny_directories for prefix in prefixes):
            raise PathAccessError("forbidden_path", f"path is denied by policy: {text}")
        if text in self.deny_files:
            raise PathAccessError("forbidden_path", f"file is denied by policy: {text}")

    def verify_identity(self, path: Path) -> None:
        """Detect a path replacement between validation and the actual operation."""
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or not path.samefile(resolved):
            raise PathAccessError("identity_changed", "target identity changed after validation")


ToolHandler = Callable[[BaseModel], ToolResponse]


class Tool:
    """A uniquely named, schema-described callable tool."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        permission: ToolPermission,
        input_model: type[BaseModel],
        handler: ToolHandler,
    ) -> None:
        self.name = name
        self.description = description
        self.permission = permission
        self.input_model = input_model
        self._handler = handler

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.input_model.model_json_schema(),
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResponse:
        try:
            parsed = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            return ToolResponse.failure(
                self.name,
                "invalid_arguments",
                "tool arguments failed schema validation",
                validation_errors=exc.errors(include_url=False),
            )
        try:
            return self._handler(parsed)
        except PathAccessError as exc:
            return ToolResponse.failure(self.name, exc.code, str(exc))
        except (OSError, ValueError) as exc:
            return ToolResponse.failure(self.name, "tool_error", str(exc))


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
