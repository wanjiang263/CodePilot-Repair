"""Phase32 T09: index performance on a real repository (cold / no-change / one-file).

Why this script exists: every earlier profile script builds a *synthetic* repo
(test50 / test300 / M4). Phase32 requires a real codebase so the numbers are not
extrapolated from generated shapes. The script copies the tracked files of a real
git checkout into a scratch directory (the source checkout is never modified),
then runs three isolated child processes against one SQLite database:

1. ``cold``     - fresh database, first full index of the copied tree;
2. ``nochange`` - same database, same bytes: everything should be reused;
3. ``change``   - one new function is added to a real module (symbol set changes, so the
   conservative full-rebuild guard in Indexer.index must trigger);
4. ``change_body`` - the same module gets a body-only edit (symbols identical, so the
   Phase29/30 incremental fast path is allowed to run).

Each child is a fresh process, so the in-process parse cache cannot carry over
and the wall time includes only that child's index call (child startup is timed
separately). Absolute numbers are machine-specific; the ratio between the three
scenarios is the signal.

Usage::

    .venv/Scripts/python.exe scripts/phase32_index_profile.py --repo <path>
    .venv/Scripts/python.exe scripts/phase32_index_profile.py --run cold --db <db> --root <copy>

Output: ``.codex-pytest-tmp/phase32/index-profile.json``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH = REPO_ROOT / ".codex-pytest-tmp" / "phase32"
DEFAULT_REF = SCRATCH / "realrepo"
DEFAULT_DB = SCRATCH / "index-profile.sqlite3"
RAW_JSON = SCRATCH / "index-profile.json"
PROBE = "\n\ndef _phase32_probe() -> int:\n    return 42\n"


def load_p28() -> Any:
    spec = importlib.util.spec_from_file_location(
        "phase28_index_profile", REPO_ROOT / "scripts" / "phase28_index_profile.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("phase28_index_profile", module)
    spec.loader.exec_module(module)
    return module


def export_tracked(repo: Path, dest: Path) -> dict[str, Any]:
    """Copy the tracked files of ``repo`` at HEAD into ``dest`` (no working-tree drift)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = dest.parent / "realrepo.tar"
    with archive.open("wb") as handle:
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"],
            stdout=handle,
            check=True,
        )
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    archive.unlink()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    files = sum(1 for path in dest.rglob("*") if path.is_file())
    return {"source_repo": str(repo), "source_head": head, "exported_files": files}


def run_one(scenario: str, db: Path, root: Path, source_commit: str) -> dict[str, Any]:
    """Index ``root`` once in this process and report the observable counts."""
    p28 = load_p28()
    p28.WORKSPACE_DIR = root
    p28.BASE_COMMIT = source_commit
    workspace = p28.make_workspace_object()
    indexer, events = p28.make_indexer()
    if scenario == "cold":
        p28.delete_db_files(db)
    stores = p28.open_db(db, fresh=False)
    try:
        started = time.perf_counter()
        result: dict[str, Any] = p28.execute_index(
            indexer, stores, workspace, db, record=True, events=events
        )
        wall = time.perf_counter() - started
    finally:
        p28.close_db(stores)
    result.update(
        {
            "scenario": scenario,
            "db_path": str(db),
            "workspace": str(root),
            "child_total_wall_s": round(wall, 4),
            "base_commit": source_commit,
        }
    )
    return result


def pick_probe_file(root: Path) -> Path:
    """Largest Python module outside tests: the edit is guaranteed to change a real body."""
    candidates = [
        path
        for path in root.rglob("*.py")
        if "tests" not in path.parts and "__pycache__" not in path.parts
    ]
    if not candidates:
        raise SystemExit(f"no python module under {root}")
    return max(candidates, key=lambda path: path.stat().st_size)


def child(args: list[str]) -> tuple[int, str]:
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return done.returncode, done.stdout.strip()


def drive(repo: Path) -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    export = export_tracked(repo, DEFAULT_REF)
    if DEFAULT_DB.exists():
        DEFAULT_DB.unlink()
    runs: list[dict[str, Any]] = []
    durations: dict[str, float] = {}
    probe = pick_probe_file(DEFAULT_REF)
    original = probe.read_text(encoding="utf-8")

    def run_scenario(scenario: str) -> int:
        started = time.perf_counter()
        code, out = child(
            [
                "--run",
                scenario,
                "--db",
                str(DEFAULT_DB),
                "--root",
                str(DEFAULT_REF),
                "--source-commit",
                export["source_head"],
            ]
        )
        durations[scenario] = round(time.perf_counter() - started, 4)
        if code != 0:
            print(out)
            return code
        runs.append(json.loads(out))
        return 0

    for scenario in ("cold", "nochange"):
        code = run_scenario(scenario)
        if code:
            return code
    # Scenario 3 changes the symbol set (new function): the conservative fast-path
    # guard in Indexer.index must fall back to a full rebuild here.
    probe.write_text(original + PROBE, encoding="utf-8", newline="\n")
    code = run_scenario("change")
    if code:
        return code
    # Scenario 4 edits a body only: symbols stay identical, so the fast path is
    # allowed to restrict edge work to the changed file.
    probe.write_text(
        original + PROBE.replace("return 42", "return 43"),
        encoding="utf-8",
        newline="\n",
    )
    code = run_scenario("change_body")
    if code:
        return code
    for run in runs:
        run["child_process_wall_s"] = durations[run["scenario"]]
    payload = {
        "phase": 32,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "export": export,
        "probe_file": str(probe.relative_to(DEFAULT_REF)).replace("\\", "/"),
        "probe_edit": PROBE.strip(),
        "association_mode": runs[-1]["association_mode"],
        "runs": runs,
    }
    RAW_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(payload, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--run", choices=["cold", "nochange", "change", "change_body"])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--root", default=str(DEFAULT_REF))
    parser.add_argument("--source-commit", default="")
    args = parser.parse_args()
    if args.run:
        result = run_one(args.run, Path(args.db), Path(args.root), args.source_commit)
        print(json.dumps(result))
        return 0
    return drive(Path(args.repo).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
