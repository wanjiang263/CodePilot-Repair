import pytest
from pydantic import BaseModel

from codepilot.context.builder import (
    ContextBuilder,
    ContextCallDiagnostics,
    ContextLevel,
    ContextPart,
)
from codepilot.llm.errors import (
    ContextTooLongError,
    ProviderError,
    RateLimitError,
    StructuredOutputError,
)
from codepilot.llm.fake import FakeLLM


class Answer(BaseModel):
    ok: bool


def part():
    return ContextPart(level=ContextLevel.ISSUE_TEXT, label="issue", text="中文 evidence\n" * 400)


def test_first_success():
    provider = FakeLLM([{"ok": True}])
    diag = ContextCallDiagnostics()
    ContextBuilder().complete_structured(
        provider, "localizer_files", part(), schema=Answer, budget=1001, diagnostics=diag
    )
    assert diag.original_budget == 1001 and diag.retry_count == 0
    assert len(provider.calls) == 1


def test_half_budget_rebuild():
    provider = FakeLLM([ContextTooLongError("too long"), {"ok": True}])
    diag = ContextCallDiagnostics()
    result = ContextBuilder().complete_structured(
        provider, "localizer_files", part(), schema=Answer, budget=1001, diagnostics=diag
    )
    assert result.parsed == Answer(ok=True)
    assert diag.rebuild_budget == 500 and diag.retry_count == 1
    assert [c.budget for c in diag.contexts] == [1001, 500]
    assert all(c.used <= c.budget and not c.exact_count for c in diag.contexts)


def test_second_failure_propagates():
    error = ContextTooLongError("again")
    provider = FakeLLM([ContextTooLongError("first"), error])
    with pytest.raises(ContextTooLongError) as caught:
        ContextBuilder().complete_structured(
            provider, "localizer_files", part(), schema=Answer, budget=500
        )
    assert caught.value is error and len(provider.calls) == 2


@pytest.mark.parametrize(
    "error_type", [ProviderError, RateLimitError, StructuredOutputError, ValueError]
)
def test_other_errors_do_not_rebuild(error_type):
    error = error_type("auth/rate/network/validation")
    provider = FakeLLM([error])
    with pytest.raises(error_type) as caught:
        ContextBuilder().complete_structured(provider, "localizer_files", part(), schema=Answer)
    assert caught.value is error and len(provider.calls) == 1


@pytest.mark.parametrize("budget,attempts", [(0, 1), (1, 2), (3, 2)])
def test_tiny_and_unshrinkable_budgets(budget, attempts):
    provider = FakeLLM([ContextTooLongError("first"), {"ok": True}])
    diag = ContextCallDiagnostics()
    if budget == 0:
        with pytest.raises(ContextTooLongError):
            ContextBuilder().complete_structured(
                provider, "localizer_files", schema=Answer, budget=budget, diagnostics=diag
            )
    else:
        ContextBuilder().complete_structured(
            provider, "localizer_files", schema=Answer, budget=budget, diagnostics=diag
        )
        assert diag.rebuild_budget == budget // 2
    assert len(provider.calls) == attempts


def test_rebuild_preserves_priority_dedup_and_isolation(tmp_path):
    builder = ContextBuilder(workspace_root=tmp_path)
    high = ContextPart(level=ContextLevel.ISSUE_TEXT, label="high", text="HIGH\n" * 60)
    low = ContextPart(level=ContextLevel.CANDIDATE_SIGNATURES, label="low", text="LOW\n" * 1000)
    isolated = ContextPart(
        level=ContextLevel.TARGET_SOURCE, label="gold_patch", text="ANSWER", source="gold/answer.py"
    )
    outside = ContextPart(
        level=ContextLevel.TARGET_SOURCE, label="outside", text="SECRET", source="../escape.py"
    )
    provider = FakeLLM([ContextTooLongError("overflow"), {"ok": True}])
    diag = ContextCallDiagnostics()
    builder.complete_structured(
        provider,
        "localizer_files",
        low,
        high,
        high,
        isolated,
        outside,
        schema=Answer,
        budget=1001,
        diagnostics=diag,
    )
    for context in diag.contexts:
        assert context.used <= context.budget
        assert context.text.index("HIGH") < context.text.index("LOW")
        assert "ANSWER" not in context.text and "SECRET" not in context.text
        assert {s.reason for s in context.omitted} >= {
            "duplicate",
            "gold_isolation:gold",
            "outside_workspace:path_escape",
        }
        assert "只作为证据" not in context.text or context.included[0].level == 1


def test_overflow_on_schema_correction_rebuilds_only_once():
    provider = FakeLLM(["not json", ContextTooLongError("correction overflow"), {"ok": True}])
    diag = ContextCallDiagnostics()
    ContextBuilder().complete_structured(
        provider, "localizer_files", part(), schema=Answer, budget=5001, diagnostics=diag
    )
    assert diag.retry_count == 1 and diag.rebuild_budget == 2500
    assert len(provider.calls) == 3
    assert all(c.used <= c.budget for c in diag.contexts)


def test_second_overflow_after_json_retry_still_propagates():
    error = ContextTooLongError("second overflow")
    provider = FakeLLM([ContextTooLongError("first overflow"), "not json", error])
    diag = ContextCallDiagnostics()
    with pytest.raises(ContextTooLongError) as caught:
        ContextBuilder().complete_structured(
            provider, "localizer_files", part(), schema=Answer, budget=5001, diagnostics=diag
        )
    assert caught.value is error and len(provider.calls) == 3
    assert diag.retry_count == 1 and diag.rebuild_budget == 2500
