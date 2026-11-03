"""Build docs/notes/phase32-runtime-results.json from the phase32 evidence files."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".codex-pytest-tmp" / "phase32"


def load(name: str) -> Any:
    path = SCRATCH / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"


core = load("final-gate-core.json")
graph = load("final-gate-graph.json")
gate = load("test-gate.json")
static = load("static-checks.json")
activation = load("activation-dryrun.json")
provider = load("provider-matrix.json")
official = load("official-probe.json")
index = load("index-profile.json")

rows = []
for run in core["runs"]:
    log = SCRATCH / f"gate-{run['suite']}-run{run['run']}.log"
    junit = SCRATCH / f"gate-{run['suite']}-run{run['run']}.xml"
    rows.append(
        {
            "group": run["suite"],
            "run": run["run"],
            "command": run["command"],
            "exit_code": run["exit_code"],
            "summary_line": run["summary_line"],
            "wall_s": run["wall_s"],
            "failed": run.get("failed", 0),
            "log": f".codex-pytest-tmp/phase32/gate-{run['suite']}-run{run['run']}.log",
            "log_sha256": sha256(log),
            "junit": f".codex-pytest-tmp/phase32/gate-{run['suite']}-run{run['run']}.xml",
            "junit_sha256": sha256(junit),
            "memory_before": run.get("memory_before"),
            "memory_after": run.get("memory_after"),
        }
    )

graph_row = {
    "group": "graph",
    "run": graph["runs"][0]["run"],
    "command": graph["runs"][0]["command"],
    "exit_code": graph["runs"][0]["exit_code"],
    "summary_line": graph["runs"][0]["summary_line"],
    "wall_s": graph["runs"][0]["wall_s"],
    "log": ".codex-pytest-tmp/phase32/gate-graph-run1.log",
    "log_sha256": sha256(SCRATCH / "gate-graph-run1.log"),
    "junit": ".codex-pytest-tmp/phase32/gate-graph-run1.xml",
    "junit_sha256": sha256(SCRATCH / "gate-graph-run1.xml"),
    "note": "13 docker-marked tests deselected with -m 'not docker'; the ten archive files reappear inside tests/unit and are not added twice",
}

payload = {
    "phase": 32,
    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    "base_commit": "0dbddfeef327410aac292ef0e7df7182c5222f02",
    "code_commits": {
        "docs_counts": "0d9a385",
        "errata": "d2c20bc",
        "receipt_generator": "f2d8612",
        "test_gate": "b251ac7",
        "phase32_harness": "5e18317",
    },
    "final_regression": {
        "graph": graph_row,
        "groups": rows,
    },
    "first_round_failures": [
        {
            "group": "unit",
            "run": 2,
            "summary_line": gate["runs"][1]["summary_line"]
            if gate and len(gate.get("runs", [])) > 1
            else None,
            "log": ".codex-pytest-tmp/phase32/gate-unit-run2.log",
            "log_sha256": sha256(SCRATCH / "gate-unit-run2.log"),
        },
        {
            "group": "api",
            "run": 1,
            "summary_line": gate["runs"][2]["summary_line"]
            if gate and len(gate.get("runs", [])) > 2
            else None,
            "log": ".codex-pytest-tmp/phase32/gate-api-run1.log",
            "log_sha256": sha256(SCRATCH / "gate-api-run1.log"),
        },
    ],
    "static_checks": {
        "command": ".venv/Scripts/python.exe scripts/phase32_static_checks.py",
        "file_count": static["file_count"],
        "files": static["files"],
        "failed": static["failed"],
        "checks": [
            {"name": check["name"], "exit_code": check["exit_code"]} for check in static["checks"]
        ],
        "docs_copy": "docs/notes/phase32-static-checks.json",
        "docs_copy_sha256": sha256(ROOT / "docs" / "notes" / "phase32-static-checks.json"),
    },
    "archive_verify_v5": {
        "command": ".venv/Scripts/python.exe scripts/phase28_archive_verify.py --all --ignore-raw-drift --receipt configs/harness/receipts/phase31-candidate-v5.json --expect-receipt d2689f7afcfee18d89c362eb81cfdc87a66d464c187b9dcbed6bcc1fee0595d8",
        "log": ".codex-pytest-tmp/phase32/verify-v5-postchange.log",
        "log_sha256": sha256(SCRATCH / "verify-v5-postchange.log"),
        "versions": 11,
        "new": 4,
        "old": 7,
        "new_failures": 0,
        "unexplained": 0,
        "historical_files_modified": False,
        "manifest_binding": "manifest_commit",
        "witness": "verified",
        "historical_integrity": "ok",
        "phase28_candidate_v2_note": "legacy_worktree_drift over 11 files; ignored by design with --ignore-raw-drift",
    },
    "activation_dryrun": {
        "real_pointers_before": activation["real_pointers_before"],
        "temp_archive": activation["temp_archive"],
        "v5_candidate_sha256_before": activation["v5_candidate_sha256_before"],
        "v4_candidate_sha256_before": activation["v4_candidate_sha256_before"],
        "v5_still_pending": True,
        "active_pointer_created": False,
    },
    "provider_matrix": {
        "fake_pytest_summary": provider["pytest"]["summary"],
        "fake_totals": provider["totals_from_junit"],
        "real_requests": 0,
        "real_cost_cny": 0,
        "budget_env_present": provider["real_provider"]["budget"]["raw_present"],
        "blocked_rows": [
            {"row": row["row"], "missing": row["missing"]}
            for row in provider["real_provider"]["rows"]
        ],
        "docs_copy": "docs/notes/phase32-provider-matrix.json",
        "docs_copy_sha256": sha256(ROOT / "docs" / "notes" / "phase32-provider-matrix.json"),
    },
    "docker": {
        "official_probe": {
            "instance_id": official["instance_id"],
            "status": official["status"],
            "image_ref": official["image"]["image_ref"],
            "image_id": official["image"]["image_id"],
            "duration_s": round(official["duration_seconds"], 3),
            "from_cache": official["environment"]["from_cache"],
            "log": ".codex-pytest-tmp/phase32/official-probe.json",
        },
        "minimal_e2e": {
            "exit_code": 1,
            "wall_s": 310.36,
            "log": ".codex-pytest-tmp/phase32/docker-minimal.log",
            "reason": "pytest-timeout fired while the controlled fixture ran on real containers",
        },
    },
    "index_profile": {
        "export": index["export"],
        "probe_file": index["probe_file"],
        "runs": [
            {
                "scenario": run["scenario"],
                "association_mode": run["association_mode"],
                "fast_mode": run.get("fast_mode"),
                "wall_index_s": run["wall_index_s"],
                "wall_commit_s": run["wall_commit_s"],
                "scan_files": run["scan_files"],
                "symbol_rows": run["symbol_rows"],
                "edge_rows": run["edge_rows"],
                "parsed": run["parsed"],
                "content_reused": run["content_reused"],
                "parse_errors": run["parse_errors"],
                "db_bytes": run["db_bytes"],
                "peak_working_set_bytes": run["peak_working_set_bytes"],
                "child_total_wall_s": run["child_total_wall_s"],
            }
            for run in index["runs"]
        ],
    },
    "page_e2e": {
        "command": "git diff --name-only 0dbddfe..HEAD -- web/src",
        "changed_files": 0,
        "tracked_files_in_scope": 75,
        "result": "conditionally skipped: web/src has zero diff in this phase",
    },
}

text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
out = ROOT / "docs" / "notes" / "phase32-runtime-results.json"
out.write_text(text, encoding="utf-8", newline="\n")
print(json.dumps({"written": str(out.relative_to(ROOT)), "groups": len(rows)}, indent=2))
