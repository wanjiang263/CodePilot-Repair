"""D-05 / D-06 / D-07：复现目录的权限例外与最终 Patch 的隔离。

这三条合在一个文件里测，因为它们防的是同一个失败模式：**复现测试一边负责"证明
修好了"，一边混进交付补丁**。所以断言分成两层——写不写得到（权限），以及最终
diff 与统计里有没有它（隔离）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codepilot.agents.reproducer import write_reproduction_test
from codepilot.patch.diff import generate_git_diff, is_excluded_path
from codepilot.patch.unified_diff import apply_unified_diff
from codepilot.patch.validator import PatchValidator, ValidatorConfig
from codepilot.repo.gitcmd import run_git
from codepilot.schemas.reproduction import REPRO_TEST_DIRECTORY
from codepilot.schemas.validation import CheckName
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.edit_tools import create_file_tool, edit_file_tool
from support.gitfixture import init_repo

REPRO = f"{REPRO_TEST_DIRECTORY}/test_repro_boundary.py"
REPRO_TWO = f"{REPRO_TEST_DIRECTORY}/test_repro_second.py"


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    """返回 (原始仓库, workspace, base commit)。原始仓库只读，绝不被写。"""
    original = init_repo(
        tmp_path / "original",
        {
            "src/pkg/core.py": "def normalize(value: int) -> int:\n    return value - 1\n",
            "tests/test_core.py": "def test_normalize() -> None:\n    assert normalize(3) == 3\n",
            ".gitignore": "build/\n",
        },
    )
    workspace = tmp_path / "workspace"
    shutil.copytree(original.path, workspace)
    base = run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    return original.path, workspace, base


def _write(workspace: Path, relative: str, text: str) -> None:
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ D-05 权限


def test_reproduction_directory_is_closed_to_an_ungranted_context(tmp_path: Path) -> None:
    _, workspace, _ = _fixture(tmp_path)
    tool = create_file_tool(WorkspacePathPolicy(workspace))
    result = tool.execute({"path": REPRO, "content": "def test_x() -> None:\n    assert 1 == 2\n"})
    assert result.ok is False
    assert result.error is not None and result.error.code == "forbidden_path"
    assert not (workspace / REPRO).exists()


def test_only_the_trusted_reproduction_context_can_write_the_directory(
    tmp_path: Path,
) -> None:
    _, workspace, _ = _fixture(tmp_path)
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    relative = write_reproduction_test(
        granted, "test_repro_boundary.py", "def test_x() -> None:\n    assert 1 == 2\n"
    )
    assert relative == REPRO
    assert (workspace / REPRO).is_file()
    # 同一个目录内的第二次写入（重试替换候选）依然合法。
    resolved = granted.resolve(REPRO, must_exist=True, for_write=True)
    resolved.write_text("def test_x() -> None:\n    assert 1 == 3\n", encoding="utf-8")
    assert "1 == 3" in (workspace / REPRO).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    [
        f"{REPRO_TEST_DIRECTORY.upper()}/test_repro.py",
        "TESTS/_codepilot_repro/test_repro.py",
        "src/_codepilot_repro/test_repro.py",
        "tests/_codepilot_repro/nested/test_repro.py",
        "tests/_codepilot_repro/conftest.py",
        "tests/_codepilot_repro/__init__.py",
        "tests/_codepilot_repro/helper.py",
        ".git/tests/_codepilot_repro/test_repro.py",
        "tests/_codepilot_repro/../../../outside.py",
        "/tests/_codepilot_repro/test_repro.py",
        "\\\\server\\share\\_codepilot_repro\\test_repro.py",
    ],
)
def test_forged_directory_names_and_escapes_are_rejected_even_with_a_grant(
    tmp_path: Path, path: str
) -> None:
    _, workspace, _ = _fixture(tmp_path)
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    with pytest.raises(ValueError):
        granted.resolve(path, must_exist=False, for_write=True)


def test_symlink_inside_the_reproduction_directory_cannot_escape(tmp_path: Path) -> None:
    _, workspace, _ = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "tests").mkdir(exist_ok=True)
    link = workspace / REPRO_TEST_DIRECTORY
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:  # pragma: no cover - Windows 默认无符号链接权限
        pytest.skip("symlink creation is unavailable on this host")
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    with pytest.raises(ValueError, match=r"escapes|not the workspace"):
        write_reproduction_test(
            granted, "test_repro_boundary.py", "def test_x() -> None:\n    assert 1 == 1\n"
        )
    assert not (outside / "test_repro_boundary.py").exists()


def test_model_cannot_widen_permissions_through_allow_test_edits(tmp_path: Path) -> None:
    """``allow_test_edits`` 是 ValidatorConfig 的可信构造参数，与写入凭证无关。"""
    _, workspace, base = _fixture(tmp_path)
    tool = create_file_tool(WorkspacePathPolicy(workspace))
    result = tool.execute(
        {
            "path": f"{REPRO_TEST_DIRECTORY}/test_repro.py",
            "content": "def test_x() -> None:\n    assert 1 == 2\n",
        }
    )
    assert result.ok is False

    # 改动一个已有测试（不含削弱断言的写法）：默认拒绝，allow_test_edits 才放行。
    _write(
        workspace,
        "tests/test_core.py",
        "def test_normalize() -> None:\n    assert normalize(3) == 4\n",
    )
    policy = WorkspacePathPolicy(workspace)
    lenient = PatchValidator(policy, base, config=ValidatorConfig(allow_test_edits=True))
    strict = PatchValidator(policy, base)
    assert lenient.validate().passed is True
    failures = strict.validate().errors
    assert any("existing test file modification" in item for item in failures)
    assert any(
        check.name is CheckName.TEST_PROTECTION and not check.passed
        for check in strict.validate().checks
    )


def test_forging_the_reproduction_directory_name_hides_nothing(tmp_path: Path) -> None:
    """伪装目录名不会让改动躲过交付补丁与校验——例外只认精确目录。"""
    _, workspace, base = _fixture(tmp_path)
    forged = "tests/_codepilot_repro_extra/test_repro.py"
    result = create_file_tool(WorkspacePathPolicy(workspace)).execute(
        {"path": forged, "content": "def test_x() -> None:\n    assert 1 == 2\n"}
    )
    assert result.ok is True

    patch = generate_git_diff(workspace, base, policy=WorkspacePathPolicy(workspace))
    assert patch.files == [forged]
    assert patch.excluded_files == []
    assert forged in patch.text


def test_existing_cheat_detection_still_rejects_with_a_repro_file_present(
    tmp_path: Path,
) -> None:
    """复现目录的例外不能把作弊检测一起关掉。"""
    original, workspace, base = _fixture(tmp_path)
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    write_reproduction_test(
        granted,
        "test_repro_boundary.py",
        "def test_normalize_returns_the_input() -> None:\n    assert normalize(3) == 3\n",
    )
    edit = edit_file_tool(granted)
    result = edit.execute(
        {
            "path": "src/pkg/core.py",
            "search": "    return value - 1",
            "replace": "    return value if value != 3 else 3",
        }
    )
    assert result.ok is True

    validation = PatchValidator(WorkspacePathPolicy(workspace), base).validate(
        related_tests=[REPRO]
    )
    assert validation.files_changed == 1
    assert not any("reproduction" in item and "protected" in item for item in validation.errors)
    assert (workspace / REPRO).is_file()
    assert run_git(["status", "--porcelain"], cwd=original).stdout == ""


# ------------------------------------------------------- D-06 / D-07 patch 隔离


def test_reproduction_only_change_is_not_a_deliverable_patch(tmp_path: Path) -> None:
    _, workspace, base = _fixture(tmp_path)
    write_reproduction_test(
        WorkspacePathPolicy(workspace, repro_write=True),
        "test_repro_boundary.py",
        "def test_x() -> None:\n    assert 1 == 2\n",
    )

    patch = generate_git_diff(workspace, base, policy=WorkspacePathPolicy(workspace))
    assert patch.text == ""
    assert patch.files == []
    assert patch.lines_added == 0 and patch.lines_deleted == 0
    assert patch.excluded_files == [REPRO]

    validation = PatchValidator(WorkspacePathPolicy(workspace), base).validate()
    assert validation.passed is False
    assert any(item == "patch is empty" for item in validation.errors)
    assert validation.files_changed == 0


def test_delivered_patch_keeps_source_changes_and_drops_reproduction_files(
    tmp_path: Path,
) -> None:
    original, workspace, base = _fixture(tmp_path)
    _write(workspace, "src/pkg/core.py", "def normalize(value: int) -> int:\n    return value\n")
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    write_reproduction_test(
        granted,
        "test_repro_boundary.py",
        "def test_x() -> None:\n    assert 1 == 2\n",
    )
    write_reproduction_test(
        granted,
        "test_repro_second.py",
        "def test_y() -> None:\n    assert 1 == 2\n",
    )

    patch = generate_git_diff(workspace, base, policy=WorkspacePathPolicy(workspace))
    assert patch.files == ["src/pkg/core.py"]
    assert "tests/_codepilot_repro" not in patch.text
    assert "+    return value" in patch.text
    assert patch.excluded_files == [REPRO, REPRO_TWO]
    assert run_git(["status", "--porcelain"], cwd=original).stdout == ""
    assert (original / REPRO_TEST_DIRECTORY).exists() is False


def test_multi_file_patch_excludes_reproduction_consistently(tmp_path: Path) -> None:
    _, workspace, base = _fixture(tmp_path)
    _write(workspace, "src/pkg/core.py", "def normalize(value: int) -> int:\n    return value\n")
    _write(workspace, "src/pkg/extra.py", "def helper() -> int:\n    return 2\n")
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    write_reproduction_test(
        granted, "test_repro_boundary.py", "def test_x() -> None:\n    assert 1\n"
    )
    write_reproduction_test(
        granted, "test_repro_second.py", "def test_y() -> None:\n    assert 1\n"
    )

    patch = generate_git_diff(workspace, base, policy=WorkspacePathPolicy(workspace))
    assert patch.files == ["src/pkg/core.py", "src/pkg/extra.py"]

    raw = generate_git_diff(
        workspace, base, policy=WorkspacePathPolicy(workspace), excluded_directories=()
    )
    assert raw.files == [*patch.files, REPRO, REPRO_TWO]
    assert raw.lines_added > patch.lines_added


def test_unified_diff_sourced_changes_are_filtered_the_same_way(tmp_path: Path) -> None:
    """补丁来源不重要：所有来源最后都过同一个 generate_git_diff。"""
    _, workspace, base = _fixture(tmp_path)
    policy = WorkspacePathPolicy(workspace)
    unified = (
        "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
        "--- a/src/pkg/core.py\n"
        "+++ b/src/pkg/core.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def normalize(value: int) -> int:\n"
        "-    return value - 1\n"
        "+    return value\n"
    )
    result = apply_unified_diff(unified, policy)
    assert result.applied is True
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    write_reproduction_test(
        granted, "test_repro_boundary.py", "def test_x() -> None:\n    assert 1 == 2\n"
    )

    patch = generate_git_diff(workspace, base, policy=policy)
    assert patch.files == ["src/pkg/core.py"]
    assert patch.excluded_files == [REPRO]


def test_patch_statistics_ignore_reproduction_files(tmp_path: Path) -> None:
    """D-07：复现文件的行数与文件数都不进正式统计，也不触发规模误报。"""
    _, workspace, base = _fixture(tmp_path)
    _write(workspace, "src/pkg/core.py", "def normalize(value: int) -> int:\n    return value\n")
    bulky = "def test_x() -> None:\n" + "".join(f"    assert {i} == {i}\n" for i in range(160))
    write_reproduction_test(
        WorkspacePathPolicy(workspace, repro_write=True), "test_repro_boundary.py", bulky
    )

    patch = generate_git_diff(workspace, base, policy=WorkspacePathPolicy(workspace))
    assert patch.lines_added == 1
    validation = PatchValidator(
        WorkspacePathPolicy(workspace), base, config=ValidatorConfig(max_lines_changed=10)
    ).validate()
    assert validation.passed is True
    assert validation.files_changed == 1
    assert validation.lines_added == 1
    assert any("excluded from the delivered patch" in item for item in validation.evidence)


def test_user_gitignore_is_not_rewritten_and_other_files_stay_tracked(
    tmp_path: Path,
) -> None:
    """忽略语义在 diff 生成层实现，不动用户仓库的 ``.gitignore``。"""
    original, workspace, base = _fixture(tmp_path)
    granted = WorkspacePathPolicy(workspace, repro_write=True)
    write_reproduction_test(
        granted, "test_repro_boundary.py", "def test_x() -> None:\n    assert 1 == 2\n"
    )
    _write(workspace, "src/pkg/new.py", "VALUE = 1\n")

    assert (original / ".gitignore").read_text(encoding="utf-8") == "build/\n"
    assert (workspace / ".gitignore").read_text(encoding="utf-8") == "build/\n"

    patch = generate_git_diff(workspace, base, policy=WorkspacePathPolicy(workspace))
    assert patch.files == ["src/pkg/new.py"]
    status = run_git(["status", "--porcelain", "-uall"], cwd=workspace).stdout
    assert REPRO in status, "复现文件仍然在工作树里，只是不进入交付补丁"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (REPRO, True),
        (f"{REPRO_TEST_DIRECTORY}/nested/test_repro.py", True),
        (f"{REPRO_TEST_DIRECTORY.upper()}/test_repro.py", False),
        ("tests/_codepilot_repro_extra/test_repro.py", False),
        ("tests/test_core.py", False),
        ("src/pkg/core.py", False),
    ],
)
def test_exclusion_is_an_exact_directory_prefix(path: str, expected: bool) -> None:
    assert is_excluded_path(path, (REPRO_TEST_DIRECTORY,)) is expected


def test_no_exclusions_means_the_raw_workspace_diff_is_available(tmp_path: Path) -> None:
    """审计原始工作树 diff 仍然是可达的——剔除是交付层的行为，不是数据丢失。"""
    _, workspace, base = _fixture(tmp_path)
    write_reproduction_test(
        WorkspacePathPolicy(workspace, repro_write=True),
        "test_repro_boundary.py",
        "def test_x() -> None:\n    assert 1 == 2\n",
    )
    raw = generate_git_diff(
        workspace, base, policy=WorkspacePathPolicy(workspace), excluded_directories=()
    )
    assert raw.files == [REPRO]
    assert raw.excluded_files == []
