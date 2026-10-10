"""Replay the preserved R-23 child without changing the project environment."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHILD = Path(__file__).with_name("minimal_child.py")


def run_child(case: str, target: Path | None) -> dict[str, object]:
    paths = [str(ROOT / "src")]
    if target is not None:
        paths.insert(0, str(target))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "PYTHONUTF8": "1"}
    child = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-u", str(CHILD), case],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    return {
        "case": case,
        "returncode": child.returncode,
        "exit_hex": f"0x{child.returncode & 0xFFFFFFFF:08X}",
        "stdout": child.stdout,
        "stderr": child.stderr,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compare", action="store_true", help="Install both bindings into temporary targets"
    )
    parser.add_argument("--case", choices=["row", "column", "extract", "lifetime"])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, help="Write a new result file; refuse to overwrite")
    parser.add_argument(
        "--uv", default=shutil.which("uv"), help="uv executable, needed for --compare"
    )
    parser.add_argument(
        "--temp-root", type=Path, help="Accessible parent for temporary binding targets"
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    if args.output is not None and args.output.exists():
        parser.error("--output already exists; use a fresh path")
    if args.compare and not args.uv:
        parser.error("--compare needs uv in PATH or --uv <path-to-uv>")
    if importlib.metadata.version("tree-sitter-python") != "0.25.0":
        parser.error("use uv sync --frozen --extra dev to restore grammar 0.25.0")

    cases = [args.case] if args.case else ["row", "column", "extract"]
    rows: list[dict[str, object]] = []
    versions = ["0.26.0", "0.25.2"] if args.compare else [importlib.metadata.version("tree-sitter")]
    if args.compare and args.temp_root is not None:
        args.temp_root.mkdir(parents=True, exist_ok=True)
    temporary = (
        tempfile.TemporaryDirectory(prefix="r23-binding-replay-", dir=args.temp_root)
        if args.compare
        else contextlib.nullcontext(None)
    )
    with temporary as tmp:
        for binding_version in versions:
            target = Path(tmp) / binding_version if tmp is not None else None
            if target is not None:
                subprocess.run(
                    [
                        args.uv,
                        "pip",
                        "install",
                        "--python",
                        sys.executable,
                        "--target",
                        str(target),
                        "--no-deps",
                        f"tree-sitter=={binding_version}",
                    ],
                    check=True,
                )
            for case in cases:
                for repeat in range(args.repeat):
                    rows.append(
                        {"version": binding_version, "repeat": repeat, **run_child(case, target)}
                    )
    report = {
        "python": sys.version,
        "platform": platform.platform(),
        "grammar": importlib.metadata.version("tree-sitter-python"),
        "results": rows,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
    print(encoded, end="")

    # A comparison intentionally records the old binding's failures.
    def expected(row: dict[str, object]) -> bool:
        if not args.compare or row["version"] != "0.26.0" or row["case"] == "lifetime":
            return row["returncode"] == 0
        stderr = str(row["stderr"])
        if row["case"] in {"row", "column"}:
            return row["returncode"] == 1 and f"AssertionError: ('{row['case']}'," in stderr
        return row["returncode"] in {3221225477, -1073741819, -11, -6} and (
            "Windows fatal exception: access violation" in stderr or "Fatal Python error:" in stderr
        )

    correct = all(expected(row) for row in rows)
    return 0 if correct else 1


if __name__ == "__main__":
    raise SystemExit(main())
