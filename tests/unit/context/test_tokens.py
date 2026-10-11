"""C-05：精确计数与保守估算必须各走各的路。"""

from __future__ import annotations

import math
import sys

import pytest

from codepilot.context.tokens import (
    CHARS_PER_TOKEN,
    MIN_ESTIMATE_MARGIN,
    TIKTOKEN_DISABLE_ENV,
    CountMethod,
    EncoderResolution,
    TokenCounter,
    load_tiktoken_encoder,
)


class WordEncoder:
    """把文本按空白切成"token"，用来验证精确路径而不依赖 tiktoken。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def encode(self, text: str) -> list[int]:
        self.seen.append(text)
        return list(range(len(text.split())))


class ExplodingEncoder:
    def encode(self, text: str) -> list[int]:
        raise RuntimeError("该编码不接受这段文本")


def exact_counter(**kwargs: object) -> TokenCounter:
    encoder = kwargs.pop("encoder", None) or WordEncoder()
    return TokenCounter(
        encoding="words",
        loader=lambda model, encoding: EncoderResolution("words", encoder),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def offline_counter(**kwargs: object) -> TokenCounter:
    return TokenCounter(
        loader=lambda model, encoding: EncoderResolution(note="本地无 tokenizer 缓存"),
        **kwargs,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ 精确路径


def test_exact_path_reports_real_tokens_without_margin() -> None:
    counter = exact_counter()
    count = counter.count("one two three four")

    assert count.tokens == 4
    assert count.charged == 4
    assert count.method is CountMethod.EXACT
    assert count.exact is True
    assert count.encoding == "words"


def test_empty_text_counts_zero_on_both_paths() -> None:
    assert exact_counter().count("").tokens == 0
    offline = offline_counter().count("")
    assert (offline.tokens, offline.charged) == (0, 0)
    assert offline.method is CountMethod.ESTIMATED


# ------------------------------------------------------------------ 估算路径


def test_estimate_uses_chars_per_3_5_plus_margin() -> None:
    counter = offline_counter()
    text = "a" * 350

    count = counter.count(text)
    expected_tokens = math.ceil(len(text) / CHARS_PER_TOKEN)

    assert count.tokens == expected_tokens == 100
    # 100 + 恰好 10% = 110。float 的 1.1 大于真值，直接乘再向上取整会得到 111，
    # "留了多少余量"就失去确定答案，所以余量按十进制算。
    assert count.charged == 110
    assert count.exact is False
    assert count.note == "本地无 tokenizer 缓存"


@pytest.mark.parametrize("margin", [MIN_ESTIMATE_MARGIN, 0.25, 0.50])
def test_margin_floor_is_enforced_and_respected(margin: float) -> None:
    count = offline_counter(margin=margin).count("a" * 35)

    assert count.tokens == 10
    assert count.charged / count.tokens >= 1 + margin - 1e-9
    assert count.charged < count.tokens * (1 + margin) + 1


def test_never_claim_real_tokens_for_estimates() -> None:
    count = offline_counter().count("def divide(a, b): return a / b")

    assert count.method is CountMethod.ESTIMATED
    assert count.encoding is None
    assert count.charged > count.tokens


@pytest.mark.parametrize(
    "text",
    [
        "保存按钮点击两次会产生两条记录，之前是正常的。",
        "The cache key is built from a stale timestamp.",
        "def f(x: int) -> int:\n    return x * 2\n",
        "🙂 emoji ñ combining é and \U0001f9d9‍♀️ wizard",
        "lone surrogate \ud800 tail",
        "混合 mixed 콘텐츠代码 code \u200b zero width",
    ],
    ids=["chinese", "english", "code", "emoji", "surrogate", "mixed"],
)
def test_estimates_stay_positive_and_monotonic_for_awkward_input(text: str) -> None:
    counter = offline_counter()
    count = counter.count(text)

    assert count.tokens >= 1
    assert count.charged >= count.tokens
    assert count.charged == counter.count(text + "tail").charged or count.charged <= (
        counter.count(text + "tail").charged
    )


def test_very_long_text_does_not_overflow() -> None:
    count = offline_counter().count("x" * 500_000)

    assert count.tokens == math.ceil(500_000 / CHARS_PER_TOKEN)
    assert count.charged > count.tokens
    assert count.charged < sys.maxsize


def test_exact_and_estimate_are_the_same_order_of_magnitude() -> None:
    """02 §9.1 的两条路径读数应同量级，否则估算口径就失去了参考价值。"""
    text = " ".join(["division"] * 20 + ["by"] * 10 + ["zero"] * 10)
    exact = exact_counter().count(text).tokens
    estimate = offline_counter().count(text).tokens

    assert exact == 40
    assert 0.5 <= estimate / exact <= 2.0


# ------------------------------------------------------------------ 降级与校验


def test_loader_returning_nothing_still_explains_the_fallback() -> None:
    counter = TokenCounter(loader=lambda model, encoding: EncoderResolution())

    assert counter.exact is False
    assert "未提供可用编码器" in (counter.note or "")
    assert counter.count("abc").method is CountMethod.ESTIMATED


def test_encoder_failure_degrades_to_estimate_with_a_recorded_reason() -> None:
    counter = TokenCounter(
        encoding="boom",
        loader=lambda model, encoding: EncoderResolution("boom", ExplodingEncoder()),
    )
    count = counter.count("some text here")

    assert count.method is CountMethod.ESTIMATED
    assert count.note is not None and "降级估算" in count.note


def test_counts_are_never_negative_and_margins_never_shrink_the_number() -> None:
    corpus = ["", "a", "你好", "def f():\n    return 1\n", "\U0001f600" * 7, "x" * 1000]
    counters = [offline_counter(), exact_counter()]

    for counter in counters:
        for text in corpus:
            count = counter.count(text)
            assert count.tokens >= 0
            assert count.charged >= count.tokens >= 0


def test_encoder_that_returns_nothing_countable_degrades_to_estimate() -> None:
    class NotCountable:
        def encode(self, text: str) -> int:
            return 7

    counter = TokenCounter(loader=lambda model, encoding: EncoderResolution("x", NotCountable()))
    count = counter.count("some text here")

    assert count.method is CountMethod.ESTIMATED
    assert "降级估算" in (count.note or "")


@pytest.mark.parametrize(
    "margin",
    [-0.1, 0.0, 0.05, 1.5, math.nan, math.inf, True, "0.1"],  # type: ignore[list-item]
)
def test_illegal_margins_are_rejected(margin: object) -> None:
    with pytest.raises(ValueError):
        exact_counter(margin=margin)


def test_illegal_text_type_is_rejected() -> None:
    with pytest.raises(TypeError):
        exact_counter().count(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("budget", [-1, True, 1.5, "10"], ids=["negative", "bool", "float", "str"])
def test_illegal_budget_is_rejected(budget: object) -> None:
    with pytest.raises(ValueError):
        exact_counter().fits(budget, "text")  # type: ignore[arg-type]


def test_fits_uses_the_charged_not_raw_count() -> None:
    counter = offline_counter()
    text = "a" * 350  # tokens=100, charged=110

    assert counter.fits(110, text) is True
    assert counter.fits(105, text) is False


def test_charged_sums_multiple_pieces() -> None:
    counter = exact_counter()

    assert counter.charged("one two", "three four five") == 5
    assert counter.charged() == 0


# ------------------------------------------------------------------ 默认 loader


def test_default_loader_estimates_without_model_or_encoding() -> None:
    counter = TokenCounter()

    assert counter.exact is False
    assert "未指定模型" in (counter.note or "")


def test_disable_env_short_circuits_tiktoken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIKTOKEN_DISABLE_ENV, "1")
    resolution = load_tiktoken_encoder("gpt-4o", "cl100k_base")

    assert resolution.encoder is None
    assert TIKTOKEN_DISABLE_ENV in (resolution.note or "")


class _StubTiktoken:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def encoding_name_for_model(self, model: str) -> str:
        if model == "unknown-model":
            raise KeyError(model)
        return "cl100k_base"

    def get_encoding(self, name: str) -> WordEncoder:
        self.requested.append(name)
        if name == "missing-encoding":
            raise RuntimeError("本地没有这份 BPE 数据")
        return WordEncoder()


def test_default_loader_uses_tiktoken_when_the_model_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubTiktoken()
    monkeypatch.setitem(sys.modules, "tiktoken", stub)

    resolution = load_tiktoken_encoder("some-model", None)

    assert resolution.encoding == "cl100k_base"
    assert resolution.encoder is not None and resolution.note is None
    counter = TokenCounter(model="some-model", loader=load_tiktoken_encoder)
    assert counter.exact is True
    assert counter.count("one two three").tokens == 3


def test_unknown_model_or_unavailable_encoding_falls_back_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "tiktoken", _StubTiktoken())

    unknown = load_tiktoken_encoder("unknown-model", None)
    missing = load_tiktoken_encoder(None, "missing-encoding")

    assert unknown.encoder is None and "按估算计数" in (unknown.note or "")
    assert missing.encoder is None and "missing-encoding" in (missing.note or "")


def test_absent_tiktoken_dependency_does_not_break_counting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "tiktoken", None)

    resolution = load_tiktoken_encoder("gpt-4o", None)

    assert resolution.encoder is None
    assert resolution.note is not None
