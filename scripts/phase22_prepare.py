"""Prepare the explicitly authorized recovery segment without any model requests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codepilot.llm.dispatch_recovery import (  # noqa: E402
    RecoveryLedgerSource,
    build_multi_source_recovery_manifest,
    validate_recovery_manifest,
)


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    output = ROOT / "configs/phase22"
    previous = read(ROOT / "configs/phase21/m3-opening-phase21.json")
    validate_recovery_manifest(previous, repo_root=ROOT)
    historical = [*previous["sources"], *previous["additional_settled_ledgers"]]
    prior_paths = sorted(
        (ROOT / "artifacts/phase20/m3-20261024-formal6/instances").glob("*/dispatch-ledger-*.json")
    )
    prior_paths += sorted(
        (ROOT / "artifacts/phase21/m3-phase21-recovery-6-r5/instances").glob(
            "*/dispatch-ledger-*.json"
        )
    )
    prior = []
    prior_upper = 0.0
    for path in prior_paths:
        ledger = read(path)
        amount = sum(
            float(r["cost_cny"] if r["status"] == "settled" else r["reserved_cny"])
            for r in ledger["requests"]
        )
        prior_upper += amount
        prior.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha(path),
                "upper_bound_cny": amount,
            }
        )
    if len(prior) != 3:
        raise ValueError("expected exactly the Phase20 two and Phase21 one ledgers")
    manifest = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(ROOT / entry["path"], entry["instance_id"]) for entry in historical]
        + [RecoveryLedgerSource(path, path.parent.name) for path in prior_paths],
        run_id="phase22-explicit-recovery-opening",
        reason="User explicitly authorized Phase22; carry every unknown at full reservation, not actual settlement",
        repo_root=ROOT,
    )
    write_new(output / "opening.json", manifest)
    catalog = read(ROOT / "configs/phase21/m3-catalog-phase21.json")
    catalog["batch_id"] = "phase22-six-live"
    order = [
        "sphinx-doc__sphinx-8273",
        "sympy__sympy-22714",
        "matplotlib__matplotlib-18869",
        "scikit-learn__scikit-learn-10297",
        "psf__requests-3362",
        "django__django-11422",
    ]
    catalog["instances"].sort(key=lambda item: order.index(item["instance_id"]))
    write_new(output / "catalog.json", catalog)
    llm = yaml.safe_load((ROOT / "configs/llm.yaml").read_text(encoding="utf-8"))
    llm["providers"]["bigmodel"].update(
        timeout_seconds=120,
        connect_timeout_seconds=10,
        write_timeout_seconds=30,
        pool_timeout_seconds=10,
        trust_env=False,
        max_retries=0,
    )
    output.mkdir(parents=True, exist_ok=True)
    with (output / "llm.yaml").open("x", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(llm, handle, allow_unicode=True, sort_keys=False)
    config = read(ROOT / "configs/phase21/m3-config-phase21.json")
    config.update(
        batch_id="phase22-six-live",
        batch_name="Phase22原六实例真实恢复批",
        llm_config="configs/phase22/llm.yaml",
        opening_manifest="configs/phase22/opening.json",
        historical_opening_cny=manifest["total_upper_bound_cny"],
        batch_new_limit_cny=30 - prior_upper,
        original_authorization_cny=30,
        prior_authorization_sources=prior,
        official_mode="wsl",
        cost_note="Historical opening includes Phase20/21 once; remaining original30 independently derived from their requests. No diagnostic model call.",
    )
    write_new(output / "config.json", config)
    oldroot = ROOT / "artifacts/phase21/m3-phase21-recovery-6-r5"
    oldstate = read(oldroot / "batch-state.json")
    for entry in oldstate["instances"]:
        django = entry["instance_id"] == "django__django-11422"
        entry.update(
            status="DISPATCHED_STOPPED" if django else "NOT_DISPATCHED",
            internal="PROVIDER_USAGE_UNCERTAIN" if django else "NOT_RUN",
            stop_reason="PROVIDER_USAGE_UNCERTAIN" if django else "BLOCKED_BY_FROZEN_LEDGER",
            official="NOT_EVALUATED",
            terminal=True,
        )
    write_new(
        ROOT / "artifacts/phase22/phase21-final-supplement.json",
        {
            "source_sha256": sha(oldroot / "batch-state.json"),
            "historical_original_unchanged": True,
            "state": oldstate,
        },
    )
    write_new(
        ROOT / "artifacts/phase22/opening-audit.json",
        {
            "historical_opening_cny": previous["total_upper_bound_cny"],
            "prior_authorization_sources": prior,
            "original30_used_upper_cny": prior_upper,
            "remaining_cny": 30 - prior_upper,
            "full_opening_cny": manifest["total_upper_bound_cny"],
            "diagnostic_model_calls": 0,
        },
    )
    print(
        json.dumps(
            {
                "prior_upper": prior_upper,
                "remaining": 30 - prior_upper,
                "opening": manifest["total_upper_bound_cny"],
            }
        )
    )


if __name__ == "__main__":
    main()
