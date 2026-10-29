"""Canonical deterministic projection shared by event publication and API reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def project_event(previous: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(previous)
    seq = int(event.get("seq", 0))
    runtime["run_id"] = event.get("run_id")
    extra = event.get("extra") or {}
    facts = extra.get("facts") or {}
    hook = extra.get("hook")
    for suggestion in extra.get("suggestions", []):
        if suggestion.get("resource"):
            runtime["resource"] = suggestion["resource"]
    if hook in {"stage_start", "stage_end"}:
        incoming = facts.get("runtime") or {}
        if int(incoming.get("revision", 0)) < int(runtime.get("revision", 0)):
            return runtime
        runtime.update(incoming)
    if hook == "before_model":
        runtime["activity"] = "external_wait"
    if event.get("type") == "llm_call" or hook in {
        "after_model",
        "before_tool",
        "after_tool",
        "exploration",
    }:
        runtime["activity"] = "executing"
    if hook == "exploration" and (
        facts.get("new_relevant_evidence")
        or facts.get("hypothesis_changed")
        or facts.get("candidate_changed")
    ):
        runtime["last_effective_progress"] = event.get("timestamp")
    if hook in {"exploration", "candidate_changed"} and "has_modification" in facts:
        runtime["has_modification"] = facts["has_modification"]
    if hook == "candidate_changed":
        runtime["last_effective_progress"] = event.get("timestamp")
    if hook == "resource_wait":
        runtime["activity"] = "queued"
    if hook == "resource_acquired":
        runtime["activity"] = "executing"
        runtime["resource_queued_seconds"] = float(
            runtime.get("resource_queued_seconds", 0)
        ) + facts.get("queued_seconds", 0)
    if hook in {"task_start", "resume"}:
        runtime["policy_snapshot"] = facts.get("snapshot", {})
    if hook == "preparation_progress":
        runtime["preparation_progress"] = {
            "phase": facts.get("phase"),
            "counts": facts.get("counts"),
            "source": facts.get("source"),
        }
    if hook == "resume":
        runtime["recovery"] = {"reuse": facts.get("reuse"), "redo": facts.get("redo")}
    if hook == "contract_ready":
        runtime["contract"] = facts.get("contract")
    if hook == "after_validation":
        runtime["verification"] = facts.get("view")
        runtime["failure_summary"] = facts.get("failure_summary")
    if hook == "after_tool" and facts.get("cache"):
        runtime["last_cache"] = facts["cache"]
    if hook == "stage_end":
        runtime["activity"] = "idle"
    runtime["event_seq"] = max(seq, int(runtime.get("event_seq", 0)))
    return runtime


def activity_view(events: list[dict[str, Any]], *, now: datetime, terminal: bool) -> dict[str, Any]:
    runtime: dict[str, Any] = {}
    seen: set[int] = set()
    for event in sorted(events, key=lambda item: int(item.get("seq", 0))):
        seq = int(event.get("seq", 0))
        if seq and seq in seen:
            continue
        seen.add(seq)
        runtime["event_seq"] = max(seq, int(runtime.get("event_seq", 0)))
        projection = (event.get("extra") or {}).get("runtime_projection")
        if isinstance(projection, dict) and projection.get("run_id") == event.get("run_id"):
            runtime.update(projection)
        else:
            runtime = project_event(runtime, event)
    if terminal:
        runtime["activity"] = "finished"
    elif runtime.get("activity") != "idle" and runtime.get("stage_started_at"):
        try:
            start = datetime.fromisoformat(runtime["stage_started_at"])
            runtime["stage_elapsed_seconds"] = max(0, (now - start).total_seconds())
        except (ValueError, TypeError):
            pass
    return runtime
