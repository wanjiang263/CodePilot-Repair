"""Phase32 T09 / Phase33 T05: index performance on a real repository.

Scenarios (each an isolated child process, its own SQLite database file):

1. "cold"        - fresh database, first full index of the copied tree;
2. "nochange"    - copy of the cold database, same bytes: verified-snapshot reuse;
3. "change"      - one new function is added to a real module (symbol set changes);
4. "change_body" - the same module gets a body-only edit (symbols identical).

Phase33 T05 changes over the Phase32 version:

* never writes into .codex-pytest-tmp/phase32/ (historical evidence, read-only);
  every artifact is redirected through --scratch / --dbroot / --out /
  --checkpoint-dir;
* each scenario gets its own database directory "<dbroot>-<scenario>/index.sqlite3",
  chained by copying the previous scenario's database files (no shared mutable DB);
* tolerant instrumentation: phase timing labels are installed per-target with
  hasattr guards, so the script works both before and after the index.py refactor
  (Phase32's runs had empty phases because run_one never installed them);
* the commit under test is pinned (git archive <sha>) and the export can be
  reused (--reuse-export) so baseline and after runs index identical bytes;
* built-in verification: after the chain, fresh full indexes of the probe-42 and
  probe-43 states are dump-compared (tests/support/indexdump.dump_index, pickled
  bytes -> sha256) against the incremental change / change_body databases, plus
  nochange vs cold;
* --checkpoint-dir progress files "index-profile[-tag]-<scenario>.done" so an
  interrupted drive can be resumed/audited (OOM-history machine).

Usage:

    # full baseline drive (tag=baseline)
    .venv/Scripts/python.exe scripts/phase32_index_profile.py \
        --scratch .codex-pytest-tmp/phase33/evidence/t05/scratch \
        --dbroot .codex-pytest-tmp/phase33/evidence/t05/db \
        --out .codex-pytest-tmp/phase33/evidence/t05/profile-baseline.json

    # single scenario (child), compatible with the Phase32 CLI
    .venv/Scripts/python.exe scripts/phase32_index_profile.py \
        --run cold --db <db> --root <export> --source-commit <sha>

    # byte-compare two index databases of the same workspace state
    .venv/Scripts/python.exe scripts/phase32_index_profile.py \
        --compare <dbA> <dbB> --root <export> --source-commit <sha>
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
DEFAULT_SCRATCH = REPO_ROOT / ".codex-pytest-tmp" / "phase33" / "evidence" / "t05" / "scratch"
DEFAULT_DBROOT = REPO_ROOT / ".codex-pytest-tmp" / "phase33" / "evidence" / "t05" / "db"
DEFAULT_OUT = REPO_ROOT / ".codex-pytest-tmp" / "phase33" / "evidence" / "t05" / "profile.json"
DEFAULT_CHECKPOINTS = REPO_ROOT / ".codex-pytest-tmp" / "phase33" / "runs"
PROBE = "\n\ndef _phase32_probe() -> int:\n    return 42\n"
PROBE_BODY = PROBE.replace("return 42", "return 43")


def load_p28() -> Any:
    spec = importlib.util.spec_from_file_location(
        "phase28_index_profile", REPO_ROOT / "scripts" / "phase28_index_profile.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("phase28_index_profile", module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- instrumentation


def install_instrumentation_tolerant(p28: Any) -> list[str]:
    """Install p28 phase timers, skipping targets that no longer exist.

    The Phase33 closure refactor may rename or add module-level helpers in
    codepilot.repo.index; every label is optional so one script works against
    both the old and the new implementation. Returns the missing labels.
    """
    import codepilot.repo.index as index_module
    from codepilot.storage.repository import EdgeStore, FileStore, SymbolStore

    missing: list[str] = []
    targets: list[tuple[Any, str, str]] = [
        (p28.RepositoryScanner, "scan", "scan"),
        (p28.TreeSitterParser, "parse", "parse"),
        (p28.AnalysisContext, "__init__", "context_build"),
        (p28.ImportResolver, "__init__", "imports_resolve"),
        (index_module, "_extract_all_edges", "extract_all_edges"),
        (index_module, "_affected_closure", "affected_closure"),
        (index_module, "_closure_source_maps", "closure_source_maps"),
        (index_module, "extract_calls", "extract_calls"),
        (index_module, "extract_edges", "extract_edges"),
        (index_module, "extract_test_links", "extract_test_links"),
        (index_module, "_edge_rows", "edge_rows_build"),
        (index_module, "_resolve_dangling", "resolve_dangling"),
        (index_module, "_replace_test_edges", "replace_test_edges"),
        (index_module, "_import_bindings_changed", "import_bindings_guard"),
        (SymbolStore, "replace_for_files", "symbols.replace_for_files"),
        (SymbolStore, "list_by_repo", "symbols.list_by_repo"),
        (SymbolStore, "count_by_kind", "symbols.count_by_kind"),
        (FileStore, "upsert_many", "files.upsert_many"),
        (FileStore, "list_by_repo", "files.list_by_repo"),
        (EdgeStore, "delete_by_kind", "edges.delete_by_kind"),
        (EdgeStore, "delete_by_ids", "edges.delete_by_ids"),
        (EdgeStore, "list_dangling_targets", "edges.list_dangling_targets"),
        (EdgeStore, "count_by_kind_confidence", "edges.count_by_kind_confidence"),
    ]
    for owner, attr, label in targets:
        if hasattr(owner, attr):
            p28._install_patch(owner, attr, label)
        else:
            missing.append(f"{getattr(owner, '__name__', owner)}.{attr}")

    # Same timed copy p28 uses: splits replace_for_sources into delete/insert/flush.
    from itertools import batched

    from sqlalchemy import delete, insert

    from codepilot.storage.models import GraphEdge

    timer = p28._TIMER

    def timed_replace_for_sources(
        self: Any, repo_id: str, source_ids: list[str], rows: Any
    ) -> None:
        if source_ids:
            with timer.phase("edges.replace_for_sources.delete"):
                self._s.execute(
                    delete(GraphEdge).where(
                        GraphEdge.repository_id == repo_id,
                        GraphEdge.source_id.in_(list(source_ids)),
                    )
                )
        with timer.phase("edges.replace_for_sources.insert"):
            for edge_batch in batched(rows, 1000):
                self._s.execute(insert(GraphEdge), list(edge_batch))
        with timer.phase("edges.replace_for_sources.flush"):
            self._s.flush()

    attr = "replace_for_sources"
    p28._PATCHES.append((EdgeStore, attr, getattr(EdgeStore, attr)))
    # dynamic setattr: instrumented method replacement, same mechanism as _install_patch
    # (a literal assignment would trip mypy method-assign; a constant setattr trips ruff B010).
    setattr(EdgeStore, attr, timer.wrap("edges.replace_for_sources", timed_replace_for_sources))
    return missing


# ---------------------------------------------------------------- export / child


def pinned_head(repo: Path, pin: str) -> str:
    if pin:
        return pin
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def export_tracked(repo: Path, dest: Path, commit: str) -> dict[str, Any]:
    """Copy the tracked files of repo at commit into dest."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = dest.parent / "realrepo.tar"
    with archive.open("wb") as handle:
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", commit],
            stdout=handle,
            check=True,
        )
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    archive.unlink()
    (dest / ".export-head").write_text(commit + "\n", encoding="utf-8")
    files = sum(1 for path in dest.rglob("*") if path.is_file())
    return {"source_repo": str(repo), "source_head": commit, "exported_files": files}


def run_one(
    scenario: str,
    db: Path,
    root: Path,
    source_commit: str,
    *,
    fresh: bool = False,
) -> dict[str, Any]:
    """Index root once in this process and report the observable counts."""
    p28 = load_p28()
    p28.WORKSPACE_DIR = root
    p28.BASE_COMMIT = source_commit
    missing = install_instrumentation_tolerant(p28)
    workspace = p28.make_workspace_object()
    indexer, events = p28.make_indexer()
    if fresh or scenario == "cold":
        p28.delete_db_files(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    stores = p28.open_db(db, fresh=False)
    try:
        started = time.perf_counter()
        result: dict[str, Any] = p28.execute_index(
            indexer, stores, workspace, db, record=True, events=events
        )
        wall = time.perf_counter() - started
    finally:
        p28.close_db(stores)
        p28.restore_instrumentation()
    result.update(
        {
            "scenario": scenario,
            "db_path": str(db),
            "workspace": str(root),
            "child_total_wall_s": round(wall, 4),
            "base_commit": source_commit,
            "instrumentation_missing": missing,
        }
    )
    return result


def pick_probe_file(root: Path) -> Path:
    """Largest Python module outside tests: the edit changes a real body."""
    candidates = [
        path
        for path in root.rglob("*.py")
        if "tests" not in path.parts and "__pycache__" not in path.parts
    ]
    if not candidates:
        raise SystemExit(f"no python module under {root}")
    return max(candidates, key=lambda path: path.stat().st_size)


def child(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    import os

    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=full_env,
        timeout=3600,
    )
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(src) + suffix)
        if candidate.exists():
            shutil.copy2(candidate, Path(str(dst) + suffix))


def write_checkpoint(directory: Path, name: str, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.done").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


# ---------------------------------------------------------------- dump compare


def compare_dumps(db_a: Path, db_b: Path, root: Path, commit: str) -> dict[str, Any]:
    """dump_index byte-equivalence of two databases of the same workspace state."""
    import hashlib
    import pickle

    p28 = load_p28()
    p28.WORKSPACE_DIR = root
    p28.BASE_COMMIT = commit
    workspace = p28.make_workspace_object()
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    import importlib

    # runtime-only path injection (tests/support); dynamic import keeps mypy/ruff clean
    dump_index = importlib.import_module("support.indexdump").dump_index

    sections = ("files", "symbols", "edges")
    dumps: list[tuple[Any, ...]] = []
    for db in (db_a, db_b):
        stores = p28.open_db(db, fresh=False)
        try:
            dumps.append(dump_index(stores, workspace.repository_id))
        finally:
            p28.close_db(stores)

    def digest(dump: tuple[Any, ...]) -> dict[str, str]:
        whole = hashlib.sha256(pickle.dumps(dump, protocol=5)).hexdigest()
        per = {
            name: hashlib.sha256(pickle.dumps(part, protocol=5)).hexdigest()
            for name, part in zip(sections, dump, strict=True)
        }
        return {"whole": whole, **per}

    digest_a, digest_b = digest(dumps[0]), digest(dumps[1])
    first_diff: dict[str, Any] | None = None
    if digest_a["whole"] != digest_b["whole"]:
        for name, part_a, part_b in zip(sections, dumps[0], dumps[1], strict=True):
            if part_a != part_b:
                rows_a = {repr(row) for row in part_a}
                rows_b = {repr(row) for row in part_b}
                first_diff = {
                    "section": name,
                    "rows_a": len(part_a),
                    "rows_b": len(part_b),
                    "only_in_a_sample": sorted(rows_a - rows_b)[:3],
                    "only_in_b_sample": sorted(rows_b - rows_a)[:3],
                }
                break
    return {
        "db_a": str(db_a),
        "db_b": str(db_b),
        "equal": digest_a["whole"] == digest_b["whole"],
        "sha_a": digest_a,
        "sha_b": digest_b,
        "first_diff": first_diff,
    }


# ---------------------------------------------------------------- driver


def drive(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    scratch = Path(args.scratch).resolve()
    dbroot = Path(args.dbroot).resolve()
    out = Path(args.out).resolve()
    checkpoints = Path(args.checkpoint_dir).resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    export_dir = scratch / "realrepo"
    commit = pinned_head(repo, args.pin_commit)

    marker = export_dir / ".export-head"
    if args.reuse_export and marker.exists() and marker.read_text().strip() == commit:
        export = {
            "source_repo": str(repo),
            "source_head": commit,
            "exported_files": sum(1 for p in export_dir.rglob("*") if p.is_file()),
            "reused": True,
        }
    else:
        export = export_tracked(repo, export_dir, commit)
        export["reused"] = False

    probe = pick_probe_file(export_dir)
    original = probe.read_text(encoding="utf-8")

    def apply_state(state: str | None) -> None:
        if state is None:
            probe.write_text(original, encoding="utf-8", newline="\n")
        elif state == "probe42":
            probe.write_text(original + PROBE, encoding="utf-8", newline="\n")
        elif state == "probe43":
            probe.write_text(original + PROBE_BODY, encoding="utf-8", newline="\n")
        else:
            raise AssertionError(state)

    def db_for(scenario: str) -> Path:
        return dbroot.parent / f"{dbroot.name}-{scenario}" / "index.sqlite3"

    child_env: dict[str, str] = {}
    if args.parse_cache_dir:
        cache_dir = Path(args.parse_cache_dir).resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)
        child_env["CODEPILOT_PARSE_CACHE_DIR"] = str(cache_dir)

    runs: list[dict[str, Any]] = []
    durations: dict[str, float] = {}
    prefix = f"index-profile-{args.tag}" if args.tag != "baseline" else "index-profile"

    plan: list[tuple[str, str | None, bool, str | None]] = [
        # (scenario, workspace state before run, fresh db, copy-from scenario)
        ("cold", None, True, None),
        ("nochange", None, False, "cold"),
        ("change", "probe42", False, "nochange"),
        ("change_body", "probe43", False, "change"),
    ]
    if not args.no_verify:
        plan.append(("verify_change", "probe42", True, None))
        plan.append(("verify_change_body", "probe43", True, None))

    for scenario, state, fresh, chain_from in plan:
        apply_state(state)
        db = db_for(scenario)
        if fresh:
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(db) + suffix)
                if candidate.exists():
                    candidate.unlink()
        elif chain_from is not None:
            copy_db(db_for(chain_from), db)
        db.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        code, stdout, stderr = child(
            [
                "--run",
                scenario,
                "--db",
                str(db),
                "--root",
                str(export_dir),
                "--source-commit",
                commit,
            ],
            env=child_env or None,
        )
        durations[scenario] = round(time.perf_counter() - started, 4)
        if code != 0:
            print(f"child {scenario} failed rc={code}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            write_checkpoint(
                checkpoints,
                f"{prefix}-{scenario}",
                {"scenario": scenario, "rc": code, "stderr_tail": stderr[-2000:]},
            )
            return code
        run = json.loads(stdout)
        runs.append(run)
        write_checkpoint(
            checkpoints,
            f"{prefix}-{scenario}",
            {
                "scenario": scenario,
                "tag": args.tag,
                "rc": 0,
                "db": str(db),
                "wall_index_s": run["wall_index_s"],
                "child_process_wall_s": durations[scenario],
                "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
        print(
            f"[{args.tag}] {scenario}: wall_index={run['wall_index_s']}s "
            f"child={durations[scenario]}s parsed={run['parsed']} "
            f"fast={run['fast_mode']} mode={run['association_mode']}",
            flush=True,
        )

    apply_state(None)  # leave the export pristine

    for run in runs:
        run["child_process_wall_s"] = durations[run["scenario"]]

    verification: dict[str, Any] = {"pairs": []}
    if not args.no_verify:
        pairs = [
            ("nochange", "cold"),
            ("change", "verify_change"),
            ("change_body", "verify_change_body"),
        ]
        for incremental, reference in pairs:
            result = compare_dumps(db_for(incremental), db_for(reference), export_dir, commit)
            result["pair"] = f"{incremental} vs {reference}"
            verification["pairs"].append(result)
            print(f"[{args.tag}] verify {result['pair']}: equal={result['equal']}", flush=True)
        verification["all_equal"] = all(pair["equal"] for pair in verification["pairs"])

    payload = {
        "phase": "phase33-t05",
        "tag": args.tag,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "export": export,
        "pinned_commit": commit,
        "probe_file": str(probe.relative_to(export_dir)).replace("\\", "/"),
        "probe_edit": PROBE.strip(),
        "parse_cache_dir": args.parse_cache_dir,
        "association_mode": runs[-1]["association_mode"],
        "runs": runs,
        "verification": verification,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"[{args.tag}] wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase33 T05 index profile driver")
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument(
        "--run",
        choices=[
            "cold",
            "nochange",
            "change",
            "change_body",
            "verify_change",
            "verify_change_body",
        ],
    )
    parser.add_argument("--db")
    parser.add_argument("--root")
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--fresh", action="store_true", help="child mode: delete the db first")
    parser.add_argument("--checkpoint", help="child mode: write a .done checkpoint here")
    parser.add_argument("--compare", nargs=2, metavar=("DB_A", "DB_B"))
    parser.add_argument("--tag", default="baseline")
    parser.add_argument("--scratch", default=str(DEFAULT_SCRATCH))
    parser.add_argument("--dbroot", default=str(DEFAULT_DBROOT))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--checkpoint-dir", default=str(DEFAULT_CHECKPOINTS))
    parser.add_argument("--pin-commit", default="")
    parser.add_argument("--reuse-export", action="store_true")
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument(
        "--parse-cache-dir",
        default="",
        help="set CODEPILOT_PARSE_CACHE_DIR for children (persistent parse cache)",
    )
    args = parser.parse_args()

    if args.compare:
        if not args.root:
            raise SystemExit("--compare needs --root (workspace the DBs indexed)")
        result = compare_dumps(
            Path(args.compare[0]), Path(args.compare[1]), Path(args.root), args.source_commit
        )
        print(json.dumps(result, indent=2))
        return 0 if result["equal"] else 1

    if args.run:
        if not args.db or not args.root:
            raise SystemExit("--run needs --db and --root")
        result = run_one(
            args.run, Path(args.db), Path(args.root), args.source_commit, fresh=args.fresh
        )
        print(json.dumps(result))
        if args.checkpoint:
            write_checkpoint(
                Path(args.checkpoint).parent,
                Path(args.checkpoint).name.removesuffix(".done"),
                {"scenario": args.run, "rc": 0, "wall_index_s": result["wall_index_s"]},
            )
        return 0

    return drive(args)


if __name__ == "__main__":
    raise SystemExit(main())
