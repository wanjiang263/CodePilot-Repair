"""Small lifecycle integration for the existing graph and checkpoint state."""

from __future__ import annotations

import json
from typing import Any

from codepilot.graph.deps import Deps
from codepilot.graph.evidence import failure_summary, verification_view
from codepilot.observability.lifecycle import snapshot_hash
from codepilot.rsi import (
    archive_root,
    load_selection,
    read_json,
    retrieve,
    snapshot_sources,
    version_dir,
)
from codepilot.schemas.state import RepairState


class SnapshotIdentityError(ValueError):
    """A resumed task must never silently switch its fixed policy."""


_SELECTION_ROLES = ("coder", "planner", "reviewer")


def _load_selection_fail_closed(deps: Deps) -> dict[str, Any]:
    """Load the policy selection; a selected candidate must be activatable.

    Phase30 fail-closed gate: when no ``active.json`` pointer exists, the
    builtin defaults apply (code default prompts are allowed only on this
    path). When a pointer exists but the candidate snapshot is unreadable,
    invalid, or missing any of the three role prompts, this raises
    ``SnapshotIdentityError`` naming the candidate, the missing roles and the
    snapshot path — it must never silently fall back to ``deps.*.system_prompt``
    (the v3 defect: a candidate with ``prompts={}`` would have run with code
    defaults while claiming candidate provenance).
    """
    pointer = archive_root() / "active.json"
    if not pointer.exists():
        return {
            "schema_version": 1,
            "version": "builtin-phase26-v1",
            "parent": None,
            "status": "pending",
            "instruction": "",
            "experiences": [],
        }
    try:
        # Pass the root explicitly: load_selection's own default would consult
        # rsi.archive_root(), which is not the harness's (possibly overridden)
        # archive root.
        selection = load_selection(archive_root())
    except (OSError, ValueError, KeyError) as exc:
        deps.lifecycle.emit("policy_load_failed", error=str(exc), fallback="fail_closed")
        raise SnapshotIdentityError(
            f"active candidate selection at {pointer} is unusable ({exc}); "
            "refusing to fall back to builtin defaults"
        ) from exc
    prompts = selection.get("prompts", {})
    missing = [role for role in _SELECTION_ROLES if not prompts.get(role)]
    if missing:
        version = selection.get("version")
        try:
            snapshot_path = version_dir(archive_root(), str(version)) / "snapshot.json"
        except ValueError as exc:
            raise SnapshotIdentityError(
                f"active candidate selection has an invalid version {version!r}: {exc}"
            ) from exc
        raise SnapshotIdentityError(
            f"candidate {version!r} is missing prompts for roles {missing}; "
            f"snapshot: {snapshot_path}; refusing builtin prompt fallback"
        )
    return selection


def begin(state: RepairState, deps: Deps, stage: str) -> RepairState:
    try:
        return _begin(state, deps, stage)
    except SnapshotIdentityError as exc:
        deps.lifecycle.emit("initialization_failed", category="identity", error=str(exc))
        if stage != "finalize":
            raise
        # Finalize must still preserve the original failure, patch and usage.
        return state
    except Exception as exc:
        deps.lifecycle.emit(
            "initialization_failed", category="optional", error=str(exc), fallback="existing_node"
        )
        return state


def _begin(state: RepairState, deps: Deps, stage: str) -> RepairState:
    policy = state.policy_snapshot
    if not deps.harness_started:
        if policy:
            try:
                snapshot = read_json(deps.artifacts.run_dir / "harness/snapshot.json")
            except (OSError, ValueError) as exc:
                raise SnapshotIdentityError(
                    "fixed snapshot unavailable; preserve recovery scene"
                ) from exc
            if snapshot.get("state_schema_version", 1) != 1:
                raise SnapshotIdentityError("snapshot state schema is incompatible")
            if snapshot_hash(snapshot) != policy.get("sha256"):
                raise SnapshotIdentityError(
                    "fixed policy snapshot identity mismatch; refusing in-flight switch"
                )
        else:
            # Phase30: no-pointer -> builtin defaults; pointer present but the
            # candidate cannot deliver role prompts -> SnapshotIdentityError
            # (fail closed, no silent code-prompt fallback).
            selection = _load_selection_fail_closed(deps)
            try:
                sources = snapshot_sources()
            except OSError as exc:
                deps.lifecycle.emit("source_snapshot_unavailable", error=str(exc))
                sources = {}
            snapshot = {
                "selection": selection,
                "selection_source": str(archive_root()),
                "sources": sources,
                "plugins": deps.lifecycle.versions(),
                "configuration": {
                    "environment": deps.env_spec.model_dump(mode="json"),
                    "budget": state.budget.model_dump(mode="json"),
                    "repository": deps.repository,
                    "base_commit": state.base_commit,
                },
                "state_schema_version": 1,
                "prompts": {
                    "coder": selection.get("prompts", {}).get(
                        "coder", getattr(deps.coder, "system_prompt", "")
                    ),
                    "planner": selection.get("prompts", {}).get(
                        "planner", getattr(deps.planner, "system_prompt", "")
                    ),
                    "reviewer": selection.get("prompts", {}).get(
                        "reviewer", getattr(deps.reviewer, "system_prompt", "")
                    ),
                },
            }
            deps.artifacts.write_json("harness/snapshot.json", snapshot)
            policy = {
                "version": selection["version"],
                "sha256": snapshot_hash(snapshot),
                "artifact": "harness/snapshot.json",
                "status": selection.get("status", "pending"),
            }
        selection = snapshot["selection"]
        deps.experience_snapshot = []
        cache = getattr(deps.tools, "query_cache", None)
        if cache is not None:
            counters = state.runtime.get("cache", {})
            cache.hits = int(counters.get("hits", 0))
            cache.misses = int(counters.get("misses", 0))
            if state.events:
                cache.last_invalidation = "worker_restart_no_memory_cache"
        for name, prompt in snapshot.get("prompts", {}).items():
            agent = getattr(deps, name, None)
            if agent is not None and hasattr(agent, "system_prompt"):
                agent.system_prompt = prompt
        deps.coder.experience_feedback = json.dumps(
            {"instruction": selection.get("instruction", ""), "entries": deps.experience_snapshot},
            ensure_ascii=False,
        )
        deps.fixed_selection = selection
        deps.harness_started = True
        deps.lifecycle.bind(
            {"task_id": state.task_id, "stage": stage, "iteration": state.iteration},
            str(policy.get("sha256", "")),
        )
        deps.lifecycle.emit(
            "resume" if state.events else "task_start",
            snapshot=policy,
            reuse="checkpoint-completed nodes, settled responses and identity-valid evidence"
            if state.events
            else "none",
            redo="pending or invalidated stages; no exactly-once guarantee across crash windows"
            if state.events
            else "none",
        )
    if deps.harness_started:
        try:
            audit: list[dict[str, Any]] = []
            deps.experience_snapshot = retrieve(
                deps.fixed_selection,
                deps.repository,
                stage=stage,
                failure_category=state.failure_summary.get("category"),
                environment={
                    "runner": deps.env_spec.test_command,
                    "environment": snapshot_hash(deps.env_spec.model_dump(mode="json")),
                    "version": deps.env_spec.python_version,
                },
                tags=tuple(state.issue_analysis.keywords) if state.issue_analysis else (),
                audit=audit,
            )
            deps.coder.experience_feedback = json.dumps(
                {
                    "instruction": deps.fixed_selection.get("instruction", "")[:1000],
                    "entries": deps.experience_snapshot,
                    "advisory": True,
                },
                ensure_ascii=False,
            )
            deps.lifecycle.emit("experience_selection", decisions=audit)
        except Exception as exc:
            deps.experience_snapshot = []
            deps.coder.experience_feedback = ""
            deps.lifecycle.emit("experience_unavailable", error=str(exc), fallback="no_experience")
    now = deps.clock().isoformat()
    deps.lifecycle.external_wait_seconds = 0.0
    deps.lifecycle.queued_seconds = 0.0
    runtime = {
        **state.runtime,
        "stage": stage,
        "stage_started_at": now,
        "stage_elapsed_seconds": 0.0,
        "activity": "executing",
        "revision": int(state.runtime.get("revision", 0)) + 1,
    }
    deps.lifecycle.bind(
        {
            "task_id": state.task_id,
            "stage": stage,
            "iteration": state.iteration,
            "revision": runtime["revision"],
            "candidate_hash": state.frozen_patch_hash,
        },
        str(policy.get("sha256", "")),
    )
    deps.lifecycle.emit("stage_start", started_at=now, runtime=runtime)
    return state.model_copy(
        update={
            "runtime": runtime,
            "policy_snapshot": policy,
            "experience_refs": [f"{e['id']}@{e['version']}" for e in deps.experience_snapshot],
        }
    )


def complete(
    state: RepairState, update: dict[str, Any], deps: Deps, elapsed: float
) -> dict[str, Any]:
    merged = state.model_copy(update=update)
    runtime = {
        **state.runtime,
        "stage_elapsed_seconds": round(elapsed, 3),
        "activity": "idle",
        "last_completed_at": deps.clock().isoformat(),
    }
    durations = dict(runtime.get("stage_seconds", {}))
    stage = str(runtime.get("stage", "unknown"))
    durations[stage] = round(durations.get(stage, 0) + elapsed, 3)
    runtime["stage_seconds"] = durations
    runtime["external_wait_seconds"] = round(
        float(state.runtime.get("external_wait_seconds", 0)) + deps.lifecycle.external_wait_seconds,
        3,
    )
    runtime["resource_queued_seconds"] = (
        float(state.runtime.get("resource_queued_seconds", 0)) + deps.lifecycle.queued_seconds
    )
    runtime["executing_seconds"] = round(
        float(state.runtime.get("executing_seconds", 0))
        + max(0, elapsed - deps.lifecycle.external_wait_seconds - deps.lifecycle.queued_seconds),
        3,
    )
    cache = getattr(deps.tools, "query_cache", None)
    runtime["has_modification"] = bool(merged.current_patch)
    runtime["event_seq"] = deps.events.last_seq
    runtime["run_id"] = merged.run_id
    runtime["cache"] = {"hits": cache.hits, "misses": cache.misses} if cache else {}
    if merged.current_patch != state.current_patch:
        deps.lifecycle.emit(
            "candidate_changed",
            old=state.frozen_patch_hash,
            new=merged.frozen_patch_hash,
            has_modification=bool(merged.current_patch),
        )
        runtime["last_effective_progress"] = deps.clock().isoformat()
    if stage in {"validate_patch", "run_target_tests", "run_regression"}:
        summary = failure_summary(merged)
        write_projection(deps, f"failures/{stage}-{state.iteration}.json", summary)
        update["failure_summary"] = summary
        deps.lifecycle.emit(
            "after_validation", view=verification_view(merged), failure_summary=summary
        )
        old_evidence = {
            k: v
            for k, v in state.failure_summary.items()
            if k not in {"new_since_last_attempt", "evidence_refs"}
        }
        new_evidence = {
            k: v for k, v in summary.items() if k not in {"new_since_last_attempt", "evidence_refs"}
        }
        if summary and new_evidence != old_evidence:
            runtime["last_effective_progress"] = deps.clock().isoformat()
    if stage == "plan" and merged.plan:
        contract = merged.plan.contract.model_dump(mode="json")
        write_projection(deps, "harness/contract.json", contract)
        deps.lifecycle.emit("contract_ready", contract=contract)
        if state.plan is None or (
            merged.plan.root_cause_hypothesis != state.plan.root_cause_hypothesis
            or merged.plan.evidence != state.plan.evidence
        ):
            runtime["last_effective_progress"] = deps.clock().isoformat()
    for call in update.get("tool_calls", []):
        progress = call.get("result_summary", {}).get("exploration", {})
        if (
            progress.get("new_relevant_evidence")
            or progress.get("hypothesis_changed")
            or progress.get("candidate_changed")
        ):
            runtime["last_effective_progress"] = deps.clock().isoformat()
            runtime["progress_basis"] = "planned_file_overlap_or_stated_hypothesis; heuristic"
    runtime["event_seq"] = deps.events.last_seq + 1
    deps.lifecycle.emit("stage_end", duration_seconds=elapsed, runtime=runtime)
    return {
        **update,
        "runtime": runtime,
        "policy_snapshot": state.policy_snapshot,
        "experience_refs": state.experience_refs,
    }


def write_projection(deps: Deps, path: str, payload: Any) -> bool:
    try:
        deps.artifacts.write_json(path, payload)
        return True
    except Exception as exc:
        deps.lifecycle.emit("projection_error", artifact=path, error=str(exc))
        return False
