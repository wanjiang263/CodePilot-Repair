"""Resealed payload tampering must fail the independent v5 derivation."""

from __future__ import annotations

import hashlib
import json

import pytest
from scripts.phase28_archive_verify import main, verify_new_archive
from test_phase31_archive_manifest import (
    CHILD,
    PARENT,
    commit,
    generate,
    read,
    write,
)
from test_phase31_archive_manifest import (
    env as env,
)

from codepilot.archive import canonical_sha256, create_candidate


def verify(env: dict) -> dict:
    folder = env["root"] / "candidates" / CHILD
    return verify_new_archive(
        folder,
        read(folder / "candidate.json"),
        archive_root=env["root"],
        repo=env["repo"],
        ignore_raw_drift=True,
    )


@pytest.mark.parametrize("kind", ["full", "delta"])
def test_v5_verifier_accepts_complete_sets(env: dict, kind: str) -> None:
    generate(env, manifest_kind=kind)
    result = verify(env)
    assert result["ok"], result["failures"]


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_changed",
        "forbidden_source",
        "active",
        "parent_hash",
        "missing_field",
        "source_commit",
        "invalid_manifest",
        "snapshot_prompt",
    ],
)
def test_resealed_candidate_tampering_rejected(env: dict, tamper: str) -> None:
    generate(env)
    path = env["root"] / "candidates" / CHILD / "candidate.json"
    record = read(path)
    if tamper == "missing_changed":
        record["source_hashes"].pop("src/new.py")
    elif tamper == "forbidden_source":
        record["source_hashes"]["configs/harness/candidates/evil.json"] = "0" * 64
    elif tamper == "active":
        record["status"] = "active"
    elif tamper == "parent_hash":
        record["parent_integrity"]["snapshot_file_sha256_actual"] = "0" * 64
    elif tamper == "missing_field":
        del record["source_manifest"]["source_scope"]
    elif tamper == "invalid_manifest":
        record["source_manifest"] = []
    elif tamper == "snapshot_prompt":
        snapshot_path = path.with_name("snapshot.json")
        snapshot = read(snapshot_path)
        snapshot["prompts"]["coder"] = "resealed unrelated prompt"
        write(snapshot_path, json.dumps(snapshot))
        record["snapshot_file_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        record["snapshot_canonical_sha256"] = canonical_sha256(snapshot)
    else:
        record["source_commit"] = env["base"]
    record.pop("candidate_payload_sha256")
    record["candidate_payload_sha256"] = canonical_sha256(record)
    write(path, json.dumps(record))
    assert not verify(env)["ok"]


def test_no_witness_is_internal_only(env: dict, capsys: pytest.CaptureFixture[str]) -> None:
    generate(env)
    commit(env["repo"], "archive")
    from scripts.phase29_baseline_manifest import build_manifest

    manifest, drift = build_manifest(
        env["root"],
        env["repo"],
        baseline_commit=env["head"],
        output_path=env["root"] / "candidates/historical-baseline.json",
    )
    assert not drift
    write(env["root"] / "candidates/historical-baseline.json", json.dumps(manifest))
    commit(env["repo"], "manifest")
    assert main(["--all", "--repo", str(env["repo"]), "--archive-root", str(env["root"])]) == 0
    assert "historical_integrity=internal-only" in capsys.readouterr().out


def test_v3_empty_prompts_are_legacy_and_raw_drift_is_opt_in(env: dict) -> None:
    create_candidate(
        env["root"],
        env["repo"],
        version="legacy-worktree",
        parent=PARENT,
        diff_base_commit=env["base"],
        source_origin="worktree",
        source_files=["src/feature.py"],
        candidate_schema_version=3,
    )
    folder = env["root"] / "candidates/legacy-worktree"
    candidate = read(folder / "candidate.json")
    write(env["repo"] / "src/feature.py", "value = 999\n")
    result = verify_new_archive(
        folder, candidate, archive_root=env["root"], repo=env["repo"], ignore_raw_drift=True
    )
    assert result["ok"], result["failures"]
    assert result["status"] == "legacy_non_strict"
    assert result["reasons"] == ["schema<4", "empty_role_prompts"]
    assert result["legacy_worktree_drift"]
    strict = verify_new_archive(
        folder, candidate, archive_root=env["root"], repo=env["repo"], ignore_raw_drift=False
    )
    assert not strict["ok"]


def test_v2_without_source_commit_uses_archived_bytes_after_raw_drift(env: dict) -> None:
    create_candidate(
        env["root"],
        env["repo"],
        version="legacy-v2-worktree",
        parent=PARENT,
        diff_base_commit=env["base"],
        source_origin="worktree",
        source_files=["src/feature.py"],
        candidate_schema_version=3,
    )
    folder = env["root"] / "candidates/legacy-v2-worktree"
    candidate = read(folder / "candidate.json")
    candidate["schema_version"] = 2
    write(env["repo"] / "src/feature.py", "value = 999\n")
    result = verify_new_archive(
        folder, candidate, archive_root=env["root"], repo=env["repo"], ignore_raw_drift=True
    )
    assert result["ok"], result["failures"]
    assert result["legacy_worktree_drift"]


def test_delta_missing_modified_source_fails_even_when_resealed(env: dict) -> None:
    generate(env, manifest_kind="delta")
    path = env["root"] / "candidates" / CHILD / "candidate.json"
    candidate = read(path)
    del candidate["source_hashes"]["src/feature.py"]
    candidate.pop("candidate_payload_sha256")
    candidate["candidate_payload_sha256"] = canonical_sha256(candidate)
    write(path, json.dumps(candidate))
    result = verify(env)
    assert not result["ok"]
    assert any(c["check"] == "source_manifest_exact_set" for c in result["failures"])
