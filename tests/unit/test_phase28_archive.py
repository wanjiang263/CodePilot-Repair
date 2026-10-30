"""Phase28 archive generator tests: contract A (schema v2), temporary git fixtures only.

Zero network, zero Docker, zero model calls: every test drives a throwaway git
repository and a throwaway archive root under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codepilot.archive import (
    ArchiveError,
    CandidateExistsError,
    DiffVerificationError,
    DirtyWorktreeError,
    MissingSourceError,
    create_candidate,
)
from codepilot.rsi import extract_prompts, validate_snapshot

CODER_SOURCE = '"""Coder fixture."""\n\n_SYSTEM = "coder prompt fixture"\n\n\ndef run() -> str:\n    return _SYSTEM\n'
PLANNER_SOURCE = '"""Planner fixture."""\n\n_SYSTEM = "planner prompt fixture"\n'
REVIEWER_SOURCE = '"""Reviewer fixture."""\n\n_SYSTEM = "reviewer prompt fixture"\n'
FEATURE_V1 = "def feature() -> int:\n    return 1\n"
FEATURE_V2 = "def feature() -> int:\n    return 2\n\n\ndef extra() -> str:\n    return 'extra'\n"
NOTE_V1 = "note v1\n"
NOTE_V2 = "note v2 changed\n"
UNLISTED_V1 = "unlisted v1\n"

SOURCE_PATHS = (
    "agents/coder.py",
    "agents/planner.py",
    "agents/reviewer.py",
    "src/feature.py",
    "docs/note.md",
)
ARCHIVE_FILES = ("candidate.diff", "candidate.json", "snapshot.json", "sources.json")


def git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def write_crlf(repo: Path, rel: str, text: str) -> None:
    """Write CRLF-terminated bytes so LF-normalized and raw hashes provably differ."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode("utf-8").strip()


def build_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Commit a base tree, then the candidate change; the worktree stays clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "archive@example.invalid")
    git(repo, "config", "user.name", "Archive Fixture")
    git(repo, "config", "core.autocrlf", "false")
    write_crlf(repo, "agents/coder.py", CODER_SOURCE)
    write_crlf(repo, "agents/planner.py", PLANNER_SOURCE)
    write_crlf(repo, "agents/reviewer.py", REVIEWER_SOURCE)
    write_crlf(repo, "src/feature.py", FEATURE_V1)
    write_crlf(repo, "docs/note.md", NOTE_V1)
    write_crlf(repo, "unlisted.txt", UNLISTED_V1)
    base = commit_all(repo, "base")
    write_crlf(repo, "src/feature.py", FEATURE_V2)
    write_crlf(repo, "docs/note.md", NOTE_V2)
    head = commit_all(repo, "candidate change")
    return repo, base, head


def generate(
    root: Path, repo: Path, version: str, base: str, head: str, **kwargs: Any
) -> dict[str, Any]:
    return create_candidate(
        root,
        repo,
        version=version,
        parent=kwargs.pop("parent", None),
        diff_base_commit=base,
        source_commit=head,
        source_files=list(SOURCE_PATHS),
        **kwargs,
    )


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def normalize(raw: bytes) -> bytes:
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def test_generate_writes_self_verifying_v2_directory(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    summary = generate(root, repo, "phase28-t-main", base, head)
    folder = root / "candidates" / "phase28-t-main"
    assert summary["directory"] == str(folder)
    assert summary["source_files"] == len(SOURCE_PATHS)
    assert summary["dirty_worktree"] is False and "dirty_files" not in summary
    assert sorted(path.name for path in folder.iterdir()) == sorted(ARCHIVE_FILES)
    assert not (root / "active.json").exists()
    assert not (root / "active.next.json").exists()

    candidate = read_json(folder / "candidate.json")
    assert candidate["schema_version"] == 2
    assert candidate["version"] == "phase28-t-main"
    assert candidate["parent"] is None
    assert candidate["status"] == "pending"
    assert candidate["created_from"] == "phase28_archive"
    assert candidate["snapshot_ref"] == "snapshot.json"
    assert candidate["snapshot_hash_rule"] == "file_bytes_sha256"
    assert candidate["diff_base_commit"] == base
    assert candidate["source_commit"] == head
    assert candidate["source_files"] == len(SOURCE_PATHS)
    assert (
        candidate["normalization"] == "UTF-8 LF normalized; raw worktree hashes recorded separately"
    )
    assert candidate["parent_integrity"] is None
    assert candidate["dirty_worktree"] is False
    assert "code_revision" not in candidate

    snapshot = read_json(folder / "snapshot.json")
    assert snapshot["schema_version"] == 1
    assert snapshot["version"] == "phase28-t-main"
    assert snapshot["parent"] is None
    assert snapshot["status"] == "pending"
    assert snapshot["prompts"] == {
        "coder": "coder prompt fixture",
        "planner": "planner prompt fixture",
        "reviewer": "reviewer prompt fixture",
    }
    assert validate_snapshot(snapshot, root) == snapshot

    sources = read_json(folder / "sources.json")
    assert sources["src/feature.py"] == FEATURE_V2
    assert all("\r" not in text for text in sources.values())
    assert snapshot["prompts"] == extract_prompts(
        {key: value for key, value in sources.items() if key.startswith("agents/")}
    )

    diff = (folder / "candidate.diff").read_bytes()
    assert diff and b"feature" in diff
    git(repo, "apply", "--check", "-R", "--whitespace=nowarn", stdin=diff)


PREFIXED_SOURCE_PATHS = (
    "src/codepilot/agents/coder.py",
    "src/codepilot/agents/planner.py",
    "src/codepilot/agents/reviewer.py",
    "src/codepilot/feature.py",
)


def test_prompts_resolved_from_prefixed_agent_manifest_keys(tmp_path: Path) -> None:
    """Regression (audit D1): manifest keys are repo-relative, so the roles must
    be resolved by path suffix; a bare ``agents/{role}.py`` lookup silently
    emptied snapshot prompts for ``src/codepilot/agents/...`` manifests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "archive@example.invalid")
    git(repo, "config", "user.name", "Archive Fixture")
    git(repo, "config", "core.autocrlf", "false")
    write_crlf(repo, "src/codepilot/agents/coder.py", CODER_SOURCE)
    write_crlf(repo, "src/codepilot/agents/planner.py", PLANNER_SOURCE)
    write_crlf(repo, "src/codepilot/agents/reviewer.py", REVIEWER_SOURCE)
    write_crlf(repo, "src/codepilot/feature.py", FEATURE_V1)
    base = commit_all(repo, "base")
    root = tmp_path / "archive"
    create_candidate(
        root,
        repo,
        version="phase28-t-prefixed-prompts",
        diff_base_commit=base,
        source_commit=base,
        source_files=list(PREFIXED_SOURCE_PATHS),
    )
    snapshot = read_json(root / "candidates" / "phase28-t-prefixed-prompts" / "snapshot.json")
    assert snapshot["prompts"] == {
        "coder": "coder prompt fixture",
        "planner": "planner prompt fixture",
        "reviewer": "reviewer prompt fixture",
    }


def test_hashes_recompute_independently_with_crlf_sources(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    summary = generate(root, repo, "phase28-t-hash", base, head)
    folder = Path(summary["directory"])
    candidate = read_json(folder / "candidate.json")

    assert (
        candidate["snapshot_file_sha256"]
        == hashlib.sha256((folder / "snapshot.json").read_bytes()).hexdigest()
    )
    snapshot = read_json(folder / "snapshot.json")
    assert (
        candidate["snapshot_canonical_sha256"]
        == hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    )
    diff = (folder / "candidate.diff").read_bytes()
    assert candidate["diff_sha256"] == hashlib.sha256(diff).hexdigest()

    for rel in SOURCE_PATHS:
        raw = (repo / rel).read_bytes()
        assert candidate["raw_worktree_hashes"][rel] == hashlib.sha256(raw).hexdigest()
        assert candidate["source_hashes"][rel] == hashlib.sha256(normalize(raw)).hexdigest()
    assert (
        candidate["source_hashes"]["src/feature.py"]
        != candidate["raw_worktree_hashes"]["src/feature.py"]
    )


def test_generation_is_byte_deterministic(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    first = generate(tmp_path / "a", repo, "phase28-t-det", base, head)
    second = generate(tmp_path / "b", repo, "phase28-t-det", base, head)
    for name in ARCHIVE_FILES:
        assert (Path(first["directory"]) / name).read_bytes() == (
            Path(second["directory"]) / name
        ).read_bytes()


def test_duplicate_version_rejected_without_touching_existing(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase28-t-dup", base, head)
    folder = root / "candidates" / "phase28-t-dup"
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    with pytest.raises(CandidateExistsError, match="already exists"):
        generate(root, repo, "phase28-t-dup", base, head)
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before


def test_missing_source_fails_without_creating_directory(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(MissingSourceError, match="ghost"):
        create_candidate(
            root,
            repo,
            version="phase28-t-miss",
            diff_base_commit=base,
            source_commit=head,
            source_files=["src/feature.py", "src/ghost.py"],
        )
    assert not (root / "candidates" / "phase28-t-miss").exists()


def test_dirty_worktree_rejected_by_default_and_recorded_when_allowed(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    (repo / "unlisted.txt").write_bytes(b"dirty now\n")
    root = tmp_path / "archive"
    with pytest.raises(DirtyWorktreeError):
        generate(root, repo, "phase28-t-dirty", base, head)
    assert not (root / "candidates" / "phase28-t-dirty").exists()

    summary = generate(root, repo, "phase28-t-dirty2", base, head, allow_dirty=True)
    assert summary["dirty_worktree"] is True
    assert summary["dirty_files"] and any("unlisted.txt" in line for line in summary["dirty_files"])
    candidate = read_json(Path(summary["directory"]) / "candidate.json")
    assert candidate["dirty_worktree"] is True
    assert candidate["dirty_files"] == summary["dirty_files"]
    git(repo, "checkout", "--", "unlisted.txt")


def test_failed_reverse_apply_removes_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.archive as archive_module

    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"

    def broken(repo_path: Path, diff: bytes) -> None:
        raise archive_module.DiffVerificationError("reverse apply refused")

    monkeypatch.setattr(archive_module, "_require_reversible", broken)
    with pytest.raises(DiffVerificationError, match="reverse apply refused"):
        generate(root, repo, "phase28-t-broken", base, head)
    assert not (root / "candidates" / "phase28-t-broken").exists()


def test_empty_diff_is_trivially_reversible_and_archived(tmp_path: Path) -> None:
    repo, _, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    # diff_base == the worktree state: no change to record, candidate.diff is empty.
    summary = generate(root, repo, "phase28-t-nodiff", head, head)
    assert summary["diff_bytes"] == 0
    folder = Path(summary["directory"])
    assert (folder / "candidate.diff").read_bytes() == b""
    candidate = read_json(folder / "candidate.json")
    assert candidate["diff_sha256"] == hashlib.sha256(b"").hexdigest()
    assert sorted(path.name for path in folder.iterdir()) == sorted(ARCHIVE_FILES)


def test_parent_integrity_is_recorded_not_judged(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase28-t-parent", base, head)
    child = generate(root, repo, "phase28-t-child", base, head, parent="phase28-t-parent")
    parent_snapshot = root / "candidates" / "phase28-t-parent" / "snapshot.json"
    parent_candidate = read_json(root / "candidates" / "phase28-t-parent" / "candidate.json")
    integrity = read_json(Path(child["directory"]) / "candidate.json")["parent_integrity"]
    assert integrity["version"] == "phase28-t-parent"
    assert (
        integrity["snapshot_file_sha256_actual"]
        == hashlib.sha256(parent_snapshot.read_bytes()).hexdigest()
    )
    assert integrity["declared_snapshot_sha256"] == parent_candidate["snapshot_file_sha256"]
    assert integrity["declared_snapshot_sha256_field"] == "snapshot_file_sha256"
    assert integrity["file_bytes_consistent"] is True

    # Simulate a v1-style parent: only snapshot_sha256 is declared, with a wrong value.
    parent_candidate.pop("snapshot_file_sha256")
    parent_candidate["snapshot_sha256"] = "0" * 64
    (root / "candidates" / "phase28-t-parent" / "candidate.json").write_text(
        json.dumps(parent_candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    grandchild = generate(root, repo, "phase28-t-grand", base, head, parent="phase28-t-parent")
    tampered = read_json(Path(grandchild["directory"]) / "candidate.json")["parent_integrity"]
    assert tampered["declared_snapshot_sha256_field"] == "snapshot_sha256"
    assert tampered["file_bytes_consistent"] is False
    assert tampered["canonical_consistent"] is False


def test_instruction_and_experiences_flow_into_snapshot(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    entry = {
        "id": "phase28-fixture-advice",
        "level": "L1",
        "repository": "repo",
        "source": ["public issue"],
        "source_run": "run-1",
        "applies_when": {"stage": "repair_patch"},
        "invalid_when": {},
        "action": "inspect the failing source",
        "version": "1",
        "status": "pending",
        "evaluation_exposure": "public_only",
    }
    summary = generate(
        root,
        repo,
        "phase28-t-policy",
        base,
        head,
        instruction="keep prompts stable",
        experiences=[entry],
    )
    snapshot = read_json(Path(summary["directory"]) / "snapshot.json")
    assert snapshot["instruction"] == "keep prompts stable"
    assert snapshot["experiences"] == [entry]
    assert validate_snapshot(snapshot, root) == snapshot


def test_empty_source_list_and_invalid_version_rejected(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(ArchiveError, match="empty"):
        create_candidate(
            root,
            repo,
            version="phase28-t-empty",
            diff_base_commit=base,
            source_commit=head,
            source_files=[],
        )
    with pytest.raises(ArchiveError, match="invalid archive version"):
        create_candidate(
            root,
            repo,
            version="../escape",
            diff_base_commit=base,
            source_commit=head,
            source_files=list(SOURCE_PATHS),
        )
    assert not (root / "candidates").exists()


def load_cli_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "phase28_archive.py"
    spec = importlib.util.spec_from_file_location("phase28_archive_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_source_list_accepts_json_file_and_comma_list(tmp_path: Path) -> None:
    cli = load_cli_module()
    list_file = tmp_path / "list.json"
    list_file.write_text(json.dumps(["a.py", "src/b.py"]), encoding="utf-8")
    assert cli.parse_source_list(str(list_file)) == ["a.py", "src/b.py"]
    assert cli.parse_source_list(" a.py , src\\b.py ") == ["a.py", "src/b.py"]
    bad = tmp_path / "bad.json"
    bad.write_text('{"a": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="array of strings"):
        cli.parse_source_list(str(bad))


def test_cli_generates_candidate_and_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    cli = load_cli_module()
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    exit_code = cli.main(
        [
            "--version",
            "phase28-t-cli",
            "--parent",
            "",
            "--diff-base",
            base,
            "--source-commit",
            head,
            "--source-list",
            ",".join(SOURCE_PATHS),
            "--archive-root",
            str(root),
        ]
    )
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["version"] == "phase28-t-cli"
    assert summary["source_files"] == len(SOURCE_PATHS)
    assert summary["snapshot_file_sha256"] and summary["diff_sha256"]
    assert (root / "candidates" / "phase28-t-cli" / "candidate.json").is_file()


def test_cli_dry_run_computes_hashes_but_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    cli = load_cli_module()
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    exit_code = cli.main(
        [
            "--version",
            "phase28-t-dry",
            "--parent",
            "",
            "--diff-base",
            base,
            "--source-commit",
            head,
            "--source-list",
            ",".join(SOURCE_PATHS),
            "--archive-root",
            str(root),
            "--dry-run",
        ]
    )
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["dry_run"] is True
    assert summary["diff_sha256"] and summary["snapshot_file_sha256"]
    assert not (root / "candidates" / "phase28-t-dry").exists()


def test_cli_reports_failure_as_json_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    cli = load_cli_module()
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    exit_code = cli.main(
        [
            "--version",
            "phase28-t-cli-err",
            "--parent",
            "",
            "--diff-base",
            base,
            "--source-commit",
            head,
            "--source-list",
            "src/ghost.py",
            "--archive-root",
            str(root),
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["error_type"] == "MissingSourceError"
    assert "ghost" in payload["error"]
    assert not (root / "candidates" / "phase28-t-cli-err").exists()
