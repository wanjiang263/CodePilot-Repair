"""Evaluation-only synthetic dataset and patch-to-base location validation.

Reference patches remain outside the repository and never enter RetrievalInput.
Source digests use UTF-8 text with LF newlines, independent of checkout EOLs.
"""

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from codepilot.repo.models import Symbol
from codepilot.retrieval.eval import GoldLocations
from codepilot.retrieval.input import RetrievalInput


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or "\\" in value
        or ".." in path.parts
        or path.as_posix() != value
        or value == "."
    ):
        raise ValueError(f"Expected canonical relative path: {value!r}")
    return value


def contained(root: Path, value: str) -> Path:
    path = (root / relative_path(value)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes root: {value}")
    return path


class GoldLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    qualified_name: str = Field(min_length=1)
    line: int = Field(ge=1)
    _path = field_validator("file")(relative_path)


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1)
    repo: str
    base_commit: str
    issue: str = Field(min_length=1)
    source: str = "synthetic-development"
    gold_files: list[str] = Field(min_length=1)
    gold_locations: list[GoldLocation] = Field(min_length=1)
    gold_patch: str
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hashes: dict[str, str]

    _paths = field_validator("repo", "gold_patch")(relative_path)

    @model_validator(mode="after")
    def check_labels(self) -> "RetrievalCase":
        if PurePosixPath(self.gold_patch).is_relative_to(PurePosixPath(self.repo)):
            raise ValueError("Gold patch must be outside inference repository")
        for path in self.gold_files:
            relative_path(path)
        if len(set(self.gold_files)) != len(self.gold_files):
            raise ValueError("Duplicate gold file")
        if set(self.gold_files) != {loc.file for loc in self.gold_locations}:
            raise ValueError("Gold file/location mismatch")
        if set(self.source_hashes) != set(self.gold_files) or any(
            not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in self.source_hashes.values()
        ):
            raise ValueError("Source hashes must cover all gold files")
        if self.base_commit != "snapshot" and not re.fullmatch(
            r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.base_commit
        ):
            raise ValueError("Dataset base_commit must be a full SHA or snapshot")
        if self.source != "synthetic-development":
            raise ValueError("This dataset loader accepts synthetic development cases only")
        if not self.issue.strip():
            raise ValueError("Empty issue")
        return self

    def inference(self, dataset_root: Path) -> RetrievalInput:
        repo = contained(dataset_root, self.repo)
        if contained(dataset_root, self.gold_patch).is_relative_to(repo):
            raise ValueError("Gold patch must be outside inference repository")
        return RetrievalInput(
            task_id=self.task_id,
            repo=str(repo),
            base_commit=self.base_commit,
            issue=self.issue,
        )

    def labels(self) -> GoldLocations:
        return GoldLocations(
            tuple(sorted(self.gold_files)),
            tuple(sorted({(loc.file, loc.qualified_name) for loc in self.gold_locations})),
        )


def load_dataset(path: Path) -> list[RetrievalCase]:
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"Blank dataset line {number}")
        try:
            cases.append(RetrievalCase.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"Dataset line {number}: {exc}") from exc
    if not cases or len({case.task_id for case in cases}) != len(cases):
        raise ValueError("Dataset is empty or has duplicate task IDs")
    return sorted(cases, key=lambda case: case.task_id)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def patch_lines(patch: str, root: Path) -> dict[str, set[int]]:
    """Validate unified-diff context against base text, returning changed OLD lines."""
    changes: dict[str, set[int]] = {}
    lines = patch.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- a/") or i + 1 >= len(lines):
            raise ValueError("Expected a modification patch header")
        path = relative_path(lines[i][6:])
        if lines[i + 1] != f"+++ b/{path}" or path in changes:
            raise ValueError("Patch paths mismatch or duplicate")
        source = contained(root, path).read_text(encoding="utf-8").splitlines()
        changes[path] = set()
        i += 2
        previous_end = 0
        delta = 0
        while i < len(lines) and not lines[i].startswith("--- a/"):
            match = _HUNK.match(lines[i])
            if not match:
                raise ValueError("Expected unified diff hunk")
            old_start = int(match[1])
            old_count = int(match[2]) if match[2] is not None else 1
            new_count = int(match[4]) if match[4] is not None else 1
            expected_new = (
                old_start + delta + (1 if old_count == 0 else -1 if new_count == 0 else 0)
            )
            if int(match[3]) != expected_new:
                raise ValueError("Patch new-side coordinates mismatch")
            cursor = old_start if old_count == 0 else old_start - 1
            if cursor < previous_end:
                raise ValueError("Overlapping patch hunks")
            used_old = used_new = 0
            replacement_line = None
            i += 1
            while (
                i < len(lines)
                and lines[i][:1] in {" ", "+", "-"}
                and not lines[i].startswith("--- a/")
            ):
                line = lines[i]
                prefix = line[0]
                if prefix in {" ", "-"}:
                    if cursor >= len(source) or cursor < 0 or source[cursor] != line[1:]:
                        raise ValueError("Patch context does not match base")
                    if prefix == "-":
                        changes[path].add(cursor + 1)
                        replacement_line = cursor + 1
                    else:
                        replacement_line = None
                    cursor += 1
                    used_old += 1
                if prefix == "+":
                    changes[path].add(replacement_line or max(1, min(cursor + 1, len(source))))
                if prefix in {" ", "+"}:
                    used_new += 1
                i += 1
            if used_old != old_count or used_new != new_count:
                raise ValueError("Patch hunk line counts mismatch")
            previous_end = cursor
            delta += new_count - old_count
        if not changes[path]:
            raise ValueError("Patch has no changes")
    if not changes:
        raise ValueError("Empty patch")
    return changes


def validate_gold(
    case: RetrievalCase, root: Path, symbols: Sequence[Symbol], dataset_root: Path
) -> None:
    """Run only on the evaluation side, against the indexed base snapshot."""
    patch_path = contained(dataset_root, case.gold_patch)
    if patch_path.is_relative_to(root.resolve()):
        raise ValueError("Gold patch must be outside inference repository")
    patch = patch_path.read_bytes()
    if hashlib.sha256(patch).hexdigest() != case.patch_sha256:
        raise ValueError("Gold patch digest mismatch")
    changes = patch_lines(patch.decode("utf-8"), root)
    if set(changes) != set(case.gold_files):
        raise ValueError("Gold files differ from patch")
    for path, digest in case.source_hashes.items():
        text = contained(root, path).read_text(encoding="utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Gold source digest mismatch")
    for loc in case.gold_locations:
        matching = [
            s
            for s in symbols
            if s.path == loc.file
            and s.qualified_name == loc.qualified_name
            and s.start_line <= loc.line <= s.end_line
        ]
        if len(matching) != 1 or loc.line not in changes[loc.file]:
            raise ValueError("Gold location not mapped to changed base Symbol")
    for path, changed in changes.items():
        for line in changed:
            if not any(
                loc.file == path
                and any(
                    s.path == path
                    and s.qualified_name == loc.qualified_name
                    and s.start_line <= line <= s.end_line
                    for s in symbols
                )
                for loc in case.gold_locations
            ):
                raise ValueError("Patch change has no gold Symbol")
