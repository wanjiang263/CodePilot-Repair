"""Read-only environment preflight for the already-frozen unseen test50 catalog.

No model dispatch, solution generation, official scoring, or hidden test fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import docker

ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("preflight output must stay in the D project")
    output.mkdir(parents=True, exist_ok=False)
    source = ROOT / "artifacts/phase22/next-stage"
    catalog = json.loads((source / "catalog.json").read_text(encoding="utf8"))
    exclusions = json.loads((source / "exclusions.json").read_text(encoding="utf8"))
    rows = catalog["instances"]
    ids = [row["instance_id"] for row in rows]
    assert len(ids) == len(set(ids)) == 50
    assert not set(ids) & {row["instance_id"] for row in exclusions["instances"]}
    snapshot = (
        ROOT
        / "cache/swebench"
        / ("princeton-nlp__SWE-bench_Lite__" + catalog["revision"])
        / "test.jsonl"
    )
    assert sha(snapshot) == catalog["snapshot_sha256"]
    client = docker.from_env(timeout=40)
    version = client.version()
    cached = {tag: image for image in client.images.list() for tag in image.tags}

    def inspect(row: dict) -> dict:
        iid = row["instance_id"]
        ref = "swebench/sweb.eval.x86_64." + iid.replace("__", "_1776_") + ":latest"
        image = cached.get(ref)
        result = {
            **row,
            "image_ref": ref,
            "cached_image_id": image.id if image else None,
            "runtime_preflight": "REQUIRES_IMAGE_AND_BASE_WORKSPACE",
            "runner": "django"
            if iid.startswith("django__")
            else "sympy"
            if iid.startswith("sympy__")
            else "pytest",
        }
        try:
            registry = client.images.get_registry_data(ref)
            result["registry_descriptor"] = registry.attrs.get("Descriptor")
            result["registry_status"] = "AVAILABLE"
        except Exception as exc:
            result["registry_status"] = "ERROR"
            result["registry_error"] = f"{type(exc).__name__}: {exc}"[:1000]
        return result

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(inspect, row) for row in rows]
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            with (output / "progress.jsonl").open("a", encoding="utf8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                row["instance_id"],
                row["registry_status"],
                "CACHED" if row["cached_image_id"] else "NOT_CACHED",
                flush=True,
            )
    results.sort(key=lambda row: ids.index(row["instance_id"]))
    disk = shutil.disk_usage(ROOT)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "read-only registry and daemon preflight; no paid execution",
        "catalog_source": str(source / "catalog.json"),
        "catalog_sha256": sha(source / "catalog.json"),
        "exclusions_sha256": sha(source / "exclusions.json"),
        "snapshot_sha256": sha(snapshot),
        "docker_version": version["Version"],
        "docker_os": version["Os"],
        "d_drive_free_bytes": disk.free,
        "total": 50,
        "cached": sum(bool(row["cached_image_id"]) for row in results),
        "registry_available": sum(row["registry_status"] == "AVAILABLE" for row in results),
        "runtime_ready": False,
        "model_requests": 0,
        "instances": results,
    }
    (output / "environment-preflight.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf8"
    )
    plan = {
        "status": "PREPARATION_ONLY_INDEPENDENT_BUDGET_REQUIRED",
        "catalog_sha256": report["catalog_sha256"],
        "exclusions_sha256": report["exclusions_sha256"],
        "environment_preflight_sha256": sha(output / "environment-preflight.json"),
        "model_alias": "glm-5.3-flash-main",
        "temperature": 0,
        "http_retries": 0,
        "trust_env": False,
        "funding": "No original30 balance allocated. Obtain separate explicit test50 authorization before any model request.",
        "proposed_hard_ceiling_cny": 200,
        "proposed_instance_cap_cny": 4,
        "recovery": "Typed run config, verified base/index/candidate and all historical ledger hashes; retry only with changed evidence and within one cumulative case cap.",
        "official_admission": "Every traceable, nonempty, applicable, source-only legal candidate is eligible independently of internal acceptance. Reuse a valid verdict for the same instance and canonical patch hash.",
        "hidden_evidence": "Only read official verdict after candidate inference ends. Never send hidden official tests/logs or gold patch to model.",
        "before_dispatch": [
            "Provision frozen image IDs with bounded downloads and verify disk capacity.",
            "For each frozen instance, prepare clean base-only workspace and run an issue-derived bounded real test using native Django/SymPy adapters where required.",
            "Verify generated pytest reproduction support separately from native existing tests.",
            "Freeze source/config/prices and a separately authorized opening ledger; precharge all unknown upper bounds.",
            "Serial paid dispatch initially; freeze unknown request and investigate shared service health before any independent continuation.",
        ],
        "not_claimed": "Registry availability is not a passing runtime environment test. Missing images and base workspaces remain provisioning work before test50 dispatch.",
    }
    (output / "run-plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf8"
    )


if __name__ == "__main__":
    main()
