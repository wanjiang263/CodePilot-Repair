"""L-09 的证明：结构化输出失败时带着**具体错误**重试，而不是只说"重来一遍"。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from codepilot.llm.errors import StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.llm.structured import complete_structured, extract_json
from codepilot.llm.types import LLMRequest, Message


class Localization(BaseModel):
    """要定位到的符号。"""

    file: str
    line: int
    confidence: float


REQUEST = LLMRequest(
    messages=[Message.user("定位 bug")],
    response_schema=Localization,
    purpose="localize",
)


# --------------------------------------------------------------- extract_json


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '  {"a": 1}  ',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '好的，结果是：\n{"a": 1}\n希望有帮助。',
    ],
)
def test_extract_json_accepts_realistic_shapes(text: str) -> None:
    assert extract_json(text) == {"a": 1}


def test_extract_json_handles_arrays() -> None:
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


@pytest.mark.parametrize("text", ["", "   ", "完全不是 JSON", "{未闭合"])
def test_extract_json_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        extract_json(text)


# --------------------------------------------------------------- 正常路径


def test_success_on_first_attempt() -> None:
    fake = FakeLLM([{"file": "a.py", "line": 3, "confidence": 0.9}])
    response = complete_structured(fake, REQUEST)

    assert isinstance(response.parsed, Localization)
    assert response.parsed.line == 3
    assert len(fake.calls) == 1


def test_structured_call_counts_as_one_provider_call() -> None:
    fake = FakeLLM(['{"file": "a.py", "line": 3, "confidence": 0.9}'])
    complete_structured(fake, REQUEST)
    assert fake.purposes() == ["localize"]


# --------------------------------------------------------------- 重试


def test_retries_after_invalid_json() -> None:
    fake = FakeLLM(
        [
            "我不太确定，可能是 a.py。",
            {"file": "a.py", "line": 3, "confidence": 0.9},
        ]
    )
    response = complete_structured(fake, REQUEST)

    assert response.parsed is not None
    assert len(fake.calls) == 2


def test_retries_after_schema_validation_failure() -> None:
    """字段类型不对必须能重试成功，而不是直接判死。"""
    fake = FakeLLM(
        [
            {"file": "a.py", "line": "第三行", "confidence": 0.9},
            {"file": "a.py", "line": 3, "confidence": 0.9},
        ]
    )
    response = complete_structured(fake, REQUEST)
    assert response.parsed.line == 3  # type: ignore[union-attr]


def test_correction_prompt_carries_the_specific_error() -> None:
    """只说"请重新输出合法 JSON"模型会原样再错一遍。"""
    fake = FakeLLM(
        [
            {"file": "a.py", "line": "第三行", "confidence": 0.9},
            {"file": "a.py", "line": 3, "confidence": 0.9},
        ]
    )
    complete_structured(fake, REQUEST)

    second_prompt = "\n".join(m.content or "" for m in fake.calls[1].messages)
    assert "line" in second_prompt
    assert "第三行" in second_prompt  # 上次的原文要带上


def test_correction_prompt_includes_previous_output() -> None:
    fake = FakeLLM(["完全跑偏的回答", {"file": "a.py", "line": 3, "confidence": 0.9}])
    complete_structured(fake, REQUEST)
    second_prompt = "\n".join(m.content or "" for m in fake.calls[1].messages)
    assert "完全跑偏的回答" in second_prompt


def test_original_messages_are_preserved_across_retries() -> None:
    fake = FakeLLM(["垃圾", {"file": "a.py", "line": 3, "confidence": 0.9}])
    complete_structured(fake, REQUEST)
    assert fake.calls[1].messages[0].content == "定位 bug"


# --------------------------------------------------------------- 耗尽


def test_exhaustion_raises_with_diagnostics() -> None:
    fake = FakeLLM(["不是 JSON", "还不是 JSON", "依然不是"])
    with pytest.raises(StructuredOutputError) as info:
        complete_structured(fake, REQUEST)

    assert info.value.attempts == 3
    assert info.value.last_content == "依然不是"
    assert info.value.last_error is not None
    assert len(fake.calls) == 3


def test_max_attempts_is_respected() -> None:
    fake = FakeLLM(["x", "y", "z", "w"])
    with pytest.raises(StructuredOutputError):
        complete_structured(fake, REQUEST, max_attempts=2)
    assert len(fake.calls) == 2


def test_null_content_is_a_failure() -> None:
    """模型返回空内容不能当成"没有意见"而静默放过。"""
    from codepilot.llm.types import LLMResponse

    fake = FakeLLM([LLMResponse(content=None), {"file": "a.py", "line": 1, "confidence": 0.5}])
    response = complete_structured(fake, REQUEST)
    assert response.parsed is not None
    assert len(fake.calls) == 2


def test_missing_schema_raises_value_error() -> None:
    with pytest.raises(ValueError):
        complete_structured(FakeLLM(["x"]), LLMRequest(messages=[Message.user("hi")]))


def test_on_attempt_callback_sees_each_output() -> None:
    seen: list[tuple[int, str | None]] = []
    fake = FakeLLM(["垃圾", {"file": "a.py", "line": 3, "confidence": 0.9}])
    complete_structured(fake, REQUEST, on_attempt=lambda n, c: seen.append((n, c)))
    assert seen == [(1, "垃圾"), (2, '{"file": "a.py", "line": 3, "confidence": 0.9}')]
