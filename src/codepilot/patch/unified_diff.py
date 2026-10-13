"""Validated unified-diff application (P-14)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field

from codepilot.repo.gitcmd import run_git
from codepilot.tools.base import WorkspacePathPolicy

__all__ = ["UnifiedDiffResult", "apply_unified_diff"]


class UnifiedDiffResult(BaseModel):
    applied: bool
    files: list[str] = Field(default_factory=list)
    error: str | None = None


def apply_unified_diff(patch: str, policy: WorkspacePathPolicy) -> UnifiedDiffResult:
    try:
        files = _patch_files(patch)
    except ValueError as exc:
        return UnifiedDiffResult(applied=False, error=str(exc))
    if not files:
        return UnifiedDiffResult(applied=False, error="patch contains no file entries")
    for relative, is_creation in files:
        try:
            policy.resolve(relative, must_exist=not is_creation, for_write=True)
        except ValueError as exc:
            return UnifiedDiffResult(applied=False, error=str(exc))
    check = run_git(
        ["apply", "--check", "--whitespace=nowarn", "-"],
        cwd=policy.root,
        input=patch,
        check=False,
    )
    if check.returncode != 0:
        return UnifiedDiffResult(
            applied=False, files=[item[0] for item in files], error=check.stderr.strip()
        )
    applied = run_git(
        ["apply", "--whitespace=nowarn", "-"],
        cwd=policy.root,
        input=patch,
        check=False,
    )
    if applied.returncode != 0:
        return UnifiedDiffResult(
            applied=False, files=[item[0] for item in files], error=applied.stderr.strip()
        )
    return UnifiedDiffResult(applied=True, files=[item[0] for item in files])


def _patch_files(patch: str) -> list[tuple[str, bool]]:
    headers = re.findall(r"^diff --git (\S+) (\S+)$", patch, flags=re.MULTILINE)
    old_headers = re.findall(r"^--- (\S+)", patch, flags=re.MULTILINE)
    results: list[tuple[str, bool]] = []
    for index, (old, new) in enumerate(headers):
        if not old.startswith("a/") or not new.startswith("b/"):
            raise ValueError("unified diff paths must use a/ and b/ prefixes")
        old_path = old[2:]
        new_path = new[2:]
        if old_path != new_path:
            raise ValueError("renames are not supported")
        windows = PureWindowsPath(new_path)
        posix = PurePosixPath(new_path.replace("\\", "/"))
        if windows.is_absolute() or windows.anchor or posix.is_absolute() or ".." in posix.parts:
            raise ValueError("unified diff path escapes workspace")
        is_creation = index < len(old_headers) and old_headers[index] == "/dev/null"
        results.append((posix.as_posix(), is_creation))
    return results
