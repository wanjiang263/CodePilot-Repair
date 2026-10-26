"""Official eligibility is patch legality and provenance, not internal acceptance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from codepilot.graph.stop_rules import patch_hash
from codepilot.patch.diff import generate_git_diff
from codepilot.patch.validator import PatchValidator, ValidatorConfig
from codepilot.repo.gitcmd import run_git
from codepilot.tools.base import WorkspacePathPolicy


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_candidate(
    *, project: Path, run: Path, instance: str, base: str, source_ledger: Path, revision: str
) -> Path | None:
    """Validate actual workspace bytes; freeze once all inference for this candidate ends."""
    workspace = run / "workspace"
    state_path = run / "state_final.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state["task_id"] != instance or state["base_commit"] != base:
        raise ValueError("candidate run/instance/base mismatch")
    if state["status"] == "running":
        raise ValueError("candidate inference has not ended")
    ledger = json.loads(source_ledger.read_text(encoding="utf-8"))
    recovery = run / "stage-recovery.json"
    if not ledger["requests"] and not recovery.exists():
        raise ValueError("candidate has no real Agent provenance")
    if run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip() != base:
        raise ValueError("candidate workspace base mismatch")
    policy = WorkspacePathPolicy(workspace, deny_existing_test_edits=True)
    diff = generate_git_diff(workspace, base, policy=policy)
    # Official legality is independent of the internal edit-size budget.
    validation = PatchValidator(
        policy,
        base,
        config=ValidatorConfig(
            max_files_changed=max(5, len(diff.files)),
            max_lines_changed=max(150, diff.lines_added + diff.lines_deleted),
        ),
    ).validate()
    patch = diff.text
    if not patch.strip():
        return None
    if not validation.passed:
        (run / "candidate-rejected.json").write_text(
            validation.model_dump_json(indent=2), encoding="utf-8"
        )
        return None
    traced = [
        state.get("current_patch", ""),
        *[a.get("patch_diff", "") for a in state.get("attempts", [])],
    ]
    if patch_hash(patch) not in {patch_hash(p) for p in traced if p.strip()}:
        raise ValueError("workspace candidate is not a recorded Agent candidate")
    patch_file = run / "official-candidate.diff"
    patch_file.write_text(patch, encoding="utf-8", newline="\n")
    prediction = run / "official-prediction.jsonl"
    prediction.write_text(
        json.dumps(
            {
                "instance_id": instance,
                "model_name_or_path": "glm-5.3-flash-main",
                "model_patch": patch,
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    evidence = [state_path, source_ledger]
    if recovery.exists():
        evidence.append(recovery)
    receipt = {
        "schema_version": 1,
        "instance_id": instance,
        "base_commit": base,
        "run_id": run.name,
        "dataset_revision": revision,
        "inference_finished": True,
        "internal_status": state["status"],
        "internal_stop_reason": state.get("stop_reason"),
        "patch": patch_file.relative_to(project).as_posix(),
        "patch_sha256": digest(patch_file),
        "canonical_patch_sha256": patch_hash(patch),
        "prediction": prediction.relative_to(project).as_posix(),
        "prediction_sha256": digest(prediction),
        "validation": validation.model_dump(mode="json"),
        "provenance": [
            {"path": p.relative_to(project).as_posix(), "sha256": digest(p)} for p in evidence
        ],
    }
    path = run / "official-candidate.json"
    path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def verify_candidate(project: Path, receipt: dict[str, Any]) -> tuple[Path, str]:
    def checked(name: str, expected: str) -> Path:
        path = (project / name).resolve(strict=True)
        if not path.is_relative_to(project.resolve()) or digest(path) != expected:
            raise ValueError("official candidate evidence escapes project or changed")
        return path

    if not receipt["inference_finished"] or not receipt["validation"]["passed"]:
        raise ValueError("candidate is not finished/legal")
    for item in receipt["provenance"]:
        checked(item["path"], item["sha256"])
    patch_path = checked(receipt["patch"], receipt["patch_sha256"])
    prediction = checked(receipt["prediction"], receipt["prediction_sha256"])
    rows = [json.loads(line) for line in prediction.read_text(encoding="utf-8").splitlines()]
    if len(rows) != 1 or rows[0]["instance_id"] != receipt["instance_id"]:
        raise ValueError("candidate prediction instance mismatch")
    if rows[0]["model_patch"].encode() != patch_path.read_bytes():
        raise ValueError("candidate prediction bytes mismatch")
    return prediction, receipt["patch_sha256"]
