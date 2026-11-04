"""Phase34 T02: normalization policy must match what the archive actually stores.

History: git_commit candidates only ever stored LF-normalized source_hashes, yet
their note claimed raw worktree hashes were recorded separately. New candidates
now carry a structured ``normalization_policy`` and a mode-specific note; the
verifier rejects a claim without structure and annotates legacy v5/v6 candidates
(``normalization_note_stale``) without failing them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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


def test_legacy_note_without_policy_is_annotated_not_failed(env: dict) -> None:
    generate(env)
    path = env["root"] / "candidates" / CHILD / "candidate.json"
    record = read(path)
    record["normalization"] = LEGACY_NORMALIZATION_NOTE
    record.pop("normalization_policy")
    reseal(record)
    write(path, json.dumps(record))
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


def test_worktree_candidate_keeps_raw_hashes_without_policy(tmp_path: Path) -> None:
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
    record = read(root / "candidates" / "phase34-worktree" / "candidate.json")
    assert record["normalization"] == NORMALIZATION_NOTE_WORKTREE
    assert "normalization_policy" not in record
    assert set(record["raw_worktree_hashes"]) == {"src/app.py"}


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
