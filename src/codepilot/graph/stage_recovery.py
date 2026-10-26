"""Import verified stage products into a new, explicitly budgeted run segment."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from codepilot.graph.deps import Deps
from codepilot.graph.stop_rules import patch_hash
from codepilot.observability.redaction import redact_text
from codepilot.patch.unified_diff import _patch_files, apply_unified_diff
from codepilot.schemas.run_config import StageRecovery
from codepilot.schemas.state import RepairState
from codepilot.tools.base import is_test_path


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
    previous_root = (
        recovery.source_artifacts.resolve(strict=True)
        if recovery.source_artifacts
        else source.parent
    )
    proof_root = deps.artifacts.run_dir / "reused-evidence"
    proofs = []
    for name in ("attempts", "regression", "reproduction", "reused-evidence"):
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

    fields: tuple[str, ...] = (
        "issue_analysis",
        "retrieval",
        "localization",
        "impact",
        "plan",
        "baseline",
    )
    if recovery.entry in {"review", "run_regression"}:
        # Review uses the full candidate, plan, changed-source excerpts and
        # validation facts; replaying 40MB of retrieval history adds no evidence.
        fields = tuple(key for key in fields if key != "retrieval")
    imported = {key: relocate(raw.get(key)) for key in fields}
    candidate = raw.get("current_patch", "") if recovery.candidate_sha256 else ""
    preserved = None
    if recovery.preserve_source_edits:
        if recovery.entry != "plan":
            raise ValueError("source-only recovery must restart planning")
        original_patch = raw.get("current_patch", "")
        blocks = re.split(r"(?=^diff --git )", original_patch, flags=re.MULTILINE)
        kept: list[str] = []
        excluded: list[str] = []
        for block in blocks:
            if not block.strip():
                continue
            files = _patch_files(block)
            if not files:
                raise ValueError("source recovery contains an unrecognized diff block")
            if any(is_test_path(path) for path, _ in files):
                excluded.extend(path for path, _ in files)
            else:
                kept.append(block)
        candidate = "".join(kept)
        deps.artifacts.write_text("preserved-source.diff", candidate)
        preserved = {
            "original_patch_sha256": hashlib.sha256(original_patch.encode()).hexdigest(),
            "source_patch_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
            "excluded_test_paths": excluded,
        }
    if (
        recovery.candidate_sha256
        and hashlib.sha256(candidate.encode()).hexdigest() != recovery.candidate_sha256
    ):
        raise ValueError("recovery candidate bytes changed")
    if candidate:
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
    failures = list(dict.fromkeys(raw.get("failure_reasons", [])))
    blocked_paths = sorted(
        set(re.findall(r"(?:not allowed:|read-only:)\s*([^\s;]+\.py)", "\n".join(failures)))
    )
    candidates = [item.get("path", "") for item in (raw.get("localization") or {}).get("files", [])]
    candidates += [item.get("file", "") for item in (raw.get("plan") or {}).get("changes", [])]
    candidates = list(dict.fromkeys(path for path in candidates if path and not is_test_path(path)))
    if not candidates and blocked_paths and hasattr(deps, "index"):
        stems = {Path(path).stem.removeprefix("test_") for path in blocked_paths}
        candidates = [
            item.path
            for item in deps.index.files()
            if Path(item.path).stem in stems and not is_test_path(item.path)
        ][:12]
    feedback = json.dumps(
        {
            "reason": recovery.reason,
            "prior_internal_failures": [redact_text(item)[:1000] for item in failures[:4]],
            "read_only_test_paths": blocked_paths,
            "actual_source_candidates": candidates[:12],
            "policy": "Repair production source. Prior candidates modifying existing tests were invalid; preserve legal source edits and change diagnosis rather than repeat rejected writes.",
        },
        ensure_ascii=False,
    )
    for agent in (getattr(deps, "coder", None), getattr(deps, "planner", None)):
        if agent is not None:
            agent.execution_feedback = feedback

    deps.artifacts.write_json(
        "stage-recovery.json",
        {
            **recovery.model_dump(mode="json"),
            "imported_fields": list(imported),
            "execution_feedback": json.loads(feedback),
            "preserved_source_edits": preserved,
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
