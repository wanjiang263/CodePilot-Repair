"""Large cached repository observations; no task/answer fields are loaded."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import time
from pathlib import Path

from codepilot.repo.index import Indexer
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.repository import Stores
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.query_cache import QueryCache
from codepilot.tools.read_tools import read_file_tool, search_code_tool


def peak_bytes() -> int:
    if os.name != "nt":
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.c_void_p(-1)  # pseudo handle; do not truncate it to a 32-bit int
    assert ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    return int(counters.PeakWorkingSetSize)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["cold", "warm", "changed", "fresh"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root / "artifacts/phase27/index"
    out.mkdir(parents=True, exist_ok=True)
    repo = out / "workspace"
    seed = root / "artifacts/phase25/seeds/django__django-14999/repo"
    base = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()
    if not repo.exists():
        from codepilot.repo.workspace import WorkspaceManager

        WorkspaceManager(cache_root=out / "cache").create(
            source=str(seed), base_commit=base, target=repo
        )
    workspace = Workspace(repo, normalize_source(str(seed)), base, "phase27-django-performance")
    database = out / ("index-fresh.sqlite" if args.mode == "fresh" else "index.sqlite")
    engine = create_engine_from_url("sqlite:///" + database.as_posix())
    init_db(engine)
    session = create_session_factory(engine)()
    stores = Stores(session)
    progress: list[dict[str, object]] = []

    def observe(event: str, **fields: object) -> None:
        progress.append(dict(event=event, **fields))
        print(json.dumps(progress[-1]), flush=True)

    indexer = Indexer(progress=observe)

    def run(label: str) -> None:
        start = time.perf_counter()
        stats = indexer.index(stores, workspace)
        session.commit()
        result = {
            "label": label,
            "seconds": time.perf_counter() - start,
            "files": stats.files,
            "parsed": len(stats.parsed_paths),
            "parse_reused": len(stats.parse_reused_paths),
            "changed": len(stats.changed_paths),
            "reused": len(stats.reused_paths),
            "association_mode": stats.association_mode,
            "association_sources": next(
                (x["counts"]["sources"] for x in reversed(progress) if x.get("phase") == "linking"),
                0,
            ),
            "peak_working_set_bytes": peak_bytes(),
            "base": base,
            "pid": os.getpid(),
        }
        (out / f"{label}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")
        print(json.dumps(result), flush=True)
        progress.clear()

    target = repo / "django/utils/text.py"
    if args.mode == "changed":
        with target.open("a", encoding="utf8") as handle:
            handle.write("\n# phase27 independent process index observation\n")
    run(args.mode)
    if args.mode in ("cold", "warm", "fresh"):
        with target.open("a", encoding="utf8") as handle:
            handle.write("\n# phase27 same process index observation\n")
        run("same_process_change_from_cold" if args.mode == "fresh" else "same_process_change")
    if args.mode == "warm":
        cache = QueryCache(WorkspacePathPolicy(repo))
        results = []
        for tool, params in [
            (read_file_tool(cache.policy), {"path": "django/utils/text.py"}),
            (
                search_code_tool(cache.policy),
                {"pattern": "def slugify", "path_glob": "django/utils/*.py"},
            ),
        ]:
            for _ in range(2):
                start = time.perf_counter()
                result = cache.execute(tool, params, "all")
                results.append({"seconds": time.perf_counter() - start, "data": result.data})
        (out / "query-cache.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf8")
    session.close()
    engine.dispose()


if __name__ == "__main__":
    main()
