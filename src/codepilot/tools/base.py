"""工具公共契约与唯一的 workspace 路径安全实现（P-06）。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codepilot.llm.types import ToolSpec
from codepilot.schemas.reproduction import REPRO_TEST_DIRECTORY

__all__ = [
    "PathAccessError",
    "Tool",
    "ToolError",
    "ToolPermission",
    "ToolResponse",
    "WorkspacePathPolicy",
]

_REPRODUCTION_PARTS = tuple(REPRO_TEST_DIRECTORY.split("/"))
_REPRODUCTION_DIRECTORY_NAME = _REPRODUCTION_PARTS[-1]
_REPRODUCTION_FORBIDDEN_NAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "__init__.py",
    }
)


def _touches_reproduction_directory(path: PurePosixPath) -> bool:
    """大小写不敏感地判断路径是否落在复现目录里（Windows 上大小写不敏感）。"""
    directories = tuple(item.casefold() for item in path.parts[:-1])
    return (
        _REPRODUCTION_DIRECTORY_NAME.casefold() in directories
        or tuple(item.casefold() for item in path.parts[:2]) == _REPRODUCTION_PARTS
    )


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

    ``repro_write`` is the single, minimal exception for the reproduction test
    directory (Phase 6 D-05). It is a **trusted construction parameter**: no
    model-facing argument can set it, so a Coding Agent cannot widen its own
    permissions by naming a path under ``tests/_codepilot_repro/``.
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
        repro_write: bool = False,
        allow_new_tests: bool = False,
    ) -> None:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.root = root
        self.deny_directories = frozenset(self._normalize_rule(item) for item in deny_directories)
        self.deny_files = frozenset(self._normalize_rule(item) for item in deny_files)
        self.repro_write = bool(repro_write)
        self.allow_new_tests = bool(allow_new_tests)

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
        self._check_reproduction_directory(posix, for_write=for_write)

        if (
            for_write
            and not must_exist
            and not self.allow_new_tests
            and not (self.repro_write and _touches_reproduction_directory(posix))
            and is_test_path(posix.as_posix())
        ):
            raise PathAccessError("forbidden_path", "new tests require trusted task authorization")

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
        if (
            for_write
            and not must_exist
            and not self.allow_new_tests
            and is_test_path(resolved.relative_to(self.root).as_posix())
            and not (self.repro_write and _touches_reproduction_directory(posix))
        ):
            raise PathAccessError(
                "forbidden_path", "resolved new test requires trusted task authorization"
            )
        if for_write:
            self._check_reproduction_parent(posix, resolved, for_write=for_write)
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

    def _check_reproduction_directory(self, path: PurePosixPath, *, for_write: bool) -> None:
        """复现目录的写入例外：无凭证一律拒绝，有凭证也只允许规范位置。

        读取不受影响——ReproductionAgent 与 Sandbox 都要能读到复现测试（D-07）。
        """
        if not for_write or not _touches_reproduction_directory(path):
            return
        if not self.repro_write:
            raise PathAccessError(
                "forbidden_path",
                "the reproduction test directory requires a trusted reproduction context",
            )
        parts = path.parts
        if parts[:2] != _REPRODUCTION_PARTS or len(parts) != 3:
            raise PathAccessError(
                "forbidden_path",
                f"reproduction files must live exactly at {REPRO_TEST_DIRECTORY}/<test_*.py>",
            )
        leaf = parts[-1]
        if leaf.casefold() in _REPRODUCTION_FORBIDDEN_NAMES:
            raise PathAccessError("forbidden_path", f"protected configuration file: {leaf}")
        if not leaf.startswith("test_") or not leaf.endswith(".py") or "/" in leaf:
            raise PathAccessError("forbidden_path", f"not a reproduction test file: {leaf}")

    def _check_reproduction_parent(
        self, path: PurePosixPath, resolved: Path, *, for_write: bool
    ) -> None:
        """目录必须是真的在那棵树里，不是被 symlink 换出来的（D-05 第 6 条）。"""
        if not for_write or not self.repro_write or not _touches_reproduction_directory(path):
            return
        if resolved.parent != self.root.joinpath(*_REPRODUCTION_PARTS):
            raise PathAccessError(
                "path_escape", "reproduction directory is not the workspace's own directory"
            )

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


def is_test_path(relative: str) -> bool:
    path = PurePosixPath(relative.replace("\\", "/").casefold())
    return "tests" in path.parts or path.name.startswith("test_") or path.name.endswith("_test.py")
