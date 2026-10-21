"""Task/run status normalization for the HTTP surface.

The API must never flatten Phase 11/12 terminal states into a generic success or
failure. ``raw_status`` and ``stop_reason`` stay authoritative; ``ui_status`` is a
small presentation vocabulary used for grouping and colour. A budget stop is
therefore visible as a stop, not as a model failure.
"""

from __future__ import annotations

from codepilot.schemas.enums import RunStatus

__all__ = [
    "UI_STATUSES",
    "is_terminal",
    "normalize_status",
    "status_label",
    "ui_status_for",
]

#: Presentation vocabulary. ``uncertain`` is produced only by worker recovery:
#: the process died and the external side effects cannot be proven either way.
UI_STATUSES = frozenset(
    {"queued", "running", "completed", "stopped", "failed", "cancelled", "error", "uncertain"}
)

_TERMINAL_UI = frozenset({"completed", "stopped", "failed", "cancelled", "error", "uncertain"})

_RAW_TO_UI: dict[str, str] = {
    "queued": "queued",
    "running": "running",
    "succeeded": "completed",
    "provisionally_verified": "completed",
    "sampled_verified": "completed",
    "m2_verified": "completed",
    "failed": "failed",
    "tests_failed": "failed",
    "patch_rejected": "failed",
    "plan_rejected": "failed",
    "sandbox_error": "failed",
    "tool_error": "failed",
    "stopped": "stopped",
    "budget_exhausted": "stopped",
    "cancelled": "cancelled",
    "cancel_requested": "running",
    "error": "error",
    "uncertain": "uncertain",
}


def is_terminal(ui_status: str) -> bool:
    return ui_status in _TERMINAL_UI


def ui_status_for(raw_status: str, stop_reason: str | None = None) -> str:
    """Return the presentation status while keeping budget stops out of "failed".

    ``stop_reason`` is used as a secondary signal because a worker may persist a
    generic ``stopped`` status for a budget rule. A ``budget_*`` reason always
    wins; a cancelled reason always wins over a transient generic status.
    """
    normalized = (raw_status or "").strip().lower()
    reason = (stop_reason or "").strip().lower()
    if normalized in {"cancelled", "cancel_requested"} or reason == "cancelled":
        return "cancelled" if normalized != "cancel_requested" else "running"
    if reason.startswith("budget_") and normalized not in {"succeeded", "provisionally_verified"}:
        return "stopped"
    return _RAW_TO_UI.get(normalized, "error")


def normalize_status(raw: RunStatus | str, stop_reason: str | None = None) -> dict[str, str]:
    raw_value = raw.value if isinstance(raw, RunStatus) else str(raw)
    return {
        "status": raw_value,
        "raw_status": raw_value,
        "ui_status": ui_status_for(raw_value, stop_reason),
        "stop_reason": stop_reason or "",
    }


def status_label(ui_status: str) -> str:
    return {
        "queued": "Queued",
        "running": "Running",
        "completed": "Completed",
        "stopped": "Stopped",
        "failed": "Failed",
        "cancelled": "Cancelled",
        "error": "Error",
        "uncertain": "Result uncertain",
    }.get(ui_status, "Unknown")
