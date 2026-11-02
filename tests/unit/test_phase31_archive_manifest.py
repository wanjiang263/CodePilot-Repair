"""Schema 5 completeness, tombstones and strict parent/snapshot regression tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from codepilot import archive
from codepilot.archive import (
    ArchiveError,
    IncompleteSourceManifestError,
    ParentIntegrityError,
    _inherit_parent_snapshot,
    create_candidate,
    resolved_candidate_sources,
)

ROLES = [f"src/codepilot/agents/{r}.py" for r in ("coder", "planner", "reviewer")]
PARENT = "phase30-candidate-v4"
CHILD = "phase31-candidate-v5"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Phase31 fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    for role, path in zip(("coder", "planner", "reviewer"), ROLES, strict=True):
        write(repo / path, f'_SYSTEM = "{role} prompt"\n')
    write(repo / "src/feature.py", "value = 1\n")
    write(repo / "src/removed.py", "removed = True\n")
    metadata = "configs/harness/candidates/historical-baseline.json"
    write(repo / metadata, "{}\n")
    write(repo / "docs/notes/old.md", "legacy note\n")
    base = commit(repo, "base")
    root = repo / "configs/harness"
    create_candidate(
        root,
        repo,
        version=PARENT,
        diff_base_commit=base,
        source_commit=base,
        source_files=[*ROLES, "src/feature.py", "src/removed.py", metadata, "docs/notes/old.md"],
        candidate_schema_version=4,
    )
    commit(repo, "parent archive")
    write(repo / "src/feature.py", "value = 2\n")
    write(repo / "src/new.py", "new = True\n")
    write(repo / "docs/notes/new.md", "new documentation\n")
    head = commit(repo, "round changes")
    return {"repo": repo, "root": root, "base": base, "head": head}


def generate(env: dict, **kwargs: object) -> dict:
    return create_candidate(
        env["root"],
        env["repo"],
        version=CHILD,
        parent=PARENT,
        diff_base_commit=env["base"],
        source_commit=env["head"],
        candidate_schema_version=5,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("kind", "paths"),
    [
        ("full", [*ROLES, "src/feature.py"]),
        ("delta", ["src/new.py"]),
        ("delta", ["docs/notes/new.md"]),
    ],
)
def test_v5_rejects_caller_source_lists(env: dict, kind: str, paths: list[str]) -> None:
    with pytest.raises(IncompleteSourceManifestError, match="derives"):
        generate(env, manifest_kind=kind, source_files=paths)


@pytest.mark.parametrize(
    ("kind", "mode"), [("full", "omit"), ("delta", "omit"), ("delta", "unrelated")]
)
def test_generator_rejects_incomplete_materialized_source_set(
    env: dict,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    mode: str,
) -> None:
    original = archive._read_commit_sources

    def incomplete(repo: Path, commit: str, files: list[str]) -> tuple[dict, dict]:
        sources, blobs = original(repo, commit, files)
        if mode == "omit":
            sources.pop("src/new.py" if kind == "full" else "src/feature.py")
        else:
            sources = {"src/removed.py": "removed = True\n"}
        return sources, blobs

    monkeypatch.setattr(archive, "_read_commit_sources", incomplete)
    with pytest.raises(IncompleteSourceManifestError, match="exact derived"):
        generate(env, manifest_kind=kind)


@pytest.mark.parametrize("kind", ["full", "delta"])
def test_complete_derived_set(env: dict, kind: str) -> None:
    generate(env, manifest_kind=kind)
    folder = env["root"] / "candidates" / CHILD
    record = read(folder / "candidate.json")
    manifest = record["source_manifest"]
    assert set(manifest["changed_files"]) == {"src/feature.py", "src/new.py"}
    expected = {*ROLES, "src/feature.py", "src/new.py", "src/removed.py"}
    assert set(record["source_hashes"]) == (
        expected if kind == "full" else {"src/feature.py", "src/new.py"}
    )
    assert manifest["resolved_source_files"] == len(expected)
    assert "configs/harness/candidates/historical-baseline.json" in {
        p["path"] for p in manifest["excluded_files"]
    }
    assert "docs/notes/new.md" in manifest["scope_excluded_changed_files"]


def test_deleted_parent_source_is_removed_and_tombstoned(env: dict) -> None:
    (env["repo"] / "src/removed.py").unlink()
    env["head"] = commit(env["repo"], "delete")
    generate(env)
    record = read(env["root"] / "candidates" / CHILD / "candidate.json")
    assert record["source_manifest"]["deleted_files"] == ["src/removed.py"]
    assert "src/removed.py" not in record["source_hashes"]
    assert "deleted file" in (env["root"] / "candidates" / CHILD / "candidate.diff").read_text()


def test_deleted_required_role_fails(env: dict) -> None:
    (env["repo"] / ROLES[0]).unlink()
    env["head"] = commit(env["repo"], "delete role")
    with pytest.raises(IncompleteSourceManifestError, match="required role source deleted"):
        generate(env)


def test_rename_is_old_delete_and_new_add(env: dict) -> None:
    git(env["repo"], "mv", "src/removed.py", "src/renamed.py")
    env["head"] = commit(env["repo"], "rename")
    generate(env)
    record = read(env["root"] / "candidates" / CHILD / "candidate.json")
    assert record["source_manifest"]["deleted_files"] == ["src/removed.py"]
    assert "src/renamed.py" in record["source_manifest"]["changed_files"]
    assert "src/removed.py" not in record["source_hashes"]
    assert "src/renamed.py" in record["source_hashes"]


def test_parent_snapshot_mismatch_fails(env: dict) -> None:
    path = env["root"] / "candidates" / PARENT / "snapshot.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ParentIntegrityError, match="snapshot_file_sha256"):
        generate(env)


def test_v4_full_source_set_is_a_resolution_boundary(env: dict) -> None:
    path = env["root"] / "candidates" / PARENT / "candidate.json"
    record = read(path)
    record["parent"] = "old-chain-outside-resolution-boundary"
    write(path, json.dumps(record))
    assert resolved_candidate_sources(env["root"], PARENT) == set(
        read(path.with_name("sources.json"))
    )


def test_roundtrip_temp_inside_checkout_cannot_silently_skip_patch(
    env: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp = env["repo"] / "nested-temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    generate(env)
    assert (env["root"] / "candidates" / CHILD / "candidate.diff").read_bytes()


def test_prompts_inherit_per_role_and_detect_cycle(tmp_path: Path) -> None:
    root = tmp_path
    for version, parent, prompts in (
        ("near", "far", {"coder": "near coder"}),
        ("far", None, {"planner": "far planner", "reviewer": "far reviewer"}),
    ):
        folder = root / "candidates" / version
        write(folder / "candidate.json", json.dumps({"parent": parent}))
        write(folder / "snapshot.json", json.dumps({"prompts": prompts, "instruction": version}))
    result = _inherit_parent_snapshot(root, "near")
    assert result["prompts"] == {
        "coder": "near coder",
        "planner": "far planner",
        "reviewer": "far reviewer",
    }
    assert result["instruction"] == "near"
    write(root / "candidates/far/candidate.json", '{"parent":"near"}')
    with pytest.raises(ArchiveError, match="cycle"):
        _inherit_parent_snapshot(root, "near")
