from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.patch.cheat_detector import detect_ast_cheats
from codepilot.patch.validator import PatchValidator, ValidatorConfig
from codepilot.repo.gitcmd import run_git
from codepilot.schemas.validation import CheatPattern
from codepilot.tools.base import WorkspacePathPolicy


def _repo(tmp_path: Path, *, source: str, test: str) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "src/pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/pkg/core.py").write_text(source, encoding="utf-8")
    (root / "tests/test_core.py").write_text(test, encoding="utf-8")
    run_git(["init", "-q"], cwd=root)
    run_git(["config", "user.email", "tests@example.invalid"], cwd=root)
    run_git(["config", "user.name", "Tests"], cwd=root)
    run_git(["add", "."], cwd=root)
    run_git(["commit", "-qm", "base"], cwd=root)
    return root, run_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()


@pytest.mark.parametrize(
    ("after", "pattern"),
    [
        ("", CheatPattern.DELETED_TEST),
        (
            "import pytest\n@pytest.mark.skip\ndef test_value():\n    assert work() == 2\n",
            CheatPattern.SKIPPED_TEST,
        ),
        (
            "import pytest\n@pytest.mark.xfail\ndef test_value():\n    assert work() == 2\n",
            CheatPattern.SKIPPED_TEST,
        ),
        (
            "import pytest\ndef test_value():\n    pytest.skip('later')\n    assert work() == 2\n",
            CheatPattern.SKIPPED_TEST,
        ),
        ("def test_value():\n    work()\n", CheatPattern.WEAKENED_ASSERTION),
        ("def test_value():\n    assert True\n", CheatPattern.WEAKENED_ASSERTION),
    ],
)
def test_ast_detector_rejects_each_test_weakening(after: str, pattern: CheatPattern) -> None:
    before = "def test_value():\n    assert work() == 2\n"
    findings = detect_ast_cheats(before, after, "tests/test_core.py")
    assert pattern in {finding.pattern for finding in findings if finding.reject}
    assert all(finding.rule_id and finding.line for finding in findings)


@pytest.mark.parametrize(
    "after",
    [
        "def work():\n    try:\n        risky()\n    except Exception:\n        pass\n",
        "import os\ndef work():\n    return os.getenv('PYTEST_CURRENT_TEST')\n",
        "import sys\ndef work():\n    return sys.modules['pytest']\n",
    ],
)
def test_validator_rejects_new_source_cheat_patterns(tmp_path: Path, after: str) -> None:
    root, base = _repo(
        tmp_path,
        source="def work():\n    return 2\n",
        test="from pkg.core import work\ndef test_value():\n    assert work() == 2\n",
    )
    (root / "src/pkg/core.py").write_text(after, encoding="utf-8")
    result = PatchValidator(WorkspacePathPolicy(root), base).validate(
        related_tests=["tests/test_core.py"]
    )
    assert not result.passed
    assert result.rejecting_findings


def test_hardcoded_expected_value_is_warning_not_rejection(tmp_path: Path) -> None:
    root, base = _repo(
        tmp_path,
        source="def work():\n    return compute()\n",
        test="from pkg.core import work\ndef test_value():\n    assert work() == 42\n",
    )
    (root / "src/pkg/core.py").write_text("def work():\n    return 42\n", encoding="utf-8")
    result = PatchValidator(WorkspacePathPolicy(root), base).validate(
        related_tests=["tests/test_core.py"]
    )
    assert result.passed
    assert result.warnings
    assert any(not finding.reject for finding in result.cheat_findings)


def test_existing_pattern_and_normal_exception_handling_are_negative_controls() -> None:
    before = "def work():\n    try:\n        risky()\n    except Exception:\n        pass\n"
    after = (
        before + "\ndef safe():\n    try:\n        risky()\n    except ValueError:\n        pass\n"
    )
    assert detect_ast_cheats(before, after, "src/core.py") == []


def test_validator_protects_tests_config_syntax_size_and_empty_patch(tmp_path: Path) -> None:
    root, base = _repo(
        tmp_path,
        source="def work():\n    return 2\n",
        test="from pkg.core import work\ndef test_value():\n    assert work() == 2\n",
    )
    validator = PatchValidator(WorkspacePathPolicy(root), base)
    empty = validator.validate()
    assert not empty.passed and "patch is empty" in empty.errors

    (root / "tests/test_core.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    protected = validator.validate()
    assert any("existing test file" in error for error in protected.errors)

    run_git(["checkout", "--", "."], cwd=root)
    (root / "src/pkg/core.py").write_text("def broken(:\n", encoding="utf-8")
    syntax = validator.validate()
    assert any("invalid syntax" in error for error in syntax.errors)

    run_git(["checkout", "--", "."], cwd=root)
    (root / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
    config = validator.validate()
    assert any("protected path" in error for error in config.errors)

    run_git(["checkout", "--", "."], cwd=root)
    (root / "src/pkg/core.py").write_text(
        "\n".join(f"x{i} = {i}" for i in range(20)), encoding="utf-8"
    )
    too_large = PatchValidator(
        WorkspacePathPolicy(root), base, config=ValidatorConfig(max_lines_changed=2)
    ).validate()
    assert any("changed lines" in error for error in too_large.errors)
