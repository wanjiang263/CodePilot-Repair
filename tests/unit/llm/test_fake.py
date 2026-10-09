"""L-11 的证明：FakeLLM 能脚本化各种**坏**响应。

错误处理分支只能靠喂坏数据来测，而真实模型很难按要求犯错——
这是 FakeLLM 存在的核心理由，比"省掉网络调用"重要得多。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from codepilot.llm.errors import RateLimitError
from codepilot.llm.fake import FakeLLM
from codepilot.llm.types import LLMRequest, LLMResponse, Message

REQUEST = LLMRequest(messages=[Message.user("你好")], purpose="plan")


class _Payload(BaseModel):
    path: str


def test_string_reply_becomes_content() -> None:
    fake = FakeLLM(["回答"])
    assert fake.complete(REQUEST).content == "回答"


def test_dict_reply_becomes_json_content() -> None:
    fake = FakeLLM([{"path": "a.py"}])
    assert fake.complete(REQUEST).content == '{"path": "a.py"}'


def test_model_reply_is_serialised() -> None:
    fake = FakeLLM([_Payload(path="a.py")])
    assert '"path":"a.py"' in (fake.complete(REQUEST).content or "")


def test_tool_shaped_dict_becomes_a_tool_call() -> None:
    """约定：带 tool + arguments 的 dict 是一次工具调用。"""
    fake = FakeLLM([{"tool": "read_file", "arguments": {"path": "a.py"}}])
    response = fake.complete(REQUEST)
    assert response.content is None
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}


def test_exception_reply_is_raised() -> None:
    """错误分支必须能被脚本化，否则只能靠打真实接口碰运气。"""
    fake = FakeLLM([RateLimitError("429")])
    with pytest.raises(RateLimitError):
        fake.complete(REQUEST)


def test_response_reply_is_passed_through() -> None:
    preset = LLMResponse(content="原样", input_tokens=42)
    fake = FakeLLM([preset])
    assert fake.complete(REQUEST) is preset


def test_callable_reply_receives_the_request() -> None:
    fake = FakeLLM([lambda req: f"purpose={req.purpose}"])
    assert fake.complete(REQUEST).content == "purpose=plan"


def test_default_used_when_queue_is_empty() -> None:
    fake = FakeLLM([], default="兜底")
    assert fake.complete(REQUEST).content == "兜底"
    assert fake.complete(REQUEST).content == "兜底"


def test_exhaustion_raises_assertion_with_context() -> None:
    """脚本用尽时报错要说明是第几次调用、什么 purpose——否则排查会很久。"""
    fake = FakeLLM(["只有一条"])
    fake.complete(REQUEST)
    with pytest.raises(AssertionError) as info:
        fake.complete(LLMRequest(messages=[Message.user("x")], purpose="code"))
    assert "code" in str(info.value)


def test_queue_appends_in_order() -> None:
    fake = FakeLLM(["一"]).queue("二", "三")
    assert [fake.complete(REQUEST).content for _ in range(3)] == ["一", "二", "三"]


def test_remaining_counts_down() -> None:
    fake = FakeLLM(["一", "二"])
    assert fake.remaining == 2
    fake.complete(REQUEST)
    assert fake.remaining == 1


def test_calls_are_recorded() -> None:
    fake = FakeLLM(["一", "二"])
    fake.complete(REQUEST)
    fake.complete(LLMRequest(messages=[Message.user("x")], purpose="code"))
    assert fake.purposes() == ["plan", "code"]
    assert fake.last_request().purpose == "code"


def test_prompt_texts_helper() -> None:
    fake = FakeLLM(["一"])
    fake.complete(REQUEST)
    assert fake.prompt_texts() == ["你好"]


def test_last_request_without_calls_raises() -> None:
    with pytest.raises(AssertionError):
        FakeLLM().last_request()


def test_tokens_and_cost_are_reported() -> None:
    fake = FakeLLM(["一"], input_tokens=100, output_tokens=10, cost_per_call=0.5)
    response = fake.complete(REQUEST)
    assert (response.input_tokens, response.output_tokens, response.cost) == (100, 10, 0.5)
    assert response.cost_known is True


def test_unknown_reply_type_raises() -> None:
    fake = FakeLLM([42])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        fake.complete(REQUEST)


def test_closed_fake_refuses_calls() -> None:
    fake = FakeLLM(["一"])
    fake.close()
    with pytest.raises(RuntimeError):
        fake.complete(REQUEST)
