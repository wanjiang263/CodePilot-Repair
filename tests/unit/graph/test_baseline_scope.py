"""Baseline scope: impact-related, existing test files only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codepilot.graph.nodes import _baseline_scope
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.tools.base import WorkspacePathPolicy


def test_scope_keeps_existing_test_files_and_drops_repository_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests/__init__.py").write_text("", encoding="utf-8")
    impact = ImpactAnalysis(
        related_tests=["tests/test_ok.py", "tests/__init__.py", "tests/missing.py"]
    )

    scope = _baseline_scope(SimpleNamespace(impact=impact), WorkspacePathPolicy(tmp_path))

    assert scope == ("tests/test_ok.py",)
