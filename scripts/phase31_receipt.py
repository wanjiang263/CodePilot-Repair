"""Deterministic schema-5 receipt generation for any candidate in the chain.

The parent is read from the candidate record itself; an explicit parent argument is
only an assertion that must agree with candidate.json. The record status must be
pending and is copied into the receipt, and both schema_version and
candidate_schema_version must be 5. The external witness is never stored here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from codepilot.archive import ArchiveError, _git, _resolved_commit, candidate_tree_sha256

RECEIPT_FILE_NAMES = ("candidate.diff", "candidate.json", "snapshot.json", "sources.json")

REPO_ROOT = Path(__file__).resolve().parents[1]


def generate_receipt(
    *,
    repo: Path,
    candidate: str,
    archive_root: Path,
    receipt: Path,
    candidate_commit: str,
    source_commit: str,
    diff_base: str,
    manifest_commit: str,
    parent: str | None = None,
    replace: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Validate committed input identities before writing a deterministic receipt."""
    if receipt.exists() and (not replace or not reason.strip()):
        raise ArchiveError("receipt exists; --replace and a nonempty --reason are required")
    if replace and not reason.strip():
        raise ArchiveError("--replace requires --reason")
    candidate_commit = _resolved_commit(repo, "candidate_commit", candidate_commit)
    source_commit = _resolved_commit(repo, "source_commit", source_commit)
    diff_base = _resolved_commit(repo, "diff_base", diff_base)
    manifest_commit = _resolved_commit(repo, "manifest_commit", manifest_commit)
    candidates = archive_root.resolve()
    prefix = candidates.relative_to(repo.resolve()).as_posix()
    folder = candidates / candidate
    record = json.loads((folder / "candidate.json").read_text(encoding="utf-8"))
    expected = {
        "schema_version": 5,
        "candidate_schema_version": 5,
        "version": candidate,
        "source_commit": source_commit,
        "diff_base_commit": diff_base,
    }
    for field, value in expected.items():
        if record.get(field) != value:
            raise ArchiveError(f"candidate.json {field} differs from required input {value!r}")
    record_parent = record.get("parent")
    if not isinstance(record_parent, str) or not record_parent.strip():
        raise ArchiveError("candidate.json parent must be a nonempty string")
    if parent is not None and parent != record_parent:
        raise ArchiveError(
            f"candidate.json parent {record_parent!r} differs from requested parent {parent!r}"
        )
    record_status = record.get("status")
    if record_status != "pending":
        raise ArchiveError(f"candidate.json status must be pending, got {record_status!r}")
    if not (candidates / record_parent).is_dir():
        raise ArchiveError("parent directory missing")
    for name in ("active.json", "active.next.json"):
        if (candidates.parent / name).exists():
            raise ArchiveError("active pointer must be absent")
    for ancestor, descendant in (
        (source_commit, candidate_commit),
        (diff_base, source_commit),
        (candidate_commit, "HEAD"),
        (manifest_commit, "HEAD"),
    ):
        _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    hashes = {}
    for name in RECEIPT_FILE_NAMES:
        raw = (folder / name).read_bytes()
        if raw != _git(repo, "cat-file", "blob", f"{candidate_commit}:{prefix}/{candidate}/{name}"):
            raise ArchiveError(f"{name} disk bytes differ from candidate_commit blob")
        hashes[name] = hashlib.sha256(raw).hexdigest()
    manifest = _git(
        repo, "cat-file", "blob", f"{manifest_commit}:{prefix}/historical-baseline.json"
    )
    if manifest != (candidates / "historical-baseline.json").read_bytes():
        raise ArchiveError("current manifest differs from manifest_commit blob")
    payload = {
        "schema_version": 1,
        "candidate": candidate,
        "candidate_commit": candidate_commit,
        "source_commit": source_commit,
        "diff_base": diff_base,
        "status": record_status,
        "candidate_files": hashes,
        "candidate_tree_sha256": candidate_tree_sha256(hashes),
        "manifest_commit": manifest_commit,
        "manifest_blob_sha256": hashlib.sha256(manifest).hexdigest(),
        "historical_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "created_at": _git(repo, "show", "-s", "--format=%cI", manifest_commit).decode().strip(),
    }
    if replace:
        payload["replacement_reason"] = reason.strip()
        payload["replaced_receipt_sha256"] = (
            hashlib.sha256(receipt.read_bytes()).hexdigest() if receipt.exists() else None
        )
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument(
        "--archive-root", type=Path, default=REPO_ROOT / "configs/harness/candidates"
    )
    parser.add_argument("--receipt", type=Path, required=True)
    for name in ("candidate-commit", "source-commit", "diff-base", "manifest-commit"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--parent", default=None)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--reason", default="")
    args = vars(parser.parse_args(argv))
    try:
        generate_receipt(repo=REPO_ROOT, **args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps({"receipt": str(args["receipt"]), "generated": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
