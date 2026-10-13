from __future__ import annotations

from pathlib import Path

from codepilot.patch.diff import generate_git_diff
from codepilot.patch.search_replace import MatchStatus, apply_search_replace
from codepilot.patch.unified_diff import apply_unified_diff
from codepilot.repo.gitcmd import run_git
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.edit_tools import create_file_tool, edit_file_tool


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    run_git(["init", "-q"], cwd=root)
    run_git(["config", "user.email", "tests@example.invalid"], cwd=root)
    run_git(["config", "user.name", "Tests"], cwd=root)
    (root / "code.py").write_text("def greet(name):\n    return f'hi {name}'\n", encoding="utf-8")
    run_git(["add", "."], cwd=root)
    run_git(["commit", "-qm", "base"], cwd=root)
    return root, run_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()


def test_search_replace_exact_unicode_and_multi_file(tmp_path: Path) -> None:
    root, _ = _git_repo(tmp_path)
    (root / "second.py").write_text("message = '旧值'\n", encoding="utf-8")
    policy = WorkspacePathPolicy(root)
    create = create_file_tool(policy)
    assert create.execute({"path": "new.py", "content": "value = '新值'\n"}).ok
    edit = edit_file_tool(policy)
    assert edit.execute(
        {"path": "code.py", "search": "return f'hi {name}'", "replace": "return f'hello {name}'"}
    ).ok
    assert edit.execute({"path": "second.py", "search": "旧值", "replace": "新值"}).ok
    assert "hello" in (root / "code.py").read_text(encoding="utf-8")
    assert "新值" in (root / "second.py").read_text(encoding="utf-8")


def test_ambiguous_and_missing_search_leave_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_text("same\nsame\n", encoding="utf-8")
    before = path.read_bytes()
    ambiguous = apply_search_replace(path, "same", "changed")
    assert ambiguous.status is MatchStatus.AMBIGUOUS
    assert [item.start_line for item in ambiguous.matches] == [1, 2]
    assert path.read_bytes() == before
    missing = apply_search_replace(path, "somewhat similar", "changed")
    assert missing.status is MatchStatus.NOT_FOUND
    assert 1 <= len(missing.suggestions) <= 3
    assert path.read_bytes() == before


def test_trailing_whitespace_fallback_is_unique_and_preserves_newlines(tmp_path: Path) -> None:
    path = tmp_path / "x.py"
    path.write_bytes(b"a = 1   \r\nb = 2\r\n")
    result = apply_search_replace(path, "a = 1\nb = 2", "a = 3\r\nb = 4\r\n")
    assert result.status is MatchStatus.APPLIED_TRAILING_WHITESPACE
    assert path.read_bytes() == b"a = 3\r\nb = 4\r\n"


def test_canonical_diff_contains_edits_and_new_files(tmp_path: Path) -> None:
    root, base = _git_repo(tmp_path)
    (root / "code.py").write_text("def greet(name):\n    return name.upper()\n", encoding="utf-8")
    (root / "added.py").write_text("VALUE = 3\n", encoding="utf-8")
    diff = generate_git_diff(root, base)
    assert diff.base_commit == base
    assert diff.files == ["added.py", "code.py"]
    assert "diff --git a/added.py b/added.py" in diff.text
    assert "diff --git a/code.py b/code.py" in diff.text
    assert diff.lines_added > 0 and diff.lines_deleted > 0
    assert generate_git_diff(root, base).text == diff.text


def test_unified_diff_checks_before_apply_and_rejects_escape(tmp_path: Path) -> None:
    root, _ = _git_repo(tmp_path)
    policy = WorkspacePathPolicy(root)
    patch = """diff --git a/code.py b/code.py
--- a/code.py
+++ b/code.py
@@ -1,2 +1,2 @@
 def greet(name):
-    return f'hi {name}'
+    return f'hello {name}'
"""
    result = apply_unified_diff(patch, policy)
    assert result.applied
    assert "hello" in (root / "code.py").read_text(encoding="utf-8")
    before = (root / "code.py").read_bytes()
    broken = patch.replace("hi", "missing")
    assert not apply_unified_diff(broken, policy).applied
    assert (root / "code.py").read_bytes() == before
    escape = patch.replace("a/code.py", "a/../outside.py").replace("b/code.py", "b/../outside.py")
    assert not apply_unified_diff(escape, policy).applied
