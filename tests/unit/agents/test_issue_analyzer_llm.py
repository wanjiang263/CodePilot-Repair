"""C-03：LLM 只补语义，规则证据不可被覆盖。全部走 FakeLLM，零真实 API。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codepilot.agents.issue_analyzer import ISSUE_ANALYSIS_PURPOSE, IssueAnalyzer
from codepilot.agents.schemas import IssueSemanticDraft
from codepilot.llm.errors import ProviderError, StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.schemas.enums import IssueType

REPO_FILES = ["calc/main.py"]

TRACEBACK = (
    "Divide crashes on a zero divisor.\n"
    "Traceback (most recent call last):\n"
    '  File "calc/main.py", line 12, in divide\n'
    "    return a / b\n"
    "ZeroDivisionError: division by zero\n"
)

DRAFT: dict[str, Any] = {
    "issue_type": "regression",
    "summary": "除零时没有走错误分支，进程直接崩掉",
    "expected_behavior": "返回校验后的错误提示",
    "observed_behavior": "抛出 ZeroDivisionError 并中断",
    "reproduction_steps": ["模型自己补的一步"],
    "keywords": ["divisor", "validation"],
}


def analyze_with(reply: Any, issue: str = TRACEBACK) -> tuple[Any, IssueAnalyzer]:
    fake = FakeLLM([reply])
    analyzer = IssueAnalyzer(fake)
    return analyzer.analyze(issue, repo_files=REPO_FILES), fake


def test_llm_supplies_semantics_and_rules_keep_the_evidence() -> None:
    analysis, fake = analyze_with(DRAFT)

    assert analysis.issue_type is IssueType.REGRESSION
    assert analysis.summary == DRAFT["summary"]
    assert analysis.expected_behavior == DRAFT["expected_behavior"]
    assert analysis.observed_behavior == DRAFT["observed_behavior"]
    assert analysis.mentioned_files == ["calc/main.py"]
    assert [f.file for f in analysis.traceback_frames] == ["calc/main.py"]
    assert analysis.error_messages[0] == "ZeroDivisionError: division by zero"
    assert len(fake.calls) == 1
    assert fake.calls[0].purpose == ISSUE_ANALYSIS_PURPOSE
    assert fake.calls[0].response_schema is IssueSemanticDraft


def test_llm_cannot_overwrite_verifiable_facts() -> None:
    """模型返回 traceback/文件/符号/错误信息时，这些键被直接丢弃。"""
    hostile = {
        **DRAFT,
        "traceback_frames": [
            {"file": "invented.py", "line": 999, "function": "fake", "in_repo": True}
        ],
        "mentioned_files": ["invented.py", "src/root_cause.py"],
        "mentioned_symbols": ["guessed_helper"],
        "error_messages": ["RuntimeError: 我猜的"],
        "suspected_root_cause": "缓存",
    }
    analysis, _ = analyze_with(hostile)

    assert [f.file for f in analysis.traceback_frames] == ["calc/main.py"]
    assert analysis.mentioned_files == ["calc/main.py"]
    assert "guessed_helper" not in analysis.mentioned_symbols
    assert "invented.py" not in analysis.mentioned_files
    assert all("invented" not in message for message in analysis.error_messages)
    assert "suspected_root_cause" not in analysis.model_dump()
    # 语义字段仍然照收：保护只针对可验证事实。
    assert analysis.summary == DRAFT["summary"]


def test_issue_without_traceback_still_gets_a_useful_analysis() -> None:
    issue = "Clicking save twice inserts two rows instead of one."
    analysis, _ = analyze_with(
        {
            "issue_type": "bug",
            "summary": "保存按钮缺少幂等保护",
            "expected_behavior": "只写入一行",
            "observed_behavior": "写入两行",
            "reproduction_steps": ["打开编辑器", "连续点击两次保存"],
            "keywords": ["idempotency"],
        },
        issue,
    )

    assert analysis.traceback_frames == []
    assert analysis.reproduction_steps == ["打开编辑器", "连续点击两次保存"]
    assert "idempotency" in analysis.keywords
    assert analysis.issue_type is IssueType.BUG


def test_rule_reproduction_steps_win_over_the_model() -> None:
    issue = "Save inserts two rows.\n\n## Steps to Reproduce\n1. open editor\n2. click save twice\n"
    analysis, _ = analyze_with({**DRAFT, "reproduction_steps": ["改写过的步骤"]}, issue)

    assert analysis.reproduction_steps == ["open editor", "click save twice"]


def test_keywords_merge_rule_first_and_deduplicate() -> None:
    rule_keywords = IssueAnalyzer().extract(TRACEBACK, repo_files=REPO_FILES).keywords
    analysis, _ = analyze_with({**DRAFT, "keywords": ["divide", "divisor", "neologism"]})

    assert [keyword for keyword in analysis.keywords if keyword in set(rule_keywords)] == (
        rule_keywords
    )
    assert analysis.keywords.count("divide") == 1
    assert analysis.keywords[-1] == "neologism"


def test_illegal_json_is_retried_and_then_parsed() -> None:
    fake = FakeLLM(["这不是 JSON", DRAFT])
    analysis = IssueAnalyzer(fake).analyze(TRACEBACK, repo_files=REPO_FILES)

    assert len(fake.calls) == 2
    assert analysis.summary == DRAFT["summary"]
    correction = fake.calls[1].messages[-1].content or ""
    assert "不符合要求的 JSON Schema" in correction


def test_schema_mismatch_that_never_recovers_raises_instead_of_faking_results() -> None:
    fake = FakeLLM(default={"summary": 5, "issue_type": "nonsense"})
    analyzer = IssueAnalyzer(fake)

    with pytest.raises(StructuredOutputError) as excinfo:
        analyzer.analyze(TRACEBACK, repo_files=REPO_FILES)

    assert excinfo.value.attempts == 3
    assert len(fake.calls) == 3


def test_max_attempts_is_respected() -> None:
    fake = FakeLLM(default="not json at all")
    analyzer = IssueAnalyzer(fake, max_attempts=1)

    with pytest.raises(StructuredOutputError):
        analyzer.analyze(TRACEBACK, repo_files=REPO_FILES)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        ProviderError("上游 503"),
        RuntimeError("连接被重置"),
    ],
    ids=["provider error", "unexpected error"],
)
def test_provider_failures_propagate_without_silent_degradation(reply: Exception) -> None:
    with pytest.raises(type(reply)):
        analyze_with(reply)


def test_empty_model_opinions_fall_back_to_rule_derived_summary() -> None:
    analysis, _ = analyze_with(
        {
            "issue_type": None,
            "summary": "   ",
            "expected_behavior": None,
            "observed_behavior": "",
            "keywords": [],
        }
    )

    assert analysis.issue_type is IssueType.BUG  # 规则层看到 traceback 帧后的判断
    assert "Divide crashes" in analysis.summary
    assert analysis.expected_behavior is None
    assert analysis.observed_behavior is None


def test_no_provider_means_no_llm_call_at_all() -> None:
    fake = FakeLLM([DRAFT])
    analysis = IssueAnalyzer().analyze(TRACEBACK, repo_files=REPO_FILES)

    assert fake.calls == []
    assert analysis.summary.startswith("Divide crashes")
    assert analysis.issue_type is IssueType.BUG


def test_prompt_states_the_boundary_and_carries_the_rule_facts() -> None:
    fake = FakeLLM([DRAFT])
    IssueAnalyzer(fake).analyze(TRACEBACK, repo_files=REPO_FILES)

    system, user = (message.content or "" for message in fake.calls[0].messages[:2])
    assert "不要输出 traceback" in system
    assert "ZeroDivisionError: division by zero" in user
    assert 'File "calc/main.py", line 12, in divide' in user
    assert "traceback 帧数: 1" in user


@pytest.mark.parametrize("absolute", [False, True], ids=["relative path", "absolute path"])
def test_llm_path_is_deterministic_for_identical_replies(tmp_path: Path, absolute: bool) -> None:
    issue = TRACEBACK
    if absolute:
        issue = issue.replace("calc/main.py", str(tmp_path / "calc" / "main.py"))
    fake = FakeLLM(default=DRAFT)
    analyzer = IssueAnalyzer(fake)

    first = analyzer.analyze(issue, repo_files=REPO_FILES, repo_root=tmp_path)
    second = analyzer.analyze(issue, repo_files=REPO_FILES, repo_root=tmp_path)

    assert first.model_dump_json() == second.model_dump_json()
    assert len(fake.calls) == 2
