#!/usr/bin/env python3
"""Read-only comparison plus rule-based sync for Phase 18 l2cal10e closure.

Source: /root/cp-run/phase16/CodePilot-Repair (WSL)
Target: /mnt/c/Users/lenovo/Desktop/CodePilot-Repair
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

SRC = Path("/root/cp-run/phase16/CodePilot-Repair")
TGT = Path("/mnt/c/Users/lenovo/Desktop/CodePilot-Repair")
REPORTS = TGT / "artifacts/sync-reports"
BACKUPS = TGT / "artifacts/sync-backups"

CORE_DIRS = ["src", "tests", "scripts", "configs", "docs", "benchmarks", "demo", "web", "sandbox", ".github", "logs"]
CORE_ROOT_FILES = [
    "README.md", "pyproject.toml", "uv.lock", "Dockerfile", "docker-compose.yml",
    ".dockerignore", ".gitignore", ".python-version", ".env.example",
    "glm-5.3-flash-main.phase17-official-numpy1264-20261022.json",
]
EXCLUDE_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
    "dist", ".vite", "coverage", ".git",
}
ARTIFACT_ROOT = "artifacts/phase18/l2-cal10e"
ARTIFACT_EXCLUDE_PARTS = {
    "workspace", "repo-cache", "env-cache", "retrieval_snapshots", "__pycache__",
    "node_modules", "docker", "models",
}
ARTIFACT_EXCLUDE_SUFFIX = (".sqlite", ".sqlite3", ".sqlite-shm", ".sqlite-wal", ".pyc")
A10_A21_PREFIXES = tuple(f"-a{i}" for i in range(10, 22))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_core_file(rel: str) -> bool:
    parts = Path(rel).parts
    if any(p in EXCLUDE_PARTS for p in parts):
        return False
    if rel.endswith(".pyc"):
        return False
    if parts[0] in CORE_DIRS:
        return True
    return rel in CORE_ROOT_FILES


def is_artifact_file(rel: str) -> bool:
    p = Path(rel)
    parts = p.parts
    if any(part in ARTIFACT_EXCLUDE_PARTS for part in parts):
        return False
    if any(part.startswith("env-probe-") for part in parts):
        return False
    name = p.name
    if name.endswith(ARTIFACT_EXCLUDE_SUFFIX):
        return False
    if name == "state_final.json":
        return False
    return True


def iter_core() -> list[str]:
    out = []
    for d in CORE_DIRS:
        base = SRC / d
        if not base.exists():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in EXCLUDE_PARTS]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                out.append(str((Path(root) / f).relative_to(SRC)).replace(os.sep, "/"))
    for f in CORE_ROOT_FILES:
        if (SRC / f).is_file():
            out.append(f)
    return sorted(set(out))


def iter_artifacts() -> list[str]:
    base = SRC / ARTIFACT_ROOT
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if not (x in ARTIFACT_EXCLUDE_PARTS or x.startswith("env-probe-"))]
        for f in files:
            rel = str((Path(root) / f).relative_to(SRC)).replace(os.sep, "/")
            if is_artifact_file(rel):
                out.append(rel)
    return sorted(set(out))


def classify(rel: str) -> tuple[str, str | None, str | None]:
    s = SRC / rel
    t = TGT / rel
    sh = sha256(s) if s.is_file() else None
    th = sha256(t) if t.is_file() else None
    if th is None:
        return "source_only", sh, th
    if sh == th:
        return "identical", sh, th
    return "differing", sh, th


def is_a10_a21_evidence(rel: str) -> bool:
    if rel == f"{ARTIFACT_ROOT}/attempts.json":
        return True
    if not rel.startswith(ARTIFACT_ROOT + "/"):
        return False
    rel_in = rel[len(ARTIFACT_ROOT) + 1:]
    parts = rel_in.split("/")
    if len(parts) >= 2 and parts[0].endswith(A10_A21_PREFIXES):
        return True
    if len(parts) == 1:
        name = parts[0]
        if name.startswith(("batch-status-l2cal10e", "batch-console-l2cal10e", "recovery-manifest-l2cal10e")):
            if any(name.startswith(p) for p in ("batch-status-l2cal10e", "batch-console-l2cal10e", "recovery-manifest-l2cal10e")):
                return any(f"-a{i}" in name for i in range(10, 22))
    if parts[0] == "preflight" and len(parts) == 2:
        return any(f"-a{i}.json" in parts[1] for i in range(10, 22))
    return False


def main() -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORTS.mkdir(parents=True, exist_ok=True)
    BACKUPS.mkdir(parents=True, exist_ok=True)
    backup_root = BACKUPS / ts

    core = iter_core()
    arts = iter_artifacts()
    results = {"core": {}, "artifacts": {}}
    conflicts = []
    actions = []
    synced = []

    for group, rels in (("core", core), ("artifacts", arts)):
        for rel in rels:
            kind, sh, th = classify(rel)
            results[group][rel] = {"state": kind, "source_sha256": sh, "target_sha256": th}
            if kind == "identical":
                continue
            if kind == "source_only":
                dst = TGT / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(SRC / rel, dst)
                actions.append({"action": "copied_new", "group": group, "path": rel, "sha256": sh})
                synced.append(rel)
                continue
            # differing
            if group == "artifacts" and is_a10_a21_evidence(rel):
                bk = backup_root / rel
                bk.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(TGT / rel, bk)
                shutil.copy2(SRC / rel, TGT / rel)
                actions.append({
                    "action": "backed_up_and_overwritten", "group": group, "path": rel,
                    "backup": str(bk.relative_to(TGT)).replace(os.sep, "/"),
                    "old_sha256": th, "new_sha256": sh,
                })
                synced.append(rel)
            else:
                conflicts.append({
                    "group": group, "path": rel, "source_sha256": sh, "target_sha256": th,
                    "reason": "source/config conflict requires manual decision"
                    if group == "core" else "artifact outside a10-a21 evidence scope",
                })

    (REPORTS / "sync-comparison.json").write_text(
        json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "timestamp": ts,
            "source": str(SRC),
            "target": str(TGT),
            "core_total": len(core),
            "artifact_total": len(arts),
            "results": results,
            "conflicts": conflicts,
            "actions": actions,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (REPORTS / "sync-conflicts.txt").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# sync conflicts ({len(conflicts)})\n")
        for c in conflicts:
            fh.write(f"{c['group']}\t{c['path']}\nsource\t{c['source_sha256']}\ntarget\t{c['target_sha256']}\n\n")

    with (REPORTS / "sync-actions.txt").open("w", encoding="utf-8", newline="\n") as fh:
        for a in actions:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")

    with (REPORTS / "synced-files.txt").open("w", encoding="utf-8", newline="\n") as fh:
        for rel in sorted(set(synced)):
            fh.write(rel + "\n")

    summary = {
        "timestamp": ts,
        "backup_root": str(backup_root.relative_to(TGT)).replace(os.sep, "/"),
        "core_total": len(core),
        "artifact_total": len(arts),
        "copied_new": sum(1 for a in actions if a["action"] == "copied_new"),
        "backed_up_overwritten": sum(1 for a in actions if a["action"] == "backed_up_and_overwritten"),
        "conflicts": len(conflicts),
        "identical_core": sum(1 for v in results["core"].values() if v["state"] == "identical"),
        "identical_artifacts": sum(1 for v in results["artifacts"].values() if v["state"] == "identical"),
    }
    (REPORTS / "sync-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
