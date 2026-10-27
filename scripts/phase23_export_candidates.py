"""Freeze all distinct legal Agent candidates after inference, with no new model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from codepilot.eval.candidate import prepare_candidate
from codepilot.graph.stop_rules import patch_hash
from codepilot.patch.unified_diff import apply_unified_diff
from codepilot.repo.workspace import WorkspaceManager
from codepilot.tools.base import WorkspacePathPolicy

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/phase23"


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--segment", required=True, type=int)
    args = parser.parse_args()
    segment = BASE / "instances" / args.instance / f"segment-{args.segment}"
    source = segment / "runs" / f"phase23-{args.instance}-s{args.segment}"
    state_file = source / "state_final.json"
    state = read(state_file)
    if state["status"] == "running":
        raise RuntimeError("inference not finished")
    previous = next(
        x
        for x in read(BASE / "opening-audit.json")["instances"]
        if x["instance_id"] == args.instance
    )
    original = ROOT / previous["source_run"]
    identity = read(source / "workspace_identity.json")
    manager = WorkspaceManager(
        cache_root=original.parent.parent / "repo-cache", default_mode="copy"
    )
    rows = []
    seen = set()
    final = source / "official-candidate.json"
    if final.exists():
        receipt = read(final)
        canonical = patch_hash((ROOT / receipt["patch"]).read_text(encoding="utf8"))
        seen.add(canonical)
        rows.append(
            {
                "source": "final",
                "canonical_patch_sha256": canonical,
                "receipt": final.relative_to(ROOT).as_posix(),
            }
        )
    for index, attempt in enumerate(state["attempts"], start=1):
        patch = attempt.get("patch_diff", "")
        if not patch.strip():
            continue
        canonical = patch_hash(patch)
        if canonical in seen:
            continue
        seen.add(canonical)
        target = source / "official-candidates" / f"{source.name}-c{canonical[:12]}"
        target.mkdir(parents=True, exist_ok=False)
        workspace = manager.create(
            source=identity["source"],
            base_commit=state["base_commit"],
            target=target / "workspace",
            mode="copy",
        )
        applied = apply_unified_diff(
            patch, WorkspacePathPolicy(workspace.path, deny_existing_test_edits=True)
        )
        if not applied.applied:
            rows.append(
                {
                    "source_attempt": index,
                    "canonical_patch_sha256": canonical,
                    "legal": False,
                    "reason": applied.error,
                }
            )
            continue
        # Byte-identical export of actual Agent state, never a fabricated checkpoint.
        shutil.copy2(state_file, target / "state_final.json")
        recovery = source / "stage-recovery.json"
        if recovery.exists():
            shutil.copy2(recovery, target / recovery.name)
        receipt = prepare_candidate(
            project=ROOT,
            run=target,
            instance=args.instance,
            base=state["base_commit"],
            source_ledger=segment / "dispatch-ledger.json",
            revision="6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2",
        )
        export = {
            "source_agent_run": source.relative_to(ROOT).as_posix(),
            "source_state_sha256": sha(state_file),
            "copied_state_sha256": sha(target / "state_final.json"),
            "source_attempt": index,
            "canonical_patch_sha256": canonical,
            "receipt": receipt.relative_to(ROOT).as_posix() if receipt else None,
            "legal": bool(receipt),
        }
        (target / "export-receipt.json").write_text(
            json.dumps(export, indent=2) + "\n", encoding="utf8"
        )
        rows.append(export)
    output = {
        "instance_id": args.instance,
        "source_run": str(source.relative_to(ROOT)),
        "source_state_sha256": sha(state_file),
        "candidates": rows,
        "new_model_requests": 0,
        "inference_finished_before_export": True,
    }
    (segment / "candidate-inventory.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf8"
    )
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
