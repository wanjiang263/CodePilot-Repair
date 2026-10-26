"""Build a small, publishable evidence bundle after the supervised batch stops."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "artifacts/phase22"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    supervisor_path = max(BASE.glob("supervisor-r5*.json"), key=lambda p: p.stat().st_mtime)
    supervisor = read(supervisor_path)
    if supervisor["status"] != "exited":
        raise RuntimeError("batch still running")
    latest = BASE / "live-r5"
    state = read(latest / "batch-state.json")
    opening = read(BASE / "opening-audit.json")
    rows = []
    sources = []
    for entry in state["instances"]:
        iid = entry["instance_id"]
        ledgers = list(BASE.glob(f"live*/instances/{iid}/dispatch-ledger-*.json"))
        settled = unknown = 0.0
        requests = []
        for path in ledgers:
            ledger = read(path)
            settled += sum(r["cost_cny"] for r in ledger["requests"] if r["status"] == "settled")
            unknown += sum(r["reserved_cny"] for r in ledger["requests"] if r["status"] != "settled")
            requests.extend(ledger["requests"])
            sources.append({"path": path.relative_to(ROOT).as_posix(), "sha256": sha(path), "opening_balance": ledger.get("opening_balance", 0), "requests": ledger["requests"], "frozen": ledger["frozen"]})
        item = {**entry, "dispatched": bool(requests), "http_requests": len(requests), "phase22_settled_cny": settled, "phase22_unknown_reserved_cny": unknown}
        if iid == "sphinx-doc__sphinx-8273":
            item.update(status="DISPATCHED_STOPPED", internal="SYSTEM_CONFIG_MISMATCH", terminal=True)
        if iid == "sympy__sympy-22714":
            item.update(read(BASE / "sympy-final-supplement.json"))
        reports = list(BASE.glob(f"live*/instances/{iid}/runs/*/report.json"))
        if item.get("run_id"):
            reports = [p for p in reports if p.parent.name == item["run_id"]]
        elif ledgers:
            paid_roots = {p.parent for p in ledgers if read(p).get("requests")}
            reports = [p for p in reports if p.parent.parent.parent in paid_roots]
        item["workflow_report"] = None
        if len(reports) == 1:
            report = read(reports[0])
            item["run_id"] = report.get("run_id")
            wrapper_path = reports[0].parent.parent.parent / "reports" / (report["run_id"] + ".json")
            wrapper = read(wrapper_path) if wrapper_path.exists() else {}
            item["workflow_report"] = {"path": reports[0].relative_to(ROOT).as_posix(), "sha256": sha(reports[0]), "status": report.get("status"), "stop_reason": report.get("stop_reason"), "failure_reasons": report.get("failure_reasons"), "runtime_seconds": report.get("runtime_seconds"), "test_runs": report.get("test_runs"), "budget": report.get("budget") or wrapper.get("budget"), "closure": report.get("closure"), "usage": report.get("usage")}
            item["acceptance"] = []
            for p in reports[0].parent.parent.parent.glob("acceptance-*.json"):
                a = read(p)
                item["acceptance"].append({"path":p.relative_to(ROOT).as_posix(),"sha256":sha(p),"verdict":a.get("verdict"),"failed_checks":[k for k,v in a.get("checks",{}).items() if not v]})
            item["review_input_evidence"] = []
            for p in reports[0].parent.glob("review/*-model.json"):
                model = read(p)
                supplied = model.get("patch", report.get("git_diff", ""))
                request = model.get("request", "")
                item["review_input_evidence"].append({"path":p.relative_to(ROOT).as_posix(),"sha256":sha(p),"request_sha256":hashlib.sha256(request.encode()).hexdigest(),"full_patch_in_request":bool(supplied and supplied in request),"candidate_patch_sha256":model.get("candidate_patch_sha256")})
        item["official_record"] = []
        for p in BASE.glob(f"live*/instances/{iid}/official-evaluation-*.json"):
            o = read(p)
            item["official_record"].append({"path":p.relative_to(ROOT).as_posix(),"sha256":sha(p),"verdict":o.get("official_verdict"),"counts_toward_resolved_rate":o.get("official",{}).get("counts_toward_resolved_rate",False),"report_sha256":o.get("official",{}).get("report_sha256")})
        item["process"] = [read(p) for p in BASE.glob(f"live*/instances/{iid}/process.json")]
        rows.append(item)
    settled = sum(r["phase22_settled_cny"] for r in rows)
    unknown = sum(r["phase22_unknown_reserved_cny"] for r in rows)
    dispatched = sum(r["dispatched"] for r in rows)
    accepted = sum(r["internal"] == "ACCEPTED" for r in rows)
    valid = sum(r["official"] in {"RESOLVED", "UNRESOLVED"} for r in rows)
    resolved = sum(r["official"] == "RESOLVED" for r in rows)
    result = {"scope": "seen-instance engineering recovery, not unseen generalization", "batch_status": state["status"], "stop_reason": state["stop_reason"], "instances": rows, "ledger_evidence": sources,
        "coverage": {"dispatched": dispatched, "planned": 6, "trusted_terminal_records": sum(r["terminal"] for r in rows), "internal_accepted": accepted, "valid_official": valid},
        "cost": {"historical_opening_cny": opening["historical_opening_cny"], "phase20_21_authorized_upper_cny": opening["original30_used_upper_cny"], "phase22_settled_cny": settled, "phase22_unknown_reserved_cny": unknown, "original30_used_upper_cny": opening["original30_used_upper_cny"] + settled + unknown, "original30_remaining_cny": 30-opening["original30_used_upper_cny"]-settled-unknown, "all_history_upper_cny": opening["full_opening_cny"]+settled+unknown},
        "metrics": {"official_coverage": valid/6, "valid_official_resolution_rate": resolved/valid if valid else None, "task_resolution_rate": resolved/6, "internal_closure_rate": accepted/6, "provider_stop_rate_per_dispatched": sum(r["internal"] == "PROVIDER_USAGE_UNCERTAIN" for r in rows)/dispatched if dispatched else None},
        "next_stage_admitted": dispatched == 6 and valid > 0 and unknown == 0 and state["status"] == "completed" and all(r["terminal"] for r in rows),
        "source_hashes": {str(p.relative_to(ROOT)):sha(p) for p in [latest/"batch-state.json", latest/"batch-summary.json", BASE/"environment-summary.json", BASE/"provider-audit.json"]}}
    (BASE/"results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    print(json.dumps({"coverage":result["coverage"], "cost":result["cost"], "next_stage_admitted":result["next_stage_admitted"]},indent=2))


if __name__ == "__main__":
    main()
