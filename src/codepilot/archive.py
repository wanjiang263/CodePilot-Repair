"""Phase28 candidate archive generator: the controlled generation path to schema v2.

``rsi.archive_candidate`` writes v1 ``candidate.json`` without snapshot/diff
integrity fields, and historical records mix two undeclared snapshot hash rules
(v1 hashed snapshot.json file bytes; v2-v5 hashed canonical JSON without saying
so). This module is the single versioned generation path that fixes that: every
hash rule is recorded next to its value, ``code_revision`` is gone in favor of
the authoritative ``diff_base_commit``, and a produced directory is verified to
reverse-apply before it counts as complete (docs/notes/phase28-plan.md, contract A).

Directory layout (exactly four files; no active pointer is ever created here):

- ``snapshot.json``  policy snapshot, still ``schema_version=1`` so
  ``rsi.validate_snapshot`` / ``rsi.load_selection`` keep working;
- ``candidate.json`` provenance and integrity record, ``schema_version=2``;
- ``sources.json``   repo-relative path -> LF-normalized source text;
- ``candidate.diff`` raw bytes of ``git diff --binary <diff_base_commit> -- <paths>``.

No model, sandbox or network imports. git runs as ``git -C <repo>`` with the
repository root injectable, so tests drive temporary repositories. The byte
level git hardening mirrors ``codepilot.repo.gitcmd`` invariants (forced
``core.autocrlf=false``, no interactive prompts, hostile ``GIT_*`` environment
stripped) because the recorded diff must stay byte-reproducible.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from codepilot.rsi import _json_hash, extract_prompts, read_json, validate_snapshot, version_dir

__all__ = [
    "GENERATOR_ID",
    "NORMALIZATION_NOTE",
    "SNAPSHOT_HASH_RULE",
    "SNAPSHOT_REF",
    "ArchiveError",
    "CandidateExistsError",
    "DiffVerificationError",
    "DirtyWorktreeError",
    "GitCommandError",
    "MissingSourceError",
    "canonical_sha256",
    "create_candidate",
    "lf_text",
    "worktree_dirty_files",
]

GENERATOR_ID = "phase28_archive"
SNAPSHOT_REF = "snapshot.json"
SNAPSHOT_HASH_RULE = "file_bytes_sha256"
NORMALIZATION_NOTE = "UTF-8 LF normalized; raw worktree hashes recorded separately"

#: candidate.json fields that may declare the parent snapshot hash, in precedence
#: order: v2 files declare ``snapshot_file_sha256``; v1 files (and the historical
#: v2-v5 records) declare ``snapshot_sha256`` whose rule was never annotated.
_DECLARED_SNAPSHOT_FIELDS: tuple[str, ...] = ("snapshot_file_sha256", "snapshot_sha256")

_FORCED_GIT_CONFIG: tuple[tuple[str, str], ...] = (
    # Byte-identical diffs regardless of the machine's checkout settings.
    ("core.autocrlf", "false"),
    ("core.longpaths", "true"),
)

#: Environment variables that would redirect git to a different repository or
#: index than the one under ``repo``; stripped like gitcmd does.
_HOSTILE_GIT_ENV: frozenset[str] = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE"}
)

_GIT_TIMEOUT_SECONDS = 120.0


class ArchiveError(ValueError):
    """Base class for candidate archive generation failures."""


class CandidateExistsError(ArchiveError):
    """The target version directory already exists; archives are never overwritten."""


class MissingSourceError(ArchiveError):
    """One or more requested source files do not exist in the repository."""


class DirtyWorktreeError(ArchiveError):
    """The tracked worktree is dirty and ``allow_dirty`` was not requested."""

    def __init__(self, message: str, *, files: list[str]) -> None:
        super().__init__(message)
        self.files = files


class DiffVerificationError(ArchiveError):
    """The recorded candidate.diff does not reverse-apply against the worktree."""


class GitCommandError(ArchiveError):
    """A git invocation failed while building the archive."""


def _spawn_git(
    repo: Path, args: Sequence[str], stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run one git command and return raw output; never raises on non-zero exit."""
    command = ["git", "-C", str(repo)]
    for key, value in _FORCED_GIT_CONFIG:
        command += ["-c", f"{key}={value}"]
    command += list(args)
    env = {name: value for name, value in os.environ.items() if name not in _HOSTILE_GIT_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            check=False,
            env=env,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise GitCommandError(f"git executable not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS:.0f}s"
        ) from exc


def _git(repo: Path, *args: str) -> bytes:
    completed = _spawn_git(repo, args)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitCommandError(
            f"git {' '.join(args)} failed (exit {completed.returncode}): {stderr}"
        )
    return completed.stdout


def lf_text(raw: bytes) -> str:
    """Decode UTF-8 bytes and normalize universal newlines to LF."""
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def canonical_sha256(payload: dict[str, Any]) -> str:
    """Canonical JSON SHA256: ``sort_keys`` + ``ensure_ascii=False`` (rsi._json_hash rule)."""
    return _json_hash(payload)


def worktree_dirty_files(repo: Path) -> list[str]:
    """Tracked-worktree status lines; untracked files never count as dirt."""
    output = _git(repo, "status", "--porcelain", "--untracked-files=no")
    lines = output.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip()]


def _version_folder(root: Path, version: str) -> Path:
    try:
        return version_dir(root, version)
    except ValueError as exc:
        raise ArchiveError(f"invalid archive version identifier: {version!r} ({exc})") from exc


def _relative_source(path: str) -> PurePosixPath:
    normalized = PurePosixPath(path.replace("\\", "/"))
    if not normalized.parts or normalized.is_absolute() or ".." in normalized.parts:
        raise ArchiveError(f"source path must be repo-relative: {path!r}")
    return normalized


def _require_commit(repo: Path, field: str, revision: str) -> None:
    if not revision:
        raise ArchiveError(f"{field} is required")
    try:
        _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    except GitCommandError as exc:
        raise ArchiveError(f"{field} does not resolve to a commit: {revision!r}") from exc


def _build_diff(repo: Path, diff_base_commit: str, paths: Sequence[str]) -> bytes:
    """Deterministic, byte-exact diff of ``diff_base_commit`` against the worktree."""
    return _git(
        repo,
        "diff",
        "--binary",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        diff_base_commit,
        "--",
        *paths,
    )


def _require_reversible(repo: Path, diff: bytes) -> None:
    """``git apply --check -R``: the check never modifies the worktree.

    An empty diff is trivially reversible, and ``git apply`` would reject the
    empty input outright ("No valid patches in input"), so there is nothing to
    verify in that case.
    """
    if not diff.strip():
        return
    completed = _spawn_git(repo, ["apply", "--check", "-R", "--whitespace=nowarn"], stdin=diff)
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DiffVerificationError(
            f"candidate.diff does not reverse-apply against the worktree: {stderr}"
        )


def _serialize(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes: sorted keys, no BOM, trailing newline."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def _parent_integrity(root: Path, parent: str | None) -> dict[str, Any] | None:
    """Record parent snapshot hashes next to the parent's own declaration.

    Comparison only, never a verdict: historical parents declared their snapshot
    hash under unannotated rules, so both candidate rules are reported and the
    generator proceeds regardless of the outcome.
    """
    if not parent:
        return None
    folder = _version_folder(root, parent)
    snapshot_path = folder / SNAPSHOT_REF
    if not snapshot_path.is_file():
        raise ArchiveError(f"parent candidate has no snapshot.json: {parent}")
    try:
        actual_canonical = canonical_sha256(read_json(snapshot_path))
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"parent snapshot.json is unreadable: {parent} ({exc})") from exc
    actual_file = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    declared: str | None = None
    declared_field: str | None = None
    candidate_path = folder / "candidate.json"
    if candidate_path.is_file():
        try:
            record = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if isinstance(record, dict):
            for field in _DECLARED_SNAPSHOT_FIELDS:
                value = record.get(field)
                if isinstance(value, str):
                    declared = value
                    declared_field = field
                    break
    return {
        "version": parent,
        "snapshot_file_sha256_actual": actual_file,
        "snapshot_canonical_sha256_actual": actual_canonical,
        "declared_snapshot_sha256": declared,
        "declared_snapshot_sha256_field": declared_field,
        "file_bytes_consistent": None if declared is None else declared == actual_file,
        "canonical_consistent": None if declared is None else declared == actual_canonical,
    }


def _read_sources(repo: Path, source_files: Sequence[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Read every source once: LF-normalized text plus raw worktree byte hashes."""
    sources: dict[str, str] = {}
    raw_worktree_hashes: dict[str, str] = {}
    missing: list[str] = []
    seen: set[str] = set()
    repo_root = repo.resolve()
    for entry in source_files:
        rel = _relative_source(entry)
        key = rel.as_posix()
        if key in seen:
            continue
        seen.add(key)
        target = repo / Path(*rel.parts)
        if not target.resolve().is_relative_to(repo_root) or not target.is_file():
            missing.append(key)
            continue
        try:
            raw = target.read_bytes()
            sources[key] = lf_text(raw)
        except (OSError, UnicodeDecodeError) as exc:
            raise ArchiveError(f"source file cannot be read as UTF-8: {key} ({exc})") from exc
        raw_worktree_hashes[key] = hashlib.sha256(raw).hexdigest()
    if missing:
        raise MissingSourceError(f"source files are missing from the repository: {missing}")
    return sources, raw_worktree_hashes


def _extract_prompts(sources: Mapping[str, str]) -> dict[str, str]:
    """rsi.extract_prompts over the LF sources; absent agent files yield no role.

    Manifest keys are repo-relative, so agent sources usually appear as
    ``src/codepilot/agents/{role}.py`` rather than ``agents/{role}.py``;
    resolve each role by path suffix so prompts survive the prefix. When
    several manifest keys name the same role file, the shortest key wins
    deterministically.
    """
    prompt_sources: dict[str, str] = {}
    for role in ("coder", "planner", "reviewer"):
        matches = sorted(
            (
                key
                for key in sources
                if PurePosixPath(key.replace("\\", "/")).parts[-2:] == ("agents", f"{role}.py")
            ),
            key=lambda key: (len(key), key),
        )
        prompt_sources[f"agents/{role}.py"] = sources[matches[0]] if matches else ""
    return extract_prompts(prompt_sources)


def _candidate_payload(
    *,
    version: str,
    parent: str | None,
    snapshot_file_sha256: str,
    snapshot_canonical_sha256: str,
    diff_base_commit: str,
    source_commit: str,
    diff_sha256: str,
    sources: Mapping[str, str],
    raw_worktree_hashes: Mapping[str, str],
    parent_integrity: dict[str, Any] | None,
    dirty_files: Sequence[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 2,
        "version": version,
        "parent": parent,
        "status": "pending",
        "created_from": GENERATOR_ID,
        "snapshot_ref": SNAPSHOT_REF,
        "snapshot_hash_rule": SNAPSHOT_HASH_RULE,
        "snapshot_file_sha256": snapshot_file_sha256,
        "snapshot_canonical_sha256": snapshot_canonical_sha256,
        "diff_base_commit": diff_base_commit,
        "source_commit": source_commit,
        "diff_sha256": diff_sha256,
        "source_hashes": {
            path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in sources.items()
        },
        "raw_worktree_hashes": dict(raw_worktree_hashes),
        "source_files": len(sources),
        "normalization": NORMALIZATION_NOTE,
        "parent_integrity": parent_integrity,
        "dirty_worktree": bool(dirty_files),
    }
    if dirty_files:
        payload["dirty_files"] = list(dirty_files)
    return payload


def create_candidate(
    root: Path,
    repo: Path,
    *,
    version: str,
    parent: str | None = None,
    diff_base_commit: str,
    source_commit: str,
    source_files: Sequence[str],
    allow_dirty: bool = False,
    instruction: str = "",
    experiences: Sequence[Mapping[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Generate one schema-v2 candidate directory under ``root``/``candidates``.

    ``root`` is the archive root, ``repo`` the source repository working tree.
    Refuses to overwrite an existing version directory, refuses a dirty tracked
    worktree unless ``allow_dirty`` records it, fails on missing sources, and
    verifies ``candidate.diff`` reverse-applies before reporting success; a
    failed verification removes the half-made directory. ``dry_run`` validates
    and computes everything (the apply check itself never writes) without
    creating any file. Returns the summary the CLI prints.
    """
    if not source_files:
        raise ArchiveError("source file list is empty; nothing to archive")
    parent = parent or None
    folder = _version_folder(root, version)
    if folder.exists():
        raise CandidateExistsError(f"candidate version directory already exists: {folder}")
    parent_integrity = _parent_integrity(root, parent)
    _require_commit(repo, "diff_base_commit", diff_base_commit)
    _require_commit(repo, "source_commit", source_commit)
    dirty_files = worktree_dirty_files(repo)
    if dirty_files and not allow_dirty:
        raise DirtyWorktreeError(
            f"tracked worktree is dirty ({len(dirty_files)} entries); "
            "re-run with allow_dirty to record it explicitly",
            files=dirty_files,
        )

    sources, raw_worktree_hashes = _read_sources(repo, source_files)
    snapshot_payload: dict[str, Any] = {
        "schema_version": 1,
        "version": version,
        "parent": parent,
        "status": "pending",
        "instruction": instruction,
        "prompts": _extract_prompts(sources),
        "experiences": [dict(entry) for entry in (experiences or ())],
    }
    snapshot = validate_snapshot(snapshot_payload, root)
    diff = _build_diff(repo, diff_base_commit, list(sources))
    diff_sha256 = hashlib.sha256(diff).hexdigest()

    if dry_run:
        # The serializer is deterministic, so hashing the serialized bytes equals
        # hashing the file that would be written; nothing is created on disk.
        snapshot_file_sha256 = hashlib.sha256(_serialize(snapshot)).hexdigest()
        _require_reversible(repo, diff)
    else:
        folder.mkdir(parents=True, exist_ok=False)
        try:
            snapshot_path = folder / SNAPSHOT_REF
            snapshot_path.write_bytes(_serialize(snapshot))
            snapshot_file_sha256 = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            (folder / "sources.json").write_bytes(_serialize(dict(sources)))
            diff_path = folder / "candidate.diff"
            diff_path.write_bytes(diff)
            diff_sha256 = hashlib.sha256(diff_path.read_bytes()).hexdigest()
            payload = _candidate_payload(
                version=version,
                parent=parent,
                snapshot_file_sha256=snapshot_file_sha256,
                snapshot_canonical_sha256=canonical_sha256(snapshot),
                diff_base_commit=diff_base_commit,
                source_commit=source_commit,
                diff_sha256=diff_sha256,
                sources=sources,
                raw_worktree_hashes=raw_worktree_hashes,
                parent_integrity=parent_integrity,
                dirty_files=dirty_files,
            )
            (folder / "candidate.json").write_bytes(_serialize(payload))
            _require_reversible(repo, diff)
        except BaseException:
            with contextlib.suppress(OSError):
                shutil.rmtree(folder)
            raise

    summary: dict[str, Any] = {
        "directory": str(folder),
        "version": version,
        "parent": parent,
        "diff_base_commit": diff_base_commit,
        "source_commit": source_commit,
        "snapshot_file_sha256": snapshot_file_sha256,
        "snapshot_canonical_sha256": canonical_sha256(snapshot),
        "diff_sha256": diff_sha256,
        "diff_bytes": len(diff),
        "source_files": len(sources),
        "dirty_worktree": bool(dirty_files),
    }
    if dirty_files:
        summary["dirty_files"] = dirty_files
    if dry_run:
        summary["dry_run"] = True
    return summary
