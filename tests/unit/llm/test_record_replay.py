"""L-12 的证明：录制 → 回放得到同一批结果；找不到记录时**绝不**回退到真实接口。

第二条是整个模块存在的意义。一旦允许回退，"离线回放"会变成一次真实调用：
要花钱、不可复现，而且产生的是一份"半录制半新"的混合数据，比全部重跑更误导人。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codepilot.llm.errors import ProviderError, RateLimitError, ReplayMissError
from codepilot.llm.fake import FakeLLM
from codepilot.llm.record import RecordingLLM
from codepilot.llm.replay import ReplayLLM, load_records
from codepilot.llm.types import LLMRequest, Message, request_fingerprint

REQUEST = LLMRequest(messages=[Message.user("你好")], purpose="plan")
OTHER = LLMRequest(messages=[Message.user("再见")], purpose="code")


@pytest.fixture
def calls_path(tmp_path: Path) -> Path:
    return tmp_path / "llm_calls.jsonl"


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------- 指纹


def test_fingerprint_is_stable_across_instances() -> None:
    a = LLMRequest(messages=[Message.user("你好")], purpose="plan")
    b = LLMRequest(messages=[Message.user("你好")], purpose="plan")
    assert request_fingerprint(a) == request_fingerprint(b)


def test_fingerprint_changes_with_content() -> None:
    assert request_fingerprint(REQUEST) != request_fingerprint(OTHER)


def test_fingerprint_changes_with_temperature() -> None:
    """温度会改变输出，必须参与指纹，否则会回放出不匹配的结果。"""
    warm = LLMRequest(messages=[Message.user("你好")], purpose="plan", temperature=0.9)
    assert request_fingerprint(REQUEST) != request_fingerprint(warm)


def test_fingerprint_changes_with_purpose() -> None:
    same = LLMRequest(messages=[Message.user("你好")], purpose="diagnose")
    assert request_fingerprint(REQUEST) != request_fingerprint(same)


def test_fingerprint_ignores_object_identity_of_schema() -> None:
    """响应 Schema 是类型对象，跨进程的 repr 不稳定——必须用它的 JSON Schema。"""
    from pydantic import BaseModel

    class Payload(BaseModel):
        path: str

    one = LLMRequest(messages=[Message.user("x")], response_schema=Payload)
    two = LLMRequest(messages=[Message.user("x")], response_schema=Payload)
    assert request_fingerprint(one) == request_fingerprint(two)


# --------------------------------------------------------------- 录制


def test_records_successful_call(calls_path: Path) -> None:
    recorder = RecordingLLM(FakeLLM(["回答"]), calls_path)
    recorder.complete(REQUEST)

    lines = _lines(calls_path)
    assert len(lines) == 1
    assert lines[0]["purpose"] == "plan"
    assert lines[0]["response"]["content"] == "回答"
    assert lines[0]["error"] is None
    assert lines[0]["seq"] == 1


def test_records_failed_call_then_reraises(calls_path: Path) -> None:
    """失败也要录：失败分支恰恰是最需要回归的部分。"""
    recorder = RecordingLLM(FakeLLM([RateLimitError("429 了")]), calls_path)
    with pytest.raises(RateLimitError):
        recorder.complete(REQUEST)

    lines = _lines(calls_path)
    assert len(lines) == 1
    assert lines[0]["error_type"] == "RateLimitError"
    assert "429 了" in str(lines[0]["error"])
    assert lines[0]["response"] is None


def test_seq_increases_and_resumes(calls_path: Path) -> None:
    first = RecordingLLM(FakeLLM(["一", "二"]), calls_path)
    first.complete(REQUEST)
    first.complete(OTHER)

    second = RecordingLLM(FakeLLM(["三"]), calls_path)
    second.complete(REQUEST)

    assert [line["seq"] for line in _lines(calls_path)] == [1, 2, 3]


def test_secrets_are_redacted_before_writing(calls_path: Path) -> None:
    """02 §25：落盘前一律过脱敏。错误体里常有回显的请求头。"""
    recorder = RecordingLLM(
        FakeLLM([ProviderError("401: {'Authorization': 'Bearer sk-abcdefghijklmn'}")]),
        calls_path,
    )
    with pytest.raises(ProviderError):
        recorder.complete(REQUEST)

    raw = calls_path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmn" not in raw


def test_redaction_can_be_disabled(calls_path: Path) -> None:
    """默认开脱敏是对的，但要有开关——本地排查时需要看到原文。"""
    recorder = RecordingLLM(FakeLLM(["一"]), calls_path, redact_payloads=False)
    recorder.complete(REQUEST)
    assert _lines(calls_path)[0]["response"]["content"] == "一"


# --------------------------------------------------------------- 回放


def test_replay_reproduces_content(calls_path: Path) -> None:
    RecordingLLM(FakeLLM(["原始回答"]), calls_path).complete(REQUEST)

    replayed = ReplayLLM(calls_path).complete(REQUEST)
    assert replayed.content == "原始回答"


def test_replay_needs_no_network(calls_path: Path) -> None:
    """回放路径完全不碰网络，因此也不需要密钥。"""
    RecordingLLM(FakeLLM(["回答"]), calls_path).complete(REQUEST)
    replay = ReplayLLM(calls_path)
    assert replay.complete(REQUEST).content == "回答"


def test_replay_preserves_token_counts(calls_path: Path) -> None:
    RecordingLLM(
        FakeLLM(["一"], input_tokens=123, output_tokens=45, cost_per_call=0.25), calls_path
    ).complete(REQUEST)

    response = ReplayLLM(calls_path).complete(REQUEST)
    assert (response.input_tokens, response.output_tokens) == (123, 45)
    assert response.cost == 0.25


def test_replay_preserves_tool_calls(calls_path: Path) -> None:
    RecordingLLM(
        FakeLLM([{"tool": "read_file", "arguments": {"path": "a.py"}}]), calls_path
    ).complete(REQUEST)

    response = ReplayLLM(calls_path).complete(REQUEST)
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}


def test_replay_miss_raises_and_never_falls_back(calls_path: Path) -> None:
    RecordingLLM(FakeLLM(["回答"]), calls_path).complete(REQUEST)
    replay = ReplayLLM(calls_path)

    with pytest.raises(ReplayMissError) as info:
        replay.complete(OTHER)

    assert info.value.fingerprint
    assert replay.misses


def test_replay_reproduces_failures(calls_path: Path) -> None:
    recorder = RecordingLLM(FakeLLM([RateLimitError("429 了")]), calls_path)
    with pytest.raises(RateLimitError):
        recorder.complete(REQUEST)

    with pytest.raises(RateLimitError, match="429 了"):
        ReplayLLM(calls_path).complete(REQUEST)


def test_repeated_identical_requests_are_served_in_order(calls_path: Path) -> None:
    """同一个请求被调用多次时，按录制顺序依次返回。"""
    RecordingLLM(FakeLLM(["第一次", "第二次"]), calls_path).complete(REQUEST)
    RecordingLLM(FakeLLM(["第二次"]), calls_path).complete(REQUEST)

    replay = ReplayLLM(calls_path)
    assert replay.complete(REQUEST).content == "第一次"
    assert replay.complete(REQUEST).content == "第二次"


def test_third_identical_request_misses(calls_path: Path) -> None:
    RecordingLLM(FakeLLM(["唯一一次"]), calls_path).complete(REQUEST)
    replay = ReplayLLM(calls_path)
    replay.complete(REQUEST)
    with pytest.raises(ReplayMissError):
        replay.complete(REQUEST)


# --------------------------------------------------------------- 完整性


def test_unconsumed_detects_a_shorter_flow(calls_path: Path) -> None:
    """流程比录制时少走了几步——这是真实的回归，但不会表现为报错。"""
    recorder = RecordingLLM(FakeLLM(["一", "二"]), calls_path)
    recorder.complete(REQUEST)
    recorder.complete(OTHER)

    replay = ReplayLLM(calls_path)
    replay.complete(REQUEST)

    assert replay.remaining == 1
    with pytest.raises(AssertionError, match="未被消费"):
        replay.assert_fully_consumed()


def test_fully_consumed_passes(calls_path: Path) -> None:
    RecordingLLM(FakeLLM(["一"]), calls_path).complete(REQUEST)
    replay = ReplayLLM(calls_path)
    replay.complete(REQUEST)
    replay.assert_fully_consumed()


# --------------------------------------------------------------- 加载


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_records(tmp_path / "nope.jsonl")


def test_empty_lines_are_skipped(calls_path: Path) -> None:
    RecordingLLM(FakeLLM(["一"]), calls_path).complete(REQUEST)
    calls_path.write_text(calls_path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(load_records(calls_path)) == 1


def test_corrupt_line_raises_with_position(calls_path: Path) -> None:
    """坏行静默跳过会让回放少一次调用，症状是下游莫名走错分支。"""
    calls_path.write_text('{"seq": 1}\n这不是 JSON\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":1"):
        load_records(calls_path)


def test_recorder_close_forwards_to_inner(calls_path: Path) -> None:
    inner = FakeLLM(["一"])
    recorder = RecordingLLM(inner, calls_path)
    recorder.close()
    assert inner.closed is True
