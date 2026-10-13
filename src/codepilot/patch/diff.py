"""Canonical Git diff artifact generated from the actual repair workspace (P-18)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from codepilot.repo.gitcmd import GitError, run_git
from codepilot.tools.base import WorkspacePathPolicy

__all__ = ["CanonicalDiff", "generate_git_diff"]


class CanonicalDiff(BaseModel):
    base_commit: str
    text: str
    files: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0


def generate_git_diff(
    workspace: Path,
    base_commit: str,
    *,
    policy: WorkspacePathPolicy | None = None,
) -> CanonicalDiff:
    root = Path(workspace).resolve(strict=True)
    path_policy = policy or WorkspacePathPolicy(root)
    resolved_base = run_git(
        ["rev-parse", "--verify", f"{base_commit}^{{commit}}"], cwd=root
    ).stdout.strip()
    tracked = run_git(
        [
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            resolved_base,
            "--",
        ],
        cwd=root,
    ).stdout
    untracked_result = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], cwd=root
    ).stdout
    chunks = [tracked.rstrip("\n")]
    for relative in sorted(item for item in untracked_result.split("\0") if item):
        try:
            target = path_policy.resolve(relative, must_exist=True)
        except ValueError:
            continue
        if not target.is_file():
            continue
        result = run_git(
            [
                "diff",
                "--no-index",
                "--binary",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--",
                "NUL" if _is_windows() else "/dev/null",
                relative,
            ],
            cwd=root,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise GitError(
                "cannot create diff for untracked file",
                args=result.args,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        chunk = _normalize_untracked_headers(result.stdout, relative)
        chunks.append(chunk.rstrip("\n"))
    text = "\n".join(chunk for chunk in chunks if chunk).replace("\r\n", "\n")
    if text:
        text += "\n"
    files = sorted(set(_changed_files(text)))
    added, deleted = _line_counts(text)
    return CanonicalDiff(
        base_commit=resolved_base,
        text=text,
        files=files,
        lines_added=added,
        lines_deleted=deleted,
    )


def _is_windows() -> bool:
    import os

    return os.name == "nt"


def _normalize_untracked_headers(diff: str, relative: str) -> str:
    lines = diff.replace("\\", "/").splitlines()
    normalized: list[str] = []
    seen_old = False
    seen_new = False
    for line in lines:
        if line.startswith("diff --git "):
            normalized.append(f"diff --git a/{relative} b/{relative}")
        elif line.startswith("--- ") and not seen_old:
            normalized.append("--- /dev/null")
            seen_old = True
        elif line.startswith("+++ ") and not seen_new:
            normalized.append(f"+++ b/{relative}")
            seen_new = True
        else:
            normalized.append(line)
    return "\n".join(normalized)


def _changed_files(diff: str) -> list[str]:
    return [
        match.group(1)
        for line in diff.splitlines()
        if (match := re.match(r"^diff --git a/(.+?) b/(.+)$", line)) is not None
    ]


def _line_counts(diff: str) -> tuple[int, int]:
    added = 0
    deleted = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted
