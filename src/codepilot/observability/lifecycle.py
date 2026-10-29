"""Ordered read-only observers. Suggestions are committed by the graph executor."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol

from codepilot.observability.events import EventSink, EventType


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


class Observer(Protocol):
    name: str
    version: str

    def observe(
        self, hook: str, state: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class ResourceObserver:
    """Resource classification seam for existing scheduling, not a new scheduler."""

    name = "resource-classification"
    version = "1"

    def observe(
        self, hook: str, state: Mapping[str, Any], facts: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if hook == "before_model":
            return {"resource": "model", "activity": "external_wait"}
        if hook == "before_tool":
            permission = facts.get("permission")
            resource = (
                "test"
                if permission == "execute"
                else "workspace_write"
                if permission == "write"
                else "query"
            )
            return {"resource": resource, "activity": "executing"}
        if hook == "stage_start":
            stage = str(state.get("stage", ""))
            return {
                "resource": {
                    "retrieve": "index_read",
                    "build_env": "environment",
                    "run_target_tests": "test",
                    "run_regression": "test",
                }.get(stage, "orchestration")
            }
        return {}


class Lifecycle:
    def __init__(self, events: EventSink, observers: tuple[Observer, ...] | None = None) -> None:
        self.events = events
        self.observers = observers if observers is not None else (ResourceObserver(),)
        self.state: Mapping[str, Any] = MappingProxyType({})
        self.policy_hash = ""
        self.external_wait_seconds = 0.0
        self.queued_seconds = 0.0

    def bind(self, state: dict[str, Any], policy_hash: str) -> None:
        self.state = freeze(state)
        self.policy_hash = policy_hash

    def emit(self, hook: str, **facts: Any) -> list[dict[str, Any]]:
        try:
            return self._emit(hook, **facts)
        except Exception:
            logging.getLogger(__name__).exception(
                "optional lifecycle projection unavailable: %s", hook
            )
            return []

    def _emit(self, hook: str, **facts: Any) -> list[dict[str, Any]]:
        if hook == "resource_acquired":
            self.queued_seconds += float(facts.get("queued_seconds", 0.0))
        if hook == "after_model":
            self.external_wait_seconds += float(facts.get("duration_seconds", 0.0))
        suggestions = []
        for observer in self.observers:
            try:
                advice = dict(observer.observe(hook, self.state, freeze(facts)))
                if advice:
                    suggestions.append({"observer": observer.name, **advice})
            except Exception as exc:
                self.events.emit(
                    node=str(self.state.get("stage", "lifecycle")),
                    type=EventType.LIFECYCLE,
                    status="observer_error",
                    hook=hook,
                    observer=observer.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
        self.events.emit(
            node=str(self.state.get("stage", "lifecycle")),
            type=EventType.LIFECYCLE,
            status=hook,
            hook=hook,
            policy_hash=self.policy_hash,
            summary=str(facts.get("feedback") or hook),
            facts=facts,
            suggestions=suggestions,
        )
        return suggestions

    def versions(self) -> dict[str, str]:
        return {observer.name: observer.version for observer in self.observers}


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
