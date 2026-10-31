"""Phase29 candidate archive generator: the controlled generation path to schema v3.

``rsi.archive_candidate`` writes v1 ``candidate.json`` without snapshot/diff
integrity fields, and historical records mix two undeclared snapshot hash rules
(v1 hashed snapshot.json file bytes; v2-v5 hashed canonical JSON without saying
so). This module is the single versioned generation path that fixes that: every
hash rule is recorded next to its value, ``code_revision`` is gone in favor of
the authoritative ``diff_base_commit``, and a produced directory is verified to
round-trip before it counts as complete (docs/notes/phase28-plan.md, contract A;
docs/notes/phase29-archive-schema.md for the v3 extension).

Schema v3 (Phase29) binds the record to its claimed git source: in the default
``git_commit`` mode the source text is read from ``source_commit`` blobs (never
the worktree), ``diff_base_commit`` must be an ancestor of ``source_commit``,
``candidate.diff`` is generated from ``git diff diff_base source_commit`` and
the whole candidate.json is sealed by ``candidate_payload_sha256`` over its own
canonical JSON. The explicit ``worktree`` mode (the only path that tolerates a
dirty tree) reads the worktree, records ``raw_worktree_hashes``/``dirty_files``
and claims no git provenance (``source_commit`` is null).

Directory layout (exactly four files; no active pointer is ever created here):

- ``snapshot.json``  policy snapshot, still ``schema_version=1`` so
  ``rsi.validate_snapshot`` / ``rsi.load_selection`` keep working;
- ``candidate.json`` provenance and integrity record, ``schema_version=3``;
- ``sources.json``   repo-relative path -> LF-normalized source text;
- ``candidate.diff`` raw bytes of ``git diff --binary`` (commit-vs-commit in
  ``git_commit`` mode, diff_base-vs-worktree in ``worktree`` mode).

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
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from codepilot.rsi import _json_hash, extract_prompts, read_json, validate_snapshot, version_dir

__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "DEFAULT_REQUIRED_ROLES",
    "DIFF_FLAGS",
    "GENERATOR_ID",
    "MANIFEST_KINDS",
    "MANIFEST_KIND_DELTA",
    "MANIFEST_KIND_FULL",
    "NORMALIZATION_NOTE",
    "SCHEMA_VERSION",
    "SNAPSHOT_HASH_RULE",
    "SNAPSHOT_REF",
    "SOURCE_ORIGIN_GIT",
    "SOURCE_ORIGIN_WORKTREE",
    "ArchiveError",
    "CandidateExistsError",
    "DiffVerificationError",
    "DirtyWorktreeError",
    "GitCommandError",
    "IncompleteSourceManifestError",
    "MissingSourceError",
    "NonAncestorDiffBaseError",
    "SourceOriginError",
    "canonical_sha256",
    "create_candidate",
    "lf_text",
    "worktree_dirty_files",
]

GENERATOR_ID = "phase28_archive"
SCHEMA_VERSION = 3
#: Phase30 schema v4: source manifest (full/delta) plus parent snapshot
#: inheritance with strict role-prompt validation. v3 generation stays available
#: (and byte-reproducible) via ``candidate_schema_version=3``.
CANDIDATE_SCHEMA_VERSION = 4
MANIFEST_KIND_FULL = "full"
MANIFEST_KIND_DELTA = "delta"
MANIFEST_KINDS = (MANIFEST_KIND_FULL, MANIFEST_KIND_DELTA)
DEFAULT_REQUIRED_ROLES = ("coder", "planner", "reviewer")
#: Bound on the ancestor walk for snapshot inheritance; a corrupt archive must
#: not be able to send the generator into a cycle.
_MAX_INHERITANCE_HOPS = 8
SOURCE_ORIGIN_GIT = "git_commit"
SOURCE_ORIGIN_WORKTREE = "worktree"
SNAPSHOT_REF = "snapshot.json"
SNAPSHOT_HASH_RULE = "file_bytes_sha256"
NORMALIZATION_NOTE = "UTF-8 LF normalized; raw worktree hashes recorded separately"

#: Flags shared by the generator and the verifier so both sides compute the
#: exact same diff bytes for the same commits and paths.
DIFF_FLAGS = ("--binary", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames")

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
    """The tracked worktree is dirty and the mode does not tolerate dirt."""

    def __init__(self, message: str, *, files: list[str]) -> None:
        super().__init__(message)
        self.files = files


class DiffVerificationError(ArchiveError):
    """The recorded candidate.diff does not round-trip against the bound content."""


class GitCommandError(ArchiveError):
    """A git invocation failed while building the archive."""


class SourceOriginError(ArchiveError):
    """The requested source_origin mode is invalid or contradicts other arguments."""


class NonAncestorDiffBaseError(ArchiveError):
    """``diff_base_commit`` is not an ancestor of ``source_commit``."""


class IncompleteSourceManifestError(ArchiveError):
    """The source manifest is structurally invalid or an incomplete full set."""


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


def _require_ancestor(repo: Path, diff_base_commit: str, source_commit: str) -> None:
    """``git merge-base --is-ancestor``: exit 0 only when diff_base precedes source."""
    completed = _spawn_git(repo, ["merge-base", "--is-ancestor", diff_base_commit, source_commit])
    if completed.returncode != 0:
        raise NonAncestorDiffBaseError(
            f"diff_base_commit {diff_base_commit} is not an ancestor of "
            f"source_commit {source_commit}: candidate.diff could not be bound to a "
            "commit-to-commit change"
        )


def _commit_blob(repo: Path, commit: str, rel: str) -> bytes | None:
    """Raw bytes of ``commit:<rel>``, or None when the path is absent from it."""
    completed = _spawn_git(repo, ["cat-file", "blob", f"{commit}:{rel}"])
    if completed.returncode != 0:
        return None
    return completed.stdout


def _read_commit_sources(
    repo: Path, commit: str, source_files: Sequence[str]
) -> tuple[dict[str, str], dict[str, bytes]]:
    """Read every source from ``commit`` blobs: LF text plus raw blob bytes.

    This is the git_commit-mode source of truth: the worktree is never consulted
    for content, so an archived candidate cannot silently diverge from the commit
    it names.
    """
    sources: dict[str, str] = {}
    blobs: dict[str, bytes] = {}
    missing: list[str] = []
    seen: set[str] = set()
    for entry in source_files:
        rel = _relative_source(entry)
        key = rel.as_posix()
        if key in seen:
            continue
        seen.add(key)
        raw = _commit_blob(repo, commit, key)
        if raw is None:
            missing.append(key)
            continue
        try:
            sources[key] = lf_text(raw)
        except UnicodeDecodeError as exc:
            raise ArchiveError(f"blob {commit}:{key} is not valid UTF-8 text: {exc}") from exc
        blobs[key] = raw
    if missing:
        raise MissingSourceError(f"paths missing from source_commit {commit}: {missing}")
    return sources, blobs


def _read_base_blobs(
    repo: Path, diff_base_commit: str, paths: Sequence[str]
) -> dict[str, bytes | None]:
    """Raw diff_base blobs for the source paths; None marks a path it lacks
    (created after the base; the diff is expected to create the file)."""
    base: dict[str, bytes | None] = {}
    for key in paths:
        raw = _commit_blob(repo, diff_base_commit, key)
        if raw is not None:
            try:
                lf_text(raw)
            except UnicodeDecodeError as exc:
                raise ArchiveError(
                    f"blob {diff_base_commit}:{key} is not valid UTF-8 text: {exc}"
                ) from exc
        base[key] = raw
    return base


def _build_diff(
    repo: Path, diff_base_commit: str, paths: Sequence[str], source_commit: str | None = None
) -> bytes:
    """Deterministic, byte-exact diff: diff_base vs worktree, or commit-vs-commit
    when ``source_commit`` is given (git_commit mode)."""
    args: list[str] = ["diff", *DIFF_FLAGS, diff_base_commit]
    if source_commit:
        args.append(source_commit)
    args += ["--", *paths]
    return _git(repo, *args)


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


def _apply_patch_in_temp(blobs: Mapping[str, bytes | None], patch: bytes, *, reverse: bool) -> Path:
    """Materialize blobs in a throwaway directory and apply ``patch`` there.

    ``None`` entries mean the path does not exist in that tree (the patch is
    expected to create it on forward apply, or to have created it on reverse).
    git apply runs outside any repository, so neither the real worktree nor any
    ``.git`` metadata (e.g. linked worktrees) is touched. Returns the directory
    for content comparison; the caller removes it.
    """
    tmp = Path(tempfile.mkdtemp(prefix="codepilot-archive-apply-"))
    try:
        for rel, raw in blobs.items():
            if raw is None:
                continue
            target = tmp.joinpath(*PurePosixPath(rel).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        args = ["apply", "--whitespace=nowarn"]
        if reverse:
            args.append("-R")
        completed = _spawn_git(tmp, args, stdin=patch)
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise DiffVerificationError(
                f"candidate.diff failed to {'reverse-apply' if reverse else 'apply'} "
                f"in a temporary {'source' if reverse else 'base'} tree: {stderr}"
            )
        return tmp
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _verify_diff_roundtrip(
    repo: Path,
    *,
    diff: bytes,
    base_blobs: Mapping[str, bytes | None],
    source_texts: Mapping[str, str],
    source_blobs: Mapping[str, bytes | None] | None = None,
) -> None:
    """Bind diff, sources and commits together by applying it both directions.

    Forward: diff_base blobs + diff must reproduce the archived sources, file by
    file. Reverse: source blobs - diff must reproduce the diff_base blobs. With
    ``source_blobs=None`` (worktree mode) the reverse leg degrades to the read-only
    ``git apply --check -R`` against the real worktree, which holds the source
    content by construction. An empty diff is trivially consistent.
    """
    if not diff.strip():
        return
    forward_dir = _apply_patch_in_temp(base_blobs, diff, reverse=False)
    try:
        for rel, expected in source_texts.items():
            target = forward_dir.joinpath(*PurePosixPath(rel).parts)
            if not target.is_file():
                raise DiffVerificationError(f"forward apply did not produce {rel}")
            if lf_text(target.read_bytes()) != expected:
                raise DiffVerificationError(f"forward apply content mismatch: {rel}")
    finally:
        shutil.rmtree(forward_dir, ignore_errors=True)
    if source_blobs is None:
        _require_reversible(repo, diff)
        return
    reverse_dir = _apply_patch_in_temp(source_blobs, diff, reverse=True)
    try:
        for rel in source_blobs:
            base_raw = base_blobs.get(rel)
            target = reverse_dir.joinpath(*PurePosixPath(rel).parts)
            if base_raw is None:
                if target.exists():
                    raise DiffVerificationError(f"reverse apply should have deleted {rel}")
                continue
            if not target.is_file() or lf_text(target.read_bytes()) != lf_text(base_raw):
                raise DiffVerificationError(f"reverse apply content mismatch: {rel}")
    finally:
        shutil.rmtree(reverse_dir, ignore_errors=True)


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


def _resolved_commit(repo: Path, field: str, revision: str) -> str:
    """Resolve ``revision`` to the full 40-hex commit sha it names.

    Schema v4 records commits, never labels: the recorded value is the resolved
    sha, so a branch name or tag can neither be archived nor drift afterwards.
    """
    _require_commit(repo, field, revision)
    resolved = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    sha = resolved.decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ArchiveError(f"{field} did not resolve to a full commit sha: {revision!r}")
    return sha


def _read_candidate_record(root: Path, version: str) -> dict[str, Any]:
    folder = _version_folder(root, version)
    path = folder / "candidate.json"
    if not path.is_file():
        raise ArchiveError(f"candidate has no candidate.json: {version}")
    try:
        record = read_json(path)
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"candidate.json unreadable: {version} ({exc})") from exc
    return record


def _read_parent_snapshot(root: Path, version: str) -> dict[str, Any]:
    folder = _version_folder(root, version)
    path = folder / SNAPSHOT_REF
    if not path.is_file():
        raise ArchiveError(f"candidate has no snapshot.json: {version}")
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"snapshot.json unreadable: {version} ({exc})") from exc
    return payload


def _inherit_parent_snapshot(root: Path, parent: str | None) -> dict[str, Any]:
    """Walk the parent chain for the effective prompts/instruction/experiences.

    Phase30: v3 archived ``prompts={}`` and no experiences because the v3
    generator only extracted from its own (changed-files) source set — the
    defect that made v3 unactivatable. Inheritance therefore walks past an
    ancestor with empty prompts/experiences instead of propagating the defect:
    the nearest ancestor that actually declares them wins. This round's sources
    still override inherited prompts role by role.
    """
    inherited: dict[str, Any] = {"prompts": {}, "instruction": "", "experiences": []}
    chain: list[str] = []
    seen: set[str] = set()
    current = parent
    while current and len(seen) < _MAX_INHERITANCE_HOPS:
        if current in seen:
            raise ArchiveError(f"parent candidate chain contains a cycle at {current!r}")
        seen.add(current)
        chain.append(current)
        snapshot = _read_parent_snapshot(root, current)
        prompts = snapshot.get("prompts")
        if not inherited["prompts"] and isinstance(prompts, dict) and prompts:
            inherited["prompts"] = {
                role: text for role, text in prompts.items() if isinstance(text, str) and text
            }
        if not inherited["instruction"]:
            instruction = snapshot.get("instruction")
            if isinstance(instruction, str) and instruction:
                inherited["instruction"] = instruction
        if not inherited["experiences"]:
            experiences = snapshot.get("experiences")
            if isinstance(experiences, list) and experiences:
                inherited["experiences"] = [
                    dict(entry) for entry in experiences if isinstance(entry, dict)
                ]
        record = _read_candidate_record(root, current)
        current = record.get("parent") if isinstance(record.get("parent"), str) else None
    inherited["chain"] = chain
    return inherited


def _candidate_source_set(root: Path, parent: str | None) -> set[str]:
    """Union of every ancestor candidate's source paths (bounded walk)."""
    inherited: set[str] = set()
    seen: set[str] = set()
    current = parent
    while current and len(seen) < _MAX_INHERITANCE_HOPS:
        if current in seen:
            raise ArchiveError(f"parent candidate chain contains a cycle at {current!r}")
        seen.add(current)
        folder = _version_folder(root, current)
        sources_path = folder / "sources.json"
        if not sources_path.is_file():
            raise ArchiveError(f"parent candidate has no sources.json: {current}")
        try:
            payload = read_json(sources_path)
        except (OSError, ValueError) as exc:
            raise ArchiveError(f"parent sources.json unreadable: {current} ({exc})") from exc
        inherited |= {str(key) for key in payload}
        record = _read_candidate_record(root, current)
        current = record.get("parent") if isinstance(record.get("parent"), str) else None
    return inherited


def _validate_source_manifest(
    root: Path,
    *,
    parent: str | None,
    kind: str,
    required_roles: Sequence[str],
    source_keys: set[str],
) -> dict[str, Any]:
    """Enforce the schema-v4 source manifest rules; return the manifest record.

    - ``kind`` is only ``full`` or ``delta``;
    - ``delta`` requires a parent (missing files are inherited from it);
    - ``full`` must cover the union of every ancestor candidate's source set —
      archiving only this round's changed files is exactly the v3 defect — and
      must carry every required role's agent source.
    """
    if kind not in MANIFEST_KINDS:
        raise IncompleteSourceManifestError(
            f"source manifest kind must be one of {list(MANIFEST_KINDS)}, got {kind!r}"
        )
    if kind == MANIFEST_KIND_DELTA and not parent:
        raise IncompleteSourceManifestError(
            "delta candidate requires a parent; missing files can only be "
            "inherited from a parent candidate"
        )
    roles = list(dict.fromkeys(required_roles))
    unknown = [role for role in roles if role not in DEFAULT_REQUIRED_ROLES]
    if unknown:
        raise IncompleteSourceManifestError(f"unknown required roles: {unknown}")
    if kind == MANIFEST_KIND_FULL:
        inherited = _candidate_source_set(root, parent)
        missing_inherited = sorted(inherited - source_keys)
        if missing_inherited:
            raise IncompleteSourceManifestError(
                f"full source set must cover all {len(inherited)} inherited parent files; "
                f"missing {len(missing_inherited)}: {missing_inherited[:5]}"
            )
        for role in roles:
            present = any(
                PurePosixPath(key.replace("\\", "/")).parts[-2:] == ("agents", f"{role}.py")
                for key in source_keys
            )
            if not present:
                raise IncompleteSourceManifestError(
                    f"full source set is missing agents/{role}.py (required role {role!r})"
                )
    return {"kind": kind, "parent": parent, "required_roles": roles}


def _temp_source_clone(repo: Path, commit: str) -> Path:
    """Shared local clone checked out at ``commit``; never touches the worktree.

    Used when the tracked worktree is dirty and generation must still read
    ``source_commit`` blobs: a ``--shared`` clone references the original object
    database (no copy), so blob reads, ancestry and commit-vs-commit diffs are
    byte-identical while the user's uncommitted changes stay untouched (no
    stash, no checkout of the real worktree). Caller removes the directory.
    """
    tmp = Path(tempfile.mkdtemp(prefix="codepilot-archive-clone-"))
    try:
        _git(repo, "clone", "--shared", "--quiet", str(repo), str(tmp))
        _git(tmp, "checkout", "--quiet", "--detach", commit)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp


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
    source_commit: str | None,
    source_origin: str,
    diff_sha256: str,
    sources: Mapping[str, str],
    base_hashes: Mapping[str, str | None],
    raw_worktree_hashes: Mapping[str, str] | None,
    parent_integrity: dict[str, Any] | None,
    dirty_files: Sequence[str],
    candidate_schema_version: int = SCHEMA_VERSION,
    source_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": candidate_schema_version,
        "version": version,
        "parent": parent,
        "status": "pending",
        "created_from": GENERATOR_ID,
        "source_origin": source_origin,
        "source_commit": source_commit if source_origin == SOURCE_ORIGIN_GIT else None,
        "diff_base_commit": diff_base_commit,
        "snapshot_ref": SNAPSHOT_REF,
        "snapshot_hash_rule": SNAPSHOT_HASH_RULE,
        "snapshot_file_sha256": snapshot_file_sha256,
        "snapshot_canonical_sha256": snapshot_canonical_sha256,
        "diff_sha256": diff_sha256,
        "source_hashes": {
            path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in sources.items()
        },
        "base_hashes": dict(base_hashes),
        "source_files": len(sources),
        "normalization": NORMALIZATION_NOTE,
        "parent_integrity": parent_integrity,
        "dirty_worktree": bool(dirty_files),
    }
    if candidate_schema_version >= CANDIDATE_SCHEMA_VERSION:
        # v4: explicit schema mirror (single source of truth is schema_version;
        # the verifier requires the two to be equal) and the source manifest.
        payload["candidate_schema_version"] = candidate_schema_version
        payload["source_manifest"] = dict(source_manifest or {})
    if source_origin == SOURCE_ORIGIN_WORKTREE:
        payload["raw_worktree_hashes"] = dict(raw_worktree_hashes or {})
    if dirty_files:
        payload["dirty_files"] = list(dirty_files)
    # Seal the whole record: canonical JSON of everything except the seal itself.
    # Integrity checksum, not a signature; the verifier recomputes it and any
    # field change (status, parent, source_commit, ...) must break it.
    payload["candidate_payload_sha256"] = canonical_sha256(payload)
    return payload


def create_candidate(
    root: Path,
    repo: Path,
    *,
    version: str,
    parent: str | None = None,
    diff_base_commit: str,
    source_commit: str = "",
    source_files: Sequence[str],
    source_origin: str = SOURCE_ORIGIN_GIT,
    allow_dirty: bool = False,
    instruction: str = "",
    experiences: Sequence[Mapping[str, Any]] | None = None,
    dry_run: bool = False,
    candidate_schema_version: int = SCHEMA_VERSION,
    manifest_kind: str = MANIFEST_KIND_FULL,
    required_roles: Sequence[str] = DEFAULT_REQUIRED_ROLES,
    allow_dirty_via_temp_clone: bool = False,
) -> dict[str, Any]:
    """Generate one candidate directory under ``root``/``candidates``.

    ``root`` is the archive root, ``repo`` the source repository working tree.
    Two explicit modes (docs/notes/phase29-archive-schema.md):

    - ``git_commit`` (default): the worktree must be clean, ``source_commit`` is
      required, ``diff_base_commit`` must be its ancestor, source text is read
      from ``source_commit`` blobs and the diff is commit-vs-commit;
    - ``worktree``: the only path that tolerates a dirty tree (``allow_dirty``
      is implied); sources come from the worktree, ``raw_worktree_hashes`` and
      ``dirty_files`` are recorded, and no git provenance is claimed.

    ``candidate_schema_version`` selects the record schema: ``3`` (default, the
    Phase29 layout) or ``4`` (Phase30). v4 additionally records a
    ``source_manifest`` (``full``/``delta`` plus required roles), inherits
    prompts/instruction/experiences from the parent snapshot chain instead of
    archiving an empty policy, validates the snapshot strictly (all three role
    prompts non-empty; generation aborts otherwise), and resolves
    ``source_commit``/``diff_base_commit`` to full commit shas so labels can
    never be archived. A dirty worktree is still refused in git_commit mode
    unless ``allow_dirty_via_temp_clone`` is set, in which case sources are
    read from a throwaway ``--shared`` clone checked out at ``source_commit``
    (the user's uncommitted changes are never stashed or touched).

    Refuses to overwrite an existing version directory, fails on missing
    sources, and verifies ``candidate.diff`` round-trips (forward apply against
    diff_base blobs must reproduce the sources; reverse apply against source
    blobs must reproduce the base) before reporting success; a failed
    verification removes the half-made directory. ``dry_run`` validates and
    computes everything without creating the candidate directory (the apply
    checks run in throwaway directories either way). Returns the summary the
    CLI prints.
    """
    if candidate_schema_version not in (SCHEMA_VERSION, CANDIDATE_SCHEMA_VERSION):
        raise ArchiveError(
            f"candidate_schema_version must be {SCHEMA_VERSION} or "
            f"{CANDIDATE_SCHEMA_VERSION}, got {candidate_schema_version}"
        )
    if not source_files:
        raise ArchiveError("source file list is empty; nothing to archive")
    if source_origin not in (SOURCE_ORIGIN_GIT, SOURCE_ORIGIN_WORKTREE):
        raise SourceOriginError(
            f"source_origin must be {SOURCE_ORIGIN_GIT!r} or {SOURCE_ORIGIN_WORKTREE!r}, "
            f"got {source_origin!r}"
        )
    if source_origin == SOURCE_ORIGIN_GIT:
        if not source_commit:
            raise SourceOriginError("source_commit is required in git_commit mode")
        if allow_dirty:
            raise SourceOriginError(
                "allow_dirty requires source_origin='worktree'; git_commit mode "
                "refuses dirty worktrees"
            )
    elif source_commit:
        raise SourceOriginError(
            "worktree mode does not take source_commit; sources are read from the "
            "worktree and no git provenance is claimed"
        )
    parent = parent or None
    folder = _version_folder(root, version)
    if folder.exists():
        raise CandidateExistsError(f"candidate version directory already exists: {folder}")
    parent_integrity = _parent_integrity(root, parent)
    _require_commit(repo, "diff_base_commit", diff_base_commit)
    dirty_files = worktree_dirty_files(repo)
    temp_clone: Path | None = None
    effective_repo = repo
    try:
        if source_origin == SOURCE_ORIGIN_GIT:
            if candidate_schema_version >= CANDIDATE_SCHEMA_VERSION:
                # v4 records commits, never labels: resolve to full shas first.
                diff_base_commit = _resolved_commit(repo, "diff_base_commit", diff_base_commit)
                source_commit = _resolved_commit(repo, "source_commit", source_commit)
            else:
                _require_commit(repo, "source_commit", source_commit)
            _require_ancestor(repo, diff_base_commit, source_commit)
            if dirty_files:
                if allow_dirty_via_temp_clone:
                    temp_clone = _temp_source_clone(repo, source_commit)
                    effective_repo = temp_clone
                else:
                    raise DirtyWorktreeError(
                        f"tracked worktree is dirty ({len(dirty_files)} entries); "
                        "re-run with source_origin='worktree' to record it explicitly, or "
                        "allow_dirty_via_temp_clone=True to read from a throwaway clone",
                        files=dirty_files,
                    )

        if source_origin == SOURCE_ORIGIN_GIT:
            sources, source_blobs = _read_commit_sources(
                effective_repo, source_commit, source_files
            )
            raw_worktree_hashes: dict[str, str] | None = None
        else:
            sources, raw_worktree_hashes = _read_sources(repo, source_files)
            source_blobs = None
        # In git_commit mode the candidate's provenance is the commit tree; a
        # dirty worktree routed through a temp clone is not part of it.
        payload_dirty_files = [] if temp_clone is not None else dirty_files
        source_manifest: dict[str, Any] | None = None
        if candidate_schema_version >= CANDIDATE_SCHEMA_VERSION:
            source_manifest = _validate_source_manifest(
                root,
                parent=parent,
                kind=manifest_kind,
                required_roles=required_roles,
                source_keys=set(sources),
            )
        base_blobs = _read_base_blobs(effective_repo, diff_base_commit, list(sources))
        base_hashes = {
            path: None if raw is None else hashlib.sha256(lf_text(raw).encode("utf-8")).hexdigest()
            for path, raw in base_blobs.items()
        }
        if candidate_schema_version >= CANDIDATE_SCHEMA_VERSION:
            inherited = _inherit_parent_snapshot(root, parent)
            prompts = dict(inherited["prompts"])
            for role, text in _extract_prompts(sources).items():
                # Only a role whose agent source is archived this round overrides
                # the inherited value; absent roles keep the parent text.
                if text:
                    prompts[role] = text
            round_experiences = [dict(entry) for entry in (experiences or ())]
            effective_instruction = instruction or inherited["instruction"]
            effective_experiences = round_experiences or inherited["experiences"]
            snapshot_payload_v4: dict[str, Any] = {
                "schema_version": 1,
                "version": version,
                "parent": parent,
                "status": "pending",
                "instruction": effective_instruction,
                "prompts": prompts,
                "experiences": effective_experiences,
            }
            # Strict: an unactivatable candidate (empty role prompts) must abort
            # generation, not ship (Phase30 T03).
            snapshot = validate_snapshot(snapshot_payload_v4, root, strict=True)
        else:
            snapshot_payload = {
                "schema_version": 1,
                "version": version,
                "parent": parent,
                "status": "pending",
                "instruction": instruction,
                "prompts": _extract_prompts(sources),
                "experiences": [dict(entry) for entry in (experiences or ())],
            }
            snapshot = validate_snapshot(snapshot_payload, root)
        diff = _build_diff(
            effective_repo,
            diff_base_commit,
            list(sources),
            source_commit=source_commit if source_origin == SOURCE_ORIGIN_GIT else None,
        )
        diff_sha256 = hashlib.sha256(diff).hexdigest()

        if dry_run:
            # The serializer is deterministic, so hashing the serialized bytes equals
            # hashing the file that would be written; nothing is created on disk.
            snapshot_file_sha256 = hashlib.sha256(_serialize(snapshot)).hexdigest()
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
            except BaseException:
                with contextlib.suppress(OSError):
                    shutil.rmtree(folder)
                raise

        # Round-trip in throwaway directories; on failure nothing half-written is
        # allowed to survive.
        try:
            _verify_diff_roundtrip(
                repo,
                diff=diff,
                base_blobs=base_blobs,
                source_texts=sources,
                source_blobs=source_blobs,
            )
        except BaseException:
            if not dry_run:
                with contextlib.suppress(OSError):
                    shutil.rmtree(folder)
            raise

        payload = _candidate_payload(
            version=version,
            parent=parent,
            snapshot_file_sha256=snapshot_file_sha256,
            snapshot_canonical_sha256=canonical_sha256(snapshot),
            diff_base_commit=diff_base_commit,
            source_commit=source_commit or None,
            source_origin=source_origin,
            diff_sha256=diff_sha256,
            sources=sources,
            base_hashes=base_hashes,
            raw_worktree_hashes=raw_worktree_hashes,
            parent_integrity=parent_integrity,
            dirty_files=payload_dirty_files,
            candidate_schema_version=candidate_schema_version,
            source_manifest=source_manifest,
        )
        if not dry_run:
            try:
                (folder / "candidate.json").write_bytes(_serialize(payload))
            except BaseException:
                with contextlib.suppress(OSError):
                    shutil.rmtree(folder)
                raise

        summary: dict[str, Any] = {
            "directory": str(folder),
            "version": version,
            "parent": parent,
            "candidate_schema_version": candidate_schema_version,
            "source_origin": source_origin,
            "diff_base_commit": diff_base_commit,
            "source_commit": source_commit or None,
            "snapshot_file_sha256": snapshot_file_sha256,
            "snapshot_canonical_sha256": canonical_sha256(snapshot),
            "diff_sha256": diff_sha256,
            "diff_bytes": len(diff),
            "source_files": len(sources),
            "dirty_worktree": bool(dirty_files),
        }
        if source_manifest is not None:
            summary["source_manifest"] = source_manifest
        if temp_clone is not None:
            summary["temp_source_clone"] = True
        if dirty_files:
            summary["dirty_files"] = dirty_files
        if dry_run:
            summary["dry_run"] = True
        return summary
    finally:
        if temp_clone is not None:
            shutil.rmtree(temp_clone, ignore_errors=True)
