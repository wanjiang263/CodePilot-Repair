"""Shared constants/helpers with a unique module name.

`conftest` is not a safe import anchor for cross-directory tests: pytest loads
several `tests/*/conftest.py` files under the same top-level module name, and a
full-suite run can resolve the docker conftest instead. This module keeps the
Phase 13 helpers uniquely importable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from codepilot.config import PROJECT_ROOT, Settings

KEY = "test-key-0123456789abcdef"
HEADERS = {"X-API-Key": KEY}
DEMO_ISSUE = "add(2, 3) returns 4 but the contract says add must return the exact sum of 5."


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    catalog = tmp_path / "benchmarks.yaml"
    if not catalog.exists():
        catalog.write_text("benchmarks: []\n", encoding="utf-8")
    base: dict[str, object] = {
        "api_key": KEY,
        "api_queue_path": tmp_path / "api" / "queue.sqlite3",
        "artifacts_dir": tmp_path / "artifacts",
        "cache_dir": tmp_path / "cache",
        "api_demo_root": PROJECT_ROOT / "demo",
        "api_history_catalog": catalog,
        "api_worker_poll_seconds": 0.05,
        "api_sse_poll_seconds": 0.05,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def sse_payloads(text: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data: list[str] = []
        event = "message"
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if not data:
            continue
        try:
            payload = json.loads("\n".join(data))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload["_event"] = event
            payloads.append(payload)
    return payloads


def snapshot_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
