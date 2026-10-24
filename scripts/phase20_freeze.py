from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from codepilot.llm.dispatch_recovery import (
    RecoveryLedgerSource,
    build_multi_source_recovery_manifest,
    load_recovery_manifest,
)

SKIP = {".git", "workspace", "repo-cache", "env-cache", "node_modules", "__pycache__"}
PUBLIC_FIELDS = ("instance_id", "repo", "base_commit", "version", "environment_setup_commit")
EXPECTED_OPENING = 11.24078560


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new(path: Path, payload: object) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _used_instances(root: Path, valid_ids: list[str]) -> dict[str, str]:
    pattern = re.compile("|".join(re.escape(item) for item in valid_ids))
    used: dict[str, str] = {}
    for current, directories, files in os.walk(root / "artifacts"):
        current_path = Path(current)
        relative = current_path.relative_to(root).as_posix()
        for match in pattern.finditer(relative):
            used.setdefault(match.group(0), relative)
        directories[:] = [name for name in directories if name not in SKIP]
        for name in files:
            for match in pattern.finditer(name):
                used.setdefault(match.group(0), f"{relative}/{name}")
    for path in root.glob("glm-*.json"):
        if "numpy1264" in path.name and "numpy__numpy-1264" in valid_ids:
            used.setdefault("numpy__numpy-1264", path.relative_to(root).as_posix())
    return dict(sorted(used.items()))


def _catalog(root: Path, batch_id: str, seed: int, output: Path) -> None:
    revision = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
    snapshot = root / "cache/swebench" / f"princeton-nlp__SWE-bench_Lite__{revision}" / "test.jsonl"
    rows = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines()]
    excluded = _used_instances(root, [raw["instance_id"] for raw in rows])
    candidates = []
    for raw in rows:
        item = {key: raw[key] for key in PUBLIC_FIELDS}
        if item["instance_id"] not in excluded:
            candidates.append(item)
    candidates.sort(
        key=lambda item: hashlib.sha256(f"{seed}:{item['instance_id']}".encode()).hexdigest()
    )
    selected = []
    repositories = set()
    for item in candidates:
        if item["repo"] in repositories:
            continue
        selected.append(item)
        repositories.add(item["repo"])
        if len(selected) == 6:
            break
    if len(selected) != 6:
        raise RuntimeError(f"only {len(selected)} provably unused repository-diverse candidates")
    payload = {
        "schema_version": 1,
        "batch_id": batch_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": "princeton-nlp/SWE-bench_Lite",
        "split": "test",
        "dataset_revision": revision,
        "dataset_snapshot": str(snapshot.relative_to(root).as_posix()),
        "dataset_snapshot_sha256": _sha256(snapshot),
        "selection_algorithm": "sha256(f'{seed}:{instance_id}') ascending; one instance per repository; first six",
        "selection_seed": seed,
        "metadata_fields_used": list(PUBLIC_FIELDS),
        "forbidden_fields_not_used": [
            "patch",
            "test_patch",
            "FAIL_TO_PASS",
            "PASS_TO_PASS",
            "hints_text",
        ],
        "exclusion_method": "instance IDs from historical artifact directory/file names plus the numpy1264 live artifact",
        "excluded_instances": [
            {"instance_id": instance, "evidence": evidence}
            for instance, evidence in excluded.items()
        ],
        "candidate_count_after_exclusion": len(candidates),
        "instances": selected,
    }
    _write_new(output, payload)


def _opening(root: Path, output: Path) -> None:
    previous_path = root / "artifacts/phase18/l2-cal10e/recovery-manifest-l2cal10e-a21.json"
    previous = load_recovery_manifest(previous_path)
    sources = [
        RecoveryLedgerSource(root / item["path"], item["instance_id"])
        for item in previous["sources"]
    ]
    settled = [
        RecoveryLedgerSource(root / item["path"], item["instance_id"])
        for item in previous.get("additional_settled_ledgers") or []
    ]
    settled.append(
        RecoveryLedgerSource(
            root
            / "artifacts/phase18/l2-cal10e/pytest-dev__pytest-11148-a21/dispatch-ledger-l2cal10e-a21.json",
            "pytest-dev__pytest-11148",
        )
    )
    manifest = build_multi_source_recovery_manifest(
        sources,
        settled_ledgers=settled,
        run_id="m3-20261024-opening",
        reason="Phase 20 new authorization: historical upper bound recorded once, including a21",
        repo_root=root,
    )
    if abs(float(manifest["total_upper_bound_cny"]) - EXPECTED_OPENING) > 1e-9:
        raise RuntimeError(f"historical opening mismatch: {manifest['total_upper_bound_cny']}")
    _write_new(output, manifest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--batch-id", default="m3-20261024")
    parser.add_argument("--seed", type=int, default=20261024)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--opening", type=Path, required=True)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    catalog = args.catalog if args.catalog.is_absolute() else root / args.catalog
    opening = args.opening if args.opening.is_absolute() else root / args.opening
    _catalog(root, args.batch_id, args.seed, catalog)
    _opening(root, opening)


if __name__ == "__main__":
    main()
