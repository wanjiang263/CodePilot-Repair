"""Phase28 候选归档 CLI（契约 A，schema v2）。

把指定源码提交固化成 v2 候选目录：snapshot.json / candidate.json / sources.json /
candidate.diff。默认拒绝脏 tracked 工作树（``--allow-dirty`` 显式放行并记录），
拒绝覆盖已存在版本目录，生成后验证 diff 可反向应用，绝不创建 active 指针。

正常用法（最终候选由主控在源码提交之后执行，T02 只保证命令本身可用）::

    python scripts/phase28_archive.py \
        --version phase28-candidate-v2 \
        --parent phase27-candidate-v1 \
        --diff-base <旧源码提交> \
        --source-commit <本轮源码提交> \
        --source-list sources.json \
        [--archive-root <目录>] [--allow-dirty] [--dry-run]

``--source-list`` 接受一个 JSON 文件（字符串数组，相对路径先按 CWD、再按仓库根解析）
或逗号分隔的仓库相对路径。``--dry-run`` 完整执行校验与哈希计算（含 diff 反向应用
检查，``--check`` 不写工作区）但不落盘。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from codepilot.archive import ArchiveError, create_candidate
from codepilot.rsi import archive_root

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_source_list(raw: str) -> list[str]:
    """``--source-list``：JSON 文件路径（字符串数组）或逗号分隔的仓库相对路径。"""
    value = raw.strip()
    as_path = Path(value)
    json_path = as_path if as_path.is_file() else REPO_ROOT / as_path
    if json_path.is_file():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise ValueError(f"source list JSON must be an array of strings: {value}")
        entries = list(payload)
    else:
        entries = value.split(",")
    paths = [item.strip().replace("\\", "/") for item in entries]
    paths = [item for item in paths if item]
    if not paths:
        raise ValueError("source list is empty")
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="candidate version identifier")
    parser.add_argument("--parent", required=True, help='parent version ("" for a root candidate)')
    parser.add_argument("--diff-base", required=True, help="authoritative diff base commit")
    parser.add_argument("--source-commit", required=True, help="source commit being archived")
    parser.add_argument(
        "--source-list",
        required=True,
        help="JSON file (array of repo-relative paths) or comma-separated paths",
    )
    parser.add_argument("--archive-root", type=Path, default=None, help="archive root directory")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="record a dirty tracked worktree instead of refusing",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and compute hashes without writing files"
    )
    args = parser.parse_args(argv)
    root = args.archive_root if args.archive_root is not None else archive_root()
    try:
        summary = create_candidate(
            root,
            REPO_ROOT,
            version=args.version,
            parent=args.parent or None,
            diff_base_commit=args.diff_base,
            source_commit=args.source_commit,
            source_files=parse_source_list(args.source_list),
            allow_dirty=args.allow_dirty,
            dry_run=args.dry_run,
        )
    except ArchiveError as exc:
        print(
            json.dumps({"error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
