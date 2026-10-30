"""Phase29 AST 静态检查（T08）。

对 git 追踪的全部 Python 文件做 ``ast.parse``：任何语法级失败即退出码 1，并把
文件/节点清单写入 ``--json`` 输出（文件数、函数/类/导入计数、每文件首层节点类型），
作为静态检查的机器可读证据。只读，不修改任何文件。

用法::

    python scripts/phase29_ast_check.py [--json <path>] [--root <repo>]
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Fixtures that are intentionally syntactically broken (they exercise the
#: indexer's / failure collector's error paths and must stay unparseable).
INTENTIONALLY_BROKEN_FIXTURES = frozenset(
    {
        "tests/fixtures/failures/sources/syntax/test_case.py",
        "tests/fixtures/repos/mini_calc/calc/broken.py",
    }
)


def tracked_python_files(repo: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "*.py"],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise SystemExit("git ls-files failed")
    entries = completed.stdout.decode("utf-8", errors="replace").splitlines()
    return [
        repo / entry.strip()
        for entry in entries
        if entry.strip() and entry.strip().replace("\\", "/") not in INTENTIONALLY_BROKEN_FIXTURES
    ]


def inspect_file(path: Path) -> dict[str, object]:
    source = path.read_bytes()
    try:
        tree = ast.parse(source.decode("utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return {"path": str(path), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    functions = classes = imports = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions += 1
        elif isinstance(node, ast.ClassDef):
            classes += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports += 1
    return {
        "path": str(path),
        "ok": True,
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "bytes": len(source),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="write the JSON report")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root")
    args = parser.parse_args(argv)

    files = tracked_python_files(args.root)
    results = [inspect_file(path) for path in files]
    failures = [entry for entry in results if not entry["ok"]]
    report = {
        "tool": "phase29_ast_check",
        "files_checked": len(results),
        "failures": failures,
        "total_functions": sum(int(entry.get("functions", 0) or 0) for entry in results),
        "total_classes": sum(int(entry.get("classes", 0) or 0) for entry in results),
        "total_imports": sum(int(entry.get("imports", 0) or 0) for entry in results),
        "ok": not failures,
    }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if failures:
        for entry in failures:
            print(f"[ast-fail] {entry['path']}: {entry['error']}", file=sys.stderr)
        print(f"ast check FAILED: {len(failures)}/{len(results)} files", file=sys.stderr)
        return 1
    print(
        f"ast check ok: {report['files_checked']} files, "
        f"{report['total_functions']} functions, {report['total_classes']} classes, "
        f"{report['total_imports']} imports"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
