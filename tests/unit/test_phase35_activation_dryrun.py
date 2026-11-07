"""Phase35 activation dry-run against the real, latest candidate archive.

The repository archive must stay pointer-free. The test copies the real archive
to a temporary root, stages the candidate there, and drives the harness with fake
agents: it proves the three role prompts come from the candidate snapshot (never
the sentinel code defaults), that a pending candidate is refused without
``allow_pending``, and that the real archive and candidate bytes are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codepilot.schemas.state import RepairState

ROOT = Path(__file__).resolve().parents[2]
REAL_ARCHIVE = ROOT / "configs" / "harness"
ROLES = ("coder", "planner", "reviewer")
SENTINELS = {
    "coder": "CODE-DEFAULT-CODER",
    "planner": "CODE-DEFAULT-PLANNER",
    "reviewer": "CODE-DEFAULT-REVIEWER",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pointer_names(archive: Path) -> list[str]:
    return sorted(name for name in ("active.json", "active.next.json") if (archive / name).exists())


def latest_candidate(archive: Path) -> str:
    versions = [
        path.name for path in (archive / "candidates").glob("phase34-candidate-v*") if path.is_dir()
    ]
    assert versions, "no phase34 candidate archive found"
    return max(versions, key=lambda name: int(name.rsplit("-v", 1)[1]))


def make_deps(run_dir: Path) -> Any:
    from codepilot.artifacts import ArtifactStore
    from codepilot.observability.events import EventSink
    from codepilot.observability.lifecycle import Lifecycle
    from codepilot.sandbox.env_spec import EnvSpec

    artifacts = ArtifactStore(run_dir, "run")
    events = EventSink(run_id="run", task_id="task", jsonl_path=artifacts.run_dir / "events.jsonl")
    return SimpleNamespace(
        artifacts=artifacts,
        lifecycle=Lifecycle(events),
        harness_started=False,
        coder=SimpleNamespace(system_prompt=SENTINELS["coder"]),
        planner=SimpleNamespace(system_prompt=SENTINELS["planner"]),
        reviewer=SimpleNamespace(system_prompt=SENTINELS["reviewer"]),
        repository="repo",
        env_spec=EnvSpec(),
        clock=lambda: datetime.now(UTC),
        experience_snapshot=[],
        tools=SimpleNamespace(query_cache=None),
    )


def make_state() -> RepairState:
    return RepairState(
        task_id="task",
        run_id="run",
        repository_id="repo",
        base_commit="abc",
        issue="activation dry-run",
        started_at=datetime.now(UTC),
    )


def test_latest_candidate_activation_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.graph.harness as harness
    from codepilot import rsi

    version = latest_candidate(REAL_ARCHIVE)
    folder = REAL_ARCHIVE / "candidates" / version
    record = json.loads((folder / "candidate.json").read_text(encoding="utf-8"))
    assert record["status"] == "pending"
    assert record["schema_version"] == 5
    snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
    prompts = snapshot["prompts"]
    assert all(prompts.get(role) for role in ROLES)
    assert all(prompts[role] != SENTINELS[role] for role in ROLES)

    files = ("candidate.diff", "candidate.json", "snapshot.json", "sources.json")
    real_before = {name: sha256(folder / name) for name in files}
    assert pointer_names(REAL_ARCHIVE) == []

    temp_archive = tmp_path / "archive"
    shutil.copytree(REAL_ARCHIVE, temp_archive)
    assert pointer_names(temp_archive) == []

    builtin = rsi.load_selection(temp_archive)
    assert builtin["version"] == "builtin-phase26-v1"

    with pytest.raises(ValueError):
        rsi.select(temp_archive, version)
    assert pointer_names(temp_archive) == []

    rsi.select(temp_archive, version, allow_pending=True)
    assert pointer_names(temp_archive) == ["active.json"]
    selection = rsi.load_selection(temp_archive)
    assert selection["version"] == version
    assert selection["status"] == "pending"

    monkeypatch.setattr(harness, "archive_root", lambda: temp_archive)
    deps = make_deps(tmp_path / "runs")
    state = harness.begin(make_state(), deps, "plan")

    for role in ROLES:
        assert getattr(deps, role).system_prompt == prompts[role]
        assert getattr(deps, role).system_prompt != SENTINELS[role]
    assert state.policy_snapshot["version"] == version
    assert state.policy_snapshot["status"] == "pending"

    assert pointer_names(REAL_ARCHIVE) == []
    assert {name: sha256(folder / name) for name in files} == real_before
