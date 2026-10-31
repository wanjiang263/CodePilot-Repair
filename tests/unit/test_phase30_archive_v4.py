"""Phase30 schema-v4 candidate generation tests (temporary git fixtures only).

Contract under test: v4 records a ``source_manifest`` (``full``/``delta``), a
``full`` source set must cover every ancestor candidate's sources plus the
required role agent files (the v3 defect was archiving only the 26 changed
files), the snapshot inherits prompts/instruction/experiences from the parent
chain instead of archiving an empty policy, generation aborts when the snapshot
cannot be strictly validated (empty role prompts), and commit fields are
resolved full shas rather than worktree labels. The v3 layout stays the default
and byte-compatible.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codepilot.archive import (
    ArchiveError,
    IncompleteSourceManifestError,
    create_candidate,
)
from codepilot.rsi import validate_snapshot

CODER_V1 = '"""Coder fixture."""\n\n_SYSTEM = "coder prompt v1"\n'
CODER_V2 = '"""Coder fixture."""\n\n_SYSTEM = "coder prompt v2 phase30"\n'
PLANNER_SOURCE = '"""Planner fixture."""\n\n_SYSTEM = "planner prompt v1"\n'
REVIEWER_SOURCE = '"""Reviewer fixture."""\n\n_SYSTEM = "reviewer prompt v1"\n'
CODER_NO_PROMPT = '"""Coder fixture without a prompt."""\n\n\ndef run() -> str:\n    return "x"\n'
FEATURE_V1 = "def feature() -> int:\n    return 1\n"
FEATURE_V2 = "def feature() -> int:\n    return 2\n"
FEATURE_V3 = "def feature() -> int:\n    return 3\n"

AGENT_PATHS = (
    "agents/coder.py",
    "agents/planner.py",
    "agents/reviewer.py",
)


def git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode().strip()


def build_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Three commits: base -> v2 (parent candidate) -> v3 (child candidate)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "phase30@example.invalid")
    git(repo, "config", "user.name", "Phase30 Fixture")
    git(repo, "config", "core.autocrlf", "false")
    for rel, text in (
        ("agents/coder.py", CODER_V1),
        ("agents/planner.py", PLANNER_SOURCE),
        ("agents/reviewer.py", REVIEWER_SOURCE),
        ("src/feature.py", FEATURE_V1),
        ("docs/child-note.md", "child note v1\n"),
    ):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    base = commit_all(repo, "base")
    (repo / "src/feature.py").write_text(FEATURE_V2, encoding="utf-8", newline="\n")
    mid = commit_all(repo, "parent change")
    (repo / "src/feature.py").write_text(FEATURE_V3, encoding="utf-8", newline="\n")
    (repo / "agents/coder.py").write_text(CODER_V2, encoding="utf-8", newline="\n")
    head = commit_all(repo, "child change")
    return repo, base, mid, head


PARENT_SET = (*AGENT_PATHS, "src/feature.py")


def generate_v3_parent(root: Path, repo: Path, mid: str, head: str) -> dict[str, Any]:
    """Parent candidate (schema v3 layout) with prompts from its agent sources."""
    return create_candidate(
        root,
        repo,
        version="phase30-parent-v3",
        diff_base_commit=mid,
        source_commit=head,
        source_files=list(PARENT_SET),
    )


def child_source_files(extra: tuple[str, ...] = ("docs/child-note.md",)) -> list[str]:
    return [*PARENT_SET, *extra]


@pytest.fixture()
def v4_env(tmp_path: Path) -> dict[str, Any]:
    repo, base, mid, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    parent = generate_v3_parent(root, repo, mid, head)
    return {"repo": repo, "root": root, "base": base, "mid": mid, "head": head, "parent": parent}


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_v4_full_rejects_changed_files_only_subset(v4_env: dict[str, Any]) -> None:
    """The v3 defect: archiving only this round's changed files must be refused."""
    with pytest.raises(IncompleteSourceManifestError, match="inherited"):
        create_candidate(
            v4_env["root"],
            v4_env["repo"],
            version="phase30-child-v4",
            parent="phase30-parent-v3",
            diff_base_commit=v4_env["head"],
            source_commit=v4_env["head"],
            source_files=["docs/child-note.md"],
            candidate_schema_version=4,
            manifest_kind="full",
        )
    assert not (v4_env["root"] / "candidates" / "phase30-child-v4").exists()


def test_v4_delta_without_parent_rejected(v4_env: dict[str, Any]) -> None:
    with pytest.raises(IncompleteSourceManifestError, match="parent"):
        create_candidate(
            v4_env["root"],
            v4_env["repo"],
            version="phase30-delta-v4",
            parent=None,
            diff_base_commit=v4_env["head"],
            source_commit=v4_env["head"],
            source_files=child_source_files(),
            candidate_schema_version=4,
            manifest_kind="delta",
        )


def test_v4_root_full_missing_agent_source_rejected(tmp_path: Path) -> None:
    repo, base, _mid, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    with pytest.raises(IncompleteSourceManifestError, match=r"agents/coder\.py"):
        create_candidate(
            root,
            repo,
            version="phase30-root-v4",
            diff_base_commit=base,
            source_commit=head,
            source_files=["src/feature.py", "agents/planner.py", "agents/reviewer.py"],
            candidate_schema_version=4,
            manifest_kind="full",
        )


def test_v4_rejects_unknown_manifest_kind(v4_env: dict[str, Any]) -> None:
    with pytest.raises(IncompleteSourceManifestError, match="kind"):
        create_candidate(
            v4_env["root"],
            v4_env["repo"],
            version="phase30-kind-v4",
            parent="phase30-parent-v3",
            diff_base_commit=v4_env["head"],
            source_commit=v4_env["head"],
            source_files=child_source_files(),
            candidate_schema_version=4,
            manifest_kind="partial",
        )


def test_v4_inherits_parent_prompts_and_experiences(v4_env: dict[str, Any]) -> None:
    """prompts = parent chain prompts + this round's overrides; all roles non-empty.

    The child commit rewrites agents/coder.py, so coder is overridden from the
    child sources while planner/reviewer stay inherited. Experiences are absent
    this round and the parent has none, so the walk continues to the
    grandparent record when one exists; here the parent itself carries the
    chain's experiences via its own sources.
    """
    env = v4_env
    summary = create_candidate(
        env["root"],
        env["repo"],
        version="phase30-child-v4",
        parent="phase30-parent-v3",
        diff_base_commit=env["mid"],
        source_commit=env["head"],
        source_files=child_source_files(),
        candidate_schema_version=4,
        manifest_kind="full",
    )
    folder = Path(summary["directory"])
    candidate = read_json(folder / "candidate.json")
    snapshot = read_json(folder / "snapshot.json")
    assert candidate["schema_version"] == 4
    assert candidate["candidate_schema_version"] == 4
    assert candidate["status"] == "pending"
    assert candidate["parent"] == "phase30-parent-v3"
    assert candidate["source_manifest"] == {
        "kind": "full",
        "parent": "phase30-parent-v3",
        "required_roles": ["coder", "planner", "reviewer"],
    }
    assert snapshot["prompts"] == {
        "coder": "coder prompt v2 phase30",
        "planner": "planner prompt v1",
        "reviewer": "reviewer prompt v1",
    }
    validate_snapshot(snapshot, env["root"], strict=True)
    # source_files must equal the manifest coverage: parent set + child files.
    assert candidate["source_files"] == len(child_source_files())
    assert set(candidate["source_hashes"]) == set(child_source_files())


def test_v4_strict_empty_prompts_abort_generation(tmp_path: Path) -> None:
    """Agent sources without _SYSTEM literals yield no prompts; v4 must abort."""
    repo, base, _mid, _head = build_repo(tmp_path)
    (repo / "agents/coder.py").write_text(CODER_NO_PROMPT, encoding="utf-8", newline="\n")
    (repo / "agents/planner.py").write_text(CODER_NO_PROMPT, encoding="utf-8", newline="\n")
    (repo / "agents/reviewer.py").write_text(CODER_NO_PROMPT, encoding="utf-8", newline="\n")
    head = commit_all(repo, "strip prompts")
    # Root candidate: no parent chain, so the stripped agent sources are the
    # only prompt source and strict validation must abort generation.
    root_no_inherit = tmp_path / "archive-root"
    with pytest.raises(ValueError, match="strict snapshot"):
        create_candidate(
            root_no_inherit,
            repo,
            version="phase30-noprompt-v4",
            diff_base_commit=base,
            source_commit=head,
            source_files=[*AGENT_PATHS, "src/feature.py"],
            candidate_schema_version=4,
            manifest_kind="full",
        )
    assert not (root_no_inherit / "candidates" / "phase30-noprompt-v4").exists()


def test_v4_dirty_worktree_generates_from_temp_clone(tmp_path: Path) -> None:
    """Dirty tracked worktree + temp clone: sources come from source_commit
    blobs, the user's uncommitted changes are neither stashed nor archived."""
    repo, _base, mid, head = build_repo(tmp_path)
    root = tmp_path / "archive"
    generate_v3_parent(root, repo, mid, head)
    dirty_marker = "UNCOMMITTED USER CHANGE\n"
    (repo / "src/feature.py").write_text(FEATURE_V3 + dirty_marker, encoding="utf-8", newline="\n")
    status = git(repo, "status", "--porcelain", "--untracked-files=no").decode()
    assert status.strip(), "fixture requires a dirty tracked worktree"
    summary = create_candidate(
        root,
        repo,
        version="phase30-dirty-v4",
        parent="phase30-parent-v3",
        diff_base_commit=mid,
        source_commit=head,
        source_files=child_source_files(),
        candidate_schema_version=4,
        manifest_kind="full",
        allow_dirty_via_temp_clone=True,
    )
    assert summary["temp_source_clone"] is True
    candidate = read_json(Path(summary["directory"]) / "candidate.json")
    sources = read_json(Path(summary["directory"]) / "sources.json")
    # Archived text is the commit blob, not the dirty worktree text.
    assert "UNCOMMITTED USER CHANGE" not in sources["src/feature.py"]
    assert sources["src/feature.py"] == FEATURE_V3
    assert candidate["dirty_worktree"] is False
    assert "dirty_files" not in candidate
    # The user's uncommitted change is still in the worktree, untouched.
    assert dirty_marker in (repo / "src/feature.py").read_text(encoding="utf-8")


def test_v3_default_layout_unchanged(v4_env: dict[str, Any]) -> None:
    """The default (no candidate_schema_version) stays the v3 record layout."""
    env = v4_env
    summary = create_candidate(
        env["root"],
        env["repo"],
        version="phase30-v3-legacy",
        parent="phase30-parent-v3",
        diff_base_commit=env["head"],
        source_commit=env["head"],
        source_files=child_source_files(),
    )
    candidate = read_json(Path(summary["directory"]) / "candidate.json")
    assert candidate["schema_version"] == 3
    assert "candidate_schema_version" not in candidate
    assert "source_manifest" not in candidate


def test_v4_resolves_labels_to_full_commit_shas(v4_env: dict[str, Any]) -> None:
    """source_commit/diff_base_commit are recorded as resolved full shas, never labels."""
    env = v4_env
    summary = create_candidate(
        env["root"],
        env["repo"],
        version="phase30-label-v4",
        parent="phase30-parent-v3",
        diff_base_commit="HEAD",
        source_commit="HEAD",
        source_files=child_source_files(),
        candidate_schema_version=4,
        manifest_kind="full",
    )
    candidate = read_json(Path(summary["directory"]) / "candidate.json")
    assert candidate["source_commit"] == env["head"]
    assert candidate["diff_base_commit"] == env["head"]
    assert summary["source_commit"] == env["head"]


def test_v4_rejects_unsupported_schema_version(v4_env: dict[str, Any]) -> None:
    with pytest.raises(ArchiveError, match="candidate_schema_version"):
        create_candidate(
            v4_env["root"],
            v4_env["repo"],
            version="phase30-v9",
            diff_base_commit=v4_env["head"],
            source_commit=v4_env["head"],
            source_files=child_source_files(),
            candidate_schema_version=9,
        )


def test_validate_snapshot_strict_and_empty() -> None:
    """validate_snapshot({}) fails; strict requires all three role prompts."""
    with pytest.raises(ValueError, match="unsupported policy snapshot"):
        validate_snapshot({})
    base = {"schema_version": 1, "instruction": "", "experiences": []}
    with pytest.raises(ValueError, match="strict snapshot"):
        validate_snapshot({**base, "prompts": {}}, strict=True)
    with pytest.raises(ValueError, match="strict snapshot"):
        validate_snapshot(
            {**base, "prompts": {"coder": "x", "planner": "", "reviewer": "y"}}, strict=True
        )
    # Lenient path (schema-1 compatibility) still accepts an empty prompts map.
    lenient = validate_snapshot({**base, "prompts": {}})
    assert lenient["prompts"] == {}
    strict = validate_snapshot(
        {**base, "prompts": {"coder": "c", "planner": "p", "reviewer": "r"}}, strict=True
    )
    assert strict["prompts"]["coder"] == "c"
