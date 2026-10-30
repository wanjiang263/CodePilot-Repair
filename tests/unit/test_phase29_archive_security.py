"""Phase29 adversarial archive matrix: generator refusals and the tamper grid.

Every case asserts the exact failing check name(s) from
docs/notes/phase29-archive-schema.md §4 — never a bare non-zero exit. Zero
network, zero Docker, zero model calls; throwaway git repositories only.
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
    DirtyWorktreeError,
    NonAncestorDiffBaseError,
    SourceOriginError,
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
    git(repo, "config", "user.email", "sec@example.invalid")
    git(repo, "config", "user.name", "Security Fixture")
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
    spec = importlib.util.spec_from_file_location("phase29_security_verify", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_manifest_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "phase29_baseline_manifest.py"
    spec = importlib.util.spec_from_file_location("phase29_security_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(
    cli: Any,
    tmp_path: Path,
    *,
    version: str,
    archive_root: Path,
    repo: Path,
    json_name: str = "report.json",
    extra: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any]]:
    json_path = tmp_path / json_name
    args = ["--version", version, "--archive-root", str(archive_root), "--repo", str(repo)]
    args += ["--json", str(json_path), *extra]
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


def write_candidate_json(folder: Path, payload: dict[str, Any], *, reseal: bool) -> None:
    """Write candidate.json; with reseal, recompute the payload seal first."""
    if reseal:
        payload.pop("candidate_payload_sha256", None)
        payload["candidate_payload_sha256"] = canonical_sha256(payload)
    (folder / "candidate.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_candidate(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "candidate.json").read_text(encoding="utf-8"))


def assert_fails_with(
    tmp_path: Path,
    root: Path,
    repo: Path,
    version: str,
    expected: set[str],
    *,
    json_name: str = "report.json",
    extra: tuple[str, ...] = (),
    forbidden: set[str] = frozenset(),
) -> dict[str, Any]:
    cli = load_cli_module()
    exit_code, report = verify(
        cli,
        tmp_path,
        version=version,
        archive_root=root,
        repo=repo,
        json_name=json_name,
        extra=extra,
    )
    assert exit_code == 1, report
    entry = entry_for(report, version)
    assert entry["ok"] is False
    actual = set(failed_checks(entry))
    assert expected <= actual, f"expected {expected}, got {actual}"
    assert actual.isdisjoint(forbidden), f"unexpected failures {actual & forbidden}"
    return report


# ---------------------------------------------------------------- 生成器拦截


def test_generator_rejects_unresolvable_source_commit(tmp_path: Path) -> None:
    repo, base, _ = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(Exception, match="does not resolve to a commit"):
        create_candidate(
            root,
            repo,
            version="sec-g1",
            diff_base_commit=base,
            source_commit="0" * 40,
            source_files=list(SOURCE_PATHS),
        )
    assert not (root / "candidates" / "sec-g1").exists()


def test_generator_rejects_non_ancestor_diff_base(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(NonAncestorDiffBaseError, match="not an ancestor"):
        generate(root, repo, "sec-g2", head, base)
    assert not (root / "candidates" / "sec-g2").exists()


def test_generator_rejects_dirty_worktree_in_git_mode(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    write_file(repo, "src/feature.py", "def feature() -> int:\n    return 8\n")
    root = tmp_path / "archive"
    with pytest.raises(DirtyWorktreeError):
        generate(root, repo, "sec-g3", base, head)
    assert not (root / "candidates" / "sec-g3").exists()


def test_generator_rejects_allow_dirty_outside_worktree_mode(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    write_file(repo, "docs/note.md", "dirty\n")
    root = tmp_path / "archive"
    with pytest.raises(SourceOriginError, match="worktree"):
        create_candidate(
            root,
            repo,
            version="sec-g4",
            diff_base_commit=base,
            source_commit=head,
            source_files=list(SOURCE_PATHS),
            allow_dirty=True,
        )
    assert not (root / "candidates" / "sec-g4").exists()


def test_generator_rejects_path_escaping_repo(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(Exception, match="repo-relative"):
        create_candidate(
            root,
            repo,
            version="sec-g5",
            diff_base_commit=base,
            source_commit=head,
            source_files=["../outside.py"],
        )
    assert not (root / "candidates" / "sec-g5").exists()


# ---------------------------------------------------------------- 篡改矩阵


def build_tamper_fixture(tmp_path: Path, version: str) -> tuple[Path, Path, str, str, Path]:
    root = tmp_path / "archive"
    repo, base, head = build_repo(tmp_path)
    folder = Path(generate(root, repo, version, base, head)["directory"])
    return root, repo, base, head, folder


def test_v1_status_flip_without_reseal_breaks_payload_seal(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t1")
    payload = load_candidate(folder)
    payload["status"] = "active"
    write_candidate_json(folder, payload, reseal=False)
    assert_fails_with(tmp_path, root, repo, "sec-t1", {"candidate_payload_sha256"})


def test_v2_parent_swap_without_reseal_breaks_payload_seal(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t2")
    payload = load_candidate(folder)
    payload["parent"] = "ghost-candidate"
    write_candidate_json(folder, payload, reseal=False)
    assert_fails_with(tmp_path, root, repo, "sec-t2", {"candidate_payload_sha256"})


def test_v3_diff_base_swap_without_reseal_breaks_payload_seal(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t3")
    payload = load_candidate(folder)
    payload["diff_base_commit"] = "1" * 40
    write_candidate_json(folder, payload, reseal=False)
    assert_fails_with(tmp_path, root, repo, "sec-t3", {"candidate_payload_sha256"})


def test_v4_seal_replacement_itself_is_detected(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t4")
    payload = load_candidate(folder)
    payload["candidate_payload_sha256"] = "f" * 64
    write_candidate_json(folder, payload, reseal=False)
    assert_fails_with(tmp_path, root, repo, "sec-t4", {"candidate_payload_sha256"})


def test_v5_resealed_source_commit_swap_breaks_blob_binding(tmp_path: Path) -> None:
    root, repo, base, _head, folder = build_tamper_fixture(tmp_path, "sec-t5")
    payload = load_candidate(folder)
    payload["source_commit"] = base
    write_candidate_json(folder, payload, reseal=True)
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t5",
        {"source_commit_blob"},
        forbidden={"candidate_payload_sha256"},
    )


def test_v6_resealed_bogus_source_commit_breaks_resolution(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t6")
    payload = load_candidate(folder)
    payload["source_commit"] = "d" * 40
    write_candidate_json(folder, payload, reseal=True)
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t6",
        {"source_commit_resolves", "source_commit_ancestor", "source_commit_blob"},
    )


def test_v7_resealed_descendant_diff_base_breaks_ancestor_check(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t7")
    payload = load_candidate(folder)
    # Swap the roles: the claimed base is now the descendant of the claimed source.
    payload["diff_base_commit"], payload["source_commit"] = (
        payload["source_commit"],
        payload["diff_base_commit"],
    )
    write_candidate_json(folder, payload, reseal=True)
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t7",
        {"source_commit_ancestor"},
        forbidden={"candidate_payload_sha256"},
    )


def test_v8_consistent_sources_and_hash_drift_breaks_blob_check(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t8")
    forged = "def feature() -> int:\n    return 999\n"
    sources_path = folder / "sources.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    sources["src/feature.py"] = forged
    sources_path.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    payload = load_candidate(folder)
    payload["source_hashes"]["src/feature.py"] = hashlib.sha256(forged.encode("utf-8")).hexdigest()
    write_candidate_json(folder, payload, reseal=True)
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t8",
        {"source_commit_blob", "diff_forward_apply"},
        forbidden={"source_hashes", "candidate_payload_sha256"},
    )


def test_v9_resealed_base_hash_drift_breaks_base_blob_check(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t9")
    payload = load_candidate(folder)
    payload["base_hashes"]["docs/note.md"] = "0" * 64
    write_candidate_json(folder, payload, reseal=True)
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t9",
        {"base_commit_blob"},
        forbidden={"candidate_payload_sha256"},
    )


def test_v10_corrupted_diff_breaks_forward_and_reverse_apply(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t10")
    diff_path = folder / "candidate.diff"
    diff = diff_path.read_bytes()
    assert b"return 2" in diff
    diff_path.write_bytes(diff.replace(b"return 2", b"return 7"))
    payload = load_candidate(folder)
    payload["diff_sha256"] = hashlib.sha256(diff_path.read_bytes()).hexdigest()
    write_candidate_json(folder, payload, reseal=True)
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t10",
        {"diff_forward_apply", "diff_reverse_apply"},
        forbidden={"diff_sha256"},
    )


def test_v11_snapshot_tamper_breaks_both_snapshot_rules(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t11")
    snapshot_path = folder / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["instruction"] = "tampered"
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t11",
        {"snapshot_file_sha256", "snapshot_canonical_sha256"},
    )


def test_v12_sources_entry_deletion_breaks_hash_and_count(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t12")
    sources_path = folder / "sources.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    del sources["docs/note.md"]
    sources_path.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t12",
        {"source_hashes", "source_files_count"},
    )


def test_v13_worktree_dirty_flag_flip_is_cross_checked(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    repo, base, _head = build_repo(tmp_path)
    write_file(repo, "src/feature.py", "def feature() -> int:\n    return 42\n")
    folder = Path(
        create_candidate(
            root,
            repo,
            version="sec-t13",
            diff_base_commit=base,
            source_files=list(SOURCE_PATHS),
            source_origin="worktree",
        )["directory"]
    )
    payload = load_candidate(folder)
    payload["dirty_worktree"] = False  # lie about the recorded dirty state
    write_candidate_json(folder, payload, reseal=True)
    assert_fails_with(
        tmp_path,
        root,
        repo,
        "sec-t13",
        {"origin_dirty_consistency"},
        forbidden={"candidate_payload_sha256"},
    )
    git(repo, "checkout", "--", "src/feature.py")


def test_v14_committed_historical_tamper_breaks_baseline_check(tmp_path: Path) -> None:
    repo, base, head = build_repo(tmp_path)
    archive_root = repo / "_archive"
    folder = Path(generate(archive_root, repo, "sec-t14", base, head)["directory"])
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "archive")
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
    git(repo, "commit", "-m", "freeze")
    # modify + commit: git status stays clean, last-touching commit moves —
    # only the baseline manifest can see this.
    payload = load_candidate(folder)
    payload["status"] = "active"
    write_candidate_json(folder, payload, reseal=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "committed tamper")
    cli = load_cli_module()
    exit_code, report = verify(
        cli,
        tmp_path,
        version="sec-t14",
        archive_root=archive_root,
        repo=repo,
        json_name="report-baseline.json",
    )
    # The candidate-level checks all pass (the tamper was resealed); only the
    # report-level historical baseline catches the committed modification.
    assert exit_code == 1
    hb = report["historical_baseline"]
    assert hb["ok"] is False
    assert hb["files_checked"] >= 4
    assert any("candidate.json" in rel for rel in hb["mismatches"])
    assert report["totals"]["historical_files_modified"] is False


def test_v15_all_tamper_runs_leave_the_archive_read_only(tmp_path: Path) -> None:
    root, repo, _base, _head, folder = build_tamper_fixture(tmp_path, "sec-t15")
    cli = load_cli_module()
    exit_code, _ = verify(cli, tmp_path, version="sec-t15", archive_root=root, repo=repo)
    assert exit_code == 0
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    # one tamper + verify round: the verifier must not "repair" or rewrite anything
    payload = load_candidate(folder)
    payload["status"] = "active"
    write_candidate_json(folder, payload, reseal=False)
    assert_fails_with(tmp_path, root, repo, "sec-t15", {"candidate_payload_sha256"})
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    assert sorted(before) == sorted(after)
    assert not (root / "active.json").exists()
    assert not (root / "active.next.json").exists()
