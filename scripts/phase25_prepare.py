"""Prepare the fixed catalog and byte-verified original30 opening (no model calls)."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from codepilot.llm.dispatch_recovery import (
    RecoveryLedgerSource,
    build_multi_source_recovery_manifest,
    write_recovery_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/phase25"


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf8"))


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        assert read(path) == value, f"refusing to overwrite evidence: {path}"
    else:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    closure = read(ROOT / "artifacts/phase23/closure-r2/audit.json")
    authorized = {x["path"]: x["sha256"] for x in closure["historical_ledger_hashes_verified"]}
    for path in sorted(
        (ROOT / "artifacts/phase23/instances").glob("*/segment-*/dispatch-ledger.json")
    ):
        authorized[path.relative_to(ROOT).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    upper = Decimal(0)
    for name, expected in authorized.items():
        path = ROOT / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        for row in read(path)["requests"]:
            upper += Decimal(
                str(row["cost_cny"] if row["status"] == "settled" else row["reserved_cny"])
            )
    remaining = min(Decimal("26.80236808"), Decimal(30) - upper)
    assert remaining > 0
    previous = read(ROOT / "artifacts/phase23/opening.json")
    sources = {
        x["path"]
        for key in ("sources", "additional_settled_ledgers")
        for x in previous.get(key, [])
    }
    sources.update(authorized)
    manifest_path = OUT / "opening.json"
    if not manifest_path.exists():
        manifest = build_multi_source_recovery_manifest(
            [
                RecoveryLedgerSource(
                    ROOT / p,
                    Path(p).parent.parent.name
                    if p.startswith("artifacts/phase23/instances/")
                    else Path(p).parent.name,
                )
                for p in sorted(sources)
            ],
            run_id="phase25-test50",
            reason="Authorized original30 continuation; stopped historical processes; retain unknown in full",
            repo_root=ROOT,
        )
        write_recovery_manifest(manifest, manifest_path)
    manifest = read(manifest_path)
    catalog = read(ROOT / "artifacts/phase22/next-stage/catalog.json")
    assert (
        hashlib.sha256(
            (ROOT / "artifacts/phase22/next-stage/catalog.json").read_bytes()
        ).hexdigest()
        == "3cfa3b13cbbf281aafb216a6942bfbdc4c249c5c082c715218ea786c2cc0c3be"
    )
    ids = [r["instance_id"] for r in catalog["instances"]]
    assert len(ids) == len(set(ids)) == 50
    write(OUT / "catalog.json", dict(catalog, batch_id="phase25-test50"))
    preflight = read(ROOT / "artifacts/phase23/test50-preflight/environment-preflight.json")
    runners = {}
    seeds = {}
    for row in preflight["instances"]:
        iid, kind = row["instance_id"], row["runner"]
        command = {
            "django": "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1",
            "sympy": "bin/test -C --verbose",
        }.get(kind, "pytest -rA")
        runners[iid] = {"kind": kind, "test_command": command, "timeout_seconds": 600}
        seeds[iid] = f"artifacts/phase25/seeds/{iid}"
    write(OUT / "runners.json", {"schema_version": 1, "instances": runners})
    write(OUT / "seeds.json", {"seeds": seeds})
    config = read(ROOT / "configs/phase22/config-r5.json")
    for key in ("index_snapshots", "completed_segment_sources", "segment1_new_settled_cny"):
        config.pop(key, None)
    config.update(
        batch_id="phase25-test50",
        batch_name="fixed test50 original30 continuation",
        selection_seed=catalog["seed"],
        opening_manifest="artifacts/phase25/opening.json",
        historical_opening_cny=manifest["total_upper_bound_cny"],
        batch_new_limit_cny=float(remaining),
        main_batch_limit_cny=float(remaining),
        per_instance_new_limit_cny=2.0,
        runner_overrides="artifacts/phase25/runners.json",
        repo_cache_seeds="artifacts/phase25/seeds.json",
        official_mode="local",
        require_preflight=False,
        linux_repo_root=str(ROOT),
        linux_python=str(ROOT / ".venv-linux/bin/python"),
        harness_python=str(ROOT / ".venv-swebench-linux/bin/python"),
        prior_authorization_sources=[
            {"path": p, "sha256": h} for p, h in sorted(authorized.items())
        ],
    )
    write(OUT / "config.json", config)
    write(
        OUT / "opening-audit.json",
        {
            "authorized_upper_cny": float(upper),
            "remaining_cny": float(remaining),
            "sources": authorized,
        },
    )
    print(
        json.dumps(
            {
                "authorized_upper_cny": float(upper),
                "remaining_cny": float(remaining),
                "instances": ids[:5],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
