"""Check public imports in fresh interpreters, before pytest warms the modules."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "first_module",
    ["codepilot.errors", "codepilot.eval.runner", "codepilot.llm.structured", "codepilot.llm"],
)
def test_public_imports_do_not_depend_on_order(first_module: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {first_module}; "
            "from codepilot.llm import complete_structured; "
            "from codepilot.llm.structured import complete_structured as implementation; "
            "from codepilot.errors import AgentFailureError; "
            "assert complete_structured is implementation",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
