"""Phase29 T09：索引性能基线（复用 Phase28 剖析机制，新增语义摘要与 2000 文件趋势仓）。

边界（与 phase28_index_profile.py 一致，不可省略）：确定性合成小仓库（450 个
1KB 级 Python 文件；趋势仓 2501 个）只支撑机制与相对占比结论，绝对耗时不可
外推大型真实仓库。零网络、零模型请求、零 Docker。

相对 Phase28 的两点增量：
- ``semantic_digest``：对 files/symbols/edges 全量行做 canonical JSON SHA256，
  作为 T10 优化前后 ``dump_index`` 逐字段等价的判定依据；
- ``--trend``：SOURCE_MODULES=2000 的趋势对照（冷 1 次 + 同进程单文件变化
  2 预热 + 3 测量），只看方向不看绝对值。

场景（每场景 2 次预热 + 3 次测量取中位，趋势仓冷索引除外——单次并如实标注）：
cold / sameproc_nochange / sameproc_change / newproc_change /
cache_128 / cache_512 / cache_unlimited / trend。

输出：``docs/notes/phase29-index-profile.json``（汇报）、
``.codex-pytest-tmp/phase29/``（原始日志、子进程 JSON、SQLite、工作区）。

用法：``.venv/Scripts/python.exe scripts/phase29_index_profile.py [--trend]``
子模式：``--child`` / ``--child-cached`` 由父进程调用，不单独使用。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codepilot.repo.index import RepositoryIndex
from codepilot.storage.repository import Stores

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = Path(__file__).resolve()
SCRATCH = REPO_ROOT / ".codex-pytest-tmp" / "phase29"
WORK_DIR = SCRATCH / "work"
CHILD_DIR = SCRATCH / "child"
WORKSPACE_DIR = WORK_DIR / "workspace"
RAW_JSON = SCRATCH / "phase29-index-profile-raw.json"
LOG_PATH = SCRATCH / "phase29-index-profile.log"
NOTES_JSON = REPO_ROOT / "docs" / "notes" / "phase29-index-profile.json"

TREND_SOURCE_MODULES = 2000


class Logger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8")

    def __call__(self, message: str) -> None:
        line = f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}"
        print(line, flush=True)
        self._handle.write(line + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def load_p28() -> Any:
    """Import the Phase28 profiling module and repoint its scratch/output paths.

    All gitignored temp evidence for this round stays under .codex-pytest-tmp/phase29;
    the Phase28 deliverable JSON is never written by this script.
    """
    spec = importlib.util.spec_from_file_location(
        "phase28_index_profile", REPO_ROOT / "scripts" / "phase28_index_profile.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SCRATCH = SCRATCH
    mod.WORK_DIR = WORK_DIR
    mod.CHILD_DIR = CHILD_DIR
    mod.WORKSPACE_DIR = WORKSPACE_DIR
    mod.RAW_JSON = RAW_JSON
    mod.LOG_PATH = LOG_PATH
    return mod


# ---------------------------------------------------------------- 语义摘要


def semantic_digest(stores: Stores, repository_id: str) -> dict[str, Any]:
    """Full-database content digest: the T10 equivalence oracle.

    Files/symbols/edges are dumped row by row (sorted, canonical JSON) and
    hashed; any field-level difference between two indexes changes the hash.
    """
    index = RepositoryIndex(stores, repository_id)
    files = sorted(
        (
            row.path,
            row.sha256,
            row.module,
            row.size,
            row.line_count,
            row.is_test,
            row.parse_error,
        )
        for row in index.files()
    )
    symbols = sorted(
        (
            row.id,
            row.file_id,
            row.name,
            row.qualified_name,
            row.kind,
            row.start_line,
            row.end_line,
            row.signature,
            row.docstring,
            tuple(row.decorators),
            row.parent_id,
        )
        for row in index.symbols()
    )
    edges = sorted(
        (
            row.id,
            row.kind,
            row.source_id,
            row.target_id,
            row.target_name,
            row.confidence,
            row.line,
        )
        for row in index.edges()
    )
    payload = json.dumps(
        {"edges": edges, "files": files, "symbols": symbols},
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "files": len(files),
        "symbols": len(symbols),
        "edges": len(edges),
        "bytes": len(payload),
    }


def digest_of_full_index(p28: Any, db_path: Path) -> dict[str, Any]:
    """Fresh database, full index of the current workspace, then digest it."""
    stores = p28.open_db(db_path, fresh=True)
    indexer, _events = p28.make_indexer()
    workspace = p28.make_workspace_object()
    p28.execute_index(indexer, stores, workspace, db_path, record=False)
    digest = semantic_digest(stores, workspace.repository_id)
    p28.close_db(stores)
    return digest


# ---------------------------------------------------------------- 子进程模式


def run_child(args: argparse.Namespace) -> int:
    p28 = load_p28()
    p28.install_instrumentation()
    workspace = p28.make_workspace_object()
    db_path = Path(args.db)
    if args.child:
        stores = p28.open_db(db_path, fresh=False)
        indexer, _events = p28.make_indexer(args.cache_entries, args.cache_bytes)
        result = p28.execute_index(indexer, stores, workspace, db_path, record=True)
        p28.close_db(stores)
        payload = {"label": args.label, "run": result}
    else:  # --child-cached
        stores = p28.open_db(db_path, fresh=True)
        indexer, _events = p28.make_indexer(args.cache_entries, args.cache_bytes)
        baseline = p28.execute_index(indexer, stores, workspace, db_path, record=False)
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_variant(args.variant), encoding="utf-8", newline="\n"
        )
        measured = p28.execute_index(indexer, stores, workspace, db_path, record=True)
        p28.close_db(stores)
        payload = {"label": args.label, "baseline": baseline, "measured": measured}
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"child {args.label} done wall={payload.get('run', payload.get('measured'))['wall_index_s']}s"
    )
    return 0


def spawn_child(
    *,
    mode: str,
    db_path: Path,
    out_path: Path,
    label: str,
    variant: int = 0,
    cache_entries: int = 128,
    cache_bytes: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        f"--{mode}",
        "--db",
        str(db_path),
        "--out",
        str(out_path),
        "--label",
        label,
        "--variant",
        str(variant),
        "--cache-entries",
        str(cache_entries),
        "--cache-bytes",
        str(cache_bytes),
    ]
    last_error: BaseException | None = None
    for attempt in (1, 2):
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1200,
            )
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            if attempt == 2:
                payload["child_retried"] = True
            return payload
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ) as exc:
            last_error = exc
    raise RuntimeError(f"child {label} failed twice: {last_error}") from last_error


# ---------------------------------------------------------------- 场景


def scenario_sameproc_nochange(p28: Any, log: Any) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for i in range(p28.WARMUPS + p28.MEASURED):
        db_path = WORK_DIR / "sameproc_nochange.sqlite"
        stores = p28.open_db(db_path, fresh=(i == 0))
        indexer, events = p28.make_indexer()
        if i == 0:
            p28.execute_index(
                indexer, stores, p28.make_workspace_object(), db_path, record=False, events=events
            )
        run = p28.execute_index(
            indexer,
            stores,
            p28.make_workspace_object(),
            db_path,
            record=i >= p28.WARMUPS,
            events=events,
        )
        runs.append(p28.mark_run(run, i))
        p28.close_db(stores)
        log(
            f"  sameproc_nochange run {i}: {runs[-1]['wall_index_s']}s mode={runs[-1]['association_mode']}"
        )
    return runs


def scenario_newproc_change_p29(p28: Any, log: Any) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for i in range(p28.WARMUPS + p28.MEASURED):
        db_path = WORK_DIR / "newproc_change.sqlite"
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_source(), encoding="utf-8", newline="\n"
        )
        p28.delete_db_files(db_path)
        setup = spawn_child(
            mode="child",
            db_path=db_path,
            out_path=CHILD_DIR / f"newproc_change-setup-{i}.json",
            label=f"newproc_change-setup-{i}",
        )
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_variant(i), encoding="utf-8", newline="\n"
        )
        measured = spawn_child(
            mode="child",
            db_path=db_path,
            out_path=CHILD_DIR / f"newproc_change-{i}.json",
            label=f"newproc_change-{i}",
        )
        runs.append(p28.mark_run(measured["run"], i))
        runs[-1]["setup_baseline_s"] = setup["run"]["wall_index_s"]
        log(f"  newproc_change run {i}: {runs[-1]['wall_index_s']}s parsed={runs[-1]['parsed']}")
    return runs


def scenario_cache_p29(
    p28: Any, log: Any, label: str, cache_entries: int, cache_bytes: int
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for i in range(p28.WARMUPS + p28.MEASURED):
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_source(), encoding="utf-8", newline="\n"
        )
        payload = spawn_child(
            mode="child-cached",
            db_path=WORK_DIR / f"{label}.sqlite",
            out_path=CHILD_DIR / f"{label}-{i}.json",
            label=f"{label}-{i}",
            variant=i,
            cache_entries=cache_entries,
            cache_bytes=cache_bytes,
        )
        runs.append(p28.mark_run(payload["measured"], i))
        runs[-1]["cache_limit_entries"] = cache_entries
        runs[-1]["cache_limit_bytes"] = cache_bytes
        runs[-1]["setup_baseline_s"] = payload["baseline"]["wall_index_s"]
        log(
            f"  {label} run {i}: {runs[-1]['wall_index_s']}s parsed={runs[-1]['parsed']} "
            f"reused={runs[-1]['parse_reused']} evictions={runs[-1]['cache_evictions']}"
        )
    return runs


def scenario_trend(p28: Any, log: Any) -> dict[str, Any]:
    """2000-file trend repo: cold (single run, labeled) + sameproc_change 2+3."""
    p28.SOURCE_MODULES = TREND_SOURCE_MODULES
    try:
        p28.generate_workspace(WORKSPACE_DIR)
        log(f"  trend workspace regenerated with {TREND_SOURCE_MODULES} source modules")
        db_path = WORK_DIR / "trend_cold.sqlite"
        stores = p28.open_db(db_path, fresh=True)
        indexer, _events = p28.make_indexer()
        cold = p28.execute_index(indexer, stores, p28.make_workspace_object(), db_path, record=True)
        p28.close_db(stores)
        log(f"  trend cold single run: {cold['wall_index_s']}s parsed={cold['parsed']}")
        change_runs = p28.scenario_sameproc_change(log)
    finally:
        p28.SOURCE_MODULES = 360
        p28.generate_workspace(WORKSPACE_DIR)
        log("  workspace restored to 360 source modules")
    return {"cold_single_run": cold, "sameproc_change": p28.aggregate(change_runs)}


# ---------------------------------------------------------------- 汇总


def build_report(
    p28: Any, scenarios: dict[str, Any], trend: dict[str, Any] | None
) -> dict[str, Any]:
    sameproc = scenarios["sameproc_change"]["wall_index_s_median"]
    report: dict[str, Any] = {
        "tool": "phase29_index_profile",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": {
            "reuse": "phase28_index_profile.py machinery (imported, scratch dirs repointed; no Phase28 deliverable touched)",
            "synthetic_repo": {
                "source_modules": p28.SOURCE_MODULES,
                "seed": p28.SEED,
                "boundary": "synthetic small repo; absolute times must not be extrapolated to large real repositories",
            },
            "warmups": p28.WARMUPS,
            "measured": p28.MEASURED,
            "aggregation": "median of measured runs",
        },
        "environment": p28.probe_environment(),
        "scenarios": scenarios,
        "trend": trend,
        "acceptance": {
            "sameproc_change_median_s": sameproc,
            "t10_target": "single-file change median wall at least 10% below this baseline with identical semantic digest, or an honest 'not achieved' with data",
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--child-cached", dest="child_cached", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--db", default="", help=argparse.SUPPRESS)
    parser.add_argument("--out", default="", help=argparse.SUPPRESS)
    parser.add_argument("--label", default="", help=argparse.SUPPRESS)
    parser.add_argument("--variant", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--cache-entries", type=int, default=128, help=argparse.SUPPRESS)
    parser.add_argument("--cache-bytes", type=int, default=8 * 1024 * 1024, help=argparse.SUPPRESS)
    parser.add_argument("--trend", action="store_true", help="also run the 2000-file trend repo")
    args = parser.parse_args(argv)
    p28 = load_p28()
    if args.child or args.child_cached:
        return run_child(args)

    CHILD_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    log = Logger(LOG_PATH)
    log("phase29 index profile start")
    try:
        p28.generate_workspace(WORKSPACE_DIR)
        # Phase28 同款：父进程全程计时（子进程各自安装，见 run_child）。
        p28.install_instrumentation()
        scenarios: dict[str, Any] = {}
        scenarios["cold"] = p28.aggregate(p28.scenario_cold(log))
        scenarios["sameproc_nochange"] = p28.aggregate(scenario_sameproc_nochange(p28, log))
        scenarios["sameproc_change"] = p28.aggregate(p28.scenario_sameproc_change(log))
        scenarios["newproc_change"] = p28.aggregate(scenario_newproc_change_p29(p28, log))
        scenarios["cache_128"] = p28.aggregate(
            scenario_cache_p29(p28, log, "cache_128", 128, p28.DEFAULT_CACHE_BYTES)
        )
        scenarios["cache_512"] = p28.aggregate(
            scenario_cache_p29(p28, log, "cache_512", 512, p28.DEFAULT_CACHE_BYTES)
        )
        scenarios["cache_unlimited"] = p28.aggregate(
            scenario_cache_p29(p28, log, "cache_unlimited", p28.UNLIMITED, p28.UNLIMITED)
        )

        # Semantic digests at two fixed content points (T10 equivalence oracle).
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_source(), encoding="utf-8", newline="\n"
        )
        baseline_digest = digest_of_full_index(p28, WORK_DIR / "digest_baseline.sqlite")
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_variant(0), encoding="utf-8", newline="\n"
        )
        variant_digest = digest_of_full_index(p28, WORK_DIR / "digest_variant.sqlite")
        (WORKSPACE_DIR / p28.HUB_RELPATH).write_text(
            p28.hub_source(), encoding="utf-8", newline="\n"
        )

        trend = scenario_trend(p28, log) if args.trend else None
        report = build_report(p28, scenarios, trend)
        report["semantic_digests"] = {
            "baseline_full_index": baseline_digest,
            "variant_full_index": variant_digest,
            "note": "T10 must reproduce both digests byte-identically after optimization (dump_index equivalence)",
        }
        RAW_JSON.parent.mkdir(parents=True, exist_ok=True)
        RAW_JSON.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        NOTES_JSON.parent.mkdir(parents=True, exist_ok=True)
        NOTES_JSON.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        log(f"report written: {NOTES_JSON}")
        log(f"sameproc_change median wall: {scenarios['sameproc_change']['wall_index_s_median']}s")
        return 0
    except BaseException:
        import traceback

        log("FATAL:\n" + traceback.format_exc())
        raise
    finally:
        p28.restore_instrumentation()
        log.close()


if __name__ == "__main__":
    main()
