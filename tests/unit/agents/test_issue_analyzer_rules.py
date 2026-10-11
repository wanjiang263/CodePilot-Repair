"""C-02：规则抽取——只用 Issue 原文里逐字存在的信息。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.schemas.enums import IssueType

REPO_FILES = ["calc/main.py", "calc/util/io.py", "test_main.py"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "repo"
    (base / "calc").mkdir(parents=True)
    return base


def at(root: Path, relative: str) -> str:
    """按当前平台拼出 traceback 里的路径（Windows 给反斜杠，Linux 给正斜杠）。"""
    return str(root / relative)


def traceback_issue(root: Path, *, line: int = 12, function: str = "divide") -> str:
    return (
        "Divide crashes when the divisor is zero.\n\n"
        "```\n"
        "Traceback (most recent call last):\n"
        f'  File "{at(root, "calc/main.py")}", line {line}, in {function}\n'
        "    return a / b\n"
        "ZeroDivisionError: division by zero\n"
        "```"
    )


def rules() -> IssueAnalyzer:
    return IssueAnalyzer()


def test_standard_traceback_yields_frames_files_and_error(root: Path) -> None:
    evidence = rules().extract(traceback_issue(root), repo_files=REPO_FILES, repo_root=root)

    assert [frame.file for frame in evidence.traceback_frames] == ["calc/main.py"]
    assert evidence.traceback_frames[0].line == 12
    assert evidence.traceback_frames[0].function == "divide"
    assert evidence.traceback_frames[0].in_repo is True
    assert "calc/main.py" in evidence.mentioned_files
    assert "ZeroDivisionError: division by zero" in evidence.error_messages
    assert "ZeroDivisionError" in evidence.mentioned_symbols
    assert evidence.issue_type is IssueType.BUG


def test_files_without_repo_context_are_never_marked_internal(root: Path) -> None:
    """不给 repo_files 时宁可没有种子，也不把陌生路径当仓库代码。"""
    evidence = rules().extract(traceback_issue(root))
    assert evidence.traceback_frames[0].in_repo is False


def test_multi_level_call_stack_keeps_order_and_marks_third_party_frames(root: Path) -> None:
    issue = (
        "Crash on save.\n"
        "Traceback (most recent call last):\n"
        f'  File "{at(root, "calc/main.py")}", line 40, in save\n'
        "    payload = build()\n"
        f'  File "{at(root, "calc/main.py")}", line 12, in build\n'
        "    return render()\n"
        '  File "/usr/lib/python3.12/json/__init__.py", line 231, in dumps\n'
        "    s = _default(o)\n"
        '  File "C:\\vendor\\serializer.py", line 7, in write\n'
        "    fp.flush()\n"
        "TypeError: not serializable\n"
    )
    evidence = rules().extract(issue, repo_files=REPO_FILES, repo_root=root)

    assert [(f.file, f.function, f.in_repo) for f in evidence.traceback_frames] == [
        ("calc/main.py", "save", True),
        ("calc/main.py", "build", True),
        ("/usr/lib/python3.12/json/__init__.py", "dumps", False),
        ("C:/vendor/serializer.py", "write", False),
    ]
    assert evidence.error_messages[0].strip().startswith("TypeError")
    assert evidence.issue_type is IssueType.BUG


def test_windows_style_path_matches_repo_files_on_any_platform(tmp_path: Path) -> None:
    issue = (
        '  File "D:\\work\\repo\\calc\\main.py", line 12, in divide\n'
        "    return a / b\n"
        "ZeroDivisionError: division by zero\n"
    )
    evidence = rules().extract(issue, repo_files=REPO_FILES, repo_root=tmp_path / "missing")
    assert evidence.traceback_frames[0].in_repo is False

    indexed = tmp_path / "calc" / "main.py"
    flattened = f'File "{indexed.as_posix()}", line 12, in divide\nZeroDivisionError: boom\n'
    again = rules().extract(flattened, repo_files=REPO_FILES, repo_root=tmp_path)
    assert again.traceback_frames[0].file == "calc/main.py"
    assert again.traceback_frames[0].in_repo is True


def test_path_mentioned_without_traceback(root: Path) -> None:
    issue = "The cache key in calc/main.py is built from a stale timestamp."
    evidence = rules().extract(issue, repo_files=REPO_FILES)

    assert evidence.traceback_frames == []
    assert evidence.mentioned_files == ["calc/main.py"]
    assert "timestamp" in evidence.keywords


def test_plain_prose_issue_extracts_only_what_is_written(root: Path) -> None:
    issue = "Clicking save twice creates two records instead of one."
    evidence = rules().extract(issue, repo_files=REPO_FILES)

    assert evidence.traceback_frames == []
    assert evidence.mentioned_files == []
    assert evidence.error_messages == []
    assert evidence.issue_type is None
    assert evidence.keywords


def test_multiple_code_blocks_and_repeated_paths_are_deduplicated_stably(root: Path) -> None:
    issue = "\n\n".join(
        [
            "Reproduces with these two snippets.",
            "```python\nopen('calc/main.py')\n```",
            '```text\nFile "calc/main.py", line 12, in divide\n    return a / b\n'
            "ZeroDivisionError: division by zero\n```",
            "```python\nfrom calc.util import write\nwrite('calc/util/io.py')\n```",
        ]
    )
    evidence = rules().extract(issue, repo_files=REPO_FILES)

    assert evidence.mentioned_files.count("calc/main.py") == 1
    assert "calc/util/io.py" in evidence.mentioned_files
    assert len(evidence.traceback_frames) == 1
    assert evidence.keywords.count("calc") <= 1


def test_backticked_identifiers_are_symbols_but_paths_are_not(root: Path) -> None:
    issue = (
        "`build_index` in `calc/main.py` returns the wrong `symbol_id`, "
        "so `resolve_edge` never fires."
    )
    evidence = rules().extract(issue, repo_files=REPO_FILES)

    assert {"build_index", "resolve_edge", "symbol_id"} <= set(evidence.mentioned_symbols)
    assert not [word for word in evidence.mentioned_symbols if word.endswith(".py")]


def test_reproduction_section_steps_are_verbatim(root: Path) -> None:
    issue = (
        "Save button duplicates records.\n\n"
        "## Steps to Reproduce\n"
        "1. Open the editor\n"
        "2. Click **Save** twice\n"
        "   - ignores the debounce timer\n\n"
        "## Expected\n"
        "- one record\n"
        "## Actual\n"
        "- two records\n"
    )
    evidence = rules().extract(issue, repo_files=REPO_FILES)

    assert evidence.reproduction_steps == ["Open the editor", "Click **Save** twice"]


def test_numbered_steps_without_a_heading_are_steps_but_side_by_side_bullets_are_not() -> None:
    ordered = (
        IssueAnalyzer()
        .extract("1. run the app\n2. press save\n3. observe two rows\n")
        .reproduction_steps
    )
    assert ordered == ["run the app", "press save", "observe two rows"]

    bullets = (
        IssueAnalyzer().extract("- Expected: one row\n- Actual: two rows\n").reproduction_steps
    )
    assert bullets == []

    broken_numbering = IssueAnalyzer().extract("2. second\n3. third\n").reproduction_steps
    assert broken_numbering == []


def test_mixed_chinese_and_english_issue(root: Path) -> None:
    issue = (
        "保存按钮点击两次会产生两条记录，之前是正常的。\n"
        "复现步骤：\n"
        "1. 打开编辑器\n2. 连续点击 Save\n"
        "Traceback (most recent call last):\n"
        f'  File "{at(root, "calc/main.py")}", line 12, in divide\n'
        "    return a / b\n"
        "ZeroDivisionError: division by zero\n"
    )
    evidence = rules().extract(issue, repo_files=REPO_FILES, repo_root=root)

    assert evidence.issue_type is IssueType.REGRESSION
    assert evidence.reproduction_steps == ["打开编辑器", "连续点击 Save"]
    assert evidence.traceback_frames[0].file == "calc/main.py"
    assert "save" in evidence.keywords
    assert "divide" in evidence.keywords


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ("", None),
        ("   \n\n  ", None),
        ("Traceback (most recent call last):", IssueType.BUG),
        ("ValueError:", IssueType.BUG),
        ('  File "calc/main.py", line', None),
        ("File calc/main.py, line 3, in f", None),
        ("it used to work before the upgrade", IssueType.REGRESSION),
        ("this is very slow now", IssueType.PERFORMANCE),
        ("please add support for csv export", IssueType.FEATURE),
        ("TypeError: bad", IssueType.BUG),
        ("a plain sentence with no signals", None),
    ],
)
def test_malformed_and_special_inputs_never_fabricate_evidence(
    issue: str, expected: object
) -> None:
    evidence = rules().extract(issue, repo_files=REPO_FILES)

    assert evidence.issue_type is expected
    assert all(f.file and f.line >= 0 for f in evidence.traceback_frames)
    assert evidence.mentioned_files == list(dict.fromkeys(evidence.mentioned_files))
    assert evidence.mentioned_symbols == list(dict.fromkeys(evidence.mentioned_symbols))
    assert evidence.reproduction_steps == list(dict.fromkeys(evidence.reproduction_steps))


def test_empty_issue_still_produces_a_usable_record(root: Path) -> None:
    analysis = rules().analyze("")

    assert analysis.summary == ""
    assert analysis.issue_type is IssueType.OTHER
    assert analysis.traceback_frames == []
    assert analysis.keywords == []


def test_analysis_is_deterministic_for_identical_input(root: Path) -> None:
    analyzer = rules()
    first = analyzer.analyze(traceback_issue(root), repo_files=REPO_FILES)
    second = analyzer.analyze(traceback_issue(root), repo_files=REPO_FILES)

    assert first.model_dump_json() == second.model_dump_json()


def test_rule_evidence_query_is_the_same_object_shape_the_retriever_uses(root: Path) -> None:
    """证据必须能追到 V-01 的查询，不允许出现第二套解析结果。"""
    issue = traceback_issue(root)
    analyzer = rules()
    evidence = analyzer.extract(issue, repo_files=REPO_FILES)
    query = analyzer.retrieval_query(issue, repo_files=REPO_FILES)

    assert evidence.query == query
    assert evidence.traceback_frames == query.parsed_frames
    assert evidence.mentioned_files == query.file_paths
