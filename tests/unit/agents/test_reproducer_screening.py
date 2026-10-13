"""D-02：候选复现测试的生成契约与伪复现拒绝。

docs/05 给 D-02 的验收是"产出文件落在指定目录且**可被 pytest 收集**"。这里用一次
真实的 ``pytest --collect-only`` 子进程证明可收集性，而不只是"看起来像个测试文件"。
被执行的只有测试文件里我们亲手写下的字符串，没有生成代码进宿主 shell。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codepilot.agents.reproducer import (
    ReproTestDraft,
    issue_exception_types,
    screen_reproduction_test,
)
from codepilot.schemas.issue import IssueAnalysis

VALID = '''"""复现 verify_token 在过期整点就失效的问题。"""

from miniauth.tokens import issue_token, verify_token


def test_expiry_boundary_second_is_still_valid() -> None:
    token = issue_token("demo", ttl=0)
    assert verify_token(token) == "demo"
'''


def test_a_normal_reproduction_candidate_passes_screening() -> None:
    screening = screen_reproduction_test(VALID)
    assert screening.ok is True
    assert screening.test_names == ("test_expiry_boundary_second_is_still_valid",)
    assert screening.rejected == ()


def test_the_candidate_is_actually_collectable_by_pytest(tmp_path: Path) -> None:
    target = tmp_path / "tests" / "test_generated_repro.py"
    target.parent.mkdir(parents=True)
    target.write_text(VALID, encoding="utf-8", newline="\n")
    package = tmp_path / "miniauth"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "tokens.py").write_text(
        "def issue_token(user: str, ttl: int = 3600) -> str:\n"
        "    return f'{user}:{ttl}'\n\n\n"
        "def verify_token(token: str) -> str | None:\n    return None\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/test_generated_repro.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    assert "test_expiry_boundary_second_is_still_valid" in result.stdout, (
        result.stdout + result.stderr
    )
    assert result.returncode == 0


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        ("def test_x():\n    assert False\n", "常量断言"),
        ("def test_x():\n    assert True\n", "常量断言"),
        ("def test_x():\n    pytest.fail('broken')\n", "无条件 pytest.fail()"),
        ("def test_x():\n    raise RuntimeError('制造失败')\n", "抛出与期望行为无关的异常"),
        ("def test_x():\n    import socket\n    assert socket\n", "探测执行环境"),
        ("def test_x():\n    import subprocess\n    assert subprocess.run([])\n", "探测执行环境"),
        ("def test_x():\n    import shutil\n    assert shutil\n", "探测执行环境"),
        (
            "@pytest.mark.skip(reason='x')\ndef test_x():\n    assert 1 == 2\n",
            "被 skip/xfail 标记隐藏",
        ),
        ("@pytest.mark.xfail\ndef test_x():\n    assert 1 == 2\n", "被 skip/xfail 标记隐藏"),
        ("def test_x():\n    pass\n", "测试体是空的"),
        ("def helper():\n    return 1\n", "没有任何 pytest 可收集的 test_* 顶层函数"),
        ("raise SystemExit(1)\n\ndef test_x():\n    assert 1 == 1\n", "模块级"),
        ("def test_x():\n    sys.path.insert(0, '/tmp')\n    assert 1 == 2\n", "执行环境探测"),
        (
            "def test_x():\n    assert 1 == 2\n\n\ndef test_x():\n    assert 2 == 3\n",
            "同名测试函数",
        ),
    ],
)
def test_pseudo_reproductions_are_rejected(content: str, needle: str) -> None:
    screening = screen_reproduction_test(content)
    assert screening.ok is False
    assert any(needle in item for item in screening.rejected), screening.rejected


def test_syntax_errors_are_rejected_before_they_can_masquerade_as_failures() -> None:
    screening = screen_reproduction_test("def test_x(:\n    assert 1 == 2\n")
    assert screening.ok is False
    assert screening.test_names == ()
    assert "语法错误" in screening.rejected[0]
    assert screening.kind.value == "invalid_pseudo_reproduction"


def test_a_reference_style_candidate_with_pytest_raises_is_accepted() -> None:
    content = """import pytest

from miniauth.tokens import issue_token


def test_zero_ttl_is_rejected() -> None:
    with pytest.raises(ValueError):
        issue_token("demo", ttl=0)
"""
    screening = screen_reproduction_test(content)
    assert screening.ok is True
    assert screening.test_names == ("test_zero_ttl_is_rejected",)


def test_broad_candidates_are_flagged_as_a_risk_not_rejected() -> None:
    content = "\n\n".join(
        f"def test_branch_{index}() -> None:\n    assert {index} == {index}" for index in range(7)
    )
    screening = screen_reproduction_test(content)
    assert screening.ok is True
    assert any("复现面可能过宽" in item for item in screening.warnings)


def test_draft_schema_only_allows_the_reproduction_file_fields() -> None:
    draft = ReproTestDraft(
        filename="test_repro_boundary.py", content=VALID, expected_failure="AssertionError"
    )
    assert draft.filename == "test_repro_boundary.py"
    # 模型顺手补的越权字段被忽略，不会变成任何能力。
    extra = ReproTestDraft.model_validate(
        {
            "filename": "test_repro_boundary.py",
            "content": VALID,
            "expected_failure": "AssertionError",
            "modify_source": "src/miniauth/tokens.py",
            "shell": "rm -rf tests",
        }
    )
    assert not hasattr(extra, "modify_source")
    assert set(ReproTestDraft.model_fields) == {"filename", "content", "expected_failure"}


@pytest.mark.parametrize(
    "filename",
    [
        "conftest.py",
        "../test_escape.py",
        "tests/test_x.py",
        "test_x.PY",
        "Test_x.py",
        "",
        "x" * 120,
    ],
)
def test_draft_filenames_outside_the_reproduction_leaf_shape_are_rejected(filename: str) -> None:
    with pytest.raises(ValueError):
        ReproTestDraft(filename=filename, content=VALID, expected_failure="AssertionError")


def test_declared_exception_types_only_come_from_issue_text() -> None:
    analysis = IssueAnalysis(
        summary="verify_token 对非数字过期时间抛 ValueError",
        observed_behavior="correctly signed token with a nonnumeric expiry raises ValueError",
        error_messages=["ValueError: invalid literal for int() with base 10: 'abc'"],
        expected_behavior="返回 None 而不是抛出异常",
    )
    assert "ValueError" in issue_exception_types(analysis)
    assert "KeyError" not in issue_exception_types(analysis)


def test_issue_without_exception_evidence_declares_nothing() -> None:
    analysis = IssueAnalysis(summary="倒计时函数对负数返回 0")
    assert issue_exception_types(analysis) == frozenset()
