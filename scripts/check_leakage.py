"""E-07：合成 suite 的答案泄漏检查。

    uv run python scripts/check_leakage.py                 # 全量审计
    uv run python scripts/check_leakage.py --with-workspace  # 顺便建真实 workspace 再查

判据实现放在 :mod:`codepilot.eval.leakage`（受 mypy strict 管辖、可被单测直接调用），
这里只负责编排、退出码与人工复核线索的输出。

**这个脚本的结论有边界**，输出里会把它打出来：它能判定结构性泄漏（逐字代码行、
评测目录可达、推理视图携带 gold 内容），判不了改写过的语义泄漏。后者的正确处置是
交给人读，而不是把自动扫描说成"已确认无泄漏"。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codepilot.eval.leakage import LeakageSeverity, audit_suite  # noqa: E402
from codepilot.eval.synthetic import validate_suite  # noqa: E402

DEFAULT_SUITE = REPO_ROOT / "benchmarks" / "synthetic"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check synthetic fixtures for answer leakage.")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument(
        "--with-workspace",
        action="store_true",
        help="额外材料化推理 workspace，检查 eval/ 可达性与 symlink 逃逸。",
    )
    parser.add_argument("--json", type=Path, default=None, help="把结果写成 JSON。")
    parser.add_argument("--strict-warnings", action="store_true", help="把 WARNING 也当失败。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        suite = validate_suite(args.suite)
    except Exception as exc:
        print(f"suite 无法加载：{exc}", file=sys.stderr)
        return 2
    for task_id, reasons in sorted(suite.invalid.items()):
        print(f"[INVALID_FIXTURE] {task_id}: {reasons[0]}")
    if not suite.fixtures:
        print("没有可审计的 fixture。", file=sys.stderr)
        return 1

    work_root = Path(tempfile.mkdtemp(prefix="leakage-")) if args.with_workspace else None
    try:
        report = audit_suite(suite.fixtures, materialize=args.with_workspace, work_root=work_root)
    finally:
        if work_root is not None:
            import shutil

            shutil.rmtree(work_root, ignore_errors=True)

    payload = report.as_dict()
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    errors = [item for item in report.findings if item.severity is LeakageSeverity.ERROR]
    warnings = [item for item in report.findings if item.severity is LeakageSeverity.WARNING]
    print(
        f"审计 {len(report.checked_tasks)} 个 fixture：ERROR {len(errors)}，WARNING {len(warnings)}"
    )
    for item in errors:
        print(f"  ERROR   {item.task_id} [{item.check}] {item.detail}")
    if warnings:
        print("\n需要人工复核（自动扫描不能判定语义泄漏）：")
        for item in warnings:
            print(f"  WARNING {item.task_id} [{item.check}] {item.detail}")
    print(
        "\n边界：ERROR=0 只说明**结构性**泄漏检查没有命中。改写过的、语义层面的答案"
        "泄漏需要人工读 Issue，见 docs/notes/phase10.md 的 HUMAN_REVIEW_PENDING。"
    )
    if errors:
        return 1
    if warnings and args.strict_warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
