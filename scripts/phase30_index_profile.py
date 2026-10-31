"""Phase30 T08: index correctness and performance re-check after the re-export fix.

Phase30 makes the Phase29 incremental fast path fall back to a full rebuild
whenever a changed file's resolved IMPORT target set moves (re-export retarget).
This script re-runs the correctness oracles and the small performance set on
that changed code path:

- digest equivalence: incremental re-index after a hub body edit must reproduce
  the fresh full index's semantic digest byte-for-byte (452-file synthetic repo);
- re-export probes: 1-hop and 2-hop re-export retargets must make the
  incremental dump equal a fresh full dump (field-identical, same repo id);
- performance: cold / sameproc_nochange / sameproc_change medians (2 warmups +
  3 measured, in-process timing — the Phase29 run used subprocess isolation, so
  absolute numbers are comparable in trend only) compared against the Phase29
  baseline in docs/notes/phase29-index-profile.json.

Boundary: deterministic synthetic repos only; absolute times must not be
extrapolated to large real repositories. Zero network, zero model, zero Docker.

Usage: ``.venv/Scripts/python.exe scripts/phase30_index_profile.py``
Output: ``.codex-pytest-tmp/phase30/index-profile-raw.json`` and
``docs/notes/phase30-index-profile.json``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH = REPO_ROOT / ".codex-pytest-tmp" / "phase30"
WORK_DIR = SCRATCH / "work"
WORKSPACE_DIR = WORK_DIR / "workspace"
REEXPORT_DIR = WORK_DIR / "reexport"
RAW_JSON = SCRATCH / "index-profile-raw.json"
NOTES_JSON = REPO_ROOT / "docs" / "notes" / "phase30-index-profile.json"
P29_BASELINE = REPO_ROOT / "docs" / "notes" / "phase29-index-profile.json"


def load_profile_machinery() -> tuple[Any, Any]:
    """Import the Phase29 profile module (which loads Phase28) and repoint its
    scratch paths into .codex-pytest-tmp/phase30 so no prior deliverable moves."""
    spec = importlib.util.spec_from_file_location(
        "phase29_index_profile", REPO_ROOT / "scripts" / "phase29_index_profile.py"
    )
    assert spec is not None and spec.loader is not None
    p29 = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("phase29_index_profile", p29)
    spec.loader.exec_module(p29)
    p29.SCRATCH = SCRATCH
    p29.WORK_DIR = WORK_DIR
    p29.WORKSPACE_DIR = WORKSPACE_DIR
    p29.CHILD_DIR = SCRATCH / "child"
    p29.RAW_JSON = RAW_JSON
    p29.LOG_PATH = SCRATCH / "index-profile.log"
    p28 = p29.load_p28()
    return p28, p29


def median(runs: list[dict[str, Any]]) -> float:
    values = sorted(run["wall_index_s"] for run in runs)
    return round(values[len(values) // 2], 4)


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"runs": runs, "wall_index_s_median": median(runs)}


def semantic_digest(p29: Any, stores: Any, repository_id: str) -> dict[str, Any]:
    return p29.semantic_digest(stores, repository_id)


def digest_full_index(p28: Any, p29: Any, db_path: Path) -> dict[str, Any]:
    stores = p28.open_db(db_path, fresh=True)
    indexer, _events = p28.make_indexer()
    workspace = p28.make_workspace_object()
    p28.execute_index(indexer, stores, workspace, db_path, record=False)
    digest = semantic_digest(p29, stores, workspace.repository_id)
    p28.close_db(stores)
    return digest


def digest_incremental_after_hub_edit(
    p28: Any, p29: Any, db_path: Path, variant: int
) -> dict[str, Any]:
    """Open the existing db, edit the hub body, re-index incrementally, digest."""
    stores = p28.open_db(db_path, fresh=False)
    indexer, _events = p28.make_indexer()
    workspace = p28.make_workspace_object()
    (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
        p28.hub_variant(variant), encoding="utf-8", newline="\n"
    )
    result = p28.execute_index(indexer, stores, workspace, db_path, record=True)
    digest = semantic_digest(p29, stores, workspace.repository_id)
    p28.close_db(stores)
    return digest, result


# ---------------------------------------------------------------- re-export


REEXPORT_FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def value() -> int:\n    return 1\n",
    "pkg/b.py": "def value() -> int:\n    return 2\n",
    "pkg/hub.py": "from pkg.a import value\n",
    "pkg/mid.py": "from pkg.hub import value\n",
    "pkg/consumer.py": "from pkg.mid import value\n\n\ndef use() -> int:\n    return value()\n",
}
REEXPORT_RETARGET = "from pkg.b import value\n"


def reexport_probe() -> dict[str, Any]:
    """1-hop and 2-hop re-export retargets: incremental dump == fresh full dump."""
    from codepilot.repo.index import Indexer
    from codepilot.repo.parser import TreeSitterParser
    from codepilot.repo.workspace import Workspace, normalize_source
    from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
    from codepilot.storage.repository import Stores

    def make_stores() -> Any:
        engine = create_engine_from_url("sqlite://")
        init_db(engine)
        return Stores(create_session_factory(engine)())

    def workspace(repository_id: str) -> Any:
        return Workspace(
            path=REEXPORT_DIR,
            source=normalize_source(str(REEXPORT_DIR)),
            base_commit="0" * 40,
            repository_id=repository_id,
        )

    def dump(stores: Any, repository_id: str) -> tuple:
        spec = importlib.util.spec_from_file_location(
            "p30_indexdump", REPO_ROOT / "tests" / "support" / "indexdump.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.dump_index(stores, repository_id)

    results: dict[str, Any] = {}
    for label, _retarget in (
        ("one_hop", "from pkg.hub import value\n"),
        ("two_hop", REEXPORT_RETARGET),
    ):
        if REEXPORT_DIR.exists():
            shutil.rmtree(REEXPORT_DIR)
        files = dict(REEXPORT_FILES)
        if label == "one_hop":
            # consumer imports hub directly; retarget hub itself
            files["pkg/mid.py"] = ""
        for rel, text in files.items():
            path = REEXPORT_DIR / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        repository_id = f"repo_p30_reexport_{label}"
        stores = make_stores()
        indexer = Indexer(parser=TreeSitterParser())
        indexer.index(stores, workspace(repository_id))
        stores.session.commit()
        # Retarget the hub (1-hop: consumer sees hub change; 2-hop: mid+consumer).
        (REEXPORT_DIR / "pkg" / "hub.py").write_text(
            REEXPORT_RETARGET, encoding="utf-8", newline="\n"
        )
        stats = indexer.index(stores, workspace(repository_id))
        stores.session.commit()
        incremental = dump(stores, repository_id)[1:]
        ref = make_stores()
        Indexer(parser=TreeSitterParser()).index(ref, workspace(repository_id))
        ref.session.commit()
        full = dump(ref, repository_id)[1:]
        results[label] = {
            "association_mode": stats.association_mode,
            "incremental_equals_full": incremental == full,
        }
        if incremental != full:
            for index, (left, right) in enumerate(zip(incremental, full, strict=False)):
                if left != right:
                    results[label]["first_diff"] = {
                        "index": index,
                        "incremental": repr(left),
                        "full": repr(right),
                    }
                    break
            results[label]["lens"] = {"incremental": len(incremental), "full": len(full)}
    return results


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-workspace", action="store_true", help=argparse.SUPPRESS)
    parser.parse_args(argv)
    p28, p29 = load_profile_machinery()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    print("phase30 index profile start", flush=True)

    p28.generate_workspace(WORKSPACE_DIR)
    db_path = WORK_DIR / "phase30-main.sqlite"

    # 1. cold
    cold_runs = []
    for _ in range(3):
        stores = p28.open_db(db_path, fresh=True)
        indexer, _events = p28.make_indexer()
        workspace = p28.make_workspace_object()
        result = p28.execute_index(indexer, stores, workspace, db_path, record=False)
        cold_runs.append(
            {"wall_index_s": result["wall_index_s"], "mode": result["association_mode"]}
        )
        p28.close_db(stores)
    cold = aggregate(cold_runs)
    print(f"cold median {cold['wall_index_s_median']}s", flush=True)

    # 2. sameproc_nochange (2 warmups + 3 measured)
    stores = p28.open_db(db_path, fresh=True)
    indexer, _events = p28.make_indexer()
    workspace = p28.make_workspace_object()
    p28.execute_index(indexer, stores, workspace, db_path, record=False)
    for _ in range(p28.WARMUPS):
        p28.execute_index(indexer, stores, workspace, db_path, record=False)
    nochange_runs = []
    for _ in range(p28.MEASURED):
        result = p28.execute_index(indexer, stores, workspace, db_path, record=False)
        nochange_runs.append(
            {"wall_index_s": result["wall_index_s"], "mode": result["association_mode"]}
        )
    p28.close_db(stores)
    nochange = aggregate(nochange_runs)
    print(f"sameproc_nochange median {nochange['wall_index_s_median']}s", flush=True)

    # 3. sameproc_change (2 warmups + 3 measured, hub body edit per run)
    stores = p28.open_db(db_path, fresh=True)
    indexer, _events = p28.make_indexer()
    workspace = p28.make_workspace_object()
    p28.execute_index(indexer, stores, workspace, db_path, record=False)
    change_runs = []
    for run_index in range(p28.WARMUPS + p28.MEASURED):
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_variant(run_index), encoding="utf-8", newline="\n"
        )
        result = p28.execute_index(indexer, stores, workspace, db_path, record=False)
        entry = {"wall_index_s": result["wall_index_s"], "mode": result["association_mode"]}
        if run_index >= p28.WARMUPS:
            change_runs.append(entry)
        else:
            print(
                f"  warmup {run_index}: {entry['wall_index_s']}s mode={entry['mode']}", flush=True
            )
    p28.close_db(stores)
    change = aggregate(change_runs)
    print(f"sameproc_change median {change['wall_index_s_median']}s", flush=True)

    # 4. Digest equivalence after the last hub edit (incremental vs fresh full).
    digest_incremental, last_result = digest_incremental_after_hub_edit(
        p28, p29, db_path, variant=p28.WARMUPS + p28.MEASURED
    )
    digest_full_after = digest_full_index(p28, p29, WORK_DIR / "phase30-ref.sqlite")
    digest_baseline = digest_full_index(p28, p29, WORK_DIR / "phase30-base.sqlite")

    # 5. Re-export retarget probes (1-hop / 2-hop).
    reexport = reexport_probe()

    baseline = json.loads(P29_BASELINE.read_text(encoding="utf-8"))
    p29_scenarios = baseline["scenarios"]

    def compare(name: str, current: dict[str, Any]) -> dict[str, Any]:
        previous = p29_scenarios[name]["wall_index_s_median"]
        return {
            "phase30_median_s": current["wall_index_s_median"],
            "phase29_baseline_s": previous,
            "delta_s": round(current["wall_index_s_median"] - previous, 4),
            "delta_percent": round((current["wall_index_s_median"] - previous) / previous * 100, 1)
            if previous
            else None,
        }

    report = {
        "tool": "phase30_index_profile",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": {
            "reuse": "phase29_index_profile/phase28_index_profile machinery (imported; scratch repointed to .codex-pytest-tmp/phase30; no prior deliverable written)",
            "timing": "in-process wall time (Phase29 used subprocess isolation; compare trends, not absolute values)",
            "warmups": p28.WARMUPS,
            "measured": p28.MEASURED,
            "aggregation": "median of measured runs",
            "boundary": "deterministic synthetic repos only; absolute times must not be extrapolated",
        },
        "environment": p28.probe_environment(),
        "scenarios": {
            "cold": cold,
            "sameproc_nochange": nochange,
            "sameproc_change": change,
        },
        "comparison_to_phase29": {
            "cold": compare("cold", cold),
            "sameproc_nochange": compare("sameproc_nochange", nochange),
            "sameproc_change": compare("sameproc_change", change),
        },
        "semantic_digests": {
            "baseline_full_index": digest_baseline,
            "incremental_after_hub_edit": digest_incremental,
            "fresh_full_after_edit": digest_full_after,
            "incremental_equals_fresh_full": digest_incremental["sha256"]
            == digest_full_after["sha256"],
            "last_incremental_mode": last_result["association_mode"],
        },
        "reexport_retarget_probes": reexport,
    }
    RAW_JSON.parent.mkdir(parents=True, exist_ok=True)
    RAW_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    NOTES_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("comparison_to_phase29", "semantic_digests", "reexport_retarget_probes")
            },
            indent=2,
        )
    )
    ok = report["semantic_digests"]["incremental_equals_fresh_full"] and all(
        v["incremental_equals_full"] for v in reexport.values()
    )
    print(f"phase30 index profile done ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
