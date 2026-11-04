"""Phase29 候选归档完整性校验器（只读，v2 与 v3 双 schema 支持）。

对 ``<archive_root>/candidates/<version>/`` 下的候选目录做完整性校验，绝不写档案、
绝不创建 active 指针、绝不修改任何历史文件：

**v3 新档案**（candidate.json ``schema_version==3``，Phase29 来源绑定）在 v2 检查
之外逐条复核：``candidate_payload_sha256``（对除自身外 canonical JSON 复算，任何
字段篡改必失败）、``source_origin`` 声明一致性、``source_commit`` 可解析与祖先
关系（``merge-base --is-ancestor``）、逐文件 ``source_commit_blob`` /
``base_commit_blob``（``git show <commit>:<path>`` 的 LF 哈希）、``diff_forward_apply``
（临时目录预写 diff_base blob 后正向应用并逐文件比对 sources.json）与
``diff_reverse_apply``（git_commit 模式在临时 source blob 树上反向应用比对；
worktree 模式沿用对真实工作树的 ``git apply --check -R``）。git_commit 模式不
读工作区内容：后续工作区漂移不影响校验结论。

**v2 新档案**（``schema_version==2``）保持 Phase28 语义：snapshot 双哈希、diff
字节哈希、对工作区的反向应用检查、sources.json LF 哈希与文件数、当前工作区原始
字节哈希（漂移计入失败，``--ignore-raw-drift`` 显式放行）、parent_integrity 复算、
未被 active.json 引用。

**旧档案**（schema_version 缺失或 ==1：phase26-*、phase27-*、baseline-*）
只推断标注、不修改：读 candidate.json 声明的 snapshot SHA256（值比对决定规则，
字段名不改变语义），与实际文件字节哈希 / canonical JSON 哈希比对，推断
``inferred_rule``（``file_bytes`` / ``canonical_json_snapshot`` / 均不等 →
``unexplained_mismatch`` / 无声明 → ``null``）；另输出 ``git hash-object`` blob
sha1 与基线提交中 blob 的 SHA256（不可用时跳过并标注）。历史声明值与文件字节
不符但与 canonical 一致（phase26-v2..v5 的已知事实）由此得到解释，不再计入
unexplained。旧档案的 unexplained_mismatch 同样计入退出码。

**历史基线**（Phase29 T05）：若 ``<archive_root>/candidates/historical-baseline.json``
存在（由 ``scripts/phase29_baseline_manifest.py`` 生成），对清单中每个文件复算当前
字节 SHA256 并与记录值比对；任何不一致（含"修改后提交"式篡改）都使
``historical_baseline`` 检查失败并计入退出码。清单缺失只记注释（无法证明 ≠ 已证伪）。

用法::

    python scripts/phase28_archive_verify.py --all [--json <path>] \\
        [--archive-root <dir>] [--repo <dir>] [--ignore-raw-drift]
    python scripts/phase28_archive_verify.py --version <v> [...]（可重复）

退出码：0 全部通过（含旧档案推断全部得到解释）；1 校验失败、存在
unexplained_mismatch 或历史基线不一致；2 用法错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from codepilot.archive import (
    DEFAULT_REQUIRED_ROLES,
    GIT_NORMALIZATION_POLICY,
    MANIFEST_KINDS,
    NORMALIZATION_NOTE,
    NORMALIZATION_NOTE_WORKTREE,
    SNAPSHOT_HASH_RULE,
    WORKTREE_NORMALIZATION_POLICY,
    _apply_patch_in_temp,
    _candidate_source_set,
    _extract_prompts,
    _inherit_parent_snapshot,
    _parent_integrity,
    candidate_tree_sha256,
    canonical_sha256,
    derive_v5_manifest,
    lf_text,
    verify_v5_roundtrip,
)
from codepilot.rsi import read_json, validate_snapshot, version_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE_ROOT = REPO_ROOT / "configs" / "harness"

FILE_BYTES_RULE = "file_bytes"
CANONICAL_RULE = "canonical_json_snapshot"
NEW_SCHEMA_VERSIONS = (2, 3, 4, 5)
SOURCE_ORIGIN_GIT = "git_commit"
SOURCE_ORIGIN_WORKTREE = "worktree"
HISTORICAL_MANIFEST_NAME = "historical-baseline.json"
#: Phase28-33 wording: git_commit candidates never actually stored raw hashes.
LEGACY_NORMALIZATION_NOTE = "UTF-8 LF normalized; raw worktree hashes recorded separately"
#: Historical candidates that predate ``normalization_policy``. The seal pins the
#: exact payload, so a new candidate reusing an old version name cannot qualify.
LEGACY_NORMALIZATION_EXEMPT: dict[str, str] = {
    "phase29-candidate-v3": "2d88b1de5910e914a1b58a1b3d846caaecedb1d28210754485188cfd10e592a4",
    "phase30-candidate-v4": "7b6c125ebef80d1fa1f33e2c5c4b6814d43325661ab9ebdd37e4555be075bb3c",
    "phase31-candidate-v5": "664037d863b4cb04f1e69bbdf44c2af090a17218beaa08c80e5077ff44a4fce1",
    "phase33-candidate-v6": "d96374ef56a93b4628a29cf09907a52d2d2627362084d0862066360a2191769d",
}
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_FILE_NAMES = ("candidate.diff", "candidate.json", "snapshot.json", "sources.json")

_GIT_TIMEOUT_SECONDS = 120.0

_HOSTILE_GIT_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE"}
)


def _spawn_git(
    repo: Path, args: Sequence[str], stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes] | None:
    """Run one git command; return None instead of raising (verifier is read-only)."""
    command = ["git", "-C", str(repo)]
    command += ["-c", "core.autocrlf=false", "-c", "core.longpaths=true"]
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
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def _git_bytes(repo: Path, args: Sequence[str], stdin: bytes | None = None) -> bytes | None:
    completed = _spawn_git(repo, args, stdin=stdin)
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_repo_relative(repo: Path, path: Path) -> PurePosixPath | None:
    """POSIX repo-relative path, or None when ``path`` lies outside ``repo``."""
    try:
        relative = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return None
    return PurePosixPath(relative.as_posix())


def _read_candidate(folder: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = folder / "candidate.json"
    if not path.is_file():
        return None, "candidate.json is missing"
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        return None, f"candidate.json unreadable: {exc}"
    return payload, None


def classify(candidate: dict[str, Any] | None) -> str:
    """``new`` (schema_version 2 or 3), ``old`` (missing or 1) or ``unknown``."""
    if candidate is None:
        return "unknown"
    schema = candidate.get("schema_version")
    if schema in NEW_SCHEMA_VERSIONS:
        return "new"
    if schema is None or schema == 1:
        return "old"
    return "unknown"


def _failure(check: str, detail: str) -> dict[str, Any]:
    return {"check": check, "ok": False, "detail": detail}


def _pass(check: str, detail: str = "ok") -> dict[str, Any]:
    return {"check": check, "ok": True, "detail": detail}


def _check_declared_metadata(candidate: dict[str, Any], folder: Path, record: Any) -> None:
    version = str(candidate.get("version", ""))
    record(
        "version_matches_directory",
        version == folder.name,
        f"candidate.json version={version!r}, directory={folder.name!r}",
    )
    record(
        "snapshot_hash_rule_declared",
        candidate.get("snapshot_hash_rule") == SNAPSHOT_HASH_RULE,
        f"snapshot_hash_rule={candidate.get('snapshot_hash_rule')!r} (expected {SNAPSHOT_HASH_RULE!r})",
    )
    record(
        "snapshot_ref_fixed",
        candidate.get("snapshot_ref") == "snapshot.json",
        f"snapshot_ref={candidate.get('snapshot_ref')!r}",
    )


def _check_snapshot_hashes(
    folder: Path, candidate: dict[str, Any], record: Any
) -> tuple[str | None, str | None]:
    snapshot_path = folder / "snapshot.json"
    file_hash: str | None = None
    canonical_hash: str | None = None
    if not snapshot_path.is_file():
        record("snapshot_file_sha256", False, "snapshot.json is missing")
        record("snapshot_canonical_sha256", False, "snapshot.json is missing")
    else:
        raw = snapshot_path.read_bytes()
        file_hash = _sha256_bytes(raw)
        declared_file = candidate.get("snapshot_file_sha256")
        record(
            "snapshot_file_sha256",
            isinstance(declared_file, str) and declared_file == file_hash,
            f"declared={declared_file!r} actual_file_bytes={file_hash}",
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
            canonical_hash = canonical_sha256(payload) if isinstance(payload, dict) else None
        except (OSError, UnicodeDecodeError, ValueError):
            canonical_hash = None
        declared_canonical = candidate.get("snapshot_canonical_sha256")
        record(
            "snapshot_canonical_sha256",
            canonical_hash is not None and declared_canonical == canonical_hash,
            f"declared={declared_canonical!r} actual_canonical={canonical_hash!r}",
        )
    return file_hash, canonical_hash


def _check_sources_json(
    folder: Path, candidate: dict[str, Any], record: Any
) -> dict[str, Any] | None:
    """Recompute sources.json against the declared LF hashes; return the payload."""
    sources_path = folder / "sources.json"
    if not sources_path.is_file():
        record("source_hashes", False, "sources.json is missing")
        record("source_files_count", False, "sources.json is missing")
        return None
    try:
        sources_payload = json.loads(sources_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        record("source_hashes", False, f"sources.json unreadable: {exc}")
        record("source_files_count", False, f"sources.json unreadable: {exc}")
        return None
    if not isinstance(sources_payload, dict):
        record("source_hashes", False, "sources.json is not a JSON object")
        record("source_files_count", False, "sources.json is not a JSON object")
        return None
    source_hashes = candidate.get("source_hashes")
    hashes: dict[str, Any] = source_hashes if isinstance(source_hashes, dict) else {}
    sources: dict[str, Any] = sources_payload
    missing_keys = sorted(set(hashes) - set(sources))
    extra_keys = sorted(set(sources) - set(hashes))
    recomputed = {
        path: _sha256_bytes(text.encode("utf-8"))
        for path, text in sources.items()
        if isinstance(text, str)
    }
    non_string = sorted(path for path, text in sources.items() if not isinstance(text, str))
    bad = sorted(path for path in set(recomputed) & set(hashes) if recomputed[path] != hashes[path])
    ok = bool(hashes) and not (missing_keys or extra_keys or non_string or bad)
    record(
        "source_hashes",
        ok,
        f"mismatched={bad[:5]} only_in_candidate={missing_keys[:5]} "
        f"only_in_sources={extra_keys[:5]} non_string={non_string[:5]}",
    )
    declared_count = candidate.get("source_files")
    record(
        "source_files_count",
        declared_count == len(sources),
        f"declared={declared_count!r} actual={len(sources)}",
    )
    return sources_payload


def _check_raw_worktree_hashes(
    candidate: dict[str, Any], repo: Path, ignore_raw_drift: bool, record: Any
) -> dict[str, str]:
    raw_hashes = candidate.get("raw_worktree_hashes")
    raw_map: dict[str, Any] = raw_hashes if isinstance(raw_hashes, dict) else {}
    raw_drift: dict[str, str] = {}
    if not raw_map:
        record("raw_worktree_hashes", False, "raw_worktree_hashes missing or empty")
        return raw_drift
    for path, declared in sorted(raw_map.items()):
        posix = PurePosixPath(str(path).replace("\\", "/"))
        if posix.is_absolute() or ".." in posix.parts or not posix.parts:
            raw_drift[path] = "unsafe path"
            continue
        target = repo.joinpath(*posix.parts)
        if not target.is_file():
            raw_drift[path] = "missing from worktree"
            continue
        actual = _sha256_bytes(target.read_bytes())
        if actual != declared:
            raw_drift[path] = actual
    if raw_drift:
        detail = f"{len(raw_drift)} file(s) differ from recorded raw worktree hashes"
        if ignore_raw_drift:
            record("raw_worktree_hashes", True, f"{detail} (ignored by --ignore-raw-drift)")
        else:
            record("raw_worktree_hashes", False, detail)
    else:
        record("raw_worktree_hashes", True, f"{len(raw_map)} paths match the worktree")
    return raw_drift


def _check_parent_integrity(candidate: dict[str, Any], archive_root: Path, record: Any) -> None:
    parent = candidate.get("parent")
    if parent is None:
        return
    try:
        parent_folder = version_dir(archive_root, str(parent))
    except ValueError as exc:
        record("parent_integrity_matches", False, f"invalid parent identifier: {exc}")
        return
    parent_snapshot = parent_folder / "snapshot.json"
    if not parent_snapshot.is_file():
        record("parent_integrity_matches", False, f"parent snapshot missing: {parent}")
        return
    integrity = candidate.get("parent_integrity")
    if not isinstance(integrity, dict):
        record("parent_integrity_matches", False, "parent_integrity record missing")
        return
    parent_raw = parent_snapshot.read_bytes()
    parent_file = _sha256_bytes(parent_raw)
    try:
        parent_canonical = canonical_sha256(json.loads(parent_raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        parent_canonical = None
    problems: list[str] = []
    if integrity.get("snapshot_file_sha256_actual") != parent_file:
        problems.append("recorded file hash differs from recomputation")
    if parent_canonical is None:
        problems.append("parent snapshot.json is not a JSON object")
    elif integrity.get("snapshot_canonical_sha256_actual") != parent_canonical:
        problems.append("recorded canonical hash differs from recomputation")
    declared = integrity.get("declared_snapshot_sha256")
    expected_file_flag = None if declared is None else declared == parent_file
    expected_canonical_flag = (
        None if declared is None or parent_canonical is None else declared == parent_canonical
    )
    if integrity.get("file_bytes_consistent") != expected_file_flag:
        problems.append("file_bytes_consistent flag inconsistent with recomputation")
    if integrity.get("canonical_consistent") != expected_canonical_flag:
        problems.append("canonical_consistent flag inconsistent with recomputation")
    record(
        "parent_integrity_matches",
        not problems,
        "; ".join(problems) if problems else f"parent {parent} matches",
    )


def _legacy_normalization_exempt(candidate: dict[str, Any]) -> bool:
    version = candidate.get("version")
    if not isinstance(version, str):
        return False
    expected = LEGACY_NORMALIZATION_EXEMPT.get(version)
    return (
        expected is not None
        and candidate.get("normalization") == LEGACY_NORMALIZATION_NOTE
        and candidate.get("candidate_payload_sha256") == expected
    )


def _check_normalization(candidate: dict[str, Any], record: Any) -> bool:
    """Verify the normalization declaration against what is actually stored.

    New candidates carry a structured ``normalization_policy`` selected by
    ``source_origin``: git_commit stores LF hashes only, worktree stores raw
    hashes too. Historical v3/v4/v5/v6 candidates predate the policy; they are
    exempt only when BOTH the version and the sealed payload hash match the
    pinned table, so a new candidate reusing an old version name fails. Returns
    the ``normalization_note_stale`` flag (true only for pinned legacy records).
    """
    schema = candidate.get("schema_version")
    note = candidate.get("normalization")
    policy = candidate.get("normalization_policy")
    origin = candidate.get("source_origin")
    if policy is None:
        if _legacy_normalization_exempt(candidate):
            record(
                "normalization_policy",
                True,
                f"legacy candidate {candidate.get('version')} exempt by pinned payload seal; "
                "normalization_note_stale (raw hashes were never stored)",
            )
            return True
        if isinstance(schema, int) and schema < 3:
            return False
        record(
            "normalization_policy",
            False,
            "candidate has no normalization_policy and is not a pinned legacy archive",
        )
        return False
    problems: list[str] = []
    if not isinstance(policy, dict):
        problems.append(f"normalization_policy must be an object, got {policy!r}")
    elif origin == SOURCE_ORIGIN_GIT:
        for key, expected in GIT_NORMALIZATION_POLICY.items():
            if policy.get(key) != expected:
                problems.append(f"{key} must be {expected!r}, got {policy.get(key)!r}")
        if note != NORMALIZATION_NOTE:
            problems.append("normalization note differs from the git_commit policy text")
        if "raw_worktree_hashes" in candidate:
            problems.append("git_commit normalization policy must not carry raw_worktree_hashes")
    elif origin == SOURCE_ORIGIN_WORKTREE:
        for key, expected in WORKTREE_NORMALIZATION_POLICY.items():
            if policy.get(key) != expected:
                problems.append(f"{key} must be {expected!r}, got {policy.get(key)!r}")
        if note != NORMALIZATION_NOTE_WORKTREE:
            problems.append("normalization note differs from the worktree policy text")
        if "raw_worktree_hashes" not in candidate:
            problems.append("worktree normalization policy requires raw_worktree_hashes")
    else:
        problems.append(f"unknown source_origin {origin!r} for a normalization policy")
    record(
        "normalization_policy",
        not problems,
        "; ".join(problems) if problems else "normalization policy matches the stored bytes",
    )
    return False


def _check_not_active(version: str, archive_root: Path, record: Any) -> None:
    active_problems: list[str] = []
    for pointer_name in ("active.json", "active.next.json"):
        pointer = archive_root / pointer_name
        if not pointer.exists():
            continue
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            referenced = payload.get("version") if isinstance(payload, dict) else None
        except (OSError, UnicodeDecodeError, ValueError):
            active_problems.append(
                f"{pointer_name} exists but is unreadable; cannot prove it does not reference {version}"
            )
            continue
        if referenced == version:
            active_problems.append(f"{pointer_name} references this version")
    record(
        "not_active",
        not active_problems,
        "; ".join(active_problems)
        if active_problems
        else "no active pointer references this version",
    )


def _check_worktree_reverse_apply(repo: Path, diff: bytes, record: Any) -> None:
    """v2/worktree-mode reverse apply: read-only check against the real worktree."""
    completed = _spawn_git(repo, ["apply", "--check", "-R", "--whitespace=nowarn"], stdin=diff)
    if completed is None:
        record("diff_reverse_apply", False, "git apply --check could not be executed")
    elif completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        record("diff_reverse_apply", False, f"git apply --check -R failed: {stderr}")
    else:
        record("diff_reverse_apply", True, "candidate.diff reverse-applies cleanly")


def _check_temp_reverse_apply(
    blobs: dict[str, bytes | None],
    base_blobs: dict[str, bytes | None],
    diff: bytes,
    record: Any,
) -> None:
    """git_commit-mode reverse apply: rebuild the source tree from blobs in a
    throwaway directory, apply -R there, and compare the result to diff_base."""
    try:
        reverse_dir = _apply_patch_in_temp(blobs, diff, reverse=True)
    except Exception as exc:  # archive.DiffVerificationError or git failure
        record("diff_reverse_apply", False, f"reverse apply against source blobs failed: {exc}")
        return
    try:
        problems: list[str] = []
        for rel in blobs:
            expected_base = base_blobs.get(rel)
            target = reverse_dir.joinpath(*PurePosixPath(rel).parts)
            if expected_base is None:
                if target.exists():
                    problems.append(f"{rel} should have been deleted by reverse apply")
                continue
            if not target.is_file() or lf_text(target.read_bytes()) != lf_text(expected_base):
                problems.append(f"{rel} does not match diff_base after reverse apply")
        record(
            "diff_reverse_apply",
            not problems,
            "; ".join(problems[:5]) if problems else "reverse-applied tree equals diff_base blobs",
        )
    finally:
        shutil.rmtree(reverse_dir, ignore_errors=True)


def verify_new_archive(
    folder: Path,
    candidate: dict[str, Any],
    *,
    archive_root: Path,
    repo: Path,
    ignore_raw_drift: bool,
) -> dict[str, Any]:
    """Dispatch on schema_version: v3/v4 source binding, or the v2 rule set."""
    if candidate.get("schema_version") == 5:
        return _verify_v5_archive(folder, candidate, archive_root=archive_root, repo=repo)
    if candidate.get("schema_version") in (3, 4):
        return _verify_v3_archive(
            folder,
            candidate,
            archive_root=archive_root,
            repo=repo,
            ignore_raw_drift=ignore_raw_drift,
        )
    return _verify_v2_archive(
        folder, candidate, archive_root=archive_root, repo=repo, ignore_raw_drift=ignore_raw_drift
    )


def _verify_v5_archive(
    folder: Path,
    candidate: dict[str, Any],
    *,
    archive_root: Path,
    repo: Path,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append(_pass(name, detail) if ok else _failure(name, detail))

    _check_declared_metadata(candidate, folder, record)
    file_hash, canonical_hash = _check_snapshot_hashes(folder, candidate, record)
    sources = _check_sources_json(folder, candidate, record)
    normalization_note_stale = _check_normalization(candidate, record)
    _check_snapshot_strict_roles(folder, record)
    _check_not_active(str(candidate.get("version")), archive_root, record)
    for name in ("active.json", "active.next.json"):
        record("no_active_pointer", not (archive_root / name).exists(), f"{name} must be absent")
    record("status_pending", candidate.get("status") == "pending", "v5 status must be pending")
    record(
        "candidate_schema_version",
        candidate.get("candidate_schema_version") == 5,
        "candidate_schema_version must equal schema_version=5",
    )
    seal = {k: v for k, v in candidate.items() if k != "candidate_payload_sha256"}
    record(
        "candidate_payload_sha256",
        canonical_sha256(seal) == candidate.get("candidate_payload_sha256"),
        "canonical payload seal",
    )
    record(
        "source_origin",
        candidate.get("source_origin") == SOURCE_ORIGIN_GIT
        and candidate.get("dirty_worktree") is False
        and "raw_worktree_hashes" not in candidate
        and "dirty_files" not in candidate,
        "v5 requires clean git_commit provenance",
    )
    source, base = candidate.get("source_commit"), candidate.get("diff_base_commit")
    for field, commit in (("source_commit", source), ("diff_base_commit", base)):
        record(
            field,
            isinstance(commit, str)
            and re.fullmatch(r"[0-9a-f]{40}", commit) is not None
            and _git_bytes(repo, ["rev-parse", "--verify", f"{commit}^{{commit}}"]) is not None,
            f"{field} must be a full commit SHA",
        )
    record(
        "source_commit_ancestor",
        _git_bytes(repo, ["merge-base", "--is-ancestor", str(base), str(source)]) is not None,
        "diff_base must be ancestor of source_commit",
    )
    try:
        declared_manifest = candidate.get("source_manifest")
        if not isinstance(declared_manifest, dict):
            raise ValueError("source_manifest must be an object with all schema 5 fields")
        kind = declared_manifest.get("kind")
        if not isinstance(kind, str):
            raise ValueError("source_manifest.kind must be full or delta")
        expected_parent = _parent_integrity(archive_root, candidate.get("parent"), strict=True)
        record(
            "parent_integrity_matches",
            candidate.get("parent_integrity") == expected_parent,
            "parent declaration must equal recomputed strict integrity record",
        )
        manifest, archived, resolved = derive_v5_manifest(
            archive_root,
            repo,
            parent=candidate.get("parent"),
            kind=kind,
            diff_base=str(base),
            source_commit=str(source),
        )
        actual_manifest = candidate.get("source_manifest")
        record(
            "source_manifest_complete",
            actual_manifest == manifest,
            "all v5 manifest fields must exactly equal git/parent derivation",
        )
        record(
            "resolved_source_files",
            candidate.get("resolved_source_files") == len(resolved),
            "resolved_source_files must equal complete resolved source count",
        )
        hashes = candidate.get("source_hashes", {})
        record(
            "source_manifest_exact_set",
            set(hashes) == archived,
            f"missing={sorted(archived - set(hashes))} extra={sorted(set(hashes) - archived)}",
        )
        for field in ("changed_files", "deleted_files", "scope_excluded_changed_files"):
            record(
                field,
                isinstance(actual_manifest, dict) and actual_manifest.get(field) == manifest[field],
                f"{field} must equal git diff -M",
            )
        blob_hashes = {
            p: _lf_blob_sha256(_git_bytes(repo, ["cat-file", "blob", f"{source}:{p}"]))
            for p in archived
        }
        record(
            "source_commit_blob",
            hashes == blob_hashes and all(blob_hashes.values()),
            "source_hashes must equal archived source_commit LF blob hashes",
        )
        snapshot = read_json(folder / "snapshot.json")
        prompts = dict(_inherit_parent_snapshot(archive_root, candidate.get("parent"))["prompts"])
        prompts.update(
            {role: text for role, text in _extract_prompts(sources or {}).items() if text}
        )
        record(
            "snapshot_source_binding",
            snapshot.get("prompts") == prompts
            and all(
                snapshot.get(field) == candidate.get(field)
                for field in ("version", "parent", "status")
            ),
            "snapshot identity/status and per-role prompts must match source and inheritance",
        )
        paths = resolved | set(manifest["deleted_files"])
        expected_base = {
            p: _lf_blob_sha256(_git_bytes(repo, ["cat-file", "blob", f"{base}:{p}"]))
            for p in sorted(paths)
        }
        record(
            "base_commit_blob",
            candidate.get("base_hashes") == expected_base,
            "base_hashes bind complete resolved tree plus tombstones",
        )
        diff = (folder / "candidate.diff").read_bytes()
        record(
            "diff_sha256", _sha256_bytes(diff) == candidate.get("diff_sha256"), "diff bytes hash"
        )
        verify_v5_roundtrip(
            repo, str(base), str(source), diff, resolved, set(manifest["deleted_files"])
        )
        record("diff_forward_apply", True, "complete source tree and tombstones reproduced")
        record("diff_reverse_apply", True, "complete base tree restored")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        record("v5_contract", False, str(exc))
    failures = [check for check in checks if not check["ok"]]
    return {
        "version": folder.name,
        "archive_class": "new",
        "schema_version": 5,
        "status": "failed" if failures else "ok",
        "checks": checks,
        "failures": failures,
        "ok": not failures,
        "snapshot_file_sha256": file_hash,
        "snapshot_canonical_sha256": canonical_hash,
        "normalization_note_stale": normalization_note_stale,
        "raw_drift": {},
    }


def _check_v2_reverse_apply(
    candidate: dict[str, Any],
    repo: Path,
    diff: bytes,
    record: Any,
    sources: Mapping[str, Any] | None = None,
) -> None:
    """v2 reverse apply, drift-immune when possible.

    Phase29 fix (I1/I2): when the v2 record names a resolvable ``source_commit``
    with ``source_hashes``, the diff is checked against a throwaway tree rebuilt
    from those blobs — proving the diff belongs to the claimed source state even
    after the live worktree moved on (the Phase28 live-worktree check could not
    survive later commits). Otherwise fall back to the read-only check against
    the real worktree.
    """
    source_commit = candidate.get("source_commit")
    source_hashes = candidate.get("source_hashes")
    if (
        isinstance(source_commit, str)
        and source_commit
        and isinstance(source_hashes, dict)
        and source_hashes
    ):
        blobs: dict[str, bytes | None] = {}
        complete = True
        for path in sorted(source_hashes):
            blob = _git_bytes(repo, ["cat-file", "blob", f"{source_commit}:{path}"])
            blobs[path] = blob
            if blob is None:
                complete = False
        if complete:
            try:
                reverse_dir = _apply_patch_in_temp(blobs, diff, reverse=True)
            except Exception as exc:
                record(
                    "diff_reverse_apply",
                    False,
                    f"reverse apply against source_commit blobs failed: {exc}",
                )
                return
            shutil.rmtree(reverse_dir, ignore_errors=True)
            record(
                "diff_reverse_apply",
                True,
                f"candidate.diff reverse-applies against source_commit "
                f"{source_commit[:12]} blob tree ({len(blobs)} paths)",
            )
            return
    if sources:
        archived: dict[str, bytes | None] = {
            path: text.encode("utf-8") for path, text in sources.items() if isinstance(text, str)
        }
        base = {
            path: _git_bytes(
                repo, ["cat-file", "blob", f"{candidate.get('diff_base_commit')}:{path}"]
            )
            for path in archived
        }
        _check_temp_reverse_apply(archived, base, diff, record)
    else:
        record(
            "diff_reverse_apply",
            False,
            "archived source bytes unavailable; no live worktree fallback",
        )


def _verify_v2_archive(
    folder: Path,
    candidate: dict[str, Any],
    *,
    archive_root: Path,
    repo: Path,
    ignore_raw_drift: bool,
) -> dict[str, Any]:
    """Recompute every integrity claim a schema-v2 candidate.json makes (Phase28)."""
    version = str(candidate.get("version", ""))
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append(_pass(name, detail) if ok else _failure(name, detail))

    _check_declared_metadata(candidate, folder, record)
    file_hash, canonical_hash = _check_snapshot_hashes(folder, candidate, record)
    sources = _check_sources_json(folder, candidate, record)

    diff_path = folder / "candidate.diff"
    if not diff_path.is_file():
        record("diff_sha256", False, "candidate.diff is missing")
        record("diff_reverse_apply", False, "candidate.diff is missing")
    else:
        diff = diff_path.read_bytes()
        declared_diff = candidate.get("diff_sha256")
        record(
            "diff_sha256",
            isinstance(declared_diff, str) and declared_diff == _sha256_bytes(diff),
            f"declared={declared_diff!r} actual={_sha256_bytes(diff)}",
        )
        if not diff.strip():
            record("diff_reverse_apply", True, "empty diff is trivially reversible")
        else:
            _check_v2_reverse_apply(candidate, repo, diff, record, sources)

    raw_drift = _check_raw_worktree_hashes(candidate, repo, ignore_raw_drift, record)
    _check_parent_integrity(candidate, archive_root, record)
    _check_not_active(version, archive_root, record)

    failures = [check for check in checks if not check["ok"]]
    legacy_reasons = []
    if candidate.get("schema_version") == 3:
        legacy_reasons.append("schema<4")
        snapshot = read_json(folder / "snapshot.json")
        if any(not snapshot.get("prompts", {}).get(role) for role in DEFAULT_REQUIRED_ROLES):
            legacy_reasons.append("empty_role_prompts")
    return {
        "status": "legacy_non_strict" if legacy_reasons else ("ok" if not failures else "failed"),
        "reasons": legacy_reasons,
        "legacy_worktree_drift": raw_drift if ignore_raw_drift else {},
        "version": version or folder.name,
        "archive_class": "new",
        "schema_version": candidate.get("schema_version"),
        "snapshot_file_sha256": file_hash,
        "snapshot_canonical_sha256": canonical_hash,
        "checks": checks,
        "failures": failures,
        "raw_drift": raw_drift,
        "raw_drift_ignored": bool(raw_drift) and ignore_raw_drift,
        "ok": not failures,
    }


def _lf_blob_sha256(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        return _sha256_bytes(lf_text(raw).encode("utf-8"))
    except UnicodeDecodeError:
        return None


def _check_source_manifest(candidate: dict[str, Any], archive_root: Path, record: Any) -> None:
    """Schema-v4 source manifest rules (kind, parent requirement, full coverage)."""
    manifest = candidate.get("source_manifest")
    parent = candidate.get("parent")
    if not isinstance(manifest, dict):
        record("source_manifest_declared", False, f"source_manifest missing: {manifest!r}")
        return
    kind = manifest.get("kind")
    problems: list[str] = []
    if kind not in MANIFEST_KINDS:
        problems.append(f"kind must be one of {list(MANIFEST_KINDS)}, got {kind!r}")
    if kind == "delta" and not parent:
        problems.append("delta manifest requires a parent candidate")
    roles = manifest.get("required_roles")
    if not isinstance(roles, list) or not roles:
        problems.append(f"required_roles must be a non-empty list, got {roles!r}")
    elif any(role not in DEFAULT_REQUIRED_ROLES for role in roles):
        problems.append(
            f"unknown required_roles: {sorted(set(roles) - set(DEFAULT_REQUIRED_ROLES))}"
        )
    record(
        "source_manifest_declared",
        not problems,
        "; ".join(problems) if problems else f"kind={kind!r} parent={parent!r} roles={roles!r}",
    )
    if problems or kind != "full" or not parent:
        if kind == "full" and not parent and not problems:
            record(
                "source_manifest_full_coverage",
                True,
                "root full candidate without parent; nothing to inherit",
            )
        return
    source_hashes = candidate.get("source_hashes")
    keys = set(source_hashes) if isinstance(source_hashes, dict) else set()
    inherited_problems: list[str] = []
    try:
        inherited = _candidate_source_set(archive_root, str(parent))
    except Exception as exc:  # ArchiveError from a broken ancestor chain
        inherited_problems.append(f"ancestor walk failed: {exc}")
        inherited = set()
    missing = sorted(inherited - keys)
    if missing:
        inherited_problems.append(
            f"full source set misses {len(missing)} inherited files: {missing[:5]}"
        )
    for role in roles if isinstance(roles, list) else []:
        if not any(
            PurePosixPath(str(key).replace("\\", "/")).parts[-2:] == ("agents", f"{role}.py")
            for key in keys
        ):
            inherited_problems.append(f"full source set is missing agents/{role}.py")
    record(
        "source_manifest_full_coverage",
        not inherited_problems,
        "; ".join(inherited_problems)
        if inherited_problems
        else f"covers all {len(inherited)} inherited files and required role sources",
    )


def _check_snapshot_strict_roles(folder: Path, record: Any) -> None:
    """Schema-v4 activation gate: the archived snapshot must be activatable."""
    snapshot_path = folder / "snapshot.json"
    if not snapshot_path.is_file():
        record("snapshot_strict_roles", False, "snapshot.json is missing")
        return
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        record("snapshot_strict_roles", False, f"snapshot.json unreadable: {exc}")
        return
    try:
        validate_snapshot(snapshot, None, strict=True)
    except ValueError as exc:
        record("snapshot_strict_roles", False, f"snapshot not activatable: {exc}")
        return
    record("snapshot_strict_roles", True, "coder/planner/reviewer prompts present and non-empty")


def _verify_v3_archive(
    folder: Path,
    candidate: dict[str, Any],
    *,
    archive_root: Path,
    repo: Path,
    ignore_raw_drift: bool,
) -> dict[str, Any]:
    """Recompute every schema-v3 claim: git source binding plus payload seal."""
    version = str(candidate.get("version", ""))
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append(_pass(name, detail) if ok else _failure(name, detail))

    _check_declared_metadata(candidate, folder, record)
    file_hash, canonical_hash = _check_snapshot_hashes(folder, candidate, record)
    normalization_note_stale = _check_normalization(candidate, record)

    # 1. Payload seal: canonical JSON over everything except the seal itself.
    declared_seal = candidate.get("candidate_payload_sha256")
    sealed = {k: v for k, v in candidate.items() if k != "candidate_payload_sha256"}
    recomputed_seal = canonical_sha256(sealed)
    record(
        "candidate_payload_sha256",
        isinstance(declared_seal, str) and declared_seal == recomputed_seal,
        f"declared={declared_seal!r} recomputed={recomputed_seal}",
    )

    # 2. source_origin declaration and its cross-field consequences.
    origin = candidate.get("source_origin")
    source_commit_value = candidate.get("source_commit")
    origin_detail = f"source_origin={origin!r}"
    origin_ok = origin in (SOURCE_ORIGIN_GIT, SOURCE_ORIGIN_WORKTREE)
    if not origin_ok:
        origin_detail += "; must be 'git_commit' or 'worktree'"
    elif origin == SOURCE_ORIGIN_GIT:
        if not (isinstance(source_commit_value, str) and source_commit_value):
            origin_ok = False
            origin_detail += "; git_commit mode requires a source_commit"
        elif "raw_worktree_hashes" in candidate:
            origin_ok = False
            origin_detail += "; git_commit mode must not carry raw_worktree_hashes"
    else:
        if source_commit_value is not None:
            origin_ok = False
            origin_detail += "; worktree mode must not claim a source_commit"
    record("source_origin_declared", origin_ok, origin_detail)

    dirty = candidate.get("dirty_worktree")
    if origin == SOURCE_ORIGIN_GIT:
        dirty_ok = dirty is False and "dirty_files" not in candidate
        dirty_detail = f"dirty_worktree={dirty!r} for git_commit mode"
    else:
        dirty_ok = isinstance(dirty, bool) and (dirty is True or "dirty_files" not in candidate)
        dirty_detail = (
            f"dirty_worktree={dirty!r} with "
            f"dirty_files={'present' if 'dirty_files' in candidate else 'absent'}"
        )
    record("origin_dirty_consistency", dirty_ok, dirty_detail)

    diff_base_commit = candidate.get("diff_base_commit")

    # 3. Commit resolution and ancestry (git_commit mode only).
    if origin == SOURCE_ORIGIN_GIT:
        resolves = False
        if isinstance(source_commit_value, str) and source_commit_value:
            resolves = (
                _git_bytes(repo, ["rev-parse", "--verify", f"{source_commit_value}^{{commit}}"])
                is not None
            )
        record(
            "source_commit_resolves",
            resolves,
            f"source_commit={source_commit_value!r} "
            + ("resolves to a commit" if resolves else "does not resolve"),
        )
    ancestor_ok = False
    if (
        isinstance(diff_base_commit, str)
        and diff_base_commit
        and isinstance(source_commit_value, str)
        and source_commit_value
    ):
        ancestor_ok = (
            _git_bytes(
                repo,
                ["merge-base", "--is-ancestor", diff_base_commit, source_commit_value],
            )
            is not None
        )
        record(
            "source_commit_ancestor",
            ancestor_ok,
            f"merge-base --is-ancestor {diff_base_commit} {source_commit_value} "
            + ("holds" if ancestor_ok else "does not hold"),
        )

    # 4. Per-file blob bindings.
    sources_payload = _check_sources_json(folder, candidate, record)
    source_hashes = candidate.get("source_hashes")
    hashes: dict[str, Any] = source_hashes if isinstance(source_hashes, dict) else {}
    paths = sorted(hashes)

    if origin == SOURCE_ORIGIN_GIT:
        blob_problems: list[str] = []
        for path in paths:
            declared_hash = hashes.get(path)
            blob = _git_bytes(repo, ["cat-file", "blob", f"{source_commit_value}:{path}"])
            actual = _lf_blob_sha256(blob)
            if blob is None:
                blob_problems.append(f"{path} absent from source_commit")
            elif not isinstance(declared_hash, str) or declared_hash != actual:
                blob_problems.append(f"{path} LF hash differs from source_commit blob")
        record(
            "source_commit_blob",
            not blob_problems,
            "; ".join(blob_problems[:5])
            if blob_problems
            else f"{len(paths)} paths match source_commit blobs",
        )

    base_hashes = candidate.get("base_hashes")
    base_map: dict[str, Any] = base_hashes if isinstance(base_hashes, dict) else {}
    base_problems: list[str] = []
    base_blobs: dict[str, bytes | None] = {}
    if not base_map:
        base_problems.append("base_hashes missing or empty")
    else:
        if set(base_map) != set(hashes):
            base_problems.append(
                f"key set differs from source_hashes: only_base={sorted(set(base_map) - set(hashes))[:5]} "
                f"only_source={sorted(set(hashes) - set(base_map))[:5]}"
            )
        for path in paths:
            declared_base = base_map.get(path)
            blob = _git_bytes(repo, ["cat-file", "blob", f"{diff_base_commit}:{path}"])
            base_blobs[path] = blob
            actual_base = _lf_blob_sha256(blob)
            if declared_base is None:
                if blob is not None:
                    base_problems.append(f"{path} declared absent from diff_base but blob exists")
            elif blob is None:
                base_problems.append(f"{path} absent from diff_base_commit")
            elif declared_base != actual_base:
                base_problems.append(f"{path} LF hash differs from diff_base blob")
    record(
        "base_commit_blob",
        not base_problems,
        "; ".join(base_problems[:5]) if base_problems else "base_hashes match diff_base blobs",
    )

    # 5. Diff round-trip: forward apply against base blobs must reproduce the
    # archived sources; reverse apply must reproduce the base.
    diff_path = folder / "candidate.diff"
    if not diff_path.is_file():
        record("diff_sha256", False, "candidate.diff is missing")
        record("diff_forward_apply", False, "candidate.diff is missing")
        record("diff_reverse_apply", False, "candidate.diff is missing")
    else:
        diff = diff_path.read_bytes()
        declared_diff = candidate.get("diff_sha256")
        record(
            "diff_sha256",
            isinstance(declared_diff, str) and declared_diff == _sha256_bytes(diff),
            f"declared={declared_diff!r} actual={_sha256_bytes(diff)}",
        )
        if not diff.strip():
            record("diff_forward_apply", True, "empty diff is trivially consistent")
            record("diff_reverse_apply", True, "empty diff is trivially reversible")
        else:
            forward_dir: Path | None = None
            try:
                forward_dir = _apply_patch_in_temp(base_blobs, diff, reverse=False)
                forward_problems: list[str] = []
                text_by_path = {
                    path: text
                    for path, text in (sources_payload or {}).items()
                    if isinstance(text, str)
                }
                for path in paths:
                    expected = text_by_path.get(path)
                    target = forward_dir.joinpath(*PurePosixPath(path).parts)
                    if expected is None:
                        forward_problems.append(f"{path} has no sources.json text to compare")
                    elif not target.is_file():
                        forward_problems.append(f"{path} not produced by forward apply")
                    elif lf_text(target.read_bytes()) != expected:
                        forward_problems.append(f"{path} differs from sources.json after apply")
                record(
                    "diff_forward_apply",
                    not forward_problems,
                    "; ".join(forward_problems[:5])
                    if forward_problems
                    else "base blobs + diff reproduce sources.json",
                )
            except Exception as exc:
                record("diff_forward_apply", False, f"forward apply failed: {exc}")
            finally:
                if forward_dir is not None:
                    shutil.rmtree(forward_dir, ignore_errors=True)

            if origin == SOURCE_ORIGIN_GIT:
                source_blobs = {
                    path: _git_bytes(repo, ["cat-file", "blob", f"{source_commit_value}:{path}"])
                    for path in paths
                }
                _check_temp_reverse_apply(source_blobs, base_blobs, diff, record)
            else:
                archived_blobs: dict[str, bytes | None] = {
                    path: text.encode("utf-8")
                    for path, text in (sources_payload or {}).items()
                    if isinstance(text, str)
                }
                _check_temp_reverse_apply(archived_blobs, base_blobs, diff, record)

    # 6. Worktree raw hashes are worktree-mode provenance only.
    raw_drift: dict[str, str] = {}
    if origin == SOURCE_ORIGIN_WORKTREE:
        raw_drift = _check_raw_worktree_hashes(candidate, repo, ignore_raw_drift, record)

    if candidate.get("schema_version") == 4:
        _check_source_manifest(candidate, archive_root, record)
        _check_snapshot_strict_roles(folder, record)

    _check_parent_integrity(candidate, archive_root, record)
    _check_not_active(version, archive_root, record)

    failures = [check for check in checks if not check["ok"]]
    legacy_reasons = []
    if candidate.get("schema_version") == 3:
        legacy_reasons.append("schema<4")
        snapshot = read_json(folder / "snapshot.json")
        if any(not snapshot.get("prompts", {}).get(role) for role in DEFAULT_REQUIRED_ROLES):
            legacy_reasons.append("empty_role_prompts")
    return {
        "status": "legacy_non_strict" if legacy_reasons else ("ok" if not failures else "failed"),
        "reasons": legacy_reasons,
        "legacy_worktree_drift": raw_drift if ignore_raw_drift else {},
        "version": version or folder.name,
        "archive_class": "new",
        "schema_version": candidate.get("schema_version"),
        "source_origin": origin,
        "snapshot_file_sha256": file_hash,
        "snapshot_canonical_sha256": canonical_hash,
        "normalization_note_stale": normalization_note_stale,
        "checks": checks,
        "failures": failures,
        "raw_drift": raw_drift,
        "raw_drift_ignored": bool(raw_drift) and ignore_raw_drift,
        "ok": not failures,
    }


def _declared_snapshot_hash(candidate: dict[str, Any]) -> tuple[str | None, str | None]:
    """First declared snapshot hash string; value comparison decides the rule."""
    for field in ("snapshot_sha256", "snapshot_file_sha256"):
        value = candidate.get(field)
        if isinstance(value, str) and value:
            return value, field
    return None, None


def verify_old_archive(folder: Path, candidate: dict[str, Any], *, repo: Path) -> dict[str, Any]:
    """Infer and annotate the undeclared snapshot hash rule; never modify anything."""
    version = str(candidate.get("version", folder.name))
    notes: list[str] = []
    snapshot_path = folder / "snapshot.json"
    file_hash: str | None = None
    canonical_hash: str | None = None
    if not snapshot_path.is_file():
        notes.append("snapshot.json is missing")
    else:
        raw = snapshot_path.read_bytes()
        file_hash = _sha256_bytes(raw)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            notes.append(f"snapshot.json is not valid JSON: {exc}")
        else:
            canonical_hash = canonical_sha256(payload) if isinstance(payload, dict) else None
            if canonical_hash is None:
                notes.append("snapshot.json is not a JSON object")

    declared, declared_field = _declared_snapshot_hash(candidate)
    if declared is None:
        inferred_rule: str | None = None
        unexplained = False
        notes.append("no declared snapshot hash in candidate.json; rule not inferable")
    elif file_hash is not None and declared == file_hash:
        inferred_rule, unexplained = FILE_BYTES_RULE, False
    elif canonical_hash is not None and declared == canonical_hash:
        inferred_rule, unexplained = CANONICAL_RULE, False
    else:
        inferred_rule, unexplained = None, True

    blob_sha1 = _git_bytes(repo, ["hash-object", str(snapshot_path)])
    blob_sha1_hex = blob_sha1.decode("ascii", errors="replace").strip() if blob_sha1 else None
    if blob_sha1_hex is None:
        notes.append("git hash-object unavailable; local blob sha1 skipped")

    baseline_commit: str | None = None
    baseline_blob_sha256: str | None = None
    baseline_matches: bool | None = None
    relative = _safe_repo_relative(repo, snapshot_path)
    if relative is None:
        notes.append("archive root outside repository; git baseline blob comparison skipped")
    else:
        posix = relative.as_posix()
        log = _git_bytes(repo, ["log", "-1", "--format=%H", "--", posix])
        if log:
            baseline_commit = log.decode("ascii", errors="replace").strip() or None
        if baseline_commit is None:
            notes.append(f"no commit touches {posix}; baseline blob comparison skipped")
        else:
            blob = _git_bytes(repo, ["cat-file", "blob", f"{baseline_commit}:{posix}"])
            if blob is None:
                notes.append(f"git cat-file failed for {baseline_commit}:{posix}")
            else:
                baseline_blob_sha256 = _sha256_bytes(blob)
                baseline_matches = baseline_blob_sha256 == file_hash

    return {
        "version": version,
        "archive_class": "old",
        "schema_version": candidate.get("schema_version"),
        "declared": declared,
        "declared_field": declared_field,
        "snapshot_file_sha256": file_hash,
        "snapshot_canonical_sha256": canonical_hash,
        "declared_matches_file_bytes": declared is not None and declared == file_hash,
        "declared_matches_canonical": declared is not None and declared == canonical_hash,
        "inferred_rule": inferred_rule,
        "unexplained_mismatch": unexplained,
        "git_blob_sha1": blob_sha1_hex,
        "baseline_blob_commit": baseline_commit,
        "baseline_git_blob_sha256": baseline_blob_sha256,
        "baseline_blob_matches_file": baseline_matches,
        "notes": notes,
        "ok": not unexplained,
    }


def _unknown_archive(folder: Path, reason: str) -> dict[str, Any]:
    return {
        "version": folder.name,
        "archive_class": "unknown",
        "schema_version": None,
        "declared": None,
        "declared_field": None,
        "snapshot_file_sha256": None,
        "snapshot_canonical_sha256": None,
        "inferred_rule": None,
        "unexplained_mismatch": False,
        "notes": [reason],
        "ok": False,
    }


def tracked_changes_under(repo: Path, directory: Path) -> list[str] | None:
    """Non-untracked ``git status --porcelain`` lines under ``directory``; None if git fails."""
    relative = _safe_repo_relative(repo, directory)
    if relative is None:
        return None
    output = _git_bytes(
        repo, ["status", "--porcelain", "--untracked-files=normal", "--", relative.as_posix()]
    )
    if output is None:
        return None
    lines = output.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip() and not line.startswith("??")]


def verify_historical_baseline(archive_root: Path) -> dict[str, Any]:
    """Compare every file in historical-baseline.json against the current bytes.

    The manifest (produced by ``scripts/phase29_baseline_manifest.py``) records
    the SHA256 of each historical candidate file at freeze time; this check
    detects any later modification, including modifications that were committed
    (which a ``git log -1`` heuristic cannot see). A missing manifest is
    annotated, not failed: absence means "cannot prove", not "proven modified".
    """
    manifest_path = archive_root / "candidates" / HISTORICAL_MANIFEST_NAME
    result: dict[str, Any] = {
        "check": "historical_baseline",
        "manifest_path": str(manifest_path),
        "manifest_present": manifest_path.is_file(),
        "baseline_commit": None,
        "files_checked": 0,
        "mismatches": {},
        "notes": [],
        "ok": None,
    }
    if not manifest_path.is_file():
        result["notes"].append(
            "historical baseline manifest is absent; historical immutability is not proven here"
        )
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest is not a JSON object")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        result["ok"] = False
        result["notes"].append(f"manifest unreadable: {exc}")
        return result
    result["baseline_commit"] = manifest.get("baseline_commit")
    files = manifest.get("files")
    if not isinstance(files, list):
        result["ok"] = False
        result["notes"].append("manifest has no files list")
        return result
    for entry in files:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str) or not expected:
            continue
        result["files_checked"] += 1
        target = archive_root / rel
        if not target.is_file():
            result["mismatches"][rel] = {"expected": expected, "actual": "missing"}
            continue
        actual = _sha256_bytes(target.read_bytes())
        if actual != expected:
            result["mismatches"][rel] = {"expected": expected, "actual": actual}
    result["ok"] = not result["mismatches"]
    return result


def verify_receipt(
    receipt_path: Path,
    *,
    archive_root: Path,
    repo: Path,
    expect_receipt: str | None,
) -> dict[str, Any]:
    """Verify an external candidate receipt (Phase30 T04).

    The receipt lives outside the candidate directory and binds the archived
    bytes to a git commit: ``candidate_commit`` must be an ancestor of HEAD,
    the four candidate files must match the receipt both as blobs at that
    commit and as the current on-disk bytes, the tree hash must reproduce, the
    diff must round-trip between ``diff_base`` and ``source_commit``, the
    snapshot must pass strict validation, the status must be ``pending`` with
    no active pointer, and the historical manifest blob at ``candidate_commit``
    must match ``historical_manifest_sha256``. ``expect_receipt`` is the
    external witness: the SHA-256 of the receipt file itself, held outside the
    repository.
    """
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append(_pass(name, detail) if ok else _failure(name, detail))

    result: dict[str, Any] = {
        "check": "receipt",
        "receipt_path": str(receipt_path),
        "checks": checks,
        "failures": [],
        "witness": "none",
        "ok": False,
    }
    if not receipt_path.is_file():
        record("receipt_schema", False, f"receipt file is missing: {receipt_path}")
        result["failures"] = [c for c in checks if not c["ok"]]
        return result
    raw = receipt_path.read_bytes()
    actual_receipt_sha = _sha256_bytes(raw)
    result["receipt_sha256"] = actual_receipt_sha
    if expect_receipt is None:
        result["witness"] = "none"
        record("receipt_witness", True, "no --expect-receipt provided; witness=none")
    elif actual_receipt_sha == expect_receipt:
        result["witness"] = "verified"
        record("receipt_witness", True, "receipt sha256 matches the external witness")
    else:
        result["witness"] = "mismatch"
        record(
            "receipt_witness",
            False,
            f"receipt sha256 {actual_receipt_sha} does not match the external witness "
            f"{expect_receipt}",
        )
    try:
        receipt = json.loads(raw.decode("utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("receipt is not a JSON object")
    except (UnicodeDecodeError, ValueError) as exc:
        record("receipt_schema", False, f"receipt unreadable: {exc}")
        result["failures"] = [c for c in checks if not c["ok"]]
        return result

    required_fields = (
        "schema_version",
        "candidate",
        "candidate_commit",
        "source_commit",
        "diff_base",
        "candidate_files",
        "candidate_tree_sha256",
        "historical_manifest_sha256",
        "created_at",
    )
    schema_problems = [field for field in required_fields if field not in receipt]
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        schema_problems.append(f"schema_version must be {RECEIPT_SCHEMA_VERSION}")
    record(
        "receipt_schema",
        not schema_problems,
        "; ".join(schema_problems) if schema_problems else "schema and required fields present",
    )
    version = receipt.get("candidate")
    if not isinstance(version, str) or not version:
        result["failures"] = [c for c in checks if not c["ok"]]
        result["ok"] = False
        return result
    folder = archive_root / "candidates" / version
    result["candidate"] = version

    def blob(commit: str, rel: str) -> bytes | None:
        return _git_bytes(repo, ["cat-file", "blob", f"{commit}:{rel}"])

    # Repo-relative prefix of the candidates directory: the real archive lives
    # at <repo>/configs/harness/candidates, test archives at any depth.
    try:
        candidates_prefix = (
            (archive_root / "candidates").resolve().relative_to(repo.resolve()).as_posix()
        )
    except ValueError:
        candidates_prefix = None
    if candidates_prefix is None:
        record(
            "receipt_commit_ancestor",
            False,
            f"archive root {archive_root} is outside repo {repo}; git blob checks unavailable",
        )
        result["failures"] = [c for c in checks if not c["ok"]]
        result["ok"] = False
        return result

    candidate_commit = receipt.get("candidate_commit")
    source_commit = receipt.get("source_commit")
    diff_base = receipt.get("diff_base")

    # 1. candidate_commit is an ancestor of HEAD (git-anchored, not floating).
    head = _git_bytes(repo, ["rev-parse", "HEAD"])
    head_sha = head.decode("ascii", errors="replace").strip() if head else None
    ancestor_ok = bool(
        candidate_commit
        and head_sha
        and _git_bytes(repo, ["merge-base", "--is-ancestor", str(candidate_commit), head_sha])
        is not None
    )
    record(
        "receipt_commit_ancestor",
        ancestor_ok,
        f"merge-base --is-ancestor {candidate_commit} HEAD({head_sha}) "
        + ("holds" if ancestor_ok else "does not hold"),
    )

    # 2. The four candidate files: blobs at candidate_commit and current bytes
    # must both match the receipt's per-file hashes.
    declared_files = receipt.get("candidate_files")
    declared_map: dict[str, Any] = declared_files if isinstance(declared_files, dict) else {}
    file_problems: list[str] = []
    if set(declared_map) != set(RECEIPT_FILE_NAMES):
        file_problems.append("candidate_files must contain exactly the four candidate files")
    blob_bytes: dict[str, bytes | None] = {}
    for name in RECEIPT_FILE_NAMES:
        declared = declared_map.get(name)
        current = folder / name
        raw_blob = blob(str(candidate_commit), f"{candidates_prefix}/{version}/{name}")
        blob_bytes[name] = raw_blob
        if not isinstance(declared, str):
            file_problems.append(f"{name}: receipt has no sha256")
            continue
        if raw_blob is None:
            file_problems.append(f"{name}: absent from candidate_commit {candidate_commit}")
            continue
        if _sha256_bytes(raw_blob) != declared:
            file_problems.append(f"{name}: blob at candidate_commit differs from receipt")
        if not current.is_file() or _sha256_bytes(current.read_bytes()) != declared:
            file_problems.append(f"{name}: current file differs from receipt")
    record(
        "receipt_candidate_files",
        not file_problems,
        "; ".join(file_problems[:5]) if file_problems else "4 files match receipt at blob and disk",
    )

    # 3. Tree hash: recomputed from the receipt values and from current disk.
    tree_self = candidate_tree_sha256({k: str(v) for k, v in declared_map.items()})
    record(
        "receipt_candidate_tree_self",
        tree_self == receipt.get("candidate_tree_sha256"),
        f"declared={receipt.get('candidate_tree_sha256')!r} recomputed_from_receipt={tree_self}",
    )
    current_hashes = {
        name: _sha256_bytes((folder / name).read_bytes())
        for name in RECEIPT_FILE_NAMES
        if (folder / name).is_file()
    }
    tree_current = candidate_tree_sha256(current_hashes) if len(current_hashes) == 4 else None
    record(
        "receipt_candidate_tree_current",
        tree_current is not None and tree_current == receipt.get("candidate_tree_sha256"),
        f"declared={receipt.get('candidate_tree_sha256')!r} recomputed_from_disk={tree_current}",
    )

    # 4. Commit ancestry: diff_base must precede source_commit, both resolve.
    ancestry_ok = bool(
        source_commit
        and diff_base
        and _git_bytes(repo, ["rev-parse", "--verify", f"{source_commit}^{{commit}}"]) is not None
        and _git_bytes(repo, ["rev-parse", "--verify", f"{diff_base}^{{commit}}"]) is not None
        and _git_bytes(repo, ["merge-base", "--is-ancestor", str(diff_base), str(source_commit)])
        is not None
    )
    record(
        "receipt_source_ancestry",
        ancestry_ok,
        f"diff_base={diff_base} ancestor-of source_commit={source_commit}: "
        + ("holds" if ancestry_ok else "does not hold"),
    )

    # 5. Candidate.json blob: parse and bind source hashes to source_commit blobs.
    candidate_raw = blob_bytes.get("candidate.json")
    candidate_record: dict[str, Any] | None = None
    if candidate_raw is None:
        record("receipt_candidate_record", False, "candidate.json blob unavailable")
    else:
        try:
            parsed = json.loads(candidate_raw.decode("utf-8"))
            candidate_record = parsed if isinstance(parsed, dict) else None
        except (UnicodeDecodeError, ValueError) as exc:
            record("receipt_candidate_record", False, f"candidate.json blob unreadable: {exc}")
        else:
            record(
                "receipt_candidate_record",
                candidate_record is not None,
                f"version={candidate_record.get('version')!r} from candidate_commit blob"
                if candidate_record is not None
                else "candidate.json blob must be an object",
            )
    if candidate_record is not None:
        for receipt_field, candidate_field in (
            ("candidate", "version"),
            ("source_commit", "source_commit"),
            ("diff_base", "diff_base_commit"),
        ):
            record(
                f"receipt_cross_{receipt_field}",
                receipt.get(receipt_field) == candidate_record.get(candidate_field),
                f"receipt {receipt_field} must equal candidate.json {candidate_field}",
            )
        record(
            "receipt_source_candidate_ancestry",
            _git_bytes(
                repo, ["merge-base", "--is-ancestor", str(source_commit), str(candidate_commit)]
            )
            is not None,
            "source_commit must be ancestor of candidate_commit",
        )
        if candidate_record.get("schema_version") == 5:
            record(
                "receipt_declared_status",
                receipt.get("status") == "pending",
                "receipt.status must be pending",
            )
            verified = _verify_v5_archive(
                folder, candidate_record, archive_root=archive_root, repo=repo
            )
            record("receipt_v5_candidate", verified["ok"], str(verified["failures"]))
        declared_hashes = candidate_record.get("source_hashes")
        hashes: dict[str, Any] = declared_hashes if isinstance(declared_hashes, dict) else {}
        hash_problems: list[str] = []
        if not hashes:
            hash_problems.append("candidate.json has no source_hashes")
        for path, declared in sorted(hashes.items()):
            actual = _lf_blob_sha256(blob(str(source_commit), str(path)))
            if actual is None:
                hash_problems.append(f"{path}: absent from source_commit")
            elif actual != declared:
                hash_problems.append(f"{path}: LF blob hash differs from source_commit")
        record(
            "receipt_source_hashes_blobs",
            not hash_problems,
            "; ".join(hash_problems[:5])
            if hash_problems
            else f"{len(hashes)} paths match source_commit blobs",
        )

        status = candidate_record.get("status")
        record(
            "receipt_status_pending",
            status == "pending",
            f"candidate.json status={status!r} (must be 'pending')",
        )
        parent = candidate_record.get("parent")
        parent_dir = (archive_root / "candidates" / str(parent)) if parent else None
        record(
            "receipt_parent_present",
            bool(parent) and parent_dir is not None and parent_dir.is_dir(),
            f"parent={parent!r} directory "
            + ("present" if parent_dir is not None and parent_dir.is_dir() else "missing"),
        )
    else:
        record("receipt_source_hashes_blobs", False, "candidate.json blob unavailable")
        record("receipt_status_pending", False, "candidate.json blob unavailable")
        record("receipt_parent_present", False, "candidate.json blob unavailable")

    # 6. Diff round-trip between diff_base and source_commit.
    diff_raw = blob_bytes.get("candidate.diff")
    if candidate_record is None or diff_raw is None:
        record("receipt_diff_roundtrip", False, "candidate.diff or candidate.json unavailable")
    elif candidate_record.get("schema_version") == 5:
        try:
            _, _, resolved = derive_v5_manifest(
                archive_root,
                repo,
                parent=candidate_record.get("parent"),
                kind=candidate_record.get("source_manifest", {}).get("kind"),
                diff_base=str(diff_base),
                source_commit=str(source_commit),
            )
            verify_v5_roundtrip(
                repo,
                str(diff_base),
                str(source_commit),
                diff_raw,
                resolved,
                set(candidate_record["source_manifest"]["deleted_files"]),
            )
        except (ValueError, OSError) as exc:
            record("receipt_diff_roundtrip", False, str(exc))
        else:
            record("receipt_diff_roundtrip", True, "complete resolved trees round-trip")
    elif not diff_raw.strip():
        record("receipt_diff_roundtrip", True, "empty diff is trivially consistent")
    else:
        roundtrip_hashes = candidate_record.get("source_hashes")
        paths = sorted(roundtrip_hashes) if isinstance(roundtrip_hashes, dict) else []
        base_blobs = {path: blob(str(diff_base), path) for path in paths}
        source_blobs = {path: blob(str(source_commit), path) for path in paths}
        try:
            forward_dir = _apply_patch_in_temp(base_blobs, diff_raw, reverse=False)
            try:
                forward_problems = [
                    path
                    for path in paths
                    if not forward_dir.joinpath(*PurePosixPath(path).parts).is_file()
                ]
            finally:
                shutil.rmtree(forward_dir, ignore_errors=True)
            reverse_dir = _apply_patch_in_temp(source_blobs, diff_raw, reverse=True)
            try:
                reverse_problems: list[str] = []
                for path in paths:
                    expected_base = base_blobs.get(path)
                    target = reverse_dir.joinpath(*PurePosixPath(path).parts)
                    if expected_base is None:
                        if target.exists():
                            reverse_problems.append(f"{path} should have been deleted")
                        continue
                    if not target.is_file() or lf_text(target.read_bytes()) != lf_text(
                        expected_base
                    ):
                        reverse_problems.append(f"{path} does not match diff_base")
            finally:
                shutil.rmtree(reverse_dir, ignore_errors=True)
            record(
                "receipt_diff_roundtrip",
                not forward_problems and not reverse_problems,
                "; ".join((forward_problems + reverse_problems)[:5])
                if forward_problems or reverse_problems
                else "diff_base + diff reproduce source_commit; reverse restores diff_base",
            )
        except Exception as exc:
            record("receipt_diff_roundtrip", False, f"apply failed: {exc}")

    # 7. Snapshot blob must pass strict (activatable) validation.
    snapshot_raw = blob_bytes.get("snapshot.json")
    if snapshot_raw is None:
        record("receipt_snapshot_strict", False, "snapshot.json blob unavailable")
    else:
        try:
            snapshot = json.loads(snapshot_raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            record("receipt_snapshot_strict", False, f"snapshot.json blob unreadable: {exc}")
        else:
            try:
                validate_snapshot(snapshot, None, strict=True)
            except ValueError as exc:
                record("receipt_snapshot_strict", False, f"snapshot not activatable: {exc}")
            else:
                record("receipt_snapshot_strict", True, "role prompts present and non-empty")

    # 8. No active pointer may reference the receipt's candidate.
    active_problems: list[str] = []
    for pointer_name in ("active.json", "active.next.json"):
        pointer = archive_root / pointer_name
        if not pointer.exists():
            continue
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            referenced = payload.get("version") if isinstance(payload, dict) else None
        except (OSError, UnicodeDecodeError, ValueError):
            active_problems.append(f"{pointer_name} exists but is unreadable")
            continue
        if referenced == version:
            active_problems.append(f"{pointer_name} references {version}")
    record(
        "receipt_not_active",
        not active_problems,
        "; ".join(active_problems) if active_problems else "no active pointer references it",
    )

    # 9. Historical manifest: the receipt pins the exact manifest bytes it
    # vouches for (the manifest committed alongside the receipt). Any later
    # change — including a "tamper the history and sync the manifest" commit —
    # breaks this pin, which is the whole point of the external witness.
    manifest_path = archive_root / "candidates" / HISTORICAL_MANIFEST_NAME
    declared_manifest = receipt.get("historical_manifest_sha256")
    is_v5 = candidate_record is not None and candidate_record.get("schema_version") == 5
    manifest_commit = receipt.get("manifest_commit")
    if is_v5 or manifest_commit is not None:
        result["manifest_binding"] = "manifest_commit"
        manifest_blob = (
            blob(str(manifest_commit), f"{candidates_prefix}/{HISTORICAL_MANIFEST_NAME}")
            if manifest_commit
            else None
        )
        record(
            "receipt_manifest_commit",
            bool(manifest_commit)
            and _git_bytes(repo, ["merge-base", "--is-ancestor", str(manifest_commit), "HEAD"])
            is not None,
            "manifest_commit required and must be HEAD ancestor",
        )
        actual_manifest = _sha256_bytes(manifest_blob) if manifest_blob is not None else None
        record(
            "receipt_manifest_blob",
            actual_manifest is not None
            and actual_manifest == receipt.get("manifest_blob_sha256") == declared_manifest,
            "manifest_blob_sha256 and historical_manifest_sha256 must equal committed blob",
        )
        record(
            "receipt_historical_manifest",
            manifest_path.is_file()
            and _sha256_bytes(manifest_path.read_bytes()) == actual_manifest,
            "current manifest must equal bound blob",
        )
    else:
        result["manifest_binding"] = "current_worktree"
        record(
            "receipt_historical_manifest",
            manifest_path.is_file()
            and _sha256_bytes(manifest_path.read_bytes()) == declared_manifest,
            "v4 compatibility: current_worktree; no committed manifest binding",
        )

    result["failures"] = [c for c in checks if not c["ok"]]
    result["ok"] = not result["failures"]
    return result


def verify_versions(
    *,
    archive_root: Path,
    repo: Path,
    versions: Sequence[str],
    ignore_raw_drift: bool,
) -> dict[str, Any]:
    """Verify the requested versions and assemble the full report (read-only)."""
    candidates_root = archive_root / "candidates"
    skipped: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    for version in versions:
        folder = candidates_root / version
        if not folder.is_dir():
            reason = (
                "candidates/<version> is not a directory"
                if folder.exists()
                else "version directory does not exist"
            )
            skipped.append({"version": version, "reason": reason})
            results.append(_unknown_archive(folder, reason))
            continue
        candidate, error = _read_candidate(folder)
        if candidate is None:
            results.append(_unknown_archive(folder, error or "candidate.json unavailable"))
            continue
        kind = classify(candidate)
        if kind == "new":
            results.append(
                verify_new_archive(
                    folder,
                    candidate,
                    archive_root=archive_root,
                    repo=repo,
                    ignore_raw_drift=ignore_raw_drift,
                )
            )
        elif kind == "old":
            results.append(verify_old_archive(folder, candidate, repo=repo))
        else:
            results.append(
                _unknown_archive(
                    folder, f"unsupported schema_version={candidate.get('schema_version')!r}"
                )
            )

    old_results = [entry for entry in results if entry["archive_class"] == "old"]
    new_results = [entry for entry in results if entry["archive_class"] == "new"]
    unknown_results = [entry for entry in results if entry["archive_class"] == "unknown"]
    unexplained_total = sum(1 for entry in results if entry.get("unexplained_mismatch"))
    new_failures = sum(len(entry.get("failures", [])) for entry in new_results)

    historical = verify_historical_baseline(archive_root)

    status_lines = tracked_changes_under(repo, candidates_root)
    blob_evidence = [entry.get("baseline_blob_matches_file") for entry in old_results]
    modified_by_status = status_lines is not None and bool(status_lines)
    modified_by_blob = any(value is False for value in blob_evidence)
    has_git_evidence = status_lines is not None or any(value is not None for value in blob_evidence)
    if modified_by_status or modified_by_blob:
        historical_files_modified: bool | None = True
    elif has_git_evidence:
        historical_files_modified = False
    else:
        historical_files_modified = None

    if not new_results:
        new_archive_status = "not_present"
    elif all(entry["ok"] for entry in new_results):
        new_archive_status = "passed"
    else:
        new_archive_status = "failed"

    evidence_notes: list[str] = []
    if status_lines:
        evidence_notes.append(f"tracked changes under candidates/: {status_lines}")
    elif status_lines is not None:
        evidence_notes.append("git status shows no tracked changes under candidates/")
    if historical_files_modified is None:
        evidence_notes.append("insufficient git evidence to prove historical files unchanged")
    evidence_notes.extend(historical["notes"])
    if historical["ok"] is False:
        evidence_notes.append(f"historical baseline mismatches: {sorted(historical['mismatches'])}")

    exit_code = 0
    violated = new_failures or unexplained_total or unknown_results
    if violated or historical_files_modified is True or historical["ok"] is False:
        exit_code = 1

    return {
        "tool": "phase28_archive_verify",
        "archive_root": str(archive_root.resolve()),
        "repo": str(repo.resolve()),
        "ignore_raw_drift": ignore_raw_drift,
        "versions": results,
        "skipped_entries": skipped,
        "historical_baseline": historical,
        "totals": {
            "versions_checked": len(results),
            "new_archives": len(new_results),
            "old_archives": len(old_results),
            "unknown_entries": len(unknown_results),
            "new_archive_failures": new_failures,
            "old_unexplained_mismatches": unexplained_total,
            "historical_files_modified": historical_files_modified,
            "historical_baseline_files": historical["files_checked"],
            "historical_baseline_mismatches": len(historical["mismatches"]),
        },
        "historical_evidence_notes": evidence_notes,
        "new_archive_status": new_archive_status,
        "exit_code": exit_code,
    }


def _print_summary(report: dict[str, Any]) -> None:
    for entry in report["versions"]:
        if entry["archive_class"] == "new":
            state = (
                entry.get("status", "ok")
                if entry["ok"]
                else f"FAILED ({len(entry['failures'])} checks)"
            )
            for failure in entry["failures"]:
                print(f"[fail] {entry['version']} {failure['check']}: {failure['detail']}")
            if entry.get("raw_drift_ignored"):
                print(
                    f"[note] {entry['version']} legacy_worktree_drift: {sorted(entry['raw_drift'])}"
                )
            print(f"{entry['version']:<28} new  {state}")
            if entry.get("status") == "legacy_non_strict":
                print(f"[note] {entry['version']} reasons={entry.get('reasons', [])}")
        else:
            declared = entry.get("declared")
            declared_text = declared[:12] + "..." if isinstance(declared, str) else str(declared)
            rule = entry.get("inferred_rule") or (
                "unexplained_mismatch" if entry.get("unexplained_mismatch") else "undeclared"
            )
            state = "ok" if entry["ok"] else "UNEXPLAINED MISMATCH"
            print(
                f"{entry['version']:<28} old  declared={declared_text:<16} rule={rule:<24} {state}"
            )
    historical = report["historical_baseline"]
    if historical["manifest_present"]:
        state = "internal-only" if historical["ok"] else "MISMATCH"
        print(
            f"{'historical_baseline':<28} manifest files={historical['files_checked']} "
            f"mismatches={len(historical['mismatches'])} {state}"
        )
    else:
        print(f"{'historical_baseline':<28} manifest absent (not proven here)")
    totals = report["totals"]
    print(
        f"versions={totals['versions_checked']} new={totals['new_archives']} "
        f"old={totals['old_archives']} new_failures={totals['new_archive_failures']} "
        f"unexplained={totals['old_unexplained_mismatches']} "
        f"historical_files_modified={totals['historical_files_modified']} "
        f"new_archive_status={report['new_archive_status']}"
    )


def _usage_error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Phase29 candidate archive integrity verifier (schema v2/v3).",
    )
    parser.add_argument("--all", action="store_true", help="verify every version under candidates/")
    parser.add_argument(
        "--version",
        action="append",
        metavar="V",
        help="verify a single version (repeatable)",
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="write the JSON report to this path"
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=DEFAULT_ARCHIVE_ROOT,
        help=f"archive root (default: {DEFAULT_ARCHIVE_ROOT})",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="source repository used for git checks and worktree hashes (default: repo root)",
    )
    parser.add_argument(
        "--ignore-raw-drift",
        action="store_true",
        help="record raw_worktree_hashes drift as an annotation instead of a failure",
    )
    parser.add_argument(
        "--strict-raw-drift",
        action="store_true",
        help="fail when legacy worktree-origin raw bytes drift",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="candidate receipt JSON (Phase30); enables the external witness checks",
    )
    parser.add_argument(
        "--expect-receipt",
        type=str,
        default=None,
        metavar="SHA256",
        help="external witness: expected sha256 of the receipt file itself; without "
        "it the run is labeled witness=none and historical_integrity can never be 'ok'",
    )
    args = parser.parse_args(argv)
    if args.all and args.version:
        return _usage_error("--all and --version are mutually exclusive")
    if not args.all and not args.version:
        return _usage_error("one of --all or --version is required")

    archive_root = args.archive_root
    if not (archive_root / "candidates").is_dir():
        return _usage_error(f"no candidates directory under archive root: {archive_root}")
    if args.all:
        versions = sorted(
            entry.name
            for entry in (archive_root / "candidates").iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
        if not versions:
            return _usage_error(
                f"no candidate version directories under: {archive_root / 'candidates'}"
            )
    else:
        versions = list(args.version or [])

    report = verify_versions(
        archive_root=archive_root,
        repo=args.repo,
        versions=versions,
        ignore_raw_drift=not args.strict_raw_drift,
    )
    receipt_result: dict[str, Any] | None = None
    if args.receipt is not None:
        if args.expect_receipt is not None and not re.fullmatch(
            r"[a-f0-9]{64}", args.expect_receipt
        ):
            return _usage_error("--expect-receipt must be a 64-character sha256")
        receipt_result = verify_receipt(
            args.receipt,
            archive_root=archive_root,
            repo=args.repo,
            expect_receipt=args.expect_receipt,
        )
        report["receipt"] = receipt_result

    # Phase30 witness semantics: historical integrity is only ever reported as
    # 'ok' when an external witness (the --expect-receipt sha256, held outside
    # this repository) verified the receipt. Without it the strongest honest
    # claim is 'internal-only' — internal consistency, externally unproven.
    witness = receipt_result["witness"] if receipt_result is not None else "none"
    historical = report["historical_baseline"]
    receipt_ok = receipt_result["ok"] if receipt_result is not None else True
    if witness == "verified" and receipt_ok and historical["ok"] is True:
        historical_integrity = "ok"
    elif (receipt_result is not None and not receipt_ok) or historical["ok"] is False:
        historical_integrity = "failed"
    else:
        historical_integrity = "internal-only"
    report["witness"] = witness
    report["historical_integrity"] = historical_integrity

    exit_code = int(report["exit_code"])
    if receipt_result is not None and not receipt_ok:
        exit_code = 1
    report["exit_code"] = exit_code
    _print_summary(report)
    if receipt_result is not None:
        print(f"manifest_binding={receipt_result.get('manifest_binding', 'unverified')}")
    print(f"witness={witness} historical_integrity={historical_integrity}")
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"json report written: {args.json}")
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
