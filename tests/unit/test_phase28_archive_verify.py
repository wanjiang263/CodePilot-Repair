"""Phase28 archive verifier tests: temporary git fixtures only, never the real archive.

Zero network, zero Docker, zero model calls: every test drives a throwaway git
repository and a throwaway archive root under ``tmp_path``; the real
``configs/harness`` tree is never touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codepilot.archive import create_candidate

CODER_SOURCE = '"""Coder fixture."""\n\n_SYSTEM = "coder prompt fixture"\n\n\ndef run() -> str:\n    return _SYSTEM\n'
PLANNER_SOURCE = '"""Planner fixture."""\n\n_SYSTEM = "planner prompt fixture"\n'
REVIEWER_SOURCE = '"""Reviewer fixture."""\n\n_SYSTEM = "reviewer prompt fixture"\n'
FEATURE_V1 = "def feature() -> int:\n    return 1\n"
FEATURE_V2 = "def feature() -> int:\n    return 2\n\n\ndef extra() -> str:\n    return 'extra'\n"
NOTE_V1 = "note v1\n"
NOTE_V2 = "note v2 changed\n"

SOURCE_PATHS = (
    "agents/coder.py",
    "agents/planner.py",
    "agents/reviewer.py",
    "src/feature.py",
    "docs/note.md",
)


def git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def write_file(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode("utf-8").strip()


def build_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Commit a base tree, then the candidate change; the worktree stays clean."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "verify@example.invalid")
    git(repo, "config", "user.name", "Verify Fixture")
    git(repo, "config", "core.autocrlf", "false")
    write_file(repo, "agents/coder.py", CODER_SOURCE)
    write_file(repo, "agents/planner.py", PLANNER_SOURCE)
    write_file(repo, "agents/reviewer.py", REVIEWER_SOURCE)
    write_file(repo, "src/feature.py", FEATURE_V1)
    write_file(repo, "docs/note.md", NOTE_V1)
    base = commit_all(repo, "base")
    write_file(repo, "src/feature.py", FEATURE_V2)
    write_file(repo, "docs/note.md", NOTE_V2)
    head = commit_all(repo, "candidate change")
    return repo, base, head


def generate(root: Path, repo: Path, version: str, base: str, head: str) -> dict[str, Any]:
    return create_candidate(
        root,
        repo,
        version=version,
        diff_base_commit=base,
        source_commit=head,
        source_files=list(SOURCE_PATHS),
    )


def load_cli_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "phase28_archive_verify.py"
    spec = importlib.util.spec_from_file_location("phase28_archive_verify_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(
    cli: Any,
    tmp_path: Path,
    *,
    version: str | None = None,
    use_all: bool = False,
    archive_root: Path,
    repo: Path,
    json_name: str = "report.json",
    extra: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any]]:
    json_path = tmp_path / json_name
    args: list[str] = []
    if use_all:
        args.append("--all")
    else:
        assert version is not None
        args += ["--version", version]
    args += [
        "--archive-root",
        str(archive_root),
        "--repo",
        str(repo),
        "--json",
        str(json_path),
        *extra,
    ]
    exit_code = cli.main(args)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == exit_code
    return exit_code, payload


def entry_for(report: dict[str, Any], version: str) -> dict[str, Any]:
    matches = [entry for entry in report["versions"] if entry["version"] == version]
    assert len(matches) == 1, report["versions"]
    return matches[0]


def failed_checks(entry: dict[str, Any]) -> list[str]:
    return [check["check"] for check in entry["failures"]]


def test_generated_v2_archive_passes_every_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase28-t-ok", base, head)
    cli = load_cli_module()

    exit_code, report = verify(cli, tmp_path, version="phase28-t-ok", archive_root=root, repo=repo)
    capsys.readouterr()
    assert exit_code == 0
    entry = entry_for(report, "phase28-t-ok")
    assert entry["archive_class"] == "new" and entry["ok"] is True
    assert entry["failures"] == [] and entry["raw_drift"] == {}
    check_names = {check["check"] for check in entry["checks"]}
    assert check_names >= {
        "version_matches_directory",
        "snapshot_hash_rule_declared",
        "snapshot_ref_fixed",
        "snapshot_file_sha256",
        "snapshot_canonical_sha256",
        "diff_sha256",
        "diff_reverse_apply",
        "source_hashes",
        "source_files_count",
        "raw_worktree_hashes",
        "not_active",
    }
    assert report["totals"]["new_archive_failures"] == 0
    assert report["new_archive_status"] == "passed"
    # The verifier is read-only: no active pointer may ever appear.
    assert not (root / "active.json").exists()
    assert not (root / "active.next.json").exists()


def test_tampered_snapshot_fails_file_and_canonical_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase28-t-snap", base, head)["directory"])
    snapshot = json.loads(folder.joinpath("snapshot.json").read_text(encoding="utf-8"))
    snapshot["instruction"] = "tampered after generation"
    folder.joinpath("snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cli = load_cli_module()

    exit_code, report = verify(
        cli, tmp_path, version="phase28-t-snap", archive_root=root, repo=repo
    )
    capsys.readouterr()
    assert exit_code == 1
    entry = entry_for(report, "phase28-t-snap")
    assert set(failed_checks(entry)) >= {"snapshot_file_sha256", "snapshot_canonical_sha256"}
    assert report["new_archive_status"] == "failed"


def test_tampered_diff_fails_hash_and_reverse_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase28-t-diff", base, head)["directory"])
    diff_path = folder / "candidate.diff"
    # git apply ignores trailing garbage, so tamper with a real hunk body: the
    # byte hash must fail AND the patch must no longer reverse-apply.
    diff = diff_path.read_bytes()
    assert b"return 2" in diff
    diff_path.write_bytes(diff.replace(b"return 2", b"return 9"))
    cli = load_cli_module()

    exit_code, report = verify(
        cli, tmp_path, version="phase28-t-diff", archive_root=root, repo=repo
    )
    capsys.readouterr()
    assert exit_code == 1
    entry = entry_for(report, "phase28-t-diff")
    assert set(failed_checks(entry)) >= {"diff_sha256", "diff_reverse_apply"}


def test_worktree_change_breaks_reverse_apply_and_raw_hashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase28-t-work", base, head)
    write_file(repo, "docs/note.md", NOTE_V2 + "extra line after archival\n")
    cli = load_cli_module()

    exit_code, report = verify(
        cli, tmp_path, version="phase28-t-work", archive_root=root, repo=repo
    )
    capsys.readouterr()
    assert exit_code == 1
    entry = entry_for(report, "phase28-t-work")
    assert set(failed_checks(entry)) >= {"diff_reverse_apply", "raw_worktree_hashes"}
    candidate = json.loads(
        (root / "candidates" / "phase28-t-work" / "candidate.json").read_text(encoding="utf-8")
    )
    recorded = candidate["raw_worktree_hashes"]["docs/note.md"]
    assert entry["raw_drift"]["docs/note.md"] != recorded
    assert len(entry["raw_drift"]["docs/note.md"]) == 64
    assert entry["raw_drift_ignored"] is False


def test_raw_drift_is_ignorable_only_with_explicit_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    # diff_base == worktree state: empty diff, so reverse-apply stays trivially fine.
    generate(root, repo, "phase28-t-drift", head, head)
    write_file(repo, "docs/note.md", "drifted after archival\n")
    cli = load_cli_module()

    exit_code, report = verify(
        cli, tmp_path, version="phase28-t-drift", archive_root=root, repo=repo
    )
    capsys.readouterr()
    assert exit_code == 1
    assert failed_checks(entry_for(report, "phase28-t-drift")) == ["raw_worktree_hashes"]

    exit_code, report = verify(
        cli,
        tmp_path,
        version="phase28-t-drift",
        archive_root=root,
        repo=repo,
        json_name="report-ignored.json",
        extra=("--ignore-raw-drift",),
    )
    capsys.readouterr()
    assert exit_code == 0
    entry = entry_for(report, "phase28-t-drift")
    assert entry["raw_drift_ignored"] is True
    assert "docs/note.md" in entry["raw_drift"]
    assert failed_checks(entry) == []
    assert report["new_archive_status"] == "passed"


def test_missing_source_entry_and_worktree_file_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase28-t-src", base, head)["directory"])
    sources = json.loads(folder.joinpath("sources.json").read_text(encoding="utf-8"))
    del sources["src/feature.py"]
    folder.joinpath("sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cli = load_cli_module()

    exit_code, report = verify(cli, tmp_path, version="phase28-t-src", archive_root=root, repo=repo)
    capsys.readouterr()
    assert exit_code == 1
    entry = entry_for(report, "phase28-t-src")
    assert set(failed_checks(entry)) >= {"source_hashes", "source_files_count"}

    (repo / "src" / "feature.py").unlink()
    exit_code, report = verify(
        cli,
        tmp_path,
        version="phase28-t-src",
        archive_root=root,
        repo=repo,
        json_name="report-deleted.json",
    )
    capsys.readouterr()
    assert exit_code == 1
    entry = entry_for(report, "phase28-t-src")
    assert "raw_worktree_hashes" in failed_checks(entry)
    assert entry["raw_drift"]["src/feature.py"] == "missing from worktree"


def test_parent_integrity_drift_is_detected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase28-t-parent", base, head)
    child_summary = create_candidate(
        root,
        repo,
        version="phase28-t-child",
        parent="phase28-t-parent",
        diff_base_commit=base,
        source_commit=head,
        source_files=list(SOURCE_PATHS),
    )
    child_folder = Path(child_summary["directory"])
    child = json.loads(child_folder.joinpath("candidate.json").read_text(encoding="utf-8"))
    assert child["parent_integrity"] is not None
    child["parent_integrity"]["snapshot_file_sha256_actual"] = "0" * 64
    child_folder.joinpath("candidate.json").write_text(
        json.dumps(child, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cli = load_cli_module()

    exit_code, report = verify(
        cli, tmp_path, version="phase28-t-child", archive_root=root, repo=repo
    )
    capsys.readouterr()
    assert exit_code == 1
    assert "parent_integrity_matches" in failed_checks(entry_for(report, "phase28-t-child"))


def test_active_pointer_referencing_version_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase28-t-active", base, head)
    cli = load_cli_module()

    exit_code, report = verify(
        cli, tmp_path, version="phase28-t-active", archive_root=root, repo=repo
    )
    capsys.readouterr()
    assert exit_code == 0
    assert "not_active" not in failed_checks(entry_for(report, "phase28-t-active"))

    (root / "active.json").write_text(
        json.dumps({"version": "phase28-t-active", "new_tasks_only": True}) + "\n",
        encoding="utf-8",
    )
    exit_code, report = verify(
        cli,
        tmp_path,
        version="phase28-t-active",
        archive_root=root,
        repo=repo,
        json_name="report-active.json",
    )
    capsys.readouterr()
    assert exit_code == 1
    assert "not_active" in failed_checks(entry_for(report, "phase28-t-active"))

    (root / "active.json").write_text(
        json.dumps({"version": "some-other-version"}) + "\n", encoding="utf-8"
    )
    exit_code, report = verify(
        cli,
        tmp_path,
        version="phase28-t-active",
        archive_root=root,
        repo=repo,
        json_name="report-active-other.json",
    )
    capsys.readouterr()
    assert exit_code == 0
    # Still no pointer was created or removed by the verifier itself.
    assert (root / "active.json").is_file()


def _write_fake_old_archive(
    root: Path, version: str, snapshot: dict[str, Any], declared: str | None
) -> None:
    folder = root / "candidates" / version
    folder.mkdir(parents=True)
    raw = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    (folder / "snapshot.json").write_bytes(raw)
    candidate: dict[str, Any] = {"schema_version": 1, "version": version, "status": "pending"}
    if declared is not None:
        candidate["snapshot_sha256"] = declared
    (folder / "candidate.json").write_bytes(
        (json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def test_old_archive_rules_are_inferred_not_modified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "archive"
    snapshots = {
        "old-canonical": {"schema_version": 1, "version": "old-canonical", "status": "pending"},
        "old-filebytes": {"schema_version": 1, "version": "old-filebytes", "status": "pending"},
        "old-unexplained": {"schema_version": 1, "version": "old-unexplained", "status": "pending"},
        "old-nodecl": {"schema_version": 1, "version": "old-nodecl", "status": "pending"},
    }
    for version, snapshot in snapshots.items():
        raw = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        file_hash = hashlib.sha256(raw).hexdigest()
        canonical = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        declared = {
            "old-canonical": canonical,
            "old-filebytes": file_hash,
            "old-unexplained": "0" * 64,
            "old-nodecl": None,
        }[version]
        _write_fake_old_archive(root, version, snapshot, declared)
    cli = load_cli_module()

    exit_code, report = verify(cli, tmp_path, use_all=True, archive_root=root, repo=tmp_path)
    capsys.readouterr()
    # --all includes the deliberately unexplained archive, so the run fails...
    assert exit_code == 1
    rules = {entry["version"]: entry for entry in report["versions"]}
    assert rules["old-canonical"]["inferred_rule"] == "canonical_json_snapshot"
    assert rules["old-canonical"]["unexplained_mismatch"] is False
    assert rules["old-filebytes"]["inferred_rule"] == "file_bytes"
    assert rules["old-filebytes"]["unexplained_mismatch"] is False
    assert rules["old-nodecl"]["inferred_rule"] is None
    assert rules["old-nodecl"]["declared"] is None
    assert rules["old-nodecl"]["unexplained_mismatch"] is False

    unexplained = rules["old-unexplained"]
    assert unexplained["inferred_rule"] is None
    assert unexplained["unexplained_mismatch"] is True
    assert unexplained["declared"] == "0" * 64
    assert unexplained["snapshot_file_sha256"] not in (None, "0" * 64)
    assert unexplained["snapshot_canonical_sha256"] not in (None, "0" * 64)
    assert report["totals"]["old_unexplained_mismatches"] == 1
    assert report["exit_code"] == 1
    assert not list(root.glob("candidates/*/snapshot.json.orig"))  # nothing written back

    exit_code, report = verify(
        cli,
        tmp_path,
        version="old-filebytes",
        archive_root=root,
        repo=tmp_path,
        json_name="report-single-old.json",
    )
    capsys.readouterr()
    assert exit_code == 0
    assert [entry["version"] for entry in report["versions"]] == ["old-filebytes"]


def test_unexplained_old_mismatch_fails_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "archive"
    _write_fake_old_archive(root, "old-bad", {"schema_version": 1, "version": "old-bad"}, "f" * 64)
    cli = load_cli_module()
    exit_code, report = verify(cli, tmp_path, version="old-bad", archive_root=root, repo=tmp_path)
    capsys.readouterr()
    assert exit_code == 1
    entry = entry_for(report, "old-bad")
    assert entry["unexplained_mismatch"] is True
    assert set(entry) >= {"declared", "snapshot_file_sha256", "snapshot_canonical_sha256"}


def test_old_archive_git_blob_evidence_and_historical_files_modified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "verify@example.invalid")
    git(repo, "config", "user.name", "Verify Fixture")
    git(repo, "config", "core.autocrlf", "false")
    snapshot = {"schema_version": 1, "version": "old-committed", "status": "pending"}
    _write_fake_old_archive(repo / "configs" / "harness", "old-committed", snapshot, None)
    commit_all(repo, "archive historical candidate")
    cli = load_cli_module()

    exit_code, report = verify(
        cli, tmp_path, version="old-committed", archive_root=repo / "configs" / "harness", repo=repo
    )
    capsys.readouterr()
    assert exit_code == 0
    entry = entry_for(report, "old-committed")
    assert entry["declared"] is None and entry["inferred_rule"] is None
    snapshot_path = repo / "configs" / "harness" / "candidates" / "old-committed" / "snapshot.json"
    assert entry["git_blob_sha1"] == git(repo, "hash-object", str(snapshot_path)).decode().strip()
    assert entry["baseline_blob_commit"]
    assert (
        entry["baseline_git_blob_sha256"] == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    )
    assert entry["baseline_blob_matches_file"] is True
    assert report["totals"]["historical_files_modified"] is False

    snapshot_path.write_bytes(b'{"schema_version": 1, "tampered": true}\n')
    exit_code, report = verify(
        cli,
        tmp_path,
        version="old-committed",
        archive_root=repo / "configs" / "harness",
        repo=repo,
        json_name="report-tampered-old.json",
    )
    capsys.readouterr()
    assert exit_code == 1
    entry = entry_for(report, "old-committed")
    assert report["totals"]["historical_files_modified"] is True
    assert entry["baseline_blob_matches_file"] is False
    # No declared hash exists, so the mismatch is attributed by the git
    # evidence (baseline blob + git status), not by an unexplained declaration.
    assert entry["unexplained_mismatch"] is False
    assert entry["declared"] is None


def test_archive_outside_git_repo_annotates_skipped_blob_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "plain-archive"
    _write_fake_old_archive(root, "old-plain", {"schema_version": 1, "version": "old-plain"}, None)
    cli = load_cli_module()
    exit_code, report = verify(
        cli, tmp_path, version="old-plain", archive_root=root, repo=tmp_path / "nowhere"
    )
    capsys.readouterr()
    assert exit_code == 0
    entry = entry_for(report, "old-plain")
    assert entry["baseline_blob_commit"] is None
    assert entry["baseline_git_blob_sha256"] is None
    assert report["totals"]["historical_files_modified"] is None
    assert any("git" in note.lower() for note in entry["notes"])


def test_usage_errors_and_missing_versions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = load_cli_module()
    root = tmp_path / "archive"
    root.mkdir()
    (root / "candidates").mkdir()

    assert cli.main([]) == 2
    assert cli.main(["--all", "--version", "x"]) == 2
    assert cli.main(["--all", "--archive-root", str(tmp_path / "void")]) == 2
    assert cli.main(["--all", "--archive-root", str(root), "--repo", str(tmp_path)]) == 2  # empty
    capsys.readouterr()

    code = cli.main(["--version", "ghost", "--archive-root", str(root), "--repo", str(tmp_path)])
    capsys.readouterr()
    assert code == 1

    # --all only visits directories, so a plain file under candidates/ yields an
    # empty selection (usage error, no report file); targeting it explicitly via
    # --version produces a skipped unknown entry instead.
    (root / "candidates" / "a-file").write_text("not a directory\n", encoding="utf-8")
    assert cli.main(["--all", "--archive-root", str(root), "--repo", str(tmp_path)]) == 2
    assert not (tmp_path / "report.json").exists()
    capsys.readouterr()

    code, report = verify(cli, tmp_path, version="a-file", archive_root=root, repo=tmp_path)
    capsys.readouterr()
    assert code == 1
    assert report["skipped_entries"] == [
        {"version": "a-file", "reason": "candidates/<version> is not a directory"}
    ]
    entry = entry_for(report, "a-file")
    assert entry["archive_class"] == "unknown" and entry["ok"] is False
