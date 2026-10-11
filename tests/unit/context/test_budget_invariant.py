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
