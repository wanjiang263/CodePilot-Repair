"""Lightweight official-report serialization, independent of Agent runtime."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def jsonable(value: Any) -> Any:
    """把 pydantic / dataclass / Path / enum 统一转成可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return jsonable(dump(mode="json"))
    if hasattr(value, "__dataclass_fields__"):
        return {
            str(field_name): jsonable(getattr(value, field_name))
            for field_name in value.__dataclass_fields__
        }
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return str(value)


def harness_payload(execution: Any) -> dict[str, Any]:
    verdicts = []
    for item in getattr(execution, "verdicts", ()) or ():
        verdicts.append(
            {
                "instance_id": getattr(item, "instance_id", None),
                "verdict": jsonable(getattr(item, "verdict", None)),
                "resolved": getattr(item, "resolved", None),
                "report_path": jsonable(getattr(item, "report_path", None)),
                "report_sha256": getattr(item, "report_sha256", None),
                "error_kind": jsonable(getattr(item, "error_kind", None)),
                "error_detail": getattr(item, "error_detail", None),
                "runtime_seconds": getattr(item, "runtime_seconds", None),
            }
        )
    report = getattr(execution, "report", None)
    return {
        "run_id": getattr(execution, "run_id", None),
        "command": jsonable(getattr(execution, "command", None)),
        "decision": jsonable(getattr(execution, "decision", None)),
        "exit_code": getattr(execution, "exit_code", None),
        "timed_out": getattr(execution, "timed_out", None),
        "runtime_seconds": getattr(execution, "runtime_seconds", None),
        "error_kind": jsonable(getattr(execution, "error_kind", None)),
        "error_detail": getattr(execution, "error_detail", None),
        "reused": getattr(execution, "reused", None),
        "reused_reason": getattr(execution, "reused_reason", None),
        "warnings": list(getattr(execution, "warnings", ()) or ()),
        "verdicts": verdicts,
        "report_path": jsonable(getattr(execution, "report_path", None)),
        "report_sha256": getattr(execution, "report_sha256", None),
        "report": jsonable(report),
        "counts_toward_resolved_rate": getattr(execution, "included_in_resolved_rate", True),
    }
