"""Phase29 archive verifier tests: schema v3 source binding and tamper matrix.

Zero network, zero Docker, zero model calls: every test drives a throwaway git
repository and a throwaway archive root under ``tmp_path``. Tamper cases assert
specific check names (docs/notes/phase29-archive-schema.md §4), never a bare
non-zero exit.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

from codepilot.archive import canonical_sha256, create_candidate

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
    spec = importlib.util.spec_from_file_location("phase29_archive_verify_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(
    cli: Any,
    tmp_path: Path,
    *,
    version: str | None = None,
    archive_root: Path,
    repo: Path,
    json_name: str = "report.json",
    extra: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any]]:
    json_path = tmp_path / json_name
    args = ["--version", version, "--archive-root", str(archive_root), "--repo", str(repo)]
    if json_path is not None:
        args += ["--json", str(json_path)]
    exit_code = cli.main([*args, *extra])
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == exit_code
    return exit_code, payload


def entry_for(report: dict[str, Any], version: str) -> dict[str, Any]:
    matches = [entry for entry in report["versions"] if entry["version"] == version]
    assert len(matches) == 1, report["versions"]
    return matches[0]


def failed_checks(entry: dict[str, Any]) -> list[str]:
    return [check["check"] for check in entry["failures"]]


def rewrite_candidate(folder: Path, mutate: Any) -> None:
    """Apply ``mutate`` to candidate.json and re-seal candidate_payload_sha256."""
    path = folder / "candidate.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    payload.pop("candidate_payload_sha256", None)
    payload["candidate_payload_sha256"] = canonical_sha256(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_generated_v3_archive_passes_every_rule(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase29-t-ok", base, head)
    cli = load_cli_module()

    exit_code, report = verify(cli, tmp_path, version="phase29-t-ok", archive_root=root, repo=repo)
    assert exit_code == 0
    entry = entry_for(report, "phase29-t-ok")
    assert entry["archive_class"] == "new" and entry["ok"] is True
    check_names = {check["check"] for check in entry["checks"]}
    assert check_names >= {
        "version_matches_directory",
        "snapshot_hash_rule_declared",
        "snapshot_ref_fixed",
        "snapshot_file_sha256",
        "snapshot_canonical_sha256",
        "candidate_payload_sha256",
        "source_origin_declared",
        "origin_dirty_consistency",
        "source_commit_resolves",
        "source_commit_ancestor",
        "source_commit_blob",
        "base_commit_blob",
        "diff_sha256",
        "diff_forward_apply",
        "diff_reverse_apply",
        "source_hashes",
        "source_files_count",
        "not_active",
    }
    assert report["new_archive_status"] == "passed"
    assert not (root / "active.json").exists()


def test_v3_status_tamper_breaks_payload_seal(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase29-t-status", base, head)["directory"])
    payload = json.loads((folder / "candidate.json").read_text(encoding="utf-8"))
    payload["status"] = "active"
    (folder / "candidate.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    cli = load_cli_module()
    exit_code, report = verify(
        cli, tmp_path, version="phase29-t-status", archive_root=root, repo=repo
    )
    assert exit_code == 1
    assert failed_checks(entry_for(report, "phase29-t-status")) == ["candidate_payload_sha256"]


def test_v3_source_commit_tamper_breaks_seal_and_blob_binding(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase29-t-sc", base, head)["directory"])
    # Variant A: plain field swap without re-sealing -> seal check fires.
    payload = json.loads((folder / "candidate.json").read_text(encoding="utf-8"))
    payload["source_commit"] = base
    (folder / "candidate.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    cli = load_cli_module()
    exit_code, report = verify(cli, tmp_path, version="phase29-t-sc", archive_root=root, repo=repo)
    assert exit_code == 1
    assert "candidate_payload_sha256" in failed_checks(entry_for(report, "phase29-t-sc"))

    # Variant B: swap + re-seal -> the blob binding must catch it instead.
    rewrite_candidate(folder, lambda p: p.update(source_commit=base))
    exit_code, report = verify(cli, tmp_path, version="phase29-t-sc", archive_root=root, repo=repo)
    assert exit_code == 1
    failures = failed_checks(entry_for(report, "phase29-t-sc"))
    assert "candidate_payload_sha256" not in failures
    assert "source_commit_blob" in failures


def test_v3_missing_source_origin_fails(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase29-t-origin", base, head)["directory"])
    rewrite_candidate(folder, lambda p: p.pop("source_origin"))
    cli = load_cli_module()
    exit_code, report = verify(
        cli, tmp_path, version="phase29-t-origin", archive_root=root, repo=repo
    )
    assert exit_code == 1
    assert "source_origin_declared" in failed_checks(entry_for(report, "phase29-t-origin"))


def test_v3_non_ancestor_diff_base_detected(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase29-t-anc", base, head)["directory"])
    # diff_base == head is not an ancestor of source_commit == head? It is (equal).
    # Move diff_base forward instead: swap source_commit and diff_base_commit so
    # the claimed base is the descendant.
    rewrite_candidate(
        folder,
        lambda p: p.update(
            diff_base_commit=p["source_commit"], source_commit=p["diff_base_commit"]
        ),
    )
    cli = load_cli_module()
    exit_code, report = verify(cli, tmp_path, version="phase29-t-anc", archive_root=root, repo=repo)
    assert exit_code == 1
    failures = failed_checks(entry_for(report, "phase29-t-anc"))
    assert "source_commit_ancestor" in failures
    assert "source_commit_blob" in failures


def test_v3_source_hashes_drift_breaks_blob_check(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase29-t-src", base, head)["directory"])
    # Tamper sources.json consistently with source_hashes (re-sealed): the
    # blob check is the only rule that can still catch it.
    sources_path = folder / "sources.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    sources["src/feature.py"] = "def feature() -> int:\n    return 999\n"
    sources_path.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    def mutate(p: dict[str, Any]) -> None:
        p["source_hashes"]["src/feature.py"] = hashlib.sha256(
            sources["src/feature.py"].encode("utf-8")
        ).hexdigest()

    rewrite_candidate(folder, mutate)
    cli = load_cli_module()
    exit_code, report = verify(cli, tmp_path, version="phase29-t-src", archive_root=root, repo=repo)
    assert exit_code == 1
    failures = failed_checks(entry_for(report, "phase29-t-src"))
    assert "source_hashes" not in failures
    assert "source_commit_blob" in failures


def test_v3_base_hashes_drift_breaks_base_blob_check(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase29-t-base", base, head)["directory"])
    rewrite_candidate(folder, lambda p: p["base_hashes"].update({"src/feature.py": "0" * 64}))
    cli = load_cli_module()
    exit_code, report = verify(
        cli, tmp_path, version="phase29-t-base", archive_root=root, repo=repo
    )
    assert exit_code == 1
    assert "base_commit_blob" in failed_checks(entry_for(report, "phase29-t-base"))


def test_v3_corrupted_diff_breaks_forward_and_reverse_apply(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    folder = Path(generate(root, repo, "phase29-t-diff", base, head)["directory"])
    diff_path = folder / "candidate.diff"
    diff = diff_path.read_bytes()
    assert b"return 2" in diff
    diff_path.write_bytes(diff.replace(b"return 2", b"return 7"))

    def mutate(p: dict[str, Any]) -> None:
        p["diff_sha256"] = hashlib.sha256(diff_path.read_bytes()).hexdigest()

    rewrite_candidate(folder, mutate)
    cli = load_cli_module()
    exit_code, report = verify(
        cli, tmp_path, version="phase29-t-diff", archive_root=root, repo=repo
    )
    assert exit_code == 1
    failures = failed_checks(entry_for(report, "phase29-t-diff"))
    assert "diff_forward_apply" in failures
    assert "diff_reverse_apply" in failures


def test_v3_worktree_mode_verifies_and_drift_is_detected(tmp_path: Path) -> None:
    repo, base, _head = build_repo(tmp_path)
    root = tmp_path / "archive"
    write_file(repo, "src/feature.py", "def feature() -> int:\n    return 42\n")
    create_candidate(
        root,
        repo,
        version="phase29-t-wt",
        diff_base_commit=base,
        source_files=list(SOURCE_PATHS),
        source_origin="worktree",
    )
    cli = load_cli_module()
    exit_code, report = verify(cli, tmp_path, version="phase29-t-wt", archive_root=root, repo=repo)
    assert exit_code == 0
    entry = entry_for(report, "phase29-t-wt")
    assert entry["ok"] is True
    check_names = {check["check"] for check in entry["checks"]}
    assert {"raw_worktree_hashes", "diff_forward_apply", "diff_reverse_apply"} <= check_names

    # Phase31: explicit strict drift fails raw hashes; archive apply stays valid.
    write_file(repo, "src/feature.py", "def feature() -> int:\n    return 43\n")
    exit_code, report = verify(
        cli,
        tmp_path,
        version="phase29-t-wt",
        archive_root=root,
        repo=repo,
        extra=("--strict-raw-drift",),
    )
    assert exit_code == 1
    failures = failed_checks(entry_for(report, "phase29-t-wt"))
    assert "raw_worktree_hashes" in failures
    assert "diff_reverse_apply" not in failures
    assert entry_for(report, "phase29-t-wt")["status"] == "legacy_non_strict"
    git(repo, "checkout", "--", "src/feature.py")


def test_v3_git_commit_mode_ignores_later_worktree_drift(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase29-t-clean", base, head)
    write_file(repo, "src/feature.py", "def feature() -> int:\n    return 12345\n")
    cli = load_cli_module()
    exit_code, report = verify(
        cli, tmp_path, version="phase29-t-clean", archive_root=root, repo=repo
    )
    assert exit_code == 0, report
    entry = entry_for(report, "phase29-t-clean")
    assert entry["ok"] is True
    assert "raw_worktree_hashes" not in entry
    git(repo, "checkout", "--", "src/feature.py")


def test_v3_verifier_is_read_only(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate(root, repo, "phase29-t-readonly", base, head)
    cli = load_cli_module()
    folder = root / "candidates" / "phase29-t-readonly"
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    exit_code, _ = verify(cli, tmp_path, version="phase29-t-readonly", archive_root=root, repo=repo)
    assert exit_code == 0
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    assert before == after
    assert sorted(p.name for p in root.iterdir()) == ["candidates"]


def load_manifest_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "phase29_baseline_manifest.py"
    spec = importlib.util.spec_from_file_location("phase29_baseline_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_historical_baseline_detects_committed_tamper(tmp_path: Path) -> None:
    """A historical candidate file modified AND committed must fail the verifier.

    The manifest check is what catches this: git status stays clean and the
    last-touching commit moves to the tamper commit, so neither heuristic that
    Phase28 relied on can see it.
    """
    repo, base, head = build_repo(tmp_path)
    archive_root = repo / "_archive"
    folder = Path(
        create_candidate(
            archive_root,
            repo,
            version="hist-v1",
            diff_base_commit=base,
            source_commit=head,
            source_files=["src/feature.py"],
        )["directory"]
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "archive candidate")
    manifest_mod = load_manifest_module()
    manifest_path = archive_root / "candidates" / "historical-baseline.json"
    assert (
        manifest_mod.main(
            [
                "--archive-root",
                str(archive_root),
                "--repo",
                str(repo),
                "--baseline-commit",
                base,
                "--output",
                str(manifest_path),
            ]
        )
        == 0
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "freeze manifest")

    verify_cli = load_cli_module()
    exit_code, report = verify(
        verify_cli, tmp_path, version="hist-v1", archive_root=archive_root, repo=repo
    )
    assert exit_code == 0
    assert report["historical_baseline"]["ok"] is True

    candidate_path = folder / "candidate.json"
    payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    payload["status"] = "active"
    candidate_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "tamper historical candidate and commit it")

    exit_code, report = verify(
        verify_cli,
        tmp_path,
        version="hist-v1",
        archive_root=archive_root,
        repo=repo,
        json_name="report-tamper.json",
    )
    assert exit_code == 1
    hb = report["historical_baseline"]
    assert hb["ok"] is False
    assert hb["files_checked"] >= 4
    assert any("candidate.json" in rel for rel in hb["mismatches"])
    # The git-status heuristic cannot see a committed tamper; the manifest can.
    assert report["totals"]["historical_files_modified"] is False


def test_manifest_generator_records_baseline_and_refuses_drift(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    archive_root = repo / "_archive"
    create_candidate(
        archive_root,
        repo,
        version="hist-v2",
        diff_base_commit=base,
        source_commit=head,
        source_files=["src/feature.py", "docs/note.md"],
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "candidate")
    mid = git(repo, "rev-parse", "HEAD").decode("utf-8").strip()
    manifest_mod = load_manifest_module()

    manifest, drift = manifest_mod.build_manifest(archive_root, repo, baseline_commit=mid)
    assert drift == [] and manifest["zero_change_since_baseline"] is True
    by_path = {entry["path"]: entry for entry in manifest["files"]}
    assert "historical-baseline.json" not in by_path
    assert by_path["candidates/hist-v2/candidate.json"]["baseline_commit_sha256"] is not None

    # Against the pre-candidate baseline the file did not exist -> null blob.
    manifest2, _ = manifest_mod.build_manifest(archive_root, repo, baseline_commit=base)
    by_path2 = {entry["path"]: entry for entry in manifest2["files"]}
    assert by_path2["candidates/hist-v2/candidate.json"]["baseline_commit_sha256"] is None

    # Drifted current bytes: build reports it; main refuses to bless the state.
    candidate_path = archive_root / "candidates" / "hist-v2" / "candidate.json"
    candidate_path.write_bytes(candidate_path.read_bytes() + b" ")
    _, drift3 = manifest_mod.build_manifest(archive_root, repo, baseline_commit=mid)
    assert drift3 and "candidate.json" in drift3[0]
    output = archive_root / "candidates" / "historical-baseline.json"
    assert not output.exists()
    assert (
        manifest_mod.main(
            [
                "--archive-root",
                str(archive_root),
                "--repo",
                str(repo),
                "--baseline-commit",
                mid,
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()
    assert (
        manifest_mod.main(
            [
                "--archive-root",
                str(archive_root),
                "--repo",
                str(repo),
                "--baseline-commit",
                mid,
                "--output",
                str(output),
                "--allow-drift",
            ]
        )
        == 0
    )
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["zero_change_since_baseline"] is False
    assert written["baseline_drift"]
