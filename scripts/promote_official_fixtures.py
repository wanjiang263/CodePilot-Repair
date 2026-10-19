"""Promote the real official 4.1.0 control outputs into committed fixtures (Phase 11R).

Reads the evidence produced by scripts/swebench_official_control.py under
artifacts/phase11-wsl/official-control and writes the two fixtures plus a
provenance file under tests/fixtures/swebench_harness/. Run once, by hand::

    uv run python scripts/promote_official_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "artifacts" / "phase11-wsl" / "official-control"
FIXTURES = ROOT / "tests" / "fixtures" / "swebench_harness"

INSTANCE = "marshmallow-code__marshmallow-1343"
GOLD_REPORT = EVID / "gold/official/codepilot-oracle-control.cp-oracle-b6712c8b1352-oracle.json"
FAIL_LOG = (
    EVID / "garbage/official/logs/run_evaluation/cp-56a6f0f92b13-negative/"
    "codepilot__negative-control/marshmallow-code__marshmallow-1343/run_instance.log"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    gold = json.loads(GOLD_REPORT.read_text(encoding="utf-8"))
    assert gold["resolved_ids"] == [INSTANCE], "gold 控制必须是官方 resolved"

    tail = FAIL_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    keep: list[str] = []
    for line in tail:
        if "run_evaluation" in line or "swebench-venv" in line or "/root/" in line:
            continue
        keep.append(line)
    excerpt = "\n".join(keep[-14:]).strip() + "\n"
    assert ">>>>> Patch Apply Failed" in excerpt, "日志摘录必须含官方补丁失败标记"

    (FIXTURES / "real-gold-report-4.1.0.json").write_text(
        json.dumps(gold, ensure_ascii=False, indent=4) + "\n", encoding="utf-8", newline="\n"
    )
    (FIXTURES / "real-patch-apply-fail-4.1.0.log").write_text(
        excerpt, encoding="utf-8", newline="\n"
    )
    provenance = {
        "generated_at": "2026-10-19",
        "source": "scripts/swebench_official_control.py 的真实官方执行产物",
        "harness_version": "4.1.0",
        "dataset_name": "princeton-nlp/SWE-bench_Lite",
        "dataset_revision": "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2",
        "split": "dev",
        "instance_id": INSTANCE,
        "gold_run_id": "cp-oracle-b6712c8b1352-oracle",
        "gold_official_report_name": GOLD_REPORT.name,
        "patch_apply_fail_run_id": "cp-56a6f0f92b13-negative",
        "instance_image": "swebench/sweb.eval.x86_64.marshmallow-code_1776_marshmallow-1343:latest",
        "instance_image_id": "sha256:a61197e0b29461e34010cd83570424eb91a7f265574a81c82ff79e55c6c10f29",
        "runtime": "WSL2 Ubuntu-24.04 + Docker Desktop 29.8.2 (linux/amd64)",
        "files": {
            "real-gold-report-4.1.0.json": {
                "official_report_sha256_at_run": _sha(GOLD_REPORT),
                "note": "官方汇总报告原文，schema_version=2，client 非空故无 unstopped_* 三键",
            },
            "real-patch-apply-fail-4.1.0.log": {
                "note": "官方 run_instance.log 尾部摘录；绝对路径行已剔除，标记与 patch 报错原文保留",
            },
        },
    }
    (FIXTURES / "real-run-provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print("fixtures written to", FIXTURES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
