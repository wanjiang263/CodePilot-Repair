"""Receipt identity, committed manifest and immutable witness regression tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from scripts.phase28_archive_verify import verify_receipt
from scripts.phase29_baseline_manifest import build_manifest
from scripts.phase31_receipt import generate_receipt
from test_phase31_archive_manifest import (  # noqa: F401
    CHILD,
    PARENT,
    commit,
    generate,
    git,
    read,
    write,
)
from test_phase31_archive_manifest import (
    env as env,
)

from codepilot.archive import ArchiveError


@pytest.fixture
def receipted(env: dict) -> dict:
    generate(env)
    candidate_commit = commit(env["repo"], "candidate")
    manifest, drift = build_manifest(
        env["root"],
        env["repo"],
        baseline_commit=env["head"],
        output_path=env["root"] / "candidates/historical-baseline.json",
    )
    assert not drift
    write(env["root"] / "candidates/historical-baseline.json", json.dumps(manifest))
    manifest_commit = commit(env["repo"], "manifest")
    receipt = env["root"] / "receipts" / f"{CHILD}.json"
    args = {
        "repo": env["repo"],
        "candidate": CHILD,
        "archive_root": env["root"] / "candidates",
        "receipt": receipt,
        "candidate_commit": candidate_commit,
        "source_commit": env["head"],
        "diff_base": env["base"],
        "manifest_commit": manifest_commit,
    }
    generate_receipt(**args)
    return {
        **env,
        "args": args,
        "receipt": receipt,
        "witness": hashlib.sha256(receipt.read_bytes()).hexdigest(),
    }


def check(data: dict, witness: str | None = None) -> dict:
    return verify_receipt(
        data["receipt"], archive_root=data["root"], repo=data["repo"], expect_receipt=witness
    )


def test_v5_receipt_bound_to_manifest_commit(receipted: dict) -> None:
    result = check(receipted, receipted["witness"])
    assert result["ok"], result["failures"]
    assert result["manifest_binding"] == "manifest_commit"
    assert result["witness"] == "verified"


@pytest.mark.parametrize(
    "field",
    [
        "candidate",
        "source_commit",
        "diff_base",
        "manifest_commit",
        "manifest_blob_sha256",
        "historical_manifest_sha256",
        "missing_manifest_commit",
        "extra_candidate_file",
        "status",
    ],
)
def test_receipt_independent_fields_rejected(receipted: dict, field: str) -> None:
    receipt = read(receipted["receipt"])
    if field == "missing_manifest_commit":
        del receipt["manifest_commit"]
    elif field == "candidate":
        receipt[field] = PARENT
    elif field == "source_commit":
        # This valid older commit has most identical blobs; equality is still required.
        receipt[field] = receipted["base"]
    elif field == "diff_base":
        receipt[field] = receipted["head"]
    elif field == "manifest_commit":
        receipt[field] = receipted["base"]
    elif field == "extra_candidate_file":
        receipt["candidate_files"]["unexpected.json"] = "0" * 64
    elif field == "status":
        receipt[field] = "active"
    else:
        receipt[field] = "0" * 64
    write(receipted["receipt"], json.dumps(receipt))
    assert not check(receipted)["ok"]


def test_synchronized_receipt_edit_breaks_unchanged_witness(receipted: dict) -> None:
    receipt = read(receipted["receipt"])
    receipt["created_at"] = "edited metadata"
    write(receipted["receipt"], json.dumps(receipt))
    result = check(receipted, receipted["witness"])
    assert not result["ok"]
    assert result["witness"] == "mismatch"


def test_only_candidate_json_source_commit_change_is_rejected(receipted: dict) -> None:
    path = receipted["root"] / "candidates" / CHILD / "candidate.json"
    candidate = read(path)
    candidate["source_commit"] = receipted["base"]
    write(path, json.dumps(candidate))
    result = check(receipted)
    assert not result["ok"]
    assert any(c["check"] == "receipt_candidate_files" for c in result["failures"])


def test_generator_refuses_overwrite_and_records_replace_reason(receipted: dict) -> None:
    with pytest.raises(ArchiveError, match="receipt exists"):
        generate_receipt(**receipted["args"])
    generated = generate_receipt(**receipted["args"], replace=True, reason="fixture replacement")
    assert generated["replacement_reason"] == "fixture replacement"
    assert generated["replaced_receipt_sha256"] == receipted["witness"]


@pytest.mark.parametrize("field", ["source_commit", "diff_base", "candidate", "manifest_commit"])
def test_generator_rejects_inconsistent_inputs(receipted: dict, field: str) -> None:
    args = {**receipted["args"], "receipt": receipted["receipt"].with_name("other.json")}
    args[field] = PARENT if field == "candidate" else receipted["base"]
    if field == "diff_base":
        args[field] = receipted["head"]
    with pytest.raises((ArchiveError, OSError, ValueError)):
        generate_receipt(**args)
