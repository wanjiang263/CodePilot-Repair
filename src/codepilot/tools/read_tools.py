"""受控代码读取、ripgrep 搜索与索引 symbol 查询（P-07）。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from codepilot.repo.index import RepositoryIndex
from codepilot.tools.base import Tool, ToolPermission, ToolResponse, WorkspacePathPolicy

__all__ = [
    "FindSymbolInput",
    "ReadFileInput",
    "SearchCodeInput",
    "find_symbol_tool",
    "read_file_tool",
    "search_code_tool",
]


class ReadFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class SearchCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pattern: str = Field(min_length=1, max_length=1000)
    regex: bool = False
    path_glob: str | None = Field(default=None, max_length=500)

    @field_validator("path_glob")
    @classmethod
    def _safe_glob(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.replace("\\", "/")
        windows = PureWindowsPath(value)
        posix = PurePosixPath(normalized)
        if windows.is_absolute() or windows.anchor or posix.is_absolute() or ".." in posix.parts:
            raise ValueError("path_glob must remain inside the workspace")
        return normalized


class FindSymbolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=20, ge=1, le=50)


def read_file_tool(policy: WorkspacePathPolicy) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = ReadFileInput.model_validate(raw)
        path = policy.resolve(args.path, must_exist=True)
        policy.verify_identity(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        end = args.end_line if args.end_line is not None else min(len(lines), args.start_line + 399)
        if end < args.start_line:
            return ToolResponse.failure(
                "read_file", "invalid_range", "end_line precedes start_line"
            )
        if end - args.start_line + 1 > 400:
            return ToolResponse.failure(
                "read_file", "range_too_large", "at most 400 lines may be read"
            )
        if args.start_line > max(1, len(lines)):
            return ToolResponse.failure(
                "read_file", "invalid_range", "start_line exceeds file length"
            )
        selected = lines[args.start_line - 1 : end]
        width = len(str(end))
        content = "\n".join(
            f"{number:>{width}} | {line}"
            for number, line in enumerate(selected, start=args.start_line)
        )
        return ToolResponse.success(
            "read_file",
            path=policy.relative(path),
            start_line=args.start_line,
            end_line=min(end, len(lines)),
            total_lines=len(lines),
            content=content,
        )

    return Tool(
        name="read_file",
        description="Read at most 400 numbered lines from a repository file.",
        permission=ToolPermission.READ,
        input_model=ReadFileInput,
        handler=handle,
    )


def search_code_tool(policy: WorkspacePathPolicy) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = SearchCodeInput.model_validate(raw)
        if args.regex:
            try:
                re.compile(args.pattern)
            except re.error as exc:
                return ToolResponse.failure("search_code", "invalid_regex", str(exc))
        executable = shutil.which("rg")
        if executable is None:
            return ToolResponse.failure(
                "search_code", "dependency_missing", "ripgrep is unavailable"
            )
        command = [
            executable,
            "--line-number",
            "--column",
            "--no-heading",
            "--color=never",
        ]
        if not args.regex:
            command.append("--fixed-strings")
        if args.path_glob:
            command.extend(["--glob", args.path_glob])
        for denied in sorted(policy.deny_directories):
            command.extend(["--glob", f"!{denied}/**"])
        command.extend(["--", args.pattern, "."])
        completed = subprocess.run(
            command,
            cwd=policy.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if completed.returncode not in (0, 1):
            return ToolResponse.failure(
                "search_code",
                "search_failed",
                completed.stderr.strip()[:1000] or "ripgrep failed",
            )
        matches: list[dict[str, str | int]] = []
        for line in completed.stdout.splitlines():
            if len(matches) == 50:
                break
            match = re.match(r"^(.+?):(\d+):(\d+):(.*)$", line)
            if match is None:
                continue
            relative, line_no, column, text = match.groups()
            relative = relative.removeprefix(".\\").removeprefix("./").replace("\\", "/")
            try:
                policy.resolve(relative, must_exist=True)
            except ValueError:
                continue
            matches.append(
                {
                    "path": relative,
                    "line": int(line_no),
                    "column": int(column),
                    "text": text[:1000],
                }
            )
        matches.sort(key=lambda item: (str(item["path"]), int(item["line"]), int(item["column"])))
        return ToolResponse.success(
            "search_code", matches=matches, truncated=len(completed.stdout.splitlines()) > 50
        )

    return Tool(
        name="search_code",
        description="Search repository text with ripgrep and return at most 50 matches.",
        permission=ToolPermission.READ,
        input_model=SearchCodeInput,
        handler=handle,
    )


def find_symbol_tool(index: RepositoryIndex) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = FindSymbolInput.model_validate(raw)
        needle = args.name.casefold()
        found = []
        for symbol in index.symbol_objects():
            qname = symbol.qualified_name.casefold()
            name = symbol.name.casefold()
            if needle not in qname and needle not in name:
                continue
            basis = (
                "exact_qualified_name"
                if qname == needle
                else ("exact_name" if name == needle else "substring")
            )
            rank = {"exact_qualified_name": 0, "exact_name": 1, "substring": 2}[basis]
            found.append((rank, symbol.qualified_name, symbol.id, symbol, basis))
        found.sort(key=lambda item: item[:3])
        results = [
            {
                "symbol_id": item[3].id,
                "name": item[3].name,
                "qualified_name": item[3].qualified_name,
                "kind": item[3].kind,
                "path": item[3].path,
                "start_line": item[3].start_line,
                "end_line": item[3].end_line,
                "match_basis": item[4],
            }
            for item in found[: args.limit]
        ]
        return ToolResponse.success(
            "find_symbol", symbols=results, truncated=len(found) > args.limit
        )

    return Tool(
        name="find_symbol",
        description="Find indexed symbols by exact or substring match.",
        permission=ToolPermission.READ,
        input_model=FindSymbolInput,
        handler=handle,
    )
