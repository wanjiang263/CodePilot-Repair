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


# ------------------------------------------------- 语义阶段（Harness Hardening）


def _semantic_fail(model: BaseModel) -> None:
    from codepilot.errors import PlanSemanticError, SemanticFieldError

    raise PlanSemanticError(
        "计划不合法",
        [
            SemanticFieldError(
                field="tests_to_run[0]",
                code="intent_marker_in_tests_to_run",
                reason="tests_to_run 只接受已存在的测试",
                hint="移到 planned_new_tests",
            )
        ],
    )


def test_semantic_rejection_reuses_the_same_bounded_correction_loop() -> None:
    """Pydantic 通过、语义不过：错误反馈要进下一轮请求，且次数有界。"""
    good = {"file": "a.py", "line": 1, "confidence": 0.9}
    llm = FakeLLM([good, good, good])
    with pytest.raises(StructuredOutputError) as raised:
        complete_structured(llm, REQUEST, max_attempts=3, semantic_validator=_semantic_fail)
    assert raised.value.last_stage == "semantic"

    prompts = llm.prompt_texts()
    assert len(prompts) == 3, "预算 3 次就问 3 次，不叠加"
    assert "intent_marker_in_tests_to_run" in prompts[1]
    assert "planned_new_tests" in prompts[1]


def test_semantic_validator_sees_a_schema_valid_model() -> None:
    """校验器只在 Pydantic 通过后调用；返回 None 就放行并带上 parsed。"""
    llm = FakeLLM([{"file": "a.py", "line": 1, "confidence": 0.9}])
    seen: list[Localization] = []

    def ok(model: BaseModel) -> None:
        assert isinstance(model, Localization)
        seen.append(model)

    response = complete_structured(llm, REQUEST, semantic_validator=ok)
    assert seen == [response.parsed]
    assert response.parsed is not None and response.parsed.file == "a.py"


def test_attempt_numbers_and_correction_flags_reach_the_provider() -> None:
    """纠正轮次要能在请求上看到——否则"模型被重新问了几次"只能靠猜。"""
    seen: list[tuple[int, bool]] = []

    class _Spy:
        def __init__(self, inner: FakeLLM) -> None:
            self.inner = inner

        def complete(self, request: LLMRequest):
            seen.append((request.attempt, request.correction))
            return self.inner.complete(request)

        def close(self) -> None:
            self.inner.close()

    llm = FakeLLM(
        [
            {"file": "a.py", "line": 1, "confidence": 0.9},
            {"file": "a.py", "line": 2, "confidence": 0.8},
        ]
    )
    with pytest.raises(StructuredOutputError):
        complete_structured(_Spy(llm), REQUEST, max_attempts=2, semantic_validator=_semantic_fail)
    assert seen == [(1, False), (2, True)]


def test_non_retryable_failure_is_not_turned_into_a_correction() -> None:
    """权限拒绝之类的非模型侧失败不能被"再问一次"掩盖：它要立刻冒到节点边界。"""
    from codepilot.errors import AgentFailureError, ErrorOrigin, FailureClass

    def deny(model: BaseModel) -> None:
        raise AgentFailureError(
            "工具权限被拒",
            failure_class=FailureClass.TOOL_PERMISSION_DENIED,
            origin=ErrorOrigin.TOOL,
        )

    llm = FakeLLM([{"file": "a.py", "line": 1, "confidence": 0.9}])
    with pytest.raises(AgentFailureError):
        complete_structured(llm, REQUEST, max_attempts=3, semantic_validator=deny)
    assert len(llm.calls) == 1, "不该为不可纠正的错误再问模型"


def test_last_stage_reports_the_stage_that_actually_failed_last() -> None:
    """先语义失败、后 JSON 坏掉：归因必须是 schema 阶段，否则评测层会归错类别。"""
    llm = FakeLLM(
        [
            {"file": "a.py", "line": 1, "confidence": 0.9},
            "这不是 JSON，也修不出 JSON",
        ]
    )
    with pytest.raises(StructuredOutputError) as raised:
        complete_structured(llm, REQUEST, max_attempts=2, semantic_validator=_semantic_fail)
    assert raised.value.last_stage == "schema"
