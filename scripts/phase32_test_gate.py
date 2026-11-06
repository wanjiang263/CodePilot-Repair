"""Phase32 test gate: run the suite groups sequentially, with resource preflight.

Three environment facts drive this script (all observed on this machine, not
assumed):

1. 13 tests in ``tests/unit/retrieval`` resolve ripgrep through
   ``shutil.which("rg")`` (src/codepilot/retrieval/lexical.py:106). Outside the
   Codex shell that PATH entry is absent, so the suite fails deterministically
   with ``RetrievalError: ripgrep (rg) is required for the ripgrep channel``.
   The gate locates an rg binary, puts its directory first on the child PATH,
   and records path plus version.
2. The historical flakes are Windows resource flakes. Reproduced in Phase32:
   with ~1.5 GB free physical memory and ~2.5 GB of commit headroom left, twelve
   archive tests failed with ``fatal: Out of memory, malloc failed (tried to
   allocate 1048576 bytes)`` raised by a ``git add -A`` child process. The gate
   therefore samples free physical memory *and* commit headroom before every
   run and refuses to start (exit 3) when the machine is below the documented
   floor, so an OOM is reported as an environment condition instead of twelve
   mysterious test failures.
3. Every run leaves pytest temporary trees behind. The gate gives each group
   run its own ``--basetemp`` under ``<SCRATCH>/pytest/`` and removes exactly
   that directory when the run finishes. It never touches the shared
   ``%TEMP%/pytest-of-<user>`` root, so concurrent pytest processes on the
   same machine keep their data (Phase33 D2 fix; the previous behaviour of
   wiping the global root has been deleted, not reworded).

The gate never retries, never sleeps its way past a failure and never widens a
mocked timeout: a red run stays red.

Usage:
  ``.venv/Scripts/python.exe scripts/phase32_test_gate.py --repeat 2``
  ``.venv/Scripts/python.exe scripts/phase32_test_gate.py --suite unit --repeat 1``
  ``.venv/Scripts/python.exe scripts/phase32_test_gate.py --scratch .codex-pytest-tmp/phase33 --repeat 1``
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".codex-pytest-tmp" / "phase32"
PYTHON = Path(sys.executable)
RG_CANDIDATES: tuple[Callable[[], Path], ...] = (
    lambda: Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "rg.exe",
    lambda: Path.home() / "scoop" / "shims" / "rg.exe",
    lambda: Path("C:/ProgramData/chocolatey/bin/rg.exe"),
    lambda: Path("C:/Program Files/ripgrep/rg.exe"),
)


class MemoryStatus(ctypes.Structure):
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


def memory() -> dict[str, int]:
    if sys.platform.startswith("linux"):
        values = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, value = line.partition(":")
            values[key] = int(value.split()[0]) // 1024
        return {
            "total_phys_mb": values["MemTotal"],
            "avail_phys_mb": values["MemAvailable"],
            "total_pagefile_mb": values["MemTotal"] + values["SwapTotal"],
            "avail_pagefile_mb": values["MemAvailable"] + values["SwapFree"],
            "commit_headroom_mb": max(0, values["CommitLimit"] - values["Committed_AS"]),
            "load_pct": int(100 * (1 - values["MemAvailable"] / values["MemTotal"])),
        }
    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return {
        "total_phys_mb": int(status.ullTotalPhys) // 1048576,
        "avail_phys_mb": int(status.ullAvailPhys) // 1048576,
        "total_pagefile_mb": int(status.ullTotalPageFile) // 1048576,
        "avail_pagefile_mb": int(status.ullAvailPageFile) // 1048576,
        "commit_headroom_mb": int(status.ullAvailPageFile) // 1048576,
        "load_pct": int(status.dwMemoryLoad),
    }


def tree_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def resolve_scratch(raw: str | None) -> Path:
    """Resolve --scratch against ROOT so the gate is cwd-independent."""
    candidate = Path(raw) if raw else SCRATCH
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate


def basetemp_dir(scratch: Path, suite: str, run_index: int) -> Path:
    """One isolated pytest basetemp per group run: <scratch>/pytest/<suite>-run<n>-<ts>."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%f")
    return scratch / "pytest" / f"{suite}-run{run_index}-{stamp}"


def display_path(path: Path) -> str:
    """Repo-relative when possible, absolute when scratch lives outside ROOT."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def remove_owned_dir(path: Path) -> dict[str, object]:
    """Delete exactly one directory this gate created; never walk outside it."""
    record: dict[str, object] = {
        "path": str(path),
        "existed": path.is_dir(),
        "removed": False,
        "bytes_removed": 0,
    }
    if not record["existed"]:
        return record
    record["bytes_removed"] = tree_bytes(path)
    shutil.rmtree(path, ignore_errors=True)
    record["removed"] = not path.exists()
    return record


def locate_rg() -> Path | None:
    found = shutil.which("rg")
    if found:
        return Path(found)
    for factory in RG_CANDIDATES:
        candidate = factory()
        if candidate.is_file():
            return candidate
    return None


def child_env(rg: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    if rg is not None:
        env["PATH"] = str(rg.parent) + os.pathsep + env.get("PATH", "")
    return env


def rg_version(rg: Path | None) -> str:
    if rg is None:
        return ""
    probe = subprocess.run([str(rg), "--version"], capture_output=True, text=True, check=False)
    return (probe.stdout or "").splitlines()[0] if probe.stdout else ""


def wait_for_memory(min_free_mb: int, min_headroom_mb: int, wait_s: int) -> dict[str, int]:
    deadline = time.monotonic() + max(wait_s, 0)
    sample = memory()
    while True:
        enough = (
            sample["avail_phys_mb"] >= min_free_mb
            and sample["commit_headroom_mb"] >= min_headroom_mb
        )
        if enough or time.monotonic() >= deadline:
            sample["sufficient"] = 1 if enough else 0
            return sample
        time.sleep(5)
        sample = memory()


def run_group(
    suite: str,
    index: int,
    env: dict[str, str],
    target: str,
    extra: list[str],
    scratch: Path,
) -> dict[str, object]:
    log = scratch / f"gate-{suite}-run{index}.log"
    junit = scratch / f"gate-{suite}-run{index}.xml"
    basetemp = basetemp_dir(scratch, suite, index)
    basetemp.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    started = time.monotonic()
    paths = [part for part in target.split(",") if part]
    command = [
        str(PYTHON),
        "-m",
        "pytest",
        *paths,
        "-q",
        *extra,
        f"--junitxml={junit}",
        f"--basetemp={basetemp}",
    ]
    with log.open("wb") as handle:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    wall_s = round(time.monotonic() - started, 2)
    tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
    # Clean up only the directory this run created; the shared
    # %TEMP%/pytest-of-<user> root is never listed, never walked, never removed.
    reclaimed = remove_owned_dir(basetemp)
    record = {
        "suite": suite,
        "run": index,
        "started_at": started_at,
        "exit_code": completed.returncode,
        "wall_s": wall_s,
        "memory_after": memory(),
        "log": display_path(log),
        "junit": display_path(junit),
        "basetemp": str(basetemp),
        "command": " ".join(command),
        "summary_line": tail[0] if tail else "",
        "temp_reclaimed": reclaimed,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", choices=("unit", "api"), default=None)
    parser.add_argument(
        "--target",
        action="append",
        default=None,
        metavar="NAME=PATH",
        help="extra regression group, e.g. graph=tests/graph or archive=tests/unit/test_phase31_archive_verify.py",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        default=None,
        metavar="ARG",
        help="extra pytest argument appended to every target in that gate call",
    )
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--min-free-mb", type=int, default=1500)
    parser.add_argument("--min-headroom-mb", type=int, default=2000)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument(
        "--scratch",
        default=None,
        metavar="DIR",
        help=(
            "scratch root for logs/junit/summary and per-run pytest basetemp dirs; "
            f"relative paths resolve under the repo root (default: {SCRATCH.relative_to(ROOT).as_posix()})"
        ),
    )
    parser.add_argument("--summary", default=None)
    args = parser.parse_args(argv)
    scratch = resolve_scratch(args.scratch)
    summary_path = Path(args.summary) if args.summary else scratch / "test-gate.json"
    if not summary_path.is_absolute():
        summary_path = ROOT / summary_path
    groups: list[tuple[str, str]] = [(name, f"tests/{name}") for name in (args.suite or [])]
    for raw in args.target or []:
        name, _, path = raw.partition("=")
        if not name or not path:
            parser.error(f"--target must be NAME=PATH, got {raw!r}")
        groups.append((name, path))
    if not groups:
        groups = [("unit", "tests/unit"), ("api", "tests/api")]
    extra = list(args.pytest_arg or [])
    scratch.mkdir(parents=True, exist_ok=True)
    rg = locate_rg()
    env = child_env(rg)
    records = []
    blocked = []
    for suite, target in groups:
        for index in range(1, args.repeat + 1):
            before = wait_for_memory(args.min_free_mb, args.min_headroom_mb, args.wait_seconds)
            if not before["sufficient"]:
                blocked.append({"suite": suite, "run": index, "memory": before})
                print(json.dumps({"blocked": blocked[-1]}, ensure_ascii=False), flush=True)
                continue
            record = run_group(suite, index, env, target, extra, scratch)
            record["memory_before"] = before
            records.append(record)
    summary = {
        "tool": "phase32-test-gate",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scratch": str(scratch),
        "rg_path": str(rg) if rg else None,
        "rg_version": rg_version(rg),
        "sequential": True,
        "preconditions": {
            "min_free_mb": args.min_free_mb,
            "min_commit_headroom_mb": args.min_headroom_mb,
            "wait_seconds": args.wait_seconds,
        },
        "runs": records,
        "blocked_runs": blocked,
        "failed_runs": [f"{r['suite']}#{r['run']}" for r in records if r["exit_code"] != 0],
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    if blocked:
        return 3
    return 0 if not summary["failed_runs"] else 1


if __name__ == "__main__":
    sys.exit(main())
