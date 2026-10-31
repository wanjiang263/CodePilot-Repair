"""Phase30 candidate receipt and external witness tests.

The receipt (configs/harness/receipts/<candidate>.json, outside the candidate
directory) binds the archived bytes to a git commit and pins the historical
manifest bytes. Every negative case here commits its tamper first — "modify
then commit" is the tamper a pure worktree check cannot see — and asserts the
specific failing check. All fixtures are temporary; the real configs/harness is
never touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from codepilot.archive import create_candidate

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_FILE_NAMES = ("candidate.diff", "candidate.json", "snapshot.json", "sources.json")

CODER = '"""Coder."""\n\n_SYSTEM = "receipt coder prompt"\n'
PLANNER = '"""Planner."""\n\n_SYSTEM = "receipt planner prompt"\n'
REVIEWER = '"""Reviewer."""\n\n_SYSTEM = "receipt reviewer prompt"\n'
FEATURE_V1 = "def feature() -> int:\n    return 1\n"
FEATURE_V2 = "def feature() -> int:\n    return 2\n"
FEATURE_V3 = "def feature() -> int:\n    return 3\n"
OLD_SOURCES = json.dumps({"old/thing.py": "old v1"}, indent=2)


def _load_verifier():
    script = REPO_ROOT / "scripts" / "phase28_archive_verify.py"
    spec = importlib.util.spec_from_file_location("phase30_archive_verify", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("phase30_archive_verify", module)
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


def git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode().strip()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_sources(repo: Path, sources: dict[str, str]) -> None:
    for rel, text in sources.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def build_old_candidate(archive_root: Path) -> None:
    """A phase26-style (schema-less) candidate: only snapshot hashes are checked."""
    folder = archive_root / "candidates" / "phase26-candidate-v1"
    folder.mkdir(parents=True)
    snapshot = {
        "schema_version": 1,
        "version": "phase26-candidate-v1",
        "parent": None,
        "status": "pending",
        "instruction": "",
        "prompts": {},
        "experiences": [],
    }
    (folder / "snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (folder / "sources.json").write_text(OLD_SOURCES, encoding="utf-8")
    candidate = {
        "schema_version": 1,
        "version": "phase26-candidate-v1",
        "snapshot_sha256": sha256_bytes((folder / "snapshot.json").read_bytes()),
    }
    (folder / "candidate.json").write_text(json.dumps(candidate, indent=2), encoding="utf-8")


def write_manifest(archive_root: Path) -> Path:
    """Freeze every file under candidates/ (paths are archive-root relative)."""
    candidates = archive_root / "candidates"
    entries = []
    for path in sorted(candidates.rglob("*")):
        if path.is_file() and path.name != "historical-baseline.json":
            entries.append(
                {
                    "path": path.relative_to(archive_root).as_posix(),
                    "sha256": sha256_bytes(path.read_bytes()),
                }
            )
    manifest = {
        "baseline_commit": None,
        "files": entries,
        "note": "phase30 receipt test fixture manifest",
    }
    manifest_path = candidates / "historical-baseline.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def build_receipt(
    archive_root: Path,
    *,
    version: str,
    candidate_commit: str,
    source_commit: str,
    diff_base: str,
    manifest_path: Path,
) -> Path:
    folder = archive_root / "candidates" / version
    files = {name: sha256_bytes((folder / name).read_bytes()) for name in RECEIPT_FILE_NAMES}
    receipt = {
        "schema_version": 1,
        "candidate": version,
        "candidate_commit": candidate_commit,
        "source_commit": source_commit,
        "diff_base": diff_base,
        "candidate_files": files,
        "candidate_tree_sha256": verifier.candidate_tree_sha256(files),
        "historical_manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
        "created_at": "2026-10-31T00:00:00+00:00",
    }
    receipts = archive_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts / f"{version}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt_path


@pytest.fixture()
def receipt_env(tmp_path: Path) -> dict[str, Any]:
    """Project repo with configs/harness/{candidates,receipts}; committed archive."""
    project = tmp_path / "project"
    (project / "configs" / "harness").mkdir(parents=True)
    archive_root = project / "configs" / "harness"
    git(project, "init")
    git(project, "config", "user.email", "phase30@example.invalid")
    git(project, "config", "user.name", "Phase30 Fixture")
    git(project, "config", "core.autocrlf", "false")
    write_sources(
        project,
        {
            "agents/coder.py": CODER,
            "agents/planner.py": PLANNER,
            "agents/reviewer.py": REVIEWER,
            "src/feature.py": FEATURE_V1,
        },
    )
    base = commit_all(project, "base")
    write_sources(project, {"src/feature.py": FEATURE_V2})
    mid = commit_all(project, "parent change")
    create_candidate(
        archive_root,
        project,
        version="phase30-parent-v3",
        diff_base_commit=base,
        source_commit=mid,
        source_files=[
            "agents/coder.py",
            "agents/planner.py",
            "agents/reviewer.py",
            "src/feature.py",
        ],
    )
    write_sources(project, {"src/feature.py": FEATURE_V3})
    head = commit_all(project, "child change")
    create_candidate(
        archive_root,
        project,
        version="phase30-child-v4",
        parent="phase30-parent-v3",
        diff_base_commit=mid,
        source_commit=head,
        source_files=[
            "agents/coder.py",
            "agents/planner.py",
            "agents/reviewer.py",
            "src/feature.py",
        ],
        candidate_schema_version=4,
        manifest_kind="full",
    )
    build_old_candidate(archive_root)
    manifest_path = write_manifest(archive_root)
    candidate_commit = commit_all(project, "archive candidates")
    receipt_path = build_receipt(
        archive_root,
        version="phase30-child-v4",
        candidate_commit=candidate_commit,
        source_commit=head,
        diff_base=mid,
        manifest_path=manifest_path,
    )
    return {
        "project": project,
        "archive_root": archive_root,
        "candidate_commit": candidate_commit,
        "source_commit": head,
        "diff_base": mid,
        "receipt_path": receipt_path,
        "witness": sha256_bytes(receipt_path.read_bytes()),
    }


def run_verify(
    env: dict[str, Any], *extra: str, tmp_path: Path | None = None
) -> tuple[int, dict[str, Any]]:
    json_path = (tmp_path or env["project"]) / "verify-report.json"
    code = verifier.main(
        [
            "--all",
            "--receipt",
            str(env["receipt_path"]),
            *extra,
            "--archive-root",
            str(env["archive_root"]),
            "--repo",
            str(env["project"]),
            "--json",
            str(json_path),
        ]
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    return code, report


def receipt_failures(report: dict[str, Any]) -> list[str]:
    return [c["check"] for c in report.get("receipt", {}).get("failures", [])]


def all_failure_names(report: dict[str, Any]) -> list[str]:
    names: list[str] = list(receipt_failures(report))
    for version in report.get("versions", []):
        names.extend(c["check"] for c in version.get("failures", []))
    return names


def test_positive_verify_with_witness(receipt_env: dict[str, Any], tmp_path: Path) -> None:
    code, report = run_verify(
        receipt_env, "--expect-receipt", receipt_env["witness"], tmp_path=tmp_path
    )
    assert code == 0, all_failure_names(report)
    assert report["witness"] == "verified"
    assert report["historical_integrity"] == "ok"
    assert report["receipt"]["ok"] is True


def test_no_witness_is_internal_only_never_ok(receipt_env: dict[str, Any], tmp_path: Path) -> None:
    code, report = run_verify(receipt_env, tmp_path=tmp_path)
    assert code == 0
    assert report["witness"] == "none"
    assert report["historical_integrity"] == "internal-only"
    assert report["historical_integrity"] != "ok"


def test_sync_tamper_with_manifest_commit_is_caught_by_witness(
    receipt_env: dict[str, Any], tmp_path: Path
) -> None:
    """Tamper an old candidate file, sync the manifest, commit: the pin breaks."""
    archive_root: Path = receipt_env["archive_root"]
    target = archive_root / "candidates" / "phase26-candidate-v1" / "sources.json"
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    manifest_path = archive_root / "candidates" / "historical-baseline.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "candidates/phase26-candidate-v1/sources.json":
            entry["sha256"] = sha256_bytes(target.read_bytes())
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    commit_all(receipt_env["project"], "tamper history and sync manifest")
    code, report = run_verify(
        receipt_env, "--expect-receipt", receipt_env["witness"], tmp_path=tmp_path
    )
    assert code != 0
    assert "receipt_historical_manifest" in receipt_failures(report)


def test_tampered_candidate_commit_breaks_tree_hash(
    receipt_env: dict[str, Any], tmp_path: Path
) -> None:
    archive_root: Path = receipt_env["archive_root"]
    target = archive_root / "candidates" / "phase30-child-v4" / "candidate.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["normalization"] = payload["normalization"] + " "
    write_json(target, payload)
    commit_all(receipt_env["project"], "tamper candidate and commit")
    code, report = run_verify(
        receipt_env, "--expect-receipt", receipt_env["witness"], tmp_path=tmp_path
    )
    assert code != 0
    names = all_failure_names(report)
    assert "receipt_candidate_tree_current" in names
    assert "receipt_candidate_files" in names


def test_zero_source_commit_fails_ancestry(receipt_env: dict[str, Any], tmp_path: Path) -> None:
    receipt_path: Path = receipt_env["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_commit"] = "0" * 40
    write_json(receipt_path, receipt)
    witness = sha256_bytes(receipt_path.read_bytes())
    code, report = run_verify(receipt_env, "--expect-receipt", witness, tmp_path=tmp_path)
    assert code != 0
    names = receipt_failures(report)
    assert "receipt_source_ancestry" in names
    assert "receipt_source_hashes_blobs" in names


def test_status_active_tamper_fails(receipt_env: dict[str, Any], tmp_path: Path) -> None:
    archive_root: Path = receipt_env["archive_root"]
    target = archive_root / "candidates" / "phase30-child-v4" / "candidate.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["status"] = "active"
    write_json(target, payload)
    commit_all(receipt_env["project"], "activate candidate by editing the record")
    code, report = run_verify(
        receipt_env, "--expect-receipt", receipt_env["witness"], tmp_path=tmp_path
    )
    assert code != 0
    assert "candidate_payload_sha256" in all_failure_names(report)


def test_deleted_parent_fails(receipt_env: dict[str, Any], tmp_path: Path) -> None:
    archive_root: Path = receipt_env["archive_root"]
    shutil.rmtree(archive_root / "candidates" / "phase30-parent-v3")
    commit_all(receipt_env["project"], "delete parent candidate")
    code, report = run_verify(
        receipt_env, "--expect-receipt", receipt_env["witness"], tmp_path=tmp_path
    )
    assert code != 0
    names = all_failure_names(report)
    assert "receipt_parent_present" in names
    assert "parent_integrity_matches" in names


def test_synced_receipt_without_witness_update_fails(
    receipt_env: dict[str, Any], tmp_path: Path
) -> None:
    """Tamper the candidate AND sync the receipt, but keep the external witness."""
    archive_root: Path = receipt_env["archive_root"]
    target = archive_root / "candidates" / "phase30-child-v4" / "candidate.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["normalization"] = payload["normalization"] + " synced"
    write_json(target, payload)
    receipt_path: Path = receipt_env["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    files = {
        name: sha256_bytes((archive_root / "candidates" / "phase30-child-v4" / name).read_bytes())
        for name in RECEIPT_FILE_NAMES
    }
    receipt["candidate_files"] = files
    receipt["candidate_tree_sha256"] = verifier.candidate_tree_sha256(files)
    write_json(receipt_path, receipt)
    commit_all(receipt_env["project"], "tamper candidate and sync receipt")
    code, report = run_verify(
        receipt_env, "--expect-receipt", receipt_env["witness"], tmp_path=tmp_path
    )
    assert code != 0
    assert "receipt_witness" in receipt_failures(report)
    assert report["witness"] == "mismatch"
