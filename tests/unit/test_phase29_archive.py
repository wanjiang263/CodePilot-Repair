"""Phase29 archive generator tests: schema v3 source binding, temporary git fixtures only.

Zero network, zero Docker, zero model calls. Contract under test
(docs/notes/phase29-archive-schema.md): ``git_commit`` mode reads source text
from ``source_commit`` blobs (never the worktree), requires ``diff_base_commit``
to be an ancestor, and builds ``candidate.diff`` from
``git diff diff_base source_commit``; ``worktree`` mode is the only explicit
dirty-tolerant path and does not claim git provenance. Every candidate carries
``candidate_payload_sha256`` over its own canonical JSON.
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
    DiffVerificationError,
    DirtyWorktreeError,
    MissingSourceError,
    canonical_sha256,
    create_candidate,
)

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
        source_commit=kwargs.pop("source_commit", head),
        source_files=list(SOURCE_PATHS),
        **kwargs,
    )


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def normalize(raw: bytes) -> bytes:
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def commit_blob(repo: Path, commit: str, rel: str) -> bytes:
    return git(repo, "cat-file", "blob", f"{commit}:{rel}")


def test_git_commit_mode_reads_sources_from_commit_blobs(tmp_path: Path) -> None:
    """Worktree clean at ``head`` while archiving ``base``: sources must equal the
    base blobs, proving the generator never reads the worktree for content."""
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    summary = generate(root, repo, "phase29-t-commit-src", base, head, source_commit=base)
    folder = Path(summary["directory"])
    candidate = read_json(folder / "candidate.json")
    sources = read_json(folder / "sources.json")

    assert candidate["schema_version"] == 3
    assert candidate["source_origin"] == "git_commit"
    assert candidate["source_commit"] == base
    for rel in SOURCE_PATHS:
        blob = commit_blob(repo, base, rel)
        assert sources[rel] == normalize(blob).decode("utf-8")
        assert candidate["source_hashes"][rel] == hashlib.sha256(normalize(blob)).hexdigest()
        assert candidate["base_hashes"][rel] == hashlib.sha256(normalize(blob)).hexdigest()
    assert sources["src/feature.py"] == FEATURE_V1, "worktree content must not leak in"
    assert "raw_worktree_hashes" not in candidate


def test_diff_is_generated_from_commits_not_worktree(tmp_path: Path) -> None:
    repo, base, _head = build_repo(tmp_path)
    # Diverge the worktree further (still clean at head via a new commit) so a
    # worktree-based diff would differ from the commit-vs-commit diff.
    write_crlf(repo, "src/feature.py", "def feature() -> int:\n    return 3\n")
    later = commit_all(repo, "later change")
    root = tmp_path / "archive"
    summary = generate(root, repo, "phase29-t-diff-src", base, later)
    folder = Path(summary["directory"])
    expected = git(
        repo,
        "diff",
        "--binary",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        base,
        later,
        "--",
        *SOURCE_PATHS,
    )
    assert (folder / "candidate.diff").read_bytes() == expected
    candidate = read_json(folder / "candidate.json")
    assert candidate["source_commit"] == later
    assert (
        candidate["base_hashes"]["src/feature.py"]
        == hashlib.sha256(normalize(commit_blob(repo, base, "src/feature.py"))).hexdigest()
    )
    assert (
        candidate["source_hashes"]["src/feature.py"]
        == hashlib.sha256(normalize(commit_blob(repo, later, "src/feature.py"))).hexdigest()
    )


def test_candidate_payload_sha256_binds_the_whole_record(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    summary = generate(root, repo, "phase29-t-selfhash", base, head)
    candidate = read_json(Path(summary["directory"]) / "candidate.json")
    declared = candidate.pop("candidate_payload_sha256")
    assert isinstance(declared, str) and len(declared) == 64
    assert declared == canonical_sha256(candidate)
    # Any content change (e.g. flipping status) must invalidate the seal.
    candidate["status"] = "active"
    assert canonical_sha256(candidate) != declared


def test_non_ancestor_diff_base_rejected(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(ArchiveError, match="ancestor"):
        generate(root, repo, "phase29-t-noanc", head, base)  # diff_base=head, source=base
    assert not (root / "candidates" / "phase29-t-noanc").exists()


def test_dirty_worktree_rejected_in_git_commit_mode(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    (repo / "src/feature.py").write_bytes(FEATURE_V1.replace("1", "9").encode("utf-8"))
    root = tmp_path / "archive"
    with pytest.raises(DirtyWorktreeError):
        generate(root, repo, "phase29-t-dirty", base, head)
    assert not (root / "candidates" / "phase29-t-dirty").exists()


def test_allow_dirty_without_worktree_origin_is_rejected(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    (repo / "unlisted.txt").write_bytes(b"dirty now\n")
    root = tmp_path / "archive"
    with pytest.raises(ArchiveError, match="worktree"):
        generate(root, repo, "phase29-t-allowdirty", base, head, allow_dirty=True)
    assert not (root / "candidates" / "phase29-t-allowdirty").exists()


def test_worktree_mode_records_dirty_tree_without_git_provenance(tmp_path: Path) -> None:
    repo, base, _head = build_repo(tmp_path)
    dirty_text = "def feature() -> int:\n    return 42\n"
    (repo / "src/feature.py").write_bytes(dirty_text.encode("utf-8"))
    root = tmp_path / "archive"
    summary = create_candidate(
        root,
        repo,
        version="phase29-t-wt",
        diff_base_commit=base,
        source_files=list(SOURCE_PATHS),
        source_origin="worktree",
    )
    folder = Path(summary["directory"])
    candidate = read_json(folder / "candidate.json")
    sources = read_json(folder / "sources.json")

    assert candidate["schema_version"] == 3
    assert candidate["source_origin"] == "worktree"
    assert candidate["source_commit"] is None
    assert candidate["dirty_worktree"] is True
    assert any("src/feature.py" in line for line in candidate["dirty_files"])
    assert sources["src/feature.py"] == dirty_text, "worktree mode must read the worktree"
    assert (
        candidate["source_hashes"]["src/feature.py"]
        == hashlib.sha256(dirty_text.encode("utf-8")).hexdigest()
    )
    assert (
        candidate["raw_worktree_hashes"]["src/feature.py"]
        == hashlib.sha256((repo / "src/feature.py").read_bytes()).hexdigest()
    )
    assert (
        candidate["base_hashes"]["src/feature.py"]
        == hashlib.sha256(normalize(commit_blob(repo, base, "src/feature.py"))).hexdigest()
    )
    # The recorded diff must transform base blobs into the dirty worktree content.
    git(
        repo,
        "apply",
        "--check",
        "-R",
        "--whitespace=nowarn",
        stdin=(folder / "candidate.diff").read_bytes(),
    )


def test_worktree_mode_rejects_explicit_source_commit(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(ArchiveError, match="source_commit"):
        create_candidate(
            root,
            repo,
            version="phase29-t-wt-bad",
            diff_base_commit=base,
            source_commit=head,
            source_files=list(SOURCE_PATHS),
            source_origin="worktree",
        )
    assert not (root / "candidates" / "phase29-t-wt-bad").exists()


def test_unknown_source_origin_rejected(tmp_path: Path) -> None:
    repo, base, _head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(ArchiveError, match="source_origin"):
        create_candidate(
            root,
            repo,
            version="phase29-t-origin",
            diff_base_commit=base,
            source_files=list(SOURCE_PATHS),
            source_origin="tag",
        )


def test_path_missing_from_source_commit_rejected(tmp_path: Path) -> None:
    repo, base, _head = build_repo(tmp_path)
    root = tmp_path / "archive"
    # docs/note.md changed between base and head; ask for a path that only
    # exists in head by archiving base with a head-only file list.
    with pytest.raises(MissingSourceError, match="ghost"):
        create_candidate(
            root,
            repo,
            version="phase29-t-miss-blob",
            diff_base_commit=base,
            source_commit=base,
            source_files=["src/ghost.py"],
        )


def test_base_hash_is_null_for_file_added_after_base(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    summary = create_candidate(
        root,
        repo,
        version="phase29-t-newfile",
        diff_base_commit=base,
        source_commit=head,
        source_files=["src/feature.py", "docs/note.md"],
    )
    candidate = read_json(Path(summary["directory"]) / "candidate.json")
    # Both listed paths existed in base; add a head-only path to a second candidate.
    write_crlf(repo, "src/added_later.py", "added\n")
    later = commit_all(repo, "add file")
    summary2 = create_candidate(
        root,
        repo,
        version="phase29-t-newfile2",
        diff_base_commit=base,
        source_commit=later,
        source_files=["src/added_later.py"],
    )
    candidate2 = read_json(Path(summary2["directory"]) / "candidate.json")
    assert candidate["base_hashes"]["src/feature.py"] is not None
    assert candidate2["base_hashes"]["src/added_later.py"] is None
    # Forward apply must still hold: diff creates the file from nothing.
    git(
        repo,
        "apply",
        "--check",
        "-R",
        "--whitespace=nowarn",
        stdin=(Path(summary2["directory"]) / "candidate.diff").read_bytes(),
    )


def test_roundtrip_failure_removes_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.archive as archive_module

    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"

    def broken(*args: Any, **kwargs: Any) -> None:
        raise archive_module.DiffVerificationError("roundtrip refused")

    monkeypatch.setattr(archive_module, "_verify_diff_roundtrip", broken)
    with pytest.raises(DiffVerificationError, match="roundtrip refused"):
        generate(root, repo, "phase29-t-broken", base, head)
    assert not (root / "candidates" / "phase29-t-broken").exists()


def test_dry_run_v3_writes_nothing_and_reports_origin(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    summary = generate(root, repo, "phase29-t-dry", base, head, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["source_origin"] == "git_commit"
    assert not (root / "candidates" / "phase29-t-dry").exists()


def test_worktree_mode_dry_run_writes_nothing(tmp_path: Path) -> None:
    repo, base, _head = build_repo(tmp_path)
    (repo / "docs/note.md").write_bytes(b"dirty note\n")
    root = tmp_path / "archive"
    summary = create_candidate(
        root,
        repo,
        version="phase29-t-dry-wt",
        diff_base_commit=base,
        source_files=list(SOURCE_PATHS),
        source_origin="worktree",
        dry_run=True,
    )
    assert summary["dry_run"] is True
    assert summary["source_origin"] == "worktree"
    assert summary["dirty_worktree"] is True
    assert not (root / "candidates" / "phase29-t-dry-wt").exists()
    git(repo, "checkout", "--", "docs/note.md")


def load_cli_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "phase28_archive.py"
    spec = importlib.util.spec_from_file_location("phase29_archive_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_source_origin_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    (repo / "unlisted.txt").write_bytes(b"dirty now\n")
    root = tmp_path / "archive"
    cli = load_cli_module()
    monkeypatch.setattr(cli, "REPO_ROOT", repo)

    # --allow-dirty without --source-origin worktree: rejected with exit 1.
    exit_code = cli.main(
        [
            "--version",
            "phase29-t-cli-ad",
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
            "--allow-dirty",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["error_type"] == "SourceOriginError"

    # Explicit worktree origin: accepted and recorded.
    exit_code = cli.main(
        [
            "--version",
            "phase29-t-cli-wt",
            "--parent",
            "",
            "--diff-base",
            base,
            "--source-commit",
            "",
            "--source-list",
            ",".join(SOURCE_PATHS),
            "--archive-root",
            str(root),
            "--source-origin",
            "worktree",
        ]
    )
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["source_origin"] == "worktree"
    assert summary["dirty_worktree"] is True
    git(repo, "checkout", "--", "unlisted.txt")
