"""Canonical Git diff artifact generated from the actual repair workspace (P-18).

Phase 6 追加了复现测试的剔除语义（D-06、D-07）：``tests/_codepilot_repro/`` 是
Agent 内部用来判断"修好了没有"的临时测试，它**绝不**能出现在交付的 Git diff 里，
也不能污染修改文件数与行数统计。剔除发生在 diff 生成层，因此 Search/Replace、
Unified Diff、create_file 以及 Coding Agent 工具循环产生的补丁都走同一条路。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from codepilot.repo.gitcmd import GitError, run_git
from codepilot.schemas.reproduction import REPRO_TEST_DIRECTORY
from codepilot.tools.base import WorkspacePathPolicy, is_test_path

__all__ = ["CanonicalDiff", "generate_git_diff", "is_excluded_path"]

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$")


class CanonicalDiff(BaseModel):
    base_commit: str
    text: str
    files: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
    excluded_files: list[str] = Field(default_factory=list)
    """被剔除的变更（默认只有复现测试）。它们不参与任何统计，也不交付。"""


def is_excluded_path(relative: str, directories: Sequence[str]) -> bool:
    """逐段精确匹配目录前缀；伪造目录名与大小写变体都不算被排除。"""
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    for directory in directories:
        prefix = tuple(PurePosixPath(directory.strip("/")).parts)
        if prefix and parts[: len(prefix)] == prefix:
            return True
    return False


def generate_git_diff(
    workspace: Path,
    base_commit: str,
    *,
    policy: WorkspacePathPolicy | None = None,
    excluded_directories: Sequence[str] = (REPRO_TEST_DIRECTORY,),
    delivery: bool = False,
    new_tests_reviewed: bool = False,
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
    kept, dropped = _filter_diff_blocks(tracked, excluded_directories)
    chunks = [kept.rstrip("\n")]
    for relative in sorted(item for item in untracked_result.split("\0") if item):
        if is_excluded_path(relative, excluded_directories):
            dropped.append(relative)
            continue
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
    if delivery and (not path_policy.allow_new_tests or not new_tests_reviewed):
        base_files = set(
            run_git(["ls-tree", "-r", "--name-only", resolved_base], cwd=root).stdout.splitlines()
        )
        unauthorized = [
            name for name in _changed_files(text) if name not in base_files and is_test_path(name)
        ]
        text, omitted = _filter_diff_blocks(text, unauthorized)
        dropped.extend(omitted)
        if text and not text.endswith("\n"):
            text += "\n"
    files = sorted(set(_changed_files(text)))
    added, deleted = _line_counts(text)
    return CanonicalDiff(
        base_commit=resolved_base,
        text=text,
        files=files,
        lines_added=added,
        lines_deleted=deleted,
        excluded_files=sorted(set(dropped)),
    )


def _filter_diff_blocks(diff: str, directories: Sequence[str]) -> tuple[str, list[str]]:
    """按文件块剔除被排除路径；解析不出路径的块一律保留（fail closed）。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
            continue
        current.append(line)
    if current:
        blocks.append(current)

    kept: list[str] = []
    dropped: list[str] = []
    for block in blocks:
        match = _DIFF_HEADER.match(block[0]) if block else None
        path = match.group("new") if match else None
        if path is not None and is_excluded_path(path, directories):
            dropped.append(path)
            continue
        kept.extend(block)
    return "\n".join(kept), dropped


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
