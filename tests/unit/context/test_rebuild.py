import pytest
from pydantic import BaseModel

from codepilot.context.builder import (
    ContextBuilder,
    ContextCallDiagnostics,
    ContextLevel,
    ContextPart,
)
from codepilot.llm.errors import ContextTooLongError, ProviderError
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


def test_other_errors_do_not_rebuild():
    error = ProviderError("auth")
    provider = FakeLLM([error])
    with pytest.raises(ProviderError) as caught:
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
