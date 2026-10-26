"""Serializable execution and recovery options, shared by launcher and assembly."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codepilot.schemas.budget import RepairBudget


class IndexSnapshot(BaseModel):
    path: Path
    sha256: str


class StageRecovery(BaseModel):
    source_state: Path
    source_state_sha256: str
    source_run_id: str
    entry: Literal[
        "localize", "analyze_impact", "build_env", "reproduce", "plan", "validate_patch", "review"
    ]
    candidate_sha256: str | None = None
    historical_ledger_hashes: dict[str, str]
    reason: str = Field(min_length=1)
    previous_runtime_seconds: float = 0


class InferenceRunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_alias: str = "glm-5.3-flash-main"
    budget: RepairBudget = Field(default_factory=RepairBudget)
    deny_existing_test_edits: bool = True
    discover_missing_targets: bool = True
    index_snapshot: IndexSnapshot | None = None
    recovery: StageRecovery | None = None
    with_reproducer: bool = True
    allow_new_tests: bool = False
