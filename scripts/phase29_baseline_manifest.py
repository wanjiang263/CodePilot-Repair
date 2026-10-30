"""Phase29 历史候选基线 manifest 生成器（T05）。

为 ``<archive_root>/candidates/`` 下当时的全部候选文件冻结逐文件 SHA256，形成
``historical-baseline.json``；校验器（``scripts/phase28_archive_verify.py`` 的
``verify_historical_baseline``）据此按当前字节比对历史候选，能发现"修改后提交"
式篡改（旧的 ``git log -1`` 启发式对此失效）。

对每个文件还记录它在该 ``--baseline-commit``（默认 9ccc703，Phase27 文档提交）
中的 blob SHA256：存在即 ``baseline_commit_sha256``，否则 null（基线后新增的
文件，如 phase28-candidate-v2）。若任何"基线时已存在"的文件当前字节与基线 blob
不一致，本脚本默认拒绝写出 manifest（不祝福一个已被改动的状态），退出码 1；
``--allow-drift`` 可显式放行并在 manifest 中记录 ``zero_change_since_baseline=false``。

manifest 本身不出现在 ``files`` 清单中（自引用不可哈希）；生成后随文档一起提交，
之后即成为校验器的历史基线证据。

用法::

    python scripts/phase29_baseline_manifest.py \\
        [--archive-root <dir>] [--repo <dir>] [--baseline-commit <sha>] \\
        [--output <path>] [--allow-drift]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE_ROOT = REPO_ROOT / "configs" / "harness"
DEFAULT_BASELINE_COMMIT = "9ccc703"
MANIFEST_NAME = "historical-baseline.json"

_GIT_TIMEOUT_SECONDS = 120.0
_HOSTILE_GIT_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE"}
)


def _git_bytes(repo: Path, args: Sequence[str]) -> bytes | None:
    command = ["git", "-C", str(repo)]
    command += ["-c", "core.autocrlf=false", "-c", "core.longpaths=true"]
    command += list(args)
    env = {name: value for name, value in os.environ.items() if name not in _HOSTILE_GIT_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            command, capture_output=True, check=False, env=env, timeout=_GIT_TIMEOUT_SECONDS
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _resolve_commit(repo: Path, revision: str) -> str | None:
    output = _git_bytes(repo, ["rev-parse", "--verify", f"{revision}^{{commit}}"])
    if output is None:
        return None
    return output.decode("ascii", errors="replace").strip() or None


def _blob_at_commit(repo: Path, commit: str, posix_path: str) -> bytes | None:
    return _git_bytes(repo, ["cat-file", "blob", f"{commit}:{posix_path}"])


def build_manifest(
    archive_root: Path, repo: Path, *, baseline_commit: str, output_path: Path | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Collect the freeze manifest; return it plus a drift report (one line per
    file whose current bytes differ from its baseline blob). The manifest itself
    is excluded from the file list."""
    candidates_root = archive_root / "candidates"
    if not candidates_root.is_dir():
        raise SystemExit(f"no candidates directory under archive root: {archive_root}")
    resolved_baseline = _resolve_commit(repo, baseline_commit)
    if resolved_baseline is None:
        raise SystemExit(f"baseline commit does not resolve: {baseline_commit!r}")
    excluded = {MANIFEST_NAME}
    if output_path is not None:
        with contextlib.suppress(ValueError):
            excluded.add(output_path.resolve().relative_to(archive_root.resolve()).as_posix())
    # Repo-relative prefix for git blob lookups; skipped when the archive root
    # lies outside the repository (tests with throwaway roots).
    try:
        archive_prefix = archive_root.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        archive_prefix = ""

    files: list[dict[str, Any]] = []
    drift: list[str] = []
    for path in sorted(candidates_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.resolve().relative_to(archive_root.resolve()).as_posix()
        if rel in excluded:
            continue
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        baseline_blob = None
        if archive_prefix:
            baseline_blob = _blob_at_commit(repo, resolved_baseline, f"{archive_prefix}/{rel}")
        baseline_sha = hashlib.sha256(baseline_blob).hexdigest() if baseline_blob else None
        if baseline_sha is not None and baseline_sha != current:
            drift.append(f"{rel}: current={current[:12]} baseline={baseline_sha[:12]}")
        files.append(
            {
                "path": rel,
                "sha256": current,
                "bytes": path.stat().st_size,
                "baseline_commit_sha256": baseline_sha,
            }
        )

    manifest: dict[str, Any] = {
        "tool": "phase29_baseline_manifest",
        "baseline_commit": resolved_baseline,
        "manifest_head": _resolve_commit(repo, "HEAD"),
        "generated_at_epoch": int(time.time()),
        "archive_root": str(archive_root.resolve()),
        "files": files,
        "zero_change_since_baseline": not drift,
        "note": (
            "Per-file SHA256 of every candidate file at freeze time; the verifier "
            "compares current bytes against this manifest (catches committed "
            "tampering that a git log -1 heuristic cannot see)."
        ),
    }
    return manifest, drift


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-root", type=Path, default=DEFAULT_ARCHIVE_ROOT, help="archive root directory"
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="source repository")
    parser.add_argument(
        "--baseline-commit",
        default=DEFAULT_BASELINE_COMMIT,
        help=f"commit the zero-change claim is anchored to (default: {DEFAULT_BASELINE_COMMIT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"output path (default: <archive-root>/candidates/{MANIFEST_NAME})",
    )
    parser.add_argument(
        "--allow-drift",
        action="store_true",
        help="write the manifest even when baseline blobs differ from current bytes",
    )
    args = parser.parse_args(argv)
    output = (
        args.output if args.output is not None else args.archive_root / "candidates" / MANIFEST_NAME
    )
    try:
        manifest, drift = build_manifest(
            args.archive_root, args.repo, baseline_commit=args.baseline_commit, output_path=output
        )
    except SystemExit as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if drift and not args.allow_drift:
        print("baseline drift detected; refusing to write the manifest:", file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print("re-run with --allow-drift to record it explicitly", file=sys.stderr)
        return 1
    manifest["baseline_drift"] = drift
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "files": len(manifest["files"]),
                "baseline_commit": manifest["baseline_commit"],
                "zero_change_since_baseline": manifest["zero_change_since_baseline"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
