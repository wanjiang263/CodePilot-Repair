"""Native binding regressions must fail in a child, never take pytest down."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

CHILD = r"""
import gc
import sys
from tree_sitter import Language, Parser
import tree_sitter_python
from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.symbols import extract_symbols

case = sys.argv[1]
if case in {"row", "column"}:
    # Non-cached integers expose 0.26.0 Point getters returning borrowed refs.
    source = b"\n" * 257 + b" " * 257 + b"pass\n"
    tree = Parser(Language(tree_sitter_python.language())).parse(source)
    point = tree.root_node.children[0].start_point
    index = 0 if case == "row" else 1
    coordinate = point[index]
    assert coordinate == 257
    before = sys.getrefcount(coordinate)
    value = getattr(point, case)
    after = sys.getrefcount(coordinate)
    assert after == before + 1, (case, before, after)
    del value
    assert point[index] == 257
elif case == "extract":
    source = b"\n" * 257 + b"def f():\n    return 1\n"
    for _ in range(100):
        parsed = TreeSitterParser().parse(source)
        symbols = extract_symbols(parsed, path="sample.py", repository_id="regression")
        assert len(symbols) == 2
        assert symbols[1].name == "f"
        assert (symbols[1].start_line, symbols[1].end_line) == (258, 259)
    # Also exercise allocation churn at the scale of a real module.
    source += b"\n".join(f"def f{i}():\n    return {i}\n".encode() for i in range(400))
    symbols = extract_symbols(TreeSitterParser().parse(source), path="sample.py", repository_id="r")
    assert len(symbols) == 402
elif case == "lifetime":
    source = b"\n" * 257 + b"def f():\n    return 1\n"
    parser = TreeSitterParser()
    parsed = parser.parse(source)
    function = parsed.root.children[0]
    name = function.field("name")
    body = function.field("body")
    del parsed, parser, source
    gc.collect()
    assert name.text == "f"
    assert body.children[0].text == "return 1"
    assert function.field("name").text == "f"
    assert function.start_line == 258
    # Native children retain their tree too, without our ParsedFile wrapper.
    tree = Parser(Language(tree_sitter_python.language())).parse(b"def f(): pass\n")
    child = tree.root_node.children[0]
    del tree
    gc.collect()
    assert child.child_by_field_name("name").text == b"f"
    assert child.children[-1].text == b"pass"
print("OK", case, flush=True)
"""


@pytest.mark.parametrize("case", ["row", "column", "extract", "lifetime"])
def test_native_parser_in_subprocess(case: str) -> None:
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-u", "-c", CHILD, case],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
        check=False,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode} (0x{result.returncode & 0xFFFFFFFF:08X})\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert f"OK {case}" in result.stdout
