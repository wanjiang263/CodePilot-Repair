"""Import verified stage products into a new, explicitly budgeted run segment."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from codepilot.graph.deps import Deps
from codepilot.graph.stop_rules import patch_hash
from codepilot.patch.unified_diff import apply_unified_diff
from codepilot.schemas.run_config import StageRecovery
from codepilot.schemas.state import RepairState


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_stage(initial: RepairState, recovery: StageRecovery, deps: Deps) -> RepairState:
    source = recovery.source_state.resolve(strict=True)
    if sha256(source) != recovery.source_state_sha256:
        raise ValueError("recovery source state changed")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if (raw["run_id"], raw["task_id"], raw["base_commit"]) != (
        recovery.source_run_id,
        initial.task_id,
        initial.base_commit,
    ):
        raise ValueError("recovery run/instance/base identity mismatch")
    for name, expected in recovery.historical_ledger_hashes.items():
        if sha256(Path(name)) != expected:
            raise ValueError("historical recovery ledger changed")
    previous_root = source.parent
    proof_root = deps.artifacts.run_dir / "reused-evidence"
    proofs = []
    for name in ("attempts", "regression", "reproduction"):
        origin = previous_root / name
        if origin.is_dir():
            shutil.copytree(origin, proof_root / name)
            for path in origin.rglob("*"):
                if path.is_file():
                    proofs.append(
                        {"path": path.relative_to(previous_root).as_posix(), "sha256": sha256(path)}
                    )

    def relocate(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: relocate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relocate(v) for v in value]
        if isinstance(value, str):
            # Historical artifacts retain their bytes. Only imported path values
            # are translated to this new segment's verified proof copies.
            for prefix in (str(previous_root), str(previous_root).replace("\\", "/")):
                value = value.replace(prefix, str(proof_root))
            marker = "artifacts/phase22/"
            normalized = value.replace("\\", "/")
            if marker in normalized:
                tail = previous_root.as_posix().split(marker, 1)[1]
                old = "C:/Users/lenovo/Desktop/CodePilot-Repair/" + marker + tail
                normalized = normalized.replace(old, proof_root.as_posix())
                value = normalized
            return value
        return value

    fields = ("issue_analysis", "retrieval", "localization", "impact", "plan", "baseline")
    imported = {key: relocate(raw.get(key)) for key in fields}
    candidate = raw.get("current_patch", "") if recovery.candidate_sha256 else ""
    if recovery.candidate_sha256:
        if hashlib.sha256(candidate.encode()).hexdigest() != recovery.candidate_sha256:
            raise ValueError("recovery candidate bytes changed")
        applied = apply_unified_diff(candidate, deps.validator.policy)
        if not applied.applied:
            raise ValueError(f"recovery candidate cannot be applied: {applied.error}")
        validation = deps.validator.validate()
        if not validation.passed:
            raise ValueError(f"recovery candidate violates patch policy: {validation.errors}")
        imported.update(
            {
                key: relocate(raw.get(key))
                for key in ("attempts", "execution", "regression", "reproduction")
            }
        )
        imported["current_patch"] = candidate
        # The old blind review must never survive an explicit re-review import.
        for attempt in imported.get("attempts") or []:
            attempt["review"] = None
        imported["review"] = None
        imported["frozen_patch_hash"] = patch_hash(candidate)
    if recovery.entry in {"reproduce", "localize", "plan"}:
        imported.update(attempts=[], execution=None, regression=None, reproduction=None)
    state = RepairState.model_validate({**initial.model_dump(mode="json"), **imported})
    deps.artifacts.write_json(
        "stage-recovery.json",
        {
            **recovery.model_dump(mode="json"),
            "imported_fields": list(imported),
            "source_state_sha256": sha256(source),
            "proofs": proofs,
            "prior_usage": raw.get("usage"),
            "prior_test_runs": raw.get("test_runs"),
            "prior_started_at": raw.get("started_at"),
            "new_budget": initial.budget.model_dump(mode="json"),
            "new_run_id": initial.run_id,
            "candidate_sha256": recovery.candidate_sha256,
        },
    )
    return state
