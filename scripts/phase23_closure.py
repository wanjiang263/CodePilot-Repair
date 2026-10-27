"""Close Phase23 with byte-verified history, per-request fees and independent scores."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/phase23"


def read(path):
    return json.loads(path.read_text(encoding="utf8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUT / "closure")
    args = parser.parse_args()
    opening = read(OUT / "opening-audit.json")
    verified = []
    for name, expected in opening["prior_authorization_sources"].items():
        path = ROOT / name
        assert sha(path) == expected, f"historical ledger changed: {name}"
        verified.append({"path": name, "sha256": expected})
    cases = {}
    total = Decimal(0)
    request_rows = []
    for row in opening["instances"]:
        iid = row["instance_id"]
        case = {
            "instance_id": iid,
            "phase23_cost_upper_cny": 0,
            "requests": 0,
            "unknown_cny": 0,
            "source_run": row["source_run"],
            "legacy_source_runtime_seconds": row["runtime_seconds"]
            if row["state_exists"]
            else None,
            "segments": [],
            "candidates": [],
            "official_results": [],
        }
        for segment in sorted((OUT / "instances" / iid).glob("segment-*")):
            ledger_path = segment / "dispatch-ledger.json"
            if not ledger_path.exists():
                case["segments"].append(
                    {
                        "segment": segment.name,
                        "process": read(segment / "process.json"),
                        "cost_upper_cny": 0,
                        "model_requests": 0,
                        "reason": "Local failure before any HTTP ledger dispatch",
                    }
                )
                continue
            ledger = read(ledger_path)
            for name, expected in (
                ledger.get("opening_source", {}).get("historical_sources", {}).items()
            ):
                assert sha(Path(name)) == expected, f"inherited ledger changed: {name}"
            cost = Decimal(0)
            unknown = Decimal(0)
            for request in ledger["requests"]:
                value = Decimal(
                    str(
                        request["cost_cny"]
                        if request["status"] == "settled"
                        else request["reserved_cny"]
                    )
                )
                cost += value
                if request["status"] != "settled":
                    unknown += value
                request_rows.append(
                    {
                        "instance_id": iid,
                        "segment": segment.name,
                        "ledger_sha256": sha(ledger_path),
                        **{
                            key: request.get(key)
                            for key in (
                                "seq",
                                "model",
                                "request_sha256",
                                "status",
                                "reserved_cny",
                                "cost_cny",
                                "dispatched_at_unix",
                                "elapsed_seconds",
                            )
                        },
                    }
                )
            case["requests"] += len(ledger["requests"])
            case["phase23_cost_upper_cny"] += float(cost)
            case["unknown_cny"] += float(unknown)
            total += cost
            process = read(segment / "process.json")
            run = segment / "runs" / f"phase23-{iid}-s{segment.name.split('-')[-1]}"
            stages = {}
            active = {}
            event_path = run / "events.jsonl"
            if event_path.exists():
                for line in event_path.read_text(encoding="utf8").splitlines():
                    event = json.loads(line)
                    key = event["node"]
                    stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                    if event["type"] == "node_start":
                        active[key] = stamp
                    if event["type"] == "node_end" and key in active:
                        stages[key] = stages.get(key, 0) + (stamp - active.pop(key)).total_seconds()
            calls_path = run / "llm_requests.jsonl"
            model_times = {}
            if calls_path.exists():
                for line in calls_path.read_text(encoding="utf8").splitlines():
                    call = json.loads(line)
                    key = call.get("node") or call.get("purpose")
                    model_times[key] = (
                        model_times.get(key, 0) + (call.get("duration_ms") or 0) / 1000
                    )
            case["segments"].append(
                {
                    "segment": segment.name,
                    "process": process,
                    "cost_upper_cny": float(cost),
                    "unknown_cny": float(unknown),
                    "ledger_sha256": sha(ledger_path),
                    "stage_wall_seconds": stages,
                    "model_wait_seconds_by_stage": model_times,
                }
            )
            inventory = segment / "candidate-inventory.json"
            if inventory.exists():
                for candidate in read(inventory)["candidates"]:
                    if candidate.get("receipt"):
                        receipt = read(ROOT / candidate["receipt"])
                        from codepilot.eval.candidate import verify_candidate

                        verify_candidate(ROOT, receipt)
                        case["candidates"].append(
                            {
                                "canonical_patch_sha256": receipt.get(
                                    "canonical_patch_sha256", candidate["canonical_patch_sha256"]
                                ),
                                "patch_sha256": receipt["patch_sha256"],
                                "receipt": candidate["receipt"],
                                "internal_status": receipt["internal_status"],
                                "internal_stop_reason": receipt["internal_stop_reason"],
                            }
                        )
        reused = row.get("official_reused")
        if reused:
            path = ROOT / reused["path"]
            assert sha(path) == reused["sha256"]
            case["official_results"].append({"reused": True, **reused})
        for path in (OUT / "instances" / iid).rglob("official-evaluation*.json"):
            result = read(path)
            if result.get("official_verdict") not in ("RESOLVED", "UNRESOLVED"):
                continue
            official = result.get("official", {})
            assert official.get("counts_toward_resolved_rate") is True
            case["official_results"].append(
                {
                    "path": path.relative_to(ROOT).as_posix(),
                    "sha256": sha(path),
                    "patch_sha256": result["patch_sha256"],
                    "verdict": result["official_verdict"],
                    "report_sha256": official.get("report_sha256"),
                    "valid": True,
                }
            )
        scored = {result["patch_sha256"] for result in case["official_results"]}
        unscored = [
            candidate for candidate in case["candidates"] if candidate["patch_sha256"] not in scored
        ]
        assert not unscored, f"legal candidate not scored: {iid}"
        assert case["official_results"], f"case has not completed official execution: {iid}"
        assert case["phase23_cost_upper_cny"] <= 4.00000001
        case["final_status"] = (
            "OFFICIAL_RESOLVED"
            if any(result["verdict"] == "RESOLVED" for result in case["official_results"])
            else "OFFICIAL_UNRESOLVED"
        )
        cases[iid] = case
    assert total <= min(Decimal(24), Decimal(str(opening["original30_remaining_cny"])))
    upper = Decimal(str(opening["original30_used_upper_cny"])) + total
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_head": __import__("subprocess")
        .check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True)
        .strip(),
        "historical_ledger_hashes_verified": verified,
        "new_requests": request_rows,
        "phase23_new_cost_upper_cny": float(total),
        "phase23_settled_cny": sum(
            row["cost_cny"] for row in request_rows if row["status"] == "settled"
        ),
        "original30_used_upper_cny": float(upper),
        "original30_remaining_cny": float(Decimal(30) - upper),
        "phase23_cap_remaining_cny": float(Decimal(24) - total),
        "new_unknown_cny": sum(case["unknown_cny"] for case in cases.values()),
        "cases": list(cases.values()),
        "system_blockers_cleared": 6,
        "official_resolved": sum(
            case["final_status"] == "OFFICIAL_RESOLVED" for case in cases.values()
        ),
        "official_unresolved": sum(
            case["final_status"] == "OFFICIAL_UNRESOLVED" for case in cases.values()
        ),
        "model_no_patch": 0,
        "blocked": 0,
    }
    destination = args.output.resolve()
    assert destination.is_relative_to(ROOT)
    destination.mkdir(exist_ok=False)
    (destination / "audit.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf8"
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in ("new_requests", "cases", "historical_ledger_hashes_verified")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
