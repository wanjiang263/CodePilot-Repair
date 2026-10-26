"""Read-only historical audit and explicit opening for the authorized continuation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/phase23"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    from codepilot.llm.dispatch_recovery import (
        RecoveryLedgerSource,
        build_multi_source_recovery_manifest,
    )

    previous = read(ROOT / "configs/phase22/opening-r5.json")
    paths = {
        item["path"]
        for key in ("sources", "additional_settled_ledgers")
        for item in previous.get(key, [])
    }
    result = read(ROOT / "artifacts/phase22/results.json")
    prior = read(ROOT / "artifacts/phase22/opening-audit.json")
    authorized = {}
    for item in [*prior["prior_authorization_sources"], *result["ledger_evidence"]]:
        path = ROOT / item["path"]
        assert sha(path) == item["sha256"], path
        authorized[item["path"]] = item["sha256"]
        paths.add(item["path"])
    used = 0.0
    for name in authorized:
        ledger = read(ROOT / name)
        used += sum(
            row["cost_cny"] if row["status"] == "settled" else row["reserved_cny"]
            for row in ledger["requests"]
        )
    remaining = 30 - used
    assert abs(remaining - 27.96439928) < 1e-8
    manifest = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(ROOT / p, Path(p).parent.name) for p in sorted(paths)],
        run_id="phase23-authorized-continuation",
        reason="Explicit user authorization: purposeful recovery within original30; retain all unknown reservations",
        repo_root=ROOT,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    opening = OUT / "opening.json"
    if not opening.exists():
        opening.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    rows = []
    for entry in result["instances"]:
        iid = entry["instance_id"]
        report = entry.get("workflow_report")
        if report:
            path = ROOT / report["path"]
            assert sha(path) == report["sha256"]
            run = path.parent
        else:
            run = next((ROOT / "artifacts/phase22/live/instances" / iid / "runs").iterdir())
        state_file = run / "state_final.json"
        state = read(state_file) if state_file.exists() else {}
        rows.append(
            {
                "instance_id": iid,
                "source_run": run.relative_to(ROOT).as_posix(),
                "state_exists": state_file.exists(),
                "state_sha256": sha(state_file) if state_file.exists() else None,
                "base_commit": state.get("base_commit"),
                "patch_bytes": len(state.get("current_patch", "").encode()),
                "runtime_seconds": report.get("runtime_seconds", 0) if report else 0,
                "attempts": len(state.get("attempts", [])),
                "plan": state.get("plan"),
                "has_baseline": state.get("baseline") is not None,
                "has_reproduction": state.get("reproduction") is not None,
                "status": state.get("status"),
                "stop_reason": state.get("stop_reason"),
            }
        )
        if iid.startswith("scikit-learn"):
            record = entry["official_record"][0]
            official = ROOT / record["path"]
            assert sha(official) == record["sha256"]
            assert read(official)["official_verdict"] == "RESOLVED"
            patch = (run / "patch_final.diff").read_text(encoding="utf-8")
            predictions = list((run.parent.parent / "predictions").glob("*.jsonl"))
            matching = [p for p in predictions if p.stem == run.name]
            assert len(matching) == 1
            prediction = json.loads(matching[0].read_text(encoding="utf-8"))
            assert prediction["model_patch"] == patch and prediction["instance_id"] == iid
            rows[-1]["official_reused"] = {
                "path": record["path"],
                "sha256": record["sha256"],
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                "prediction_sha256": sha(matching[0]),
                "verdict": "RESOLVED",
            }
    audit = {
        "root": str(ROOT),
        "prior_authorization_sources": authorized,
        "original30_used_upper_cny": used,
        "original30_remaining_cny": remaining,
        "phase23_cap_cny": min(24, remaining),
        "per_unfinished_instance_cap_cny": 4,
        "opening_sha256": sha(opening),
        "instances": rows,
    }
    (OUT / "opening-audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
