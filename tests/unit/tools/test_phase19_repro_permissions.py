"""Phase 19: only a trusted Reproducer policy may write reproduction evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.tools.base import PathAccessError, WorkspacePathPolicy


def _repro_file(root: Path) -> str:
    path = root / "tests" / "_codepilot_repro" / "test_issue.py"
    path.parent.mkdir(parents=True)
    path.write_text("def test_issue():\n    assert False\n", encoding="utf-8")
    return path.relative_to(root).as_posix()


def test_coder_policy_cannot_edit_confirmed_reproduction_test(tmp_path: Path) -> None:
    relative = _repro_file(tmp_path)
    coder = WorkspacePathPolicy(
        tmp_path, repro_write=False, allow_new_tests=False, deny_existing_test_edits=True
    )
    with pytest.raises(PathAccessError, match="trusted reproduction context"):
        coder.resolve(relative, must_exist=True, for_write=True)


def test_reproducer_policy_can_create_and_correct_its_own_test(tmp_path: Path) -> None:
    relative = _repro_file(tmp_path)
    reproducer = WorkspacePathPolicy(tmp_path, repro_write=True)
    assert reproducer.resolve(relative, must_exist=True, for_write=True).is_file()
    new_relative = "tests/_codepilot_repro/test_issue_retry.py"
    assert not reproducer.resolve(
        new_relative, must_exist=False, for_write=True
    ).exists()


def test_coder_policy_can_still_read_reproduction_evidence(tmp_path: Path) -> None:
    relative = _repro_file(tmp_path)
    coder = WorkspacePathPolicy(tmp_path, repro_write=False)
    assert coder.resolve(relative, must_exist=True).is_file()
