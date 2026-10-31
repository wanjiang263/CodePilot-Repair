"""Phase30 harness fail-closed activation gate tests.

When an ``active.json`` pointer selects a candidate, the harness must refuse to
run unless that candidate's snapshot carries non-empty prompts for all three
roles — falling back to ``deps.*.system_prompt`` while claiming candidate
provenance is exactly the v3 defect. The no-pointer builtin path keeps working.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codepilot.archive import create_candidate
from codepilot.schemas.state import RepairState

CODER = '"""Coder."""\n\n_SYSTEM = "candidate coder prompt"\n'
PLANNER = '"""Planner."""\n\n_SYSTEM = "candidate planner prompt"\n'
REVIEWER = '"""Reviewer."""\n\n_SYSTEM = "candidate reviewer prompt"\n'
FEATURE_V1 = "def feature() -> int:\n    return 1\n"
FEATURE_V2 = "def feature() -> int:\n    return 2\n"


def git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").decode().strip()


def build_candidate_archive(root: Path, repo: Path) -> str:
    """One committed base, one change; v4 root candidate with all three prompts."""
    git(repo, "init")
    git(repo, "config", "user.email", "phase30@example.invalid")
    git(repo, "config", "user.name", "Phase30 Fixture")
    git(repo, "config", "core.autocrlf", "false")
    sources = {
        "agents/coder.py": CODER,
        "agents/planner.py": PLANNER,
        "agents/reviewer.py": REVIEWER,
        "src/feature.py": FEATURE_V1,
    }
    for rel, text in sources.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    base = commit_all(repo, "base")
    (repo / "src/feature.py").write_text(FEATURE_V2, encoding="utf-8", newline="\n")
    head = commit_all(repo, "change")
    create_candidate(
        root,
        repo,
        version="phase30-activatable-v4",
        diff_base_commit=base,
        source_commit=head,
        source_files=list(sources),
        candidate_schema_version=4,
        manifest_kind="full",
    )
    return "phase30-activatable-v4"


def make_deps(run_dir: Path, **overrides: Any) -> Any:
    from codepilot.artifacts import ArtifactStore
    from codepilot.observability.events import EventSink
    from codepilot.observability.lifecycle import Lifecycle
    from codepilot.sandbox.env_spec import EnvSpec

    artifacts = ArtifactStore(run_dir, "run")
    events = EventSink(run_id="run", task_id="task", jsonl_path=artifacts.run_dir / "events.jsonl")
    base = SimpleNamespace(
        artifacts=artifacts,
        lifecycle=Lifecycle(events),
        harness_started=False,
        coder=SimpleNamespace(system_prompt="CODE-DEFAULT-CODER"),
        planner=SimpleNamespace(system_prompt="CODE-DEFAULT-PLANNER"),
        reviewer=SimpleNamespace(system_prompt="CODE-DEFAULT-REVIEWER"),
        repository="repo",
        env_spec=EnvSpec(),
        clock=lambda: datetime.now(UTC),
        experience_snapshot=[],
        tools=SimpleNamespace(query_cache=None),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def make_state() -> RepairState:
    return RepairState(
        task_id="task",
        run_id="run",
        repository_id="repo",
        base_commit="abc",
        issue="public issue",
        started_at=datetime.now(UTC),
    )


def write_active_pointer(archive_root: Path, version: str) -> None:
    archive_root.joinpath("active.json").write_text(
        json.dumps({"version": version, "new_tasks_only": True}) + "\n", encoding="utf-8"
    )


@pytest.fixture()
def archive_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path | str]:
    import codepilot.graph.harness as harness

    repo = tmp_path / "repo"
    repo.mkdir()
    archive_root = tmp_path / "archive"
    version = build_candidate_archive(archive_root, repo)
    monkeypatch.setattr(harness, "archive_root", lambda: archive_root)
    return {
        "archive_root": archive_root,
        "repo": repo,
        "version": version,
        "run_dir": tmp_path / "runs",
    }


def test_builtin_path_without_pointer_is_untouched(
    archive_env: dict[str, Path | str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No active.json: builtin defaults still run; load_selection is not consulted."""
    import codepilot.graph.harness as harness

    def _explode() -> dict[str, Any]:
        raise AssertionError("builtin path must not call load_selection")

    monkeypatch.setattr(harness, "load_selection", _explode)
    deps = make_deps(archive_env["run_dir"])  # type: ignore[arg-type]
    state = harness.begin(make_state(), deps, "plan")
    assert deps.coder.system_prompt == "CODE-DEFAULT-CODER"
    assert state.policy_snapshot["version"] == "builtin-phase26-v1"


def test_fail_closed_when_selected_candidate_missing_prompts(
    archive_env: dict[str, Path | str],
) -> None:
    """Selected candidate with prompts={} raises instead of using code defaults."""
    import codepilot.graph.harness as harness

    archive_root: Path = archive_env["archive_root"]  # type: ignore[assignment]
    version = str(archive_env["version"])
    snapshot_path = archive_root / "candidates" / version / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["prompts"] = {}
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    write_active_pointer(archive_root, version)
    deps = make_deps(archive_env["run_dir"])  # type: ignore[arg-type]
    with pytest.raises(harness.SnapshotIdentityError) as excinfo:
        harness.begin(make_state(), deps, "plan")
    message = str(excinfo.value)
    assert version in message, "error must name the candidate"
    assert "coder" in message and "planner" in message and "reviewer" in message
    assert str(snapshot_path) in message, "error must name the snapshot path"
    assert deps.coder.system_prompt == "CODE-DEFAULT-CODER", "no code prompt fallback"


def test_fail_closed_when_active_snapshot_unreadable(
    archive_env: dict[str, Path | str],
) -> None:
    """A pointer to a corrupt snapshot must fail closed, not fall back."""
    import codepilot.graph.harness as harness

    archive_root: Path = archive_env["archive_root"]  # type: ignore[assignment]
    version = str(archive_env["version"])
    (archive_root / "candidates" / version / "snapshot.json").write_text(
        "{not json", encoding="utf-8"
    )
    write_active_pointer(archive_root, version)
    deps = make_deps(archive_env["run_dir"])  # type: ignore[arg-type]
    with pytest.raises(harness.SnapshotIdentityError, match="unusable"):
        harness.begin(make_state(), deps, "plan")


def test_fail_closed_when_one_role_prompt_missing(archive_env: dict[str, Path | str]) -> None:
    """Deleting a single role's prompt from the snapshot blocks activation."""
    import codepilot.graph.harness as harness

    archive_root: Path = archive_env["archive_root"]  # type: ignore[assignment]
    version = str(archive_env["version"])
    snapshot_path = archive_root / "candidates" / version / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    del snapshot["prompts"]["reviewer"]
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    write_active_pointer(archive_root, version)
    deps = make_deps(archive_env["run_dir"])  # type: ignore[arg-type]
    with pytest.raises(harness.SnapshotIdentityError) as excinfo:
        harness.begin(make_state(), deps, "plan")
    assert "reviewer" in str(excinfo.value)


def test_activation_dry_run_uses_candidate_prompts(
    archive_env: dict[str, Path | str],
) -> None:
    """Selecting the v4 candidate: agent prompts come from the snapshot, not code."""
    import codepilot.graph.harness as harness
    from codepilot.rsi import select

    archive_root: Path = archive_env["archive_root"]  # type: ignore[assignment]
    version = str(archive_env["version"])
    select(archive_root, version, allow_pending=True)
    assert archive_root.joinpath("active.json").is_file()
    deps = make_deps(archive_env["run_dir"])  # type: ignore[arg-type]
    state = harness.begin(make_state(), deps, "plan")
    assert deps.coder.system_prompt == "candidate coder prompt"
    assert deps.planner.system_prompt == "candidate planner prompt"
    assert deps.reviewer.system_prompt == "candidate reviewer prompt"
    assert state.policy_snapshot["version"] == version
    assert state.policy_snapshot["status"] == "pending"
