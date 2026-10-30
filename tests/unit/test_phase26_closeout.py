"""Authored for Phase26 static delivery. NOT EXECUTED."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codepilot.agents.exploration import ExplorationProgress
from codepilot.api.runtime_view import activity_view
from codepilot.artifacts import ArtifactStore
from codepilot.graph.harness import SnapshotIdentityError, begin
from codepilot.observability.events import EventSink
from codepilot.observability.lifecycle import Lifecycle
from codepilot.observability.resources import ResourceCancelledError, resource_slot
from codepilot.rsi import retrieve, validate_comparison
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.state import RepairState
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.query_cache import QueryCache
from codepilot.tools.read_tools import read_file_tool, search_code_tool


def state() -> RepairState:
    return RepairState(
        task_id="task",
        run_id="run",
        repository_id="repo",
        base_commit="base",
        issue="public requirement",
        started_at=datetime.now(UTC),
        current_patch="existing",
    )


def deps(root: Path) -> Any:
    artifacts = ArtifactStore(root, "run")
    events = EventSink(run_id="run", task_id="task", jsonl_path=artifacts.run_dir / "events.jsonl")
    return SimpleNamespace(
        artifacts=artifacts,
        events=events,
        lifecycle=Lifecycle(events),
        harness_started=False,
        experience_snapshot=[],
        fixed_selection={},
        coder=SimpleNamespace(system_prompt="coder"),
        planner=SimpleNamespace(system_prompt="planner"),
        reviewer=SimpleNamespace(system_prompt="reviewer"),
        repository="repo",
        env_spec=EnvSpec(),
        tools=SimpleNamespace(query_cache=None),
        clock=lambda: datetime.now(UTC),
    )


def test_experience_failure_falls_back_without_losing_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.graph.harness as module

    current = state()
    dependency = deps(tmp_path)
    monkeypatch.setattr(
        module, "retrieve", lambda *a, **kw: (_ for _ in ()).throw(OSError("offline"))
    )
    result = begin(current, dependency, "generate_patch")
    assert result.current_patch == "existing" and result.usage == current.usage
    assert dependency.coder.experience_feedback == ""
    assert "experience_unavailable" in dependency.events.path.read_text(encoding="utf-8")


def test_snapshot_write_failure_is_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = deps(tmp_path)
    monkeypatch.setattr(
        dependency.artifacts, "write_json", lambda *a: (_ for _ in ()).throw(OSError("readonly"))
    )
    current = state()
    result = begin(current, dependency, "generate_patch")
    assert result.current_patch == current.current_patch
    assert result.policy_snapshot == {} and not dependency.harness_started
    assert "initialization_failed" in dependency.events.path.read_text(encoding="utf-8")


def test_identity_conflict_blocks_work_but_allows_finalization(tmp_path: Path) -> None:
    dependency = deps(tmp_path)
    current = state().model_copy(update={"policy_snapshot": {"sha256": "wrong"}})
    dependency.artifacts.write_json("harness/snapshot.json", {})
    with pytest.raises(SnapshotIdentityError):
        begin(current, dependency, "generate_patch")
    assert begin(current, dependency, "finalize").current_patch == "existing"
    assert '"identity"' in dependency.events.path.read_text(encoding="utf-8")


def entry(identifier: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "version": "v1",
        "level": "L1",
        "repository": "repo",
        "source": ["public issue"],
        "source_run": "run",
        "status": "pending",
        "applies_when": {"stage": "repair_patch"},
        "invalid_when": {},
        "action": "inspect the failing source",
        **changes,
    }


def test_stage_environment_invalid_and_unknown_conditions() -> None:
    entries = [
        entry("wrong-stage", applies_when={"stage": "plan"}),
        entry("wrong-env", applies_when={"runner": "other"}),
        entry("invalid", invalid_when={"stage": "repair_patch"}),
        entry("unknown", applies_when={"arbitrary_python": "True"}),
        entry("ok"),
    ]
    audit: list[dict[str, Any]] = []
    selected = retrieve(
        {"experiences": entries},
        "repo",
        stage="repair_patch",
        environment={"runner": "pytest"},
        audit=audit,
    )
    assert [item["id"] for item in selected] == ["ok"]
    assert len([item for item in audit if item["decision"] == "rejected"]) == 4


def test_legacy_conditions_are_advice_and_ranking_is_bounded() -> None:
    entries = [
        entry("b"),
        entry("a"),
        entry("legacy", applies_when="after repeated failure", invalid_when="if unrelated"),
    ]
    selected = retrieve({"experiences": entries}, "repo", stage="repair_patch")
    assert [item["id"] for item in selected] == ["a", "b", "legacy"]
    assert selected[-1]["advisory"] and not selected[-1]["conditions_evaluated"]
    assert len(retrieve({"experiences": entries}, "repo", stage="repair_patch", limit=1)) == 1
    assert retrieve({"experiences": entries}, "repo", stage="repair_patch", max_chars=1) == []


def test_global_search_bypasses_identity_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.tools.query_cache as module

    cache = QueryCache(WorkspacePathPolicy(tmp_path))
    monkeypatch.setattr(
        module.os, "walk", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not scan"))
    )
    assert cache._content_identity(search_code_tool(cache.policy), {"path_glob": None}) is None


def test_expensive_scope_is_not_rescanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/large.py").write_bytes(b"x" * (1024 * 1024 + 1))
    cache = QueryCache(WorkspacePathPolicy(tmp_path))
    tool = search_code_tool(cache.policy)
    args = {"path_glob": "src/**"}
    assert cache._content_identity(tool, args) is None
    import codepilot.tools.query_cache as module

    monkeypatch.setattr(
        module.os, "walk", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("rescanned"))
    )
    assert cache._content_identity(tool, args) is None


def test_cache_byte_bound_and_deletion(tmp_path: Path) -> None:
    path = tmp_path / "core.py"
    path.write_text("x" * 1000, encoding="utf-8")
    cache = QueryCache(WorkspacePathPolicy(tmp_path))
    cache.max_bytes = 128
    tool = read_file_tool(cache.policy)
    cache.execute(tool, {"path": "core.py"}, "coder")
    assert cache.bytes_used <= 128 and not cache.entries
    path.unlink()
    assert not cache.execute(tool, {"path": "core.py"}, "coder").ok


def test_existing_patch_readonly_calls_do_not_create_progress() -> None:
    progress = ExplorationProgress()
    assert not progress.observe_candidate("old")["candidate_changed"]
    facts = progress.observe("search_code", {"pattern": "x"}, {"ok": True, "data": {}}, "")
    event = {"seq": 1, "timestamp": "now", "extra": {"hook": "exploration", "facts": facts}}
    assert "last_effective_progress" not in activity_view(
        [event], now=datetime.now(UTC), terminal=False
    )
    assert progress.observe_candidate("new")["candidate_changed"]
    assert not progress.observe_candidate("new")["candidate_changed"]
    assert progress.observe_candidate("")["candidate_changed"]
    assert not progress.modified


def test_event_order_duplicates_and_terminal_elapsed() -> None:
    start = {
        "seq": 1,
        "extra": {
            "hook": "stage_start",
            "facts": {"runtime": {"stage": "plan", "revision": 1, "stage_elapsed_seconds": 0}},
        },
    }
    end = {
        "seq": 2,
        "extra": {
            "hook": "stage_end",
            "facts": {"runtime": {"stage": "plan", "revision": 1, "stage_elapsed_seconds": 12}},
        },
    }
    view = activity_view([end, start, start], now=datetime.now(UTC), terminal=True)
    assert view["stage_elapsed_seconds"] == 12 and view["activity"] == "finished"


def test_resource_wait_cancel_and_reentrant_gate() -> None:
    events: list[tuple[str, Any]] = []
    with pytest.raises(ResourceCancelledError), resource_slot("model", cancel=lambda: True):
        raise AssertionError("not dispatched")
    with (
        resource_slot("test", observe=lambda hook, **facts: events.append((hook, facts))),
        resource_slot("heavy", observe=lambda hook, **facts: events.append((hook, facts))),
    ):
        pass
    assert [hook for hook, _ in events].count("resource_acquired") == 1


def test_comparison_rejects_missing_provenance(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provenance"):
        validate_comparison(tmp_path, {"schema_version": 1, "decision": "accept"})


def test_summary_is_deterministic_and_candidate_bound() -> None:
    from codepilot.graph.evidence import stage_handoff
    from codepilot.schemas.plan import RepairPlan

    current = state()
    plan = RepairPlan(root_cause_hypothesis="check source", alternatives=[])
    summary = stage_handoff(current, plan)
    assert summary["candidate_hash"] and summary["model_summary"] is None
    assert summary == stage_handoff(current, plan)
    assert len(summary["missing_facts"]) == 4
    assert "public requirement" in json.dumps(summary)


@pytest.mark.asyncio
async def test_guard_attributes_initialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.graph.harness as harness
    from codepilot.graph.builder import _guard
    from codepilot.graph.routing import Node

    dependency = deps(tmp_path)
    current = state()
    dependency.cancel_check = None
    dependency.bind_llm_context = lambda *args: None
    dependency.usage = current.usage
    dependency.test_runs = 0
    monkeypatch.setattr(
        harness,
        "begin",
        lambda *args: (_ for _ in ()).throw(SnapshotIdentityError("identity conflict")),
    )

    async def forbidden(*args: Any) -> dict[str, Any]:
        raise AssertionError("node must not run")

    update = await _guard(Node.GENERATE_PATCH, forbidden, dependency)(current)
    assert "identity conflict" in update["failure_reasons"][-1]
    assert update["usage"] == current.usage and "current_patch" not in update
    assert update["error_class"]


@pytest.mark.asyncio
async def test_projection_failure_preserves_produced_candidate_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codepilot.graph.harness as harness
    from codepilot.graph.builder import _guard
    from codepilot.graph.routing import Node

    dependency = deps(tmp_path)
    current = state()
    dependency.cancel_check = None
    dependency.bind_llm_context = lambda *args: None
    dependency.usage = current.usage
    dependency.test_runs = 0
    monkeypatch.setattr(
        harness, "complete", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )

    import time

    from codepilot.observability.dispatch import Deadline

    dependency.dispatch_deadline = lambda _: Deadline(time.monotonic() + 300)
    monkeypatch.setattr(
        "codepilot.graph.nodes._closure_reservation", lambda *args: SimpleNamespace(seconds=0)
    )

    async def produce(*args: Any) -> dict[str, Any]:
        return {"current_patch": "new candidate", "usage": current.usage}

    update = await _guard(Node.GENERATE_PATCH, produce, dependency)(current)
    assert update["current_patch"] == "new candidate" and update["usage"] == current.usage
    assert "projection_error" in dependency.events.path.read_text(encoding="utf-8")


def test_scoped_search_invalidates_external_edit_ignore_and_permission(tmp_path: Path) -> None:
    import os

    folder = tmp_path / "src"
    folder.mkdir()
    path = folder / "a.py"
    path.write_text("old", encoding="utf-8")
    cache = QueryCache(WorkspacePathPolicy(tmp_path))
    tool = search_code_tool(cache.policy)
    args = {"path_glob": "src/**"}
    original = cache._content_identity(tool, args)
    timestamp = path.stat()
    path.write_text("new", encoding="utf-8")
    os.utime(path, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
    edited = cache._content_identity(tool, args)
    assert edited != original
    (tmp_path / ".ignore").write_text("src/a.py", encoding="utf-8")
    ignored = cache._content_identity(tool, args)
    assert ignored != edited
    cache.policy.deny_files = cache.policy.deny_files | {"src/a.py"}
    assert cache._content_identity(tool, args) != ignored


def test_published_runtime_projection_uses_same_api_semantics(tmp_path: Path) -> None:
    dependency = deps(tmp_path)
    dependency.lifecycle.emit(
        "stage_start", runtime={"revision": 1, "stage": "plan", "stage_elapsed_seconds": 0}
    )
    dependency.lifecycle.emit("exploration", has_modification=True)
    dependency.lifecycle.emit(
        "stage_end", runtime={"revision": 1, "stage": "plan", "stage_elapsed_seconds": 3}
    )
    records = [
        json.loads(line) for line in dependency.events.path.read_text(encoding="utf-8").splitlines()
    ]
    projection = records[-1]["extra"]["runtime_projection"]
    view = activity_view(records, now=datetime.now(UTC), terminal=True)
    assert projection["stage_elapsed_seconds"] == view["stage_elapsed_seconds"] == 3
    assert "last_effective_progress" not in view


def test_comparison_import_checks_original_evidence_and_conditions(tmp_path: Path) -> None:
    import hashlib

    from codepilot.rsi import (
        _json_hash,
        import_comparison,
        promotion_readiness,
        validate_snapshot,
        version_dir,
    )

    # Phase29 (I3): the retired v1 archive entry cannot build these fixtures;
    # freeze the snapshot files directly instead.
    for version in ("baseline", "candidate"):
        snapshot = validate_snapshot(
            {
                "schema_version": 1,
                "version": version,
                "parent": None,
                "status": "pending",
                "instruction": "",
                "prompts": {},
                "experiences": [],
            },
            tmp_path,
        )
        folder = version_dir(tmp_path, version)
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"fixture": "not a real experiment"}', encoding="utf-8")
    conditions = dict.fromkeys(
        ("budget_hash", "environment_hash", "success_definition_hash", "task_set_hash"), "a" * 64
    )
    conditions.update(model="fixture", provider="fixture")
    comparison: dict[str, Any] = {
        "schema_version": 1,
        "evaluation_exposure": "public_only",
        "decision": "observe",
        "reason": "synthetic test fixture",
        "evidence_refs": [
            {"path": "evidence.json", "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
        ],
    }
    for side in ("baseline", "candidate"):
        snapshot = json.loads(
            (tmp_path / "candidates" / side / "snapshot.json").read_text(encoding="utf-8")
        )
        comparison[side] = {
            "version": side,
            "snapshot_hash": _json_hash(snapshot),
            "conditions": dict(conditions),
            "cost_known": True,
            "runs": [side],
            "metrics": {"quality": 1, "regressions": 0, "seconds": 1, "cost": 0, "tool_calls": 1},
        }
    reference = import_comparison(tmp_path, comparison)
    assert not promotion_readiness(tmp_path, "candidate", reference)["eligible_for_manual_review"]
    assert not (tmp_path / "active.json").exists()
    comparison["candidate"]["conditions"]["success_definition_hash"] = "b" * 64
    with pytest.raises(ValueError, match="identical"):
        validate_comparison(tmp_path, comparison)
    evidence.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence changed"):
        promotion_readiness(tmp_path, "candidate", reference)
