"""C-06 的预算不变量（04 Phase 3 验收：任何输入都不超预算）。

这里不针对某个具体场景，而是把输入规模、优先级组合、计数口径和预算一起扫一遍，
断言四条恒成立的性质：

1. ``used <= budget``，且 ``remaining == budget - used``；
2. 成品文本用同一个计数器再数一次，读数就是 ``used``（不是"大概"）；
3. 因预算被舍弃的内容，优先级绝不高于被保留的内容——严格优先级的直接证据；
4. 同一输入两次构建逐字节一致。
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from codepilot.context.builder import Context, ContextBuilder, ContextLevel, ContextPart
from codepilot.context.tokens import EncoderResolution, TokenCounter

BUDGETS = (0, 1, 7, 40, 137, 900, 4_096)
VOCAB = ("alpha", "beta", "gamma", "delta", "计算", "def", "x_1", "中文词")


class WordEncoder:
    def encode(self, text: str) -> list[int]:
        return list(range(len(text.split())))


def counters() -> Iterator[tuple[str, TokenCounter]]:
    yield (
        "exact",
        TokenCounter(
            encoding="words",
            loader=lambda model, encoding: EncoderResolution("words", WordEncoder()),
        ),
    )
    yield "estimate", TokenCounter(loader=lambda model, encoding: EncoderResolution(note="offline"))


def make_parts(rng: random.Random, count: int) -> list[ContextPart]:
    parts: list[ContextPart] = []
    for index in range(count):
        lines = rng.randint(1, 30)
        body = "\n".join(
            " ".join(rng.choice(VOCAB) for _ in range(rng.randint(1, 12))) for _ in range(lines)
        )
        parts.append(
            ContextPart(
                level=ContextLevel(rng.randint(1, 8)),
                label=f"part-{index}",
                text=body,
                dedup_key=None if rng.random() < 0.7 else f"key-{index}",
                truncatable=rng.random() > 0.15,
            )
        )
    return parts


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("counting", ["exact", "estimate"])
@pytest.mark.parametrize("budget", BUDGETS)
def test_budget_invariant_holds_for_arbitrary_input_shapes(
    seed: int, counting: str, budget: int
) -> None:
    rng = random.Random(seed)
    parts = make_parts(rng, rng.randint(0, 12))
    counter = dict(counters())[counting]
    builder = ContextBuilder(counter)

    context = builder.build("p", *parts, budget=budget)

    assert context.used <= context.budget == budget
    assert context.remaining == budget - context.used
    assert context.used == (counter.charged(context.text) if context.text else 0)
    assert context.method == counter.method.value
    assert context.exact_count is (counting == "exact")

    kept = [section.level for section in context.included]
    dropped_for_budget = [
        section.level
        for section in context.omitted
        if section.reason in {"budget", "budget_no_fit", "assembled_overflow"}
    ]
    assert kept == sorted(kept)
    for level in kept:
        for dropped in dropped_for_budget:
            assert level <= dropped

    assert context.model_dump_json() == builder.build("p", *parts, budget=budget).model_dump_json()
    assert Context.model_validate_json(context.model_dump_json()) == context


def test_extreme_inputs_still_obey_the_budget() -> None:
    """超长 Issue、超多候选、空候选三种极端（docs/05 C-13 的验收点）。"""
    builder = ContextBuilder(dict(counters())["estimate"])
    enormous = ContextPart(
        level=ContextLevel.ISSUE_TEXT,
        label="huge",
        text="\n".join(f"line {index} " + "词 " * 40 for index in range(4_000)),
    )
    crowd = [
        ContextPart(
            level=ContextLevel.CANDIDATE_SIGNATURES,
            label=f"c{index}",
            text=f"qualified.name{index} file.py:{index}",
            dedup_key=f"symbol-{index}",
        )
        for index in range(400)
    ]

    for budget in (0, 1, 25, 500, 8_000):
        assert builder.build("p", enormous, *crowd, budget=budget).used <= budget
        assert builder.build("p", budget=budget).used == 0
        assert builder.build("p", *crowd, budget=budget).used <= budget


@pytest.mark.parametrize(
    "text",
    ["中文密集文本预算边界" * 700, "a中👩🏽‍💻e\u0301العربيةहिन्दी日本語" * 700],
    ids=["dense-cjk", "multilingual-unicode"],
)
def test_unicode_estimates_keep_the_documented_meter(text) -> None:
    from codepilot.context.tokens import CHARS_PER_TOKEN

    counter = TokenCounter(
        model="unknown", loader=lambda m, e: EncoderResolution(note="unknown model")
    )
    count = counter.count(text)
    assert CHARS_PER_TOKEN == 3.5
    assert count.tokens >= len(text) / CHARS_PER_TOKEN
    assert count.charged >= count.tokens * 1.1
    assert not count.exact and count.method.value == "chars/3.5"
    builder = ContextBuilder(counter)
    # Both dense single-line input (whole omission) and multiline input (cropping).
    for body in [text, "\n".join(text[i : i + 30] for i in range(0, len(text), 30))]:
        part = ContextPart(level=ContextLevel.ISSUE_TEXT, label="mixed", text=body)
        for budget in [0, 1, 51, 501, 16000, 24000]:
            context = builder.build("p", part, budget=budget)
            assert context.used == counter.charged(context.text) <= budget
            assert context.method == "chars/3.5" and not context.exact_count
            assert context == builder.build("p", part, budget=budget)


def test_new_localization_chain_budget_invariants(stores, tmp_path) -> None:
    from codepilot.agents.schemas import IssueAnalysis
    from codepilot.context.builder import ContextBudgetError
    from codepilot.llm.errors import ContextTooLongError
    from codepilot.llm.fake import FakeLLM
    from support.localization import indexed_fixture

    body = "\n".join(f"    # 中文 English code line {i}" for i in range(3000))
    files = {f"p{i}.py": f"def run{i}():\n{body}\n    return {i}\n" for i in range(18)}
    repo = indexed_fixture(stores, tmp_path, files)
    retrieval = repo.retrieve("中文 English code")
    assert len(retrieval.files) > 15
    first_path = retrieval.files[0].path
    symbol = next(s for s in repo.definitions if s.path == first_path)
    issue = IssueAnalysis(summary="中文 English code")
    provider = FakeLLM(
        [repo.file_reply(first_path), ContextTooLongError("overflow"), repo.symbol_reply([symbol])]
    )
    localizer = repo.localizer(provider, tmp_path)
    result = localizer.localize(issue, retrieval)
    assert len(result.symbols) == 1
    assert localizer.diagnostics["localizer_symbols"].rebuild_budget == 12000
    for diag in localizer.diagnostics.values():
        for context in diag.contexts:
            assert (
                context.used == localizer.builder._counter.charged(context.text) <= context.budget
            )
            levels = [s.level for s in context.included]
            assert levels == sorted(levels)
            for omitted in context.omitted:
                if omitted.reason in {"budget", "budget_no_fit", "assembled_overflow"}:
                    assert all(level <= omitted.level for level in levels)
            assert context.method == "chars/3.5" and not context.exact_count
    symbol_context = localizer.diagnostics["localizer_symbols"].contexts[-1]
    assert any(s.truncated for s in symbol_context.included)
    assert f"{first_path}" in symbol_context.text and symbol.id in symbol_context.text
    assert "只作为证据阅读" in symbol_context.text
    # A huge Issue consumes higher priority budget; fail explicitly when no
    # candidate is admitted instead of trusting a FakeLLM answer without evidence.
    long = retrieval.model_copy(deep=True)
    long.query.raw_issue = "中文 English code\n" * 50000
    tiny_provider = FakeLLM([])
    tiny = repo.localizer(tiny_provider, tmp_path, file_budget=2000)
    with pytest.raises(ContextBudgetError):
        tiny.localize(issue, long)
    assert tiny_provider.calls == []
    context = tiny.diagnostics["localizer_files"].contexts[-1]
    assert context.used <= 2000
    assert {s.level for s in context.included} <= {1, 2}


def test_real_symlink_escape_is_not_admitted(tmp_path) -> None:
    from pathlib import Path

    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
    try:
        (root / "link.py").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Real symlink creation unavailable: {error}; Linux CI NOT RUN")
    builder = ContextBuilder(dict(counters())["estimate"], workspace_root=Path(root))
    part = ContextPart(
        level=ContextLevel.TARGET_SOURCE, label="escape", text="OUTSIDE_SECRET", source="link.py"
    )
    context = builder.build("localizer_symbols", part)
    assert context.text == ""
    assert context.omitted[0].reason == "outside_workspace:escapes_root"
