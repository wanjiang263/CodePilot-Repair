"""受控代码读取、ripgrep 搜索与索引 symbol 查询（P-07）。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath

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


_MAX_SEARCH_MATCHES = 50
_MAX_SEARCH_FILE_BYTES = 5 * 1024 * 1024
_MAX_SEARCH_FILES = 20_000
_SEARCH_DEADLINE_SECONDS = 20.0
_RG_TIMEOUT_SECONDS = 15
_BINARY_PROBE_BYTES = 8192


def _is_denied_directory(relative: str, policy: WorkspacePathPolicy) -> bool:
    parts = relative.split("/")
    candidates = {"/".join(parts[:index]) for index in range(1, len(parts) + 1)}
    return any(rule in candidates for rule in policy.deny_directories)


def _iter_search_files(
    policy: WorkspacePathPolicy, path_glob: str | None
) -> Iterator[tuple[str, Path]]:
    """按确定顺序遍历 workspace 内可搜索文件（不跟随符号链接）。"""
    for dirpath, dirnames, filenames in os.walk(policy.root, followlinks=False):
        base = Path(dirpath)
        relative_dir = base.relative_to(policy.root).as_posix()
        kept: list[str] = []
        for name in sorted(dirnames):
            if (base / name).is_symlink():
                continue
            relative = name if relative_dir == "." else f"{relative_dir}/{name}"
            if _is_denied_directory(relative, policy):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            relative = name if relative_dir == "." else f"{relative_dir}/{name}"
            if path_glob is not None and not PurePosixPath(relative).match(path_glob):
                continue
            yield relative, base / name


def _search_with_python(
    policy: WorkspacePathPolicy,
    args: SearchCodeInput,
    *,
    fallback_reason: str,
    started: float | None = None,
) -> ToolResponse:
    """Bounded, policy-constrained fallback used when ripgrep is absent/slow.

    The fallback is not an unbounded replacement for rg: it has its own file
    count, file size, binary-content and total-time limits.  The diagnostics
    say which engine ran and why the accelerator was skipped, so a Planner
    timeout cannot be mistaken for a harness internal error.
    """
    stage = time.perf_counter() if started is None else started
    deadline = time.monotonic() + _SEARCH_DEADLINE_SECONDS
    pattern: re.Pattern[str] | None = re.compile(args.pattern) if args.regex else None
    matches: list[dict[str, str | int]] = []
    truncated = False
    fallback_timed_out = False
    scanned = 0
    for relative, path in _iter_search_files(policy, args.path_glob):
        if time.monotonic() > deadline:
            fallback_timed_out = True
            truncated = True
            break
        if scanned >= _MAX_SEARCH_FILES:
            truncated = True
            break
        if path.is_symlink():
            continue
        try:
            policy.resolve(relative, must_exist=True)
            data = path.read_bytes()
        except (OSError, ValueError):
            continue
        scanned += 1
        if len(data) > _MAX_SEARCH_FILE_BYTES:
            continue
        if b"\x00" in data[:_BINARY_PROBE_BYTES]:
            continue
        text = data.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern is not None:
                found = pattern.search(line)
                column = found.start() + 1 if found is not None else 0
            else:
                index = line.find(args.pattern)
                column = index + 1 if index >= 0 else 0
            if column == 0:
                continue
            matches.append(
                {"path": relative, "line": line_no, "column": column, "text": line[:1000]}
            )
            if len(matches) > _MAX_SEARCH_MATCHES:
                truncated = True
                break
        if truncated:
            break
    matches.sort(key=lambda item: (str(item["path"]), int(item["line"]), int(item["column"])))
    return ToolResponse.success(
        "search_code",
        matches=matches[:_MAX_SEARCH_MATCHES],
        truncated=truncated,
        engine="python_fallback",
        fallback_reason=fallback_reason,
        fallback_timed_out=fallback_timed_out,
        files_scanned=scanned,
        max_files=_MAX_SEARCH_FILES,
        max_file_bytes=_MAX_SEARCH_FILE_BYTES,
        max_matches=_MAX_SEARCH_MATCHES,
        deadline_seconds=_SEARCH_DEADLINE_SECONDS,
        elapsed_ms=round((time.perf_counter() - stage) * 1000.0, 2),
    )


def _search_with_ripgrep(
    policy: WorkspacePathPolicy, args: SearchCodeInput, executable: str
) -> ToolResponse:
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
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=policy.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_RG_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Large repositories can exceed the external search timeout. Falling
        # back keeps an optional accelerator from turning into an internal error.
        return _search_with_python(policy, args, fallback_reason="rg_timeout", started=started)
    if completed.returncode not in (0, 1):
        return ToolResponse.failure(
            "search_code",
            "search_failed",
            completed.stderr.strip()[:1000] or "ripgrep failed",
            engine="ripgrep",
            exit_code=completed.returncode,
            stderr=completed.stderr.strip()[:1000],
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
        )
    matches: list[dict[str, str | int]] = []
    for line in completed.stdout.splitlines():
        if len(matches) == _MAX_SEARCH_MATCHES:
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
        "search_code",
        matches=matches,
        truncated=len(completed.stdout.splitlines()) > _MAX_SEARCH_MATCHES,
        engine="ripgrep",
        fallback_reason=None,
        fallback_timed_out=False,
        max_matches=_MAX_SEARCH_MATCHES,
        timeout_seconds=_RG_TIMEOUT_SECONDS,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
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
            return _search_with_python(policy, args, fallback_reason="rg_missing")
        return _search_with_ripgrep(policy, args, executable)

    return Tool(
        name="search_code",
        description=(
            "Search repository text (ripgrep when available, built-in fallback) "
            "and return at most 50 matches."
        ),
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
