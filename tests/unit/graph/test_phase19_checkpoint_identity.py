"""Phase 19: incompatible checkpoints fail explicitly (not a silent fresh run)."""

from __future__ import annotations

import json
from pathlib import Path

from codepilot.graph.checkpoint import CHECKPOINT_SCHEMA_VERSION, checkpoint_identity_problem


def _manifest(tmp_path: Path, **overrides: object) -> Path:
    checkpoint = tmp_path / "checkpoints.sqlite"
    checkpoint.write_bytes(b"")
    payload = {
        "run_id": "run-a",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "base_commit": "a" * 40,
    }
    payload.update(overrides)
    (tmp_path / "checkpoint_manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return checkpoint


def test_missing_manifest_is_not_a_fresh_run(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoints.sqlite"
    checkpoint.write_bytes(b"")
    assert "without checkpoint_manifest" in (checkpoint_identity_problem(checkpoint, "run-a") or "")


def test_schema_or_run_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    checkpoint = _manifest(tmp_path, schema_version=999)
    assert "schema 999" in (checkpoint_identity_problem(checkpoint, "run-a") or "")

    checkpoint = _manifest(tmp_path, run_id="run-b")
    assert "run_id" in (checkpoint_identity_problem(checkpoint, "run-a") or "")


def test_compatible_manifest_is_accepted(tmp_path: Path) -> None:
    checkpoint = _manifest(tmp_path)
    assert checkpoint_identity_problem(checkpoint, "run-a") is None
