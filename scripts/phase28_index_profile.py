"""Phase28 T03：索引分阶段剖析（合成子集，零网络、零模型请求）。

重要边界（不可省略）：本脚本使用 **确定性的合成小仓库**（约 450 个 1KB 级
Python 文件）验证索引器的机制与各阶段相对占比。合成子集的绝对耗时 **不可
外推到 Django 量级**（2717 个平均大得多的文件，Phase27 冷索引 104.999s）。
跨量级结论只引用 Phase27 历史数据，且两者不混算。

做法：
- 生成合成仓库（固定种子）到 ``.codex-pytest-tmp/phase28/work/workspace``。
- 通过 monkeypatch（不改生产代码）给 scanner/parser/各 store/关联提取函数
  套上带「独占时间」统计的计时包装；``EdgeStore.replace_for_sources`` 用一个
  逐行等价的计时副本替换，以便把「旧边删除 / 新边插入 / flush」分开计量。
- 场景：cold、newproc_nochange、sameproc_change、newproc_change、
  snapshot_then_change、cache_128/512/unlimited；每场景 2 次预热 + 3 次测量。
- 子进程场景（newproc_* / cache_*）用独立进程拿到干净的峰值内存。
- 输出：``docs/notes/phase28-index-profile.json``（汇报）与
  ``.codex-pytest-tmp/phase28/``（原始日志、子进程 JSON、SQLite、工作区）。

用法：``.venv/Scripts/python.exe scripts/phase28_index_profile.py [--calibrate]``
子模式：``--child`` / ``--child-cached`` 由父进程调用，不单独使用。
"""

from __future__ import annotations

import argparse
import ctypes
import functools
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert

from codepilot.repo.context import AnalysisContext
from codepilot.repo.imports import ImportResolver
from codepilot.repo.index import Indexer
from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.scanner import RepositoryScanner
from codepilot.repo.workspace import Workspace, normalize_source, repository_id
from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
from codepilot.storage.models import GraphEdge
from codepilot.storage.repository import EdgeStore, FileStore, Stores, SymbolStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = Path(__file__).resolve()
SCRATCH = REPO_ROOT / ".codex-pytest-tmp" / "phase28"
WORK_DIR = SCRATCH / "work"
CHILD_DIR = SCRATCH / "child"
WORKSPACE_DIR = WORK_DIR / "workspace"
RAW_JSON = SCRATCH / "phase28-index-profile-raw.json"
LOG_PATH = SCRATCH / "phase28-index-profile.log"
NOTES_JSON = REPO_ROOT / "docs" / "notes" / "phase28-index-profile.json"

SEED = 20281030
SOURCE_MODULES = 360  # pkg/mod_000(hub)..pkg/mod_359
TEST_STEP = 4  # tests/test_mod_004 .. tests/test_mod_356
BASE_COMMIT = "0" * 40
HUB_RELPATH = "pkg/mod_000.py"
DEFAULT_CACHE_ENTRIES = 128
DEFAULT_CACHE_BYTES = 8 * 1024 * 1024
UNLIMITED = 10**9
WARMUPS = 2
MEASURED = 3
PY_FILES = 1 + SOURCE_MODULES + len(range(TEST_STEP, SOURCE_MODULES, TEST_STEP))


# ---------------------------------------------------------------- 环境探测


def peak_bytes() -> int:
    """进程峰值工作集（Windows: PeakWorkingSetSize；POSIX: ru_maxrss）。"""
    if platform.system() != "Windows":
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


def _cpu_name() -> str:
    if platform.system() != "Windows":
        return platform.processor()
    try:
        import winreg

        key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        return platform.processor()


def _total_ram_bytes() -> int | None:
    if platform.system() != "Windows":
        return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys)


def _disk_type() -> dict[str, str]:
    root = f"{Path(REPO_ROOT).anchor}"
    drive_type = "unknown"
    if platform.system() == "Windows":
        codes = {0: "UNKNOWN", 3: "FIXED", 4: "REMOTE", 5: "CDROM", 6: "RAMDISK"}
        code = ctypes.windll.kernel32.GetDriveTypeW(root)
        drive_type = codes.get(code, f"code_{code}")
    media = "unknown"
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-PhysicalDisk | Select-Object -First 1 -ExpandProperty MediaType)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        media = result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        media = "unknown"
    return {"drive_type": drive_type, "media_type": media}


def from_metadata(package: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(package)
    except PackageNotFoundError:
        return "unavailable"


def probe_environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "cpu": _cpu_name(),
        "cpu_count": os.cpu_count(),
        "total_ram_bytes": _total_ram_bytes(),
        "disk": _disk_type(),
        "python_version": sys.version,
        "python_executable": sys.executable,
        "tree_sitter_version": from_metadata("tree-sitter"),
        "tree_sitter_python_version": from_metadata("tree-sitter-python"),
        "sqlalchemy_version": from_metadata("sqlalchemy"),
    }


# ---------------------------------------------------------------- 合成仓库


def hub_source() -> str:
    """hub 模块（mod_000）：被其余全部模块 import，是增量场景的改动目标。"""
    return (
        f"'''Synthetic hub module (seed {SEED}).'''\n"
        "\n"
        "\n"
        "class HubBase:\n"
        "    '''Synthetic base class inherited by every module class.'''\n"
        "\n"
        "    kind = 'hub'\n"
        "\n"
        "    def identify(self) -> str:\n"
        "        return self.kind\n"
        "\n"
        "\n"
        "def hub_helper(value: int) -> int:\n"
        "    return value + 7\n"
        "\n"
        "\n"
        "def hub_root(value: int) -> int:\n"
        "    return hub_helper(value) * 2\n"
    )


def hub_variant(run_index: int) -> str:
    """改 1 个文件 = 在 hub 末尾追加定长注释；sha 变化、结构不变。"""
    return hub_source() + f"\n\n# phase28 touch {run_index:02d}\n"


def _module_source(i: int) -> str:
    rng = random.Random(SEED * 1_000_003 + i)
    a = rng.randrange(1, SOURCE_MODULES)
    while a == i:
        a = rng.randrange(1, SOURCE_MODULES)
    b = rng.randrange(1, SOURCE_MODULES)
    while b in (i, a):
        b = rng.randrange(1, SOURCE_MODULES)
    c = rng.randrange(1, SOURCE_MODULES)
    while c in (i, a, b):
        c = rng.randrange(1, SOURCE_MODULES)
    k = rng.randrange(2, 97)
    return (
        f"'''Synthetic module {i:03d} (seed {SEED}).'''\n"
        "\n"
        "from pkg.mod_000 import HubBase, hub_helper\n"
        f"from pkg.mod_{a:03d} import func_{a:03d}_1\n"
        f"from pkg.mod_{b:03d} import func_{b:03d}_2\n"
        f"import pkg.mod_{c:03d}\n"
        "\n"
        "\n"
        f"class Class_{i:03d}(HubBase):\n"
        "\n"
        f"    tag = 'class_{i:03d}'\n"
        "\n"
        "    def measure(self, value: int) -> int:\n"
        "        return hub_helper(local_scale(value)) + len(self.tag)\n"
        "\n"
        "    def describe(self, value: str) -> str:\n"
        "        return self.tag + ':' + str(len(value))\n"
        "\n"
        "\n"
        "def local_scale(value: int) -> int:\n"
        f"    return value * {k} + {k + 1}\n"
        "\n"
        "\n"
        f"def func_{i:03d}_1(value: int) -> int:\n"
        f"    return local_scale(value) + func_{a:03d}_1(hub_helper(value))\n"
        "\n"
        "\n"
        f"def func_{i:03d}_2(value: int) -> int:\n"
        f"    return func_{b:03d}_2(value) - local_scale(value)\n"
        "\n"
        "\n"
        f"def func_{i:03d}_3(value: str) -> int:\n"
        f"    return pkg.mod_{c:03d}.func_{c:03d}_1(len(value))\n"
    )


def _test_source(i: int) -> str:
    return (
        f"'''Synthetic tests for pkg.mod_{i:03d}.'''\n"
        "\n"
        f"from pkg.mod_{i:03d} import Class_{i:03d}, func_{i:03d}_1, func_{i:03d}_2\n"
        "\n"
        "\n"
        f"def test_func_{i:03d}_1() -> None:\n"
        f"    assert func_{i:03d}_1(3) >= 0\n"
        "\n"
        "\n"
        f"def test_func_{i:03d}_2() -> None:\n"
        f"    assert func_{i:03d}_2(4) >= 0\n"
        "\n"
        "\n"
        f"def test_class_{i:03d}_measure() -> None:\n"
        f"    assert Class_{i:03d}().measure(2) >= 0\n"
    )


def generate_workspace(root: Path) -> dict[str, int]:
    """确定性生成合成仓库（固定种子；同种子逐字节一致）。"""
    if root.exists():
        shutil.rmtree(root)
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text(
        f"'''Synthetic package marker (seed {SEED}).'''\n", encoding="utf-8", newline="\n"
    )
    (root / HUB_RELPATH).write_text(hub_source(), encoding="utf-8", newline="\n")
    for i in range(1, SOURCE_MODULES):
        (root / "pkg" / f"mod_{i:03d}.py").write_text(
            _module_source(i), encoding="utf-8", newline="\n"
        )
    test_count = 0
    for i in range(TEST_STEP, SOURCE_MODULES, TEST_STEP):
        (root / "tests" / f"test_mod_{i:03d}.py").write_text(
            _test_source(i), encoding="utf-8", newline="\n"
        )
        test_count += 1
    (root / "README.md").write_text(f"# synthetic profile repo (seed {SEED})\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "synthetic-profile"\n', encoding="utf-8"
    )
    return {
        "python_files": 1 + SOURCE_MODULES + test_count,
        "test_files": test_count,
        "source_modules": SOURCE_MODULES,
        "non_python_files": 2,
    }


# ---------------------------------------------------------------- 计时器


class PhaseTimer:
    """带独占时间的阶段累计器。同一时刻只有一个线程在跑（索引是单线程的）。"""

    def __init__(self) -> None:
        self.inclusive: dict[str, float] = {}
        self.exclusive: dict[str, float] = {}
        self.calls: dict[str, int] = {}
        # (parent_label, label) -> [inclusive_seconds, calls]：把被嵌套调用的
        # 阶段按调用方归属（例如 replace_for_sources 的三个调用点）。
        self.by_parent: dict[tuple[str, str], list[float]] = {}
        self._stack: list[list[float]] = []

    def reset(self) -> None:
        self.inclusive.clear()
        self.exclusive.clear()
        self.calls.clear()
        self.by_parent.clear()
        self._stack.clear()

    def _record(self, label: str, elapsed: float, child_time: float) -> None:
        self.calls[label] = self.calls.get(label, 0) + 1
        self.inclusive[label] = self.inclusive.get(label, 0.0) + elapsed
        self.exclusive[label] = self.exclusive.get(label, 0.0) + (elapsed - child_time)
        parent = self._stack[-1][0] if self._stack else ""
        key = (parent, label)
        slot = self.by_parent.setdefault(key, [0.0, 0])
        slot[0] += elapsed
        slot[1] += 1
        if self._stack:
            self._stack[-1][2] += elapsed

    @contextmanager
    def phase(self, label: str):
        self._stack.append([label, time.perf_counter(), 0.0])
        try:
            yield
        finally:
            end = time.perf_counter()
            frame = self._stack.pop()
            self._record(label, end - frame[1], frame[2])

    def wrap(self, label: str, func):
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._stack.append([label, time.perf_counter(), 0.0])
            try:
                return func(*args, **kwargs)
            finally:
                end = time.perf_counter()
                frame = self._stack.pop()
                self._record(label, end - frame[1], frame[2])

        return wrapper

    def snapshot(self) -> dict[str, dict[str, Any]]:
        labels = {
            label: {
                "inclusive_s": round(self.inclusive.get(label, 0.0), 5),
                "exclusive_s": round(self.exclusive.get(label, 0.0), 5),
                "calls": self.calls.get(label, 0),
            }
            for label in sorted(self.inclusive, key=lambda k: -self.inclusive[k])
        }
        by_parent = {
            f"{parent or '<root>'} -> {label}": {
                "inclusive_s": round(seconds, 5),
                "calls": calls,
            }
            for (parent, label), (seconds, calls) in sorted(
                self.by_parent.items(), key=lambda item: -item[1][0]
            )
        }
        return {"labels": labels, "by_parent": by_parent}


_TIMER = PhaseTimer()
_PATCHES: list[tuple[Any, str, Any]] = []


def _install_patch(owner: Any, attr: str, label: str) -> None:
    original = getattr(owner, attr)
    _PATCHES.append((owner, attr, original))
    setattr(owner, attr, _TIMER.wrap(label, original))


def install_instrumentation() -> None:
    """对模块/类属性做计时包装（不改生产代码；结束后由 restore 撤销）。

    ``EdgeStore.replace_for_sources`` 特殊：换成逐行等价的计时副本，把
    DELETE / INSERT / flush 分开计量；外层再套总时长标签。
    """
    import codepilot.repo.index as index_module

    def timed_replace_for_sources(
        self: EdgeStore, repo_id: str, source_ids: list[str], rows: Any
    ) -> None:
        if source_ids:
            with _TIMER.phase("edges.replace_for_sources.delete"):
                self._s.execute(
                    delete(GraphEdge).where(
                        GraphEdge.repository_id == repo_id,
                        GraphEdge.source_id.in_(list(source_ids)),
                    )
                )
        with _TIMER.phase("edges.replace_for_sources.insert"):
            for edge_batch in batched(rows, 1000):
                self._s.execute(insert(GraphEdge), list(edge_batch))
        with _TIMER.phase("edges.replace_for_sources.flush"):
            self._s.flush()

    _install_patch(RepositoryScanner, "scan", "scan")
    _install_patch(TreeSitterParser, "parse", "parse")
    _install_patch(AnalysisContext, "__init__", "context_build")
    _install_patch(ImportResolver, "__init__", "imports_resolve")
    _install_patch(index_module, "_extract_all_edges", "extract_all_edges")
    _install_patch(index_module, "extract_calls", "extract_calls")
    _install_patch(index_module, "extract_edges", "extract_edges")
    _install_patch(index_module, "extract_test_links", "extract_test_links")
    _install_patch(index_module, "_edge_rows", "edge_rows_build")
    _install_patch(index_module, "_resolve_dangling", "resolve_dangling")
    _install_patch(index_module, "_replace_test_edges", "replace_test_edges")
    _install_patch(SymbolStore, "replace_for_files", "symbols.replace_for_files")
    _install_patch(SymbolStore, "list_by_repo", "symbols.list_by_repo")
    _install_patch(SymbolStore, "count_by_kind", "symbols.count_by_kind")
    _install_patch(FileStore, "upsert_many", "files.upsert_many")
    _install_patch(FileStore, "list_by_repo", "files.list_by_repo")
    _install_patch(EdgeStore, "delete_by_kind", "edges.delete_by_kind")
    _install_patch(EdgeStore, "delete_by_ids", "edges.delete_by_ids")
    _install_patch(EdgeStore, "list_dangling_targets", "edges.list_dangling_targets")
    _install_patch(EdgeStore, "count_by_kind_confidence", "edges.count_by_kind_confidence")
    _PATCHES.append((EdgeStore, "replace_for_sources", EdgeStore.replace_for_sources))
    wrapped = _TIMER.wrap("edges.replace_for_sources", timed_replace_for_sources)
    EdgeStore.replace_for_sources = wrapped


def restore_instrumentation() -> None:
    for owner, attr, original in reversed(_PATCHES):
        setattr(owner, attr, original)
    _PATCHES.clear()


# ---------------------------------------------------------------- 索引执行


class CountingLRU(OrderedDict):
    """带逐出计数的 OrderedDict，替换 ``Indexer._parsed``（观测用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.evictions = 0

    def popitem(self, last: bool = True) -> tuple[Any, Any]:
        self.evictions += 1
        return super().popitem(last)


def make_workspace_object() -> Workspace:
    source = normalize_source(str(WORKSPACE_DIR))
    return Workspace(
        path=WORKSPACE_DIR,
        source=source,
        base_commit=BASE_COMMIT,
        repository_id=repository_id(source, BASE_COMMIT),
    )


def make_progress_sink() -> tuple[Any, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    def observe(event: str, **fields: Any) -> None:
        events.append({"event": event, **fields})

    return observe, events


def make_indexer(
    cache_entries: int = DEFAULT_CACHE_ENTRIES,
    cache_bytes: int = DEFAULT_CACHE_BYTES,
) -> tuple[Indexer, list[dict[str, Any]]]:
    observe, events = make_progress_sink()
    indexer = Indexer(
        progress=observe, cache_max_entries=cache_entries, cache_max_bytes=cache_bytes
    )
    indexer._parsed = CountingLRU()
    return indexer, events


def delete_db_files(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()


def open_db(db_path: Path, *, fresh: bool) -> Stores:
    if fresh:
        delete_db_files(db_path)
    engine = create_engine_from_url("sqlite:///" + db_path.as_posix())
    init_db(engine)
    session = create_session_factory(engine)()
    return Stores(session)


def close_db(stores: Stores) -> None:
    stores.session.close()
    stores.session.get_bind().dispose()


def execute_index(
    indexer: Indexer,
    stores: Stores,
    workspace: Workspace,
    db_path: Path,
    *,
    record: bool,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if record:
        _TIMER.reset()
    progress_before = len(events) if events is not None else 0
    start = time.perf_counter()
    stats = indexer.index(stores, workspace)
    wall_index = time.perf_counter() - start
    commit_start = time.perf_counter()
    stores.session.commit()
    wall_commit = time.perf_counter() - commit_start
    edges_by_kind: dict[str, int] = {}
    for (kind, _confidence), count in stats.edges_by_kind_confidence.items():
        edges_by_kind[kind] = edges_by_kind.get(kind, 0) + count
    cache = indexer._parsed
    result: dict[str, Any] = {
        "association_mode": stats.association_mode,
        "wall_index_s": round(wall_index, 4),
        "wall_commit_s": round(wall_commit, 4),
        "indexer_reported_s": round(stats.duration_seconds, 4),
        "scan_files": stats.scan.get("files", 0),
        "db_files": stats.files,
        "symbol_rows": sum(stats.symbols_by_kind.values()),
        "symbols_by_kind": dict(sorted(stats.symbols_by_kind.items())),
        "edge_rows": sum(edges_by_kind.values()),
        "edge_rows_by_kind": dict(sorted(edges_by_kind.items())),
        "test_edges": edges_by_kind.get("TEST", 0),
        "parsed": len(stats.parsed_paths),
        "parse_reused": len(stats.parse_reused_paths),
        "changed": len(stats.changed_paths),
        "content_reused": len(stats.reused_paths),
        "removed": len(stats.removed_paths),
        "parse_errors": len(stats.parse_errors),
        "cache_entries": len(cache),
        "cache_source_bytes": sum(value[1] for value in cache.values()),
        "cache_evictions": cache.evictions,
        "peak_working_set_bytes": peak_bytes(),
        "peak_scope": "process_lifetime",
        "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "progress_events": len(events) - progress_before if events is not None else 0,
    }
    if record:
        snapshot = _TIMER.snapshot()
        result["phases"] = snapshot
        exclusive_total = sum(value["exclusive_s"] for value in snapshot["labels"].values())
        result["unaccounted_s"] = round(wall_index - exclusive_total, 5)
    return result


# ---------------------------------------------------------------- 子进程模式


def run_child(args: argparse.Namespace) -> int:
    install_instrumentation()
    workspace = make_workspace_object()
    db_path = Path(args.db)
    if args.child:
        stores = open_db(db_path, fresh=False)
        indexer, _events = make_indexer(args.cache_entries, args.cache_bytes)
        result = execute_index(indexer, stores, workspace, db_path, record=True)
        close_db(stores)
        payload = {"label": args.label, "run": result}
    else:  # --child-cached：同进程「基线索引 → 改 hub → 增量索引」，峰值内存归本进程
        stores = open_db(db_path, fresh=True)
        indexer, _events = make_indexer(args.cache_entries, args.cache_bytes)
        baseline = execute_index(indexer, stores, workspace, db_path, record=False)
        (WORKSPACE_DIR / HUB_RELPATH).write_text(
            hub_variant(args.variant), encoding="utf-8", newline="\n"
        )
        measured = execute_index(indexer, stores, workspace, db_path, record=True)
        close_db(stores)
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
    cache_entries: int = DEFAULT_CACHE_ENTRIES,
    cache_bytes: int = DEFAULT_CACHE_BYTES,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        f"--{mode}",
        "--db",
        str(db_path),
        "--workspace",
        str(WORKSPACE_DIR),
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
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    return json.loads(out_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 场景


def mark_run(run: dict[str, Any], index: int) -> dict[str, Any]:
    run["run_index"] = index
    run["warmup"] = index < WARMUPS
    return run


def scenario_cold(log: Logger) -> list[dict[str, Any]]:
    runs = []
    for i in range(WARMUPS + MEASURED):
        db_path = WORK_DIR / "cold.sqlite"
        stores = open_db(db_path, fresh=True)
        indexer, events = make_indexer()
        run = execute_index(
            indexer, stores, make_workspace_object(), db_path, record=i >= WARMUPS, events=events
        )
        runs.append(mark_run(run, i))
        close_db(stores)
        log(f"  cold run {i}: {runs[-1]['wall_index_s']}s parsed={runs[-1]['parsed']}")
    return runs


def scenario_newproc_nochange(log: Logger) -> list[dict[str, Any]]:
    db_path = WORK_DIR / "newproc_nochange.sqlite"
    stores = open_db(db_path, fresh=True)
    indexer, _events = make_indexer()
    setup = execute_index(indexer, stores, make_workspace_object(), db_path, record=False)
    close_db(stores)
    log(f"  setup baseline: {setup['wall_index_s']}s")
    runs = []
    for i in range(WARMUPS + MEASURED):
        payload = spawn_child(
            mode="child",
            db_path=db_path,
            out_path=CHILD_DIR / f"newproc_nochange-{i}.json",
            label=f"newproc_nochange-{i}",
        )
        runs.append(mark_run(payload["run"], i))
        log(
            f"  newproc_nochange run {i}: {runs[-1]['wall_index_s']}s "
            f"mode={runs[-1]['association_mode']}"
        )
    return runs


def scenario_sameproc_change(log: Logger) -> list[dict[str, Any]]:
    runs = []
    for i in range(WARMUPS + MEASURED):
        db_path = WORK_DIR / "sameproc_change.sqlite"
        (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_source(), encoding="utf-8", newline="\n")
        stores = open_db(db_path, fresh=True)
        indexer, events = make_indexer()
        execute_index(
            indexer, stores, make_workspace_object(), db_path, record=False, events=events
        )
        (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_variant(i), encoding="utf-8", newline="\n")
        run = execute_index(
            indexer, stores, make_workspace_object(), db_path, record=True, events=events
        )
        runs.append(mark_run(run, i))
        close_db(stores)
        log(
            f"  sameproc_change run {i}: {runs[-1]['wall_index_s']}s parsed={runs[-1]['parsed']} "
            f"reused={runs[-1]['parse_reused']}"
        )
    return runs


def scenario_newproc_change(log: Logger) -> list[dict[str, Any]]:
    runs = []
    for i in range(WARMUPS + MEASURED):
        db_path = WORK_DIR / "newproc_change.sqlite"
        (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_source(), encoding="utf-8", newline="\n")
        delete_db_files(db_path)
        setup = spawn_child(
            mode="child",
            db_path=db_path,
            out_path=CHILD_DIR / f"newproc_change-setup-{i}.json",
            label=f"newproc_change-setup-{i}",
        )
        (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_variant(i), encoding="utf-8", newline="\n")
        measured = spawn_child(
            mode="child",
            db_path=db_path,
            out_path=CHILD_DIR / f"newproc_change-{i}.json",
            label=f"newproc_change-{i}",
        )
        runs.append(mark_run(measured["run"], i))
        runs[-1]["setup_baseline_s"] = setup["run"]["wall_index_s"]
        log(f"  newproc_change run {i}: {runs[-1]['wall_index_s']}s parsed={runs[-1]['parsed']}")
    return runs


def scenario_snapshot_then_change(log: Logger) -> list[dict[str, Any]]:
    runs = []
    for i in range(WARMUPS + MEASURED):
        db_path = WORK_DIR / "snapshot_change.sqlite"
        (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_source(), encoding="utf-8", newline="\n")
        stores = open_db(db_path, fresh=True)
        indexer, events = make_indexer()
        execute_index(
            indexer, stores, make_workspace_object(), db_path, record=False, events=events
        )
        verified = execute_index(
            indexer, stores, make_workspace_object(), db_path, record=False, events=events
        )
        if verified["association_mode"] != "verified_snapshot_v2":
            raise RuntimeError(f"verified reuse 未命中: {verified['association_mode']}")
        (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_variant(i), encoding="utf-8", newline="\n")
        run = execute_index(
            indexer, stores, make_workspace_object(), db_path, record=True, events=events
        )
        runs.append(mark_run(run, i))
        runs[-1]["verified_before"] = True
        close_db(stores)
        log(
            f"  snapshot_then_change run {i}: {runs[-1]['wall_index_s']}s parsed={runs[-1]['parsed']}"
        )
    return runs


def scenario_cache(
    log: Logger, label: str, cache_entries: int, cache_bytes: int
) -> list[dict[str, Any]]:
    runs = []
    for i in range(WARMUPS + MEASURED):
        (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_source(), encoding="utf-8", newline="\n")
        payload = spawn_child(
            mode="child-cached",
            db_path=WORK_DIR / f"{label}.sqlite",
            out_path=CHILD_DIR / f"{label}-{i}.json",
            label=f"{label}-{i}",
            variant=i,
            cache_entries=cache_entries,
            cache_bytes=cache_bytes,
        )
        runs.append(mark_run(payload["measured"], i))
        runs[-1]["cache_limit_entries"] = cache_entries
        runs[-1]["cache_limit_bytes"] = cache_bytes
        runs[-1]["setup_baseline_s"] = payload["baseline"]["wall_index_s"]
        log(
            f"  {label} run {i}: {runs[-1]['wall_index_s']}s parsed={runs[-1]['parsed']} "
            f"reused={runs[-1]['parse_reused']} evictions={runs[-1]['cache_evictions']} "
            f"peak={runs[-1]['peak_working_set_bytes'] / 1024 / 1024:.1f}MiB"
        )
    return runs


# ---------------------------------------------------------------- 汇总


def median_of(values: list[float]) -> float:
    return round(statistics.median(values), 4)


def measured_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run for run in runs if not run["warmup"]]


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    measured = measured_runs(runs)
    walls = [run["wall_index_s"] for run in measured]
    keys: set[str] = set()
    for run in measured:
        keys.update(run.get("phases", {}).get("labels", {}))
    exclusive_total = statistics.median(
        [
            sum(value["exclusive_s"] for value in run.get("phases", {}).get("labels", {}).values())
            for run in measured
        ]
    )
    phases: dict[str, dict[str, Any]] = {}
    for key in sorted(
        keys,
        key=lambda k: (
            -statistics.median(
                [
                    run.get("phases", {}).get("labels", {}).get(k, {}).get("inclusive_s", 0.0)
                    for run in measured
                ]
            )
        ),
    ):
        inclusive = [
            run.get("phases", {}).get("labels", {}).get(key, {}).get("inclusive_s", 0.0)
            for run in measured
        ]
        exclusive = [
            run.get("phases", {}).get("labels", {}).get(key, {}).get("exclusive_s", 0.0)
            for run in measured
        ]
        calls = [
            run.get("phases", {}).get("labels", {}).get(key, {}).get("calls", 0) for run in measured
        ]
        median_inclusive = round(statistics.median(inclusive), 4)
        phases[key] = {
            "inclusive_s": median_inclusive,
            "exclusive_s": round(statistics.median(exclusive), 4),
            "calls": round(statistics.median(calls), 1),
            "share_of_wall": round(median_inclusive / statistics.median(walls), 4)
            if walls
            else 0.0,
        }
    simple_keys = (
        "association_mode",
        "wall_commit_s",
        "scan_files",
        "db_files",
        "symbol_rows",
        "edge_rows",
        "test_edges",
        "parsed",
        "parse_reused",
        "changed",
        "content_reused",
        "removed",
        "parse_errors",
        "cache_entries",
        "cache_source_bytes",
        "cache_evictions",
        "peak_working_set_bytes",
        "db_bytes",
        "unaccounted_s",
    )
    summary: dict[str, Any] = {
        "wall_index_s_median": round(statistics.median(walls), 4),
        "wall_index_s_runs": walls,
        "exclusive_total_s_median": round(exclusive_total, 4),
        "phases_median": phases,
    }
    for key in simple_keys:
        values = [run.get(key) for run in measured]
        summary[key] = (
            statistics.median(values)
            if all(isinstance(v, (int, float)) for v in values)
            else values[0]
        )
    return summary


def by_parent_median(runs: list[dict[str, Any]], parent: str, label: str) -> float:
    values = [
        run.get("phases", {})
        .get("by_parent", {})
        .get(f"{parent or '<root>'} -> {label}", {})
        .get("inclusive_s", 0.0)
        for run in measured_runs(runs)
    ]
    return round(statistics.median(values), 4)


class Logger:
    def __init__(self, path: Path) -> None:
        self._handle = path.open("w", encoding="utf-8")

    def __call__(self, message: str) -> None:
        stamp = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(stamp, flush=True)
        self._handle.write(stamp + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


# ---------------------------------------------------------------- 主流程


def calibrate() -> int:
    generate_workspace(WORKSPACE_DIR)
    workspace = make_workspace_object()
    install_instrumentation()
    db_path = WORK_DIR / "calibrate.sqlite"
    stores = open_db(db_path, fresh=True)
    indexer, _events = make_indexer()
    baseline = execute_index(indexer, stores, workspace, db_path, record=True)
    print(
        f"cold: {baseline['wall_index_s']}s parsed={baseline['parsed']} peak={baseline['peak_working_set_bytes'] / 1048576:.0f}MiB"
    )
    (WORKSPACE_DIR / HUB_RELPATH).write_text(hub_variant(0), encoding="utf-8", newline="\n")
    incremental = execute_index(indexer, stores, workspace, db_path, record=True)
    close_db(stores)
    print(
        f"incremental(sameproc): {incremental['wall_index_s']}s parsed={incremental['parsed']} "
        f"reused={incremental['parse_reused']} evictions={incremental['cache_evictions']}"
    )
    restore_instrumentation()
    return 0


def run_parent(log: Logger) -> int:
    started = time.monotonic()
    counts = generate_workspace(WORKSPACE_DIR)
    log(f"workspace generated: {counts}, expected python files={PY_FILES}")
    install_instrumentation()
    scenarios: dict[str, list[dict[str, Any]]] = {}
    try:
        log("scenario cold")
        scenarios["cold"] = scenario_cold(log)
        log("scenario newproc_nochange")
        scenarios["newproc_nochange"] = scenario_newproc_nochange(log)
        log("scenario sameproc_change")
        scenarios["sameproc_change"] = scenario_sameproc_change(log)
        log("scenario newproc_change")
        scenarios["newproc_change"] = scenario_newproc_change(log)
        log("scenario snapshot_then_change")
        scenarios["snapshot_then_change"] = scenario_snapshot_then_change(log)
        log("scenario cache_128")
        scenarios["cache_128"] = scenario_cache(log, "cache_128", 128, DEFAULT_CACHE_BYTES)
        log("scenario cache_512")
        scenarios["cache_512"] = scenario_cache(log, "cache_512", 512, DEFAULT_CACHE_BYTES)
        log("scenario cache_unlimited")
        scenarios["cache_unlimited"] = scenario_cache(log, "cache_unlimited", UNLIMITED, UNLIMITED)
    finally:
        restore_instrumentation()

    environment = probe_environment()
    summaries = {name: aggregate(runs) for name, runs in scenarios.items()}
    answers = build_answers(scenarios, summaries, counts)
    raw = {
        "task": "phase28-T03 index profile",
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.monotonic() - started, 1),
        "disclaimer": (
            "合成子集（450 个约 1KB 的 Python 文件）只用于机制验证与阶段占比观察，"
            "绝对耗时不可外推到 Django 量级（2717 个更大的文件）。"
        ),
        "config": {
            "seed": SEED,
            "source_modules": SOURCE_MODULES,
            "test_step": TEST_STEP,
            "python_files": PY_FILES,
            "warmups": WARMUPS,
            "measured": MEASURED,
            "hub_path": HUB_RELPATH,
            "workspace": str(WORKSPACE_DIR),
            "cache_defaults": {"entries": DEFAULT_CACHE_ENTRIES, "bytes": DEFAULT_CACHE_BYTES},
        },
        "environment": environment,
        "answers": answers,
        "scenario_summaries": summaries,
        "scenario_runs": scenarios,
        "raw_logs": {
            "raw_json": str(RAW_JSON),
            "log": str(LOG_PATH),
            "child_outputs": str(CHILD_DIR),
            "work_dir": str(WORK_DIR),
        },
    }
    RAW_JSON.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    notes = {
        "task": "phase28-T03 index profile",
        "generated_at": raw["generated_at"],
        "script": str(SCRIPT_PATH.relative_to(REPO_ROOT)),
        "disclaimer": raw["disclaimer"],
        "environment": environment,
        "config": raw["config"],
        "answers": answers,
        "scenarios": summaries,
        "raw_logs": raw["raw_logs"],
    }
    NOTES_JSON.parent.mkdir(parents=True, exist_ok=True)
    NOTES_JSON.write_text(json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"done in {raw['elapsed_s']}s; notes={NOTES_JSON}; raw={RAW_JSON}")
    return 0


def build_answers(
    scenarios: dict[str, list[dict[str, Any]]],
    summaries: dict[str, dict[str, Any]],
    counts: dict[str, int],
) -> dict[str, Any]:
    def phase_of(summary: dict[str, Any], label: str) -> float:
        return summary.get("phases_median", {}).get(label, {}).get("inclusive_s", 0.0)

    py_files = counts["python_files"]
    cache128 = summaries["cache_128"]
    sameproc = summaries["sameproc_change"]
    wall = sameproc["wall_index_s_median"]

    replace_total = phase_of(sameproc, "edges.replace_for_sources")
    under_test = by_parent_median(
        scenarios["sameproc_change"], "replace_test_edges", "edges.replace_for_sources"
    )
    under_dangling = by_parent_median(
        scenarios["sameproc_change"], "resolve_dangling", "edges.replace_for_sources"
    )
    main_edge_insert = round(replace_total - under_test - under_dangling, 4)
    test_extract = phase_of(sameproc, "extract_test_links")
    test_write = phase_of(sameproc, "replace_test_edges")
    old_edge_delete = round(
        phase_of(sameproc, "edges.replace_for_sources.delete")
        + phase_of(sameproc, "edges.delete_by_ids"),
        4,
    )
    symbol_insert = phase_of(sameproc, "symbols.replace_for_files")

    q2 = {
        "basis": "sameproc_change（增量场景，cache=128）median",
        "wall_index_s": wall,
        "shares": {
            "test_closure_extract": {"s": test_extract, "share": round(test_extract / wall, 4)},
            "test_closure_write_delete_and_insert": {
                "s": test_write,
                "share": round(test_write / wall, 4),
                "includes_delete_by_kind": phase_of(sameproc, "edges.delete_by_kind"),
            },
            "old_edge_delete": {"s": old_edge_delete, "share": round(old_edge_delete / wall, 4)},
            "symbol_insert": {"s": symbol_insert, "share": round(symbol_insert / wall, 4)},
            "main_edge_insert": {"s": main_edge_insert, "share": round(main_edge_insert / wall, 4)},
        },
        "note": (
            "test_closure_write 与 main/dangling insert 都来自 edges.replace_for_sources，"
            "按 by_parent 归属拆分，无重复计算；delete_by_kind 只被 replace_test_edges 调用。"
        ),
    }

    q3 = {}
    for name in ("cache_128", "cache_512", "cache_unlimited"):
        summary = summaries[name]
        q3[name] = {
            "wall_index_s": summary["wall_index_s_median"],
            "parsed": summary["parsed"],
            "parse_reused": summary["parse_reused"],
            "cache_evictions": summary["cache_evictions"],
            "cache_entries": summary["cache_entries"],
            "parse_inclusive_s": phase_of(summary, "parse"),
            "peak_working_set_bytes": summary["peak_working_set_bytes"],
        }
    q3["thrash_ratio_128"] = {
        "evictions": q3["cache_128"]["cache_evictions"],
        "hits": q3["cache_128"]["parse_reused"],
        "verdict": "逐出数≈逐出插入数=缺失数，命中来自快照 cached_at_start，与逐出不互斥",
    }
    q3["parse_savings"] = {
        "512_vs_128_s": round(
            q3["cache_128"]["parse_inclusive_s"] - q3["cache_512"]["parse_inclusive_s"], 4
        ),
        "unlimited_vs_128_s": round(
            q3["cache_128"]["parse_inclusive_s"] - q3["cache_unlimited"]["parse_inclusive_s"], 4
        ),
        "wall_savings_512_vs_128": round(
            q3["cache_128"]["wall_index_s"] - q3["cache_512"]["wall_index_s"], 4
        ),
        "peak_delta_512_vs_128_bytes": q3["cache_512"]["peak_working_set_bytes"]
        - q3["cache_128"]["peak_working_set_bytes"],
        "peak_delta_unlimited_vs_128_bytes": q3["cache_unlimited"]["peak_working_set_bytes"]
        - q3["cache_128"]["peak_working_set_bytes"],
    }

    return {
        "q1_2589_parses": {
            "subset_python_files": py_files,
            "cache_entries_limit": 128,
            "expected_reuse": min(128, py_files),
            "expected_parsed": py_files - min(128, py_files),
            "observed_cache_128": {
                "parsed": cache128["parsed"],
                "parse_reused": cache128["parse_reused"],
                "parsed_plus_reused": cache128["parsed"] + cache128["parse_reused"],
                "check_parsed_eq_py_minus_reused": cache128["parsed"]
                == py_files - cache128["parse_reused"],
                "check_reused_eq_min128": cache128["parse_reused"] == min(128, py_files),
            },
            "observed_newproc_change": {
                "parsed": summaries["newproc_change"]["parsed"],
                "parse_reused": summaries["newproc_change"]["parse_reused"],
            },
            "explanation": (
                "缓存是实例内 OrderedDict LRU（≤128 条且 ≤8MiB 源字节），只存解析树不持久化；"
                "冷/基线索引按路径序解析 N 个文件后只剩最后 128 棵树，其余 N-128 棵已被逐出。"
                "增量的 parsed_files 覆盖全部扫描文件且 new_symbol_ids=全仓库符号集，"
                "重建关联需要每棵树；缓存只在快照 cached_at_start 里保住 128 个命中，"
                "其余必须重解析，因此 解析数 = Python 文件数 − 缓存命中数(≤128)。"
                "Django：2589 = 2717 − 128；子集 cache_128：见 observed。"
            ),
        },
        "q2_phase_shares": q2,
        "q3_cache_thrash": q3,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--calibrate", action="store_true", help="只跑一次冷索引+一次增量用于耗时校准"
    )
    parser.add_argument("--child", action="store_true", help="子进程：对既有 DB 做一次索引")
    parser.add_argument(
        "--child-cached", action="store_true", help="子进程：基线索引→改 hub→增量索引"
    )
    parser.add_argument("--db", type=str, default="")
    parser.add_argument("--workspace", type=str, default="")
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--label", type=str, default="child")
    parser.add_argument("--variant", type=int, default=0)
    parser.add_argument("--cache-entries", type=int, default=DEFAULT_CACHE_ENTRIES)
    parser.add_argument("--cache-bytes", type=int, default=DEFAULT_CACHE_BYTES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    CHILD_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if args.child or args.child_cached:
        return run_child(args)
    if args.calibrate:
        return calibrate()
    log = Logger(LOG_PATH)
    try:
        return run_parent(log)
    except BaseException:
        import traceback

        log("FATAL:\n" + traceback.format_exc())
        raise
    finally:
        log.close()


if __name__ == "__main__":
    main()
