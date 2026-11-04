"""Phase34 T02/P1: normalization policy must match what the archive stores.

git_commit candidates store LF hashes only; worktree candidates store raw hashes
too. New candidates carry a structured ``normalization_policy`` selected by
source_origin. Historical v3/v4/v5/v6 candidates predate the policy and are
exempt only when BOTH the version and the sealed payload hash match the pinned
table, so a new candidate cannot bypass the requirement by reusing an old note.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts import phase28_archive_verify as verify_mod
from scripts.phase28_archive_verify import (
    LEGACY_NORMALIZATION_NOTE,
    verify_new_archive,
)
from test_phase28_archive import FEATURE_V2, build_repo
from test_phase28_archive import generate as generate_schema3
from test_phase31_archive_manifest import CHILD, commit, generate, read, write
from test_phase31_archive_manifest import env as env
from test_phase31_archive_manifest import git as git_cmd

from codepilot.archive import (
    GIT_NORMALIZATION_POLICY,
    NORMALIZATION_NOTE,
    NORMALIZATION_NOTE_WORKTREE,
    WORKTREE_NORMALIZATION_POLICY,
    SourceOriginError,
    canonical_sha256,
    create_candidate,
)


def reseal(record: dict[str, Any]) -> None:
    record.pop("candidate_payload_sha256", None)
    record["candidate_payload_sha256"] = canonical_sha256(record)


def verify(env: dict) -> dict:
    folder = env["root"] / "candidates" / CHILD
    return verify_new_archive(
        folder,
        read(folder / "candidate.json"),
        archive_root=env["root"],
        repo=env["repo"],
        ignore_raw_drift=True,
    )


def test_new_candidate_records_policy_and_matching_note(env: dict) -> None:
    generate(env)
    record = read(env["root"] / "candidates" / CHILD / "candidate.json")
    assert record["normalization"] == NORMALIZATION_NOTE
    assert record["normalization_policy"] == GIT_NORMALIZATION_POLICY
    result = verify(env)
    assert result["ok"], result["failures"]
    assert result["normalization_note_stale"] is False


def test_unlisted_schema5_with_legacy_note_fails(env: dict) -> None:
    """The old bypass: any schema-5 candidate could write the legacy note."""
    generate(env)
    path = env["root"] / "candidates" / CHILD / "candidate.json"
    record = read(path)
    record["normalization"] = LEGACY_NORMALIZATION_NOTE
    record.pop("normalization_policy")
    reseal(record)
    write(path, json.dumps(record))
    result = verify(env)
    assert not result["ok"]
    check = next(c for c in result["checks"] if c["check"] == "normalization_policy")
    assert not check["ok"]
    assert result["normalization_note_stale"] is False


def test_pinned_legacy_candidate_is_annotated_not_failed(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate(env)
    path = env["root"] / "candidates" / CHILD / "candidate.json"
    record = read(path)
    record["normalization"] = LEGACY_NORMALIZATION_NOTE
    record.pop("normalization_policy")
    reseal(record)
    write(path, json.dumps(record))
    monkeypatch.setattr(
        verify_mod,
        "LEGACY_NORMALIZATION_EXEMPT",
        {CHILD: record["candidate_payload_sha256"]},
    )
    result = verify(env)
    assert result["ok"], result["failures"]
    assert result["normalization_note_stale"] is True
    check = next(c for c in result["checks"] if c["check"] == "normalization_policy")
    assert check["ok"] and "normalization_note_stale" in check["detail"]


def test_policy_claiming_raw_hashes_are_stored_fails(env: dict) -> None:
    generate(env)
    path = env["root"] / "candidates" / CHILD / "candidate.json"
    record = read(path)
    record["normalization_policy"]["raw_hashes_stored"] = True
    reseal(record)
    write(path, json.dumps(record))
    result = verify(env)
    assert not result["ok"]
    assert "normalization_policy" in {failure["check"] for failure in result["failures"]}


def test_new_note_without_policy_fails(env: dict) -> None:
    generate(env)
    path = env["root"] / "candidates" / CHILD / "candidate.json"
    record = read(path)
    record.pop("normalization_policy")
    reseal(record)
    write(path, json.dumps(record))
    result = verify(env)
    assert not result["ok"]
    check = next(c for c in result["checks"] if c["check"] == "normalization_policy")
    assert not check["ok"]
    assert result["normalization_note_stale"] is False


def test_worktree_candidate_records_policy_and_verifies(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git_cmd(repo, "init", "-q")
    git_cmd(repo, "config", "user.name", "Phase34 fixture")
    git_cmd(repo, "config", "user.email", "fixture@example.invalid")
    git_cmd(repo, "config", "core.autocrlf", "false")
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("value = 1\n", encoding="utf-8", newline="\n")
    base = commit(repo, "base")
    root = repo / "configs/harness"
    create_candidate(
        root,
        repo,
        version="phase34-worktree",
        diff_base_commit=base,
        source_origin="worktree",
        source_files=["src/app.py"],
    )
    folder = root / "candidates" / "phase34-worktree"
    record = read(folder / "candidate.json")
    assert record["normalization"] == NORMALIZATION_NOTE_WORKTREE
    assert record["normalization_policy"] == WORKTREE_NORMALIZATION_POLICY
    assert set(record["raw_worktree_hashes"]) == {"src/app.py"}
    result = verify_new_archive(
        folder, record, archive_root=root, repo=repo, ignore_raw_drift=False
    )
    assert result["ok"], result["failures"]


def test_worktree_policy_mismatch_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git_cmd(repo, "init", "-q")
    git_cmd(repo, "config", "user.name", "Phase34 fixture")
    git_cmd(repo, "config", "user.email", "fixture@example.invalid")
    git_cmd(repo, "config", "core.autocrlf", "false")
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("value = 1\n", encoding="utf-8", newline="\n")
    base = commit(repo, "base")
    root = repo / "configs/harness"
    create_candidate(
        root,
        repo,
        version="phase34-worktree-bad",
        diff_base_commit=base,
        source_origin="worktree",
        source_files=["src/app.py"],
    )
    folder = root / "candidates" / "phase34-worktree-bad"
    path = folder / "candidate.json"
    record = read(path)
    record["normalization_policy"]["raw_hashes_stored"] = False
    reseal(record)
    write(path, json.dumps(record))
    result = verify_new_archive(
        folder, record, archive_root=root, repo=repo, ignore_raw_drift=False
    )
    assert not result["ok"]
    assert "normalization_policy" in {failure["check"] for failure in result["failures"]}


def test_schema5_worktree_generation_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(SourceOriginError, match="schema 5 requires git_commit"):
        create_candidate(
            tmp_path / "archive",
            repo,
            version="phase34-schema5-worktree",
            diff_base_commit="deadbeef",
            source_origin="worktree",
            candidate_schema_version=5,
        )


def test_schema3_git_no_policy_fails_unless_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    version = "phase34-schema3-legacy"
    generate_schema3(root, repo, version, base, head)
    folder = root / "candidates" / version
    path = folder / "candidate.json"
    record = read(path)
    record["normalization"] = LEGACY_NORMALIZATION_NOTE
    record.pop("normalization_policy")
    reseal(record)
    write(path, json.dumps(record))
    result = verify_new_archive(folder, record, archive_root=root, repo=repo, ignore_raw_drift=True)
    assert not result["ok"]
    assert "normalization_policy" in {failure["check"] for failure in result["failures"]}
    monkeypatch.setattr(
        verify_mod,
        "LEGACY_NORMALIZATION_EXEMPT",
        {version: record["candidate_payload_sha256"]},
    )
    result = verify_new_archive(folder, record, archive_root=root, repo=repo, ignore_raw_drift=True)
    assert result["ok"], result["failures"]
    assert result["normalization_note_stale"] is True


def test_crlf_source_hashes_only_lf_bytes(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate_schema3(root, repo, "phase34-crlf", base, head)
    record = read(root / "candidates" / "phase34-crlf" / "candidate.json")
    lf_hash = hashlib.sha256(FEATURE_V2.encode("utf-8")).hexdigest()
    raw_hash = hashlib.sha256((repo / "src/feature.py").read_bytes()).hexdigest()
    assert record["source_hashes"]["src/feature.py"] == lf_hash
    assert lf_hash != raw_hash
    assert record["normalization"] == NORMALIZATION_NOTE
    assert record["normalization_policy"] == GIT_NORMALIZATION_POLICY
