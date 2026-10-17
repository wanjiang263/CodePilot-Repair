"""三阶段冒烟引擎的离线验收。

这里最重要的两条断言都是**否定式**的：

* dry-run 一个 HTTP 请求都不发 —— 连 provider 都不许构造；
* 前置条件不满足时 live 必须拒绝，而不是"试试看会不会失败"。

其余部分验证三个阶段各自检验了对应的契约。工具阶段尤其只看一件事：最终回复里
是否出现了那个只有真正执行工具才能拿到的暗号。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from codepilot.llm.config import LLMConfig, ModelPrice, load_llm_config
from codepilot.llm.glm import GLM_API_KEY_ENV, ZHIPU_API_KEY_ENV
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.smoke import (
    TOOL_MARKER,
    SmokeReport,
    SmokeRunner,
    SmokeStage,
    SmokeStatus,
    run_smoke,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
KEY = "smoke-secret-key-9f3a"


@pytest.fixture(autouse=True)
def no_repo_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """把 ``.env`` 这条密钥来源指向空目录。

    开发机上项目根的 ``.env`` 里真的有密钥，不隔离的话这批"缺密钥"用例会本地全绿、
    在无密钥的 CI 上失败（或反过来）。前后都清一次 lru_cache，避免污染别的测试。
    """
    import codepilot.config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", tmp_path)
    app_config._dotenv_values.cache_clear()
    monkeypatch.delenv(GLM_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ZHIPU_API_KEY_ENV, raising=False)
    yield
    app_config._dotenv_values.cache_clear()


class Script:
    """按脚本回放响应，并记下每一个真实收到的请求。"""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.content]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("响应脚本已用尽，但仍收到新的请求")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _chat(content: str | None = "收到", **extra: Any) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if "tool_calls" in extra:
        message["tool_calls"] = extra["tool_calls"]
    if "reasoning_content" in extra:
        message["reasoning_content"] = extra["reasoning_content"]
    return httpx.Response(
        200,
        json={
            "model": "glm-4.7",
            "choices": [{"message": message, "finish_reason": extra.get("finish_reason", "stop")}],
            "usage": {
                "prompt_tokens": extra.get("prompt_tokens", 500),
                "completion_tokens": extra.get("completion_tokens", 20),
            },
        },
    )


def _tool_response(name: str, arguments: str, *, call_id: str = "call_1") -> httpx.Response:
    return _chat(
        None,
        finish_reason="tool_calls",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    )


def _config() -> LLMConfig:
    return load_llm_config(REPO_ROOT / "configs" / "llm.yaml")


def _runner(
    alias: str,
    script: Script,
    *,
    max_cost: float | None = None,
    max_output_tokens: int = 256,
    price: ModelPrice | None = None,
) -> SmokeRunner:
    """直接构造 provider：SmokeRunner 这层不需要密钥。"""
    config = _config()
    spec = config.models[alias]
    if price is not None:
        spec = spec.model_copy(update={"price": price})
    provider_spec = config.providers[spec.provider]
    client = httpx.Client(
        transport=httpx.MockTransport(script.handler), base_url=provider_spec.base_url
    )
    provider = OpenAICompatibleProvider(
        spec=spec,
        provider=provider_spec,
        api_key=KEY,
        client=client,
        policy=RetryPolicy(max_attempts=2, jitter_ratio=0.0),
        sleep=lambda _s: None,
        exchange_rates={"CNY": 1.0},
        budget_currency="CNY",
    )
    return SmokeRunner(
        provider=provider,
        http_attempts=lambda: provider.attempts,
        unacknowledged=lambda: provider.unacknowledged,
        spec=spec,
        provider_spec=provider_spec,
        currency="CNY",
        max_output_tokens=max_output_tokens,
        max_cost=max_cost,
    )


def _mock_registry_client(monkeypatch: pytest.MonkeyPatch, script: Script) -> None:
    """让 run_smoke 建出来的 provider 走 MockTransport，而不是真端点。"""
    import codepilot.llm.registry as registry_module

    def factory(provider_spec: Any, api_key: str) -> httpx.Client:
        del api_key
        return httpx.Client(
            transport=httpx.MockTransport(script.handler), base_url=provider_spec.base_url
        )

    monkeypatch.setattr(registry_module, "_default_client", factory)


# --------------------------------------------------------------- 普通对话阶段


def test_text_stage_passes_on_a_complete_reply() -> None:
    script = Script(_chat("收到"))
    result = _runner("glm-main", script).run_text()

    assert result.status is SmokeStatus.PASS
    assert result.requests == 1
    assert result.input_tokens == 500
    assert result.cost_known is True
    # 冒烟只问"收到"，绝不把仓库源码或 Issue 正文发到外网。
    assert "def " not in json.dumps(script.bodies, ensure_ascii=False)


@pytest.mark.parametrize(
    ("response", "expect_pass"),
    [
        (_chat("收到"), True),
        (_chat(None), False),
        (_chat("   "), False),
        (_chat("截断了", finish_reason="length"), False),
    ],
    ids=["ok", "null-content", "blank", "truncated"],
)
def test_text_stage_verdicts(response: httpx.Response, expect_pass: bool) -> None:
    result = _runner("glm-main", Script(response)).run_text()
    assert (result.status is SmokeStatus.PASS) is expect_pass


def test_text_stage_distinguishes_reasoning_only_from_empty_answer() -> None:
    """只有思考内容、没有最终答案：判 FAIL，但要说清楚 reasoning_content 确实回来了。"""
    result = _runner("glm-main", Script(_chat(None, reasoning_content="想了一些东西"))).run_text()

    assert result.status is SmokeStatus.FAIL
    assert any("reasoning_content" in note for note in result.notes)


# --------------------------------------------------------------- 结构化阶段


def test_structured_stage_asks_for_json_and_parses_it() -> None:
    payload = json.dumps(
        {"verdict": "pass", "summary": "端点可用", "evidence": ["200"], "confidence": 0.9},
        ensure_ascii=False,
    )
    script = Script(_chat(payload))
    result = _runner("glm-main", script).run_structured()

    assert result.status is SmokeStatus.PASS
    assert "verdict='pass'" in result.detail
    # GLM 支持的是 json_object（宽松）而不是 strict json_schema：请求体必须如实反映
    # 这一点，否则我们以为拿到了受约束的输出。
    assert script.bodies[0]["response_format"] == {"type": "json_object"}
    assert "verdict" in json.dumps(script.bodies[0], ensure_ascii=False)


def test_structured_stage_fails_after_correction_retries() -> None:
    """非法 JSON 走既有的纠正重试；三次都坏就判 FAIL，绝不伪造一个合法对象。"""
    bad = _chat("这不是 JSON")
    script = Script(bad, bad, bad)
    result = _runner("glm-main", script).run_structured()

    assert result.status is SmokeStatus.FAIL
    assert len(script.requests) == 3
    correction = json.dumps(script.bodies[1], ensure_ascii=False)
    assert "不符合要求" in correction
    assert "这不是 JSON" in correction, "上次输出原文要一起发回去，否则模型会原样再错一遍"


@pytest.mark.parametrize(
    "content",
    ['{"verdict": "pass", "summary": "缺了后半段', '{"verdict": "maybe", "summary": "x"}'],
    ids=["truncated-json", "bad-enum"],
)
def test_structured_stage_rejects_illegal_shapes(content: str) -> None:
    script = Script(_chat(content), _chat(content), _chat(content))
    result = _runner("glm-main", script).run_structured()
    assert result.status is SmokeStatus.FAIL


def test_structured_stage_charges_every_attempt() -> None:
    """三次尝试都消耗 token：只算成功那次会让冒烟自己变成一笔对不上账的费用。"""
    script = Script(
        _chat("nope", prompt_tokens=1000, completion_tokens=10),
        _chat(
            '{"verdict":"pass","summary":"ok","evidence":[],"confidence":0.5}',
            prompt_tokens=1000,
            completion_tokens=10,
        ),
    )
    result = _runner("glm-main", script).run_structured()
    assert result.status is SmokeStatus.PASS
    assert result.requests == 2


# --------------------------------------------------------------- 函数调用阶段


def test_tools_stage_completes_the_round_trip() -> None:
    """工具阶段验证的是闭环：请求 → ToolCall → 执行 → 回传 → 模型续答。"""
    script = Script(
        _tool_response("read_file", json.dumps({"path": "notes.txt"})),
        _chat(f"暗号是 {TOOL_MARKER}"),
    )
    result = _runner("glm-main", script).run_tools()

    assert result.status is SmokeStatus.PASS
    assert len(script.requests) == 2
    second = script.bodies[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "call_1"
    assert TOOL_MARKER in second[-1]["content"]
    assert any("工具执行" in note for note in result.notes)


def test_tools_stage_uses_only_registered_read_only_tools() -> None:
    """发给模型的工具表必须来自真实 ToolRegistry，且不含写工具。"""
    script = Script(
        _tool_response("read_file", json.dumps({"path": "notes.txt"})),
        _chat(TOOL_MARKER),
    )
    _runner("glm-main", script).run_tools()
    sent = {t["function"]["name"] for t in script.bodies[0]["tools"]}
    assert "read_file" in sent
    assert "edit_file" not in sent and "run_tests" not in sent


def test_tools_stage_fails_when_model_skips_tools() -> None:
    script = Script(_chat("我觉得文件里写着东西"))
    result = _runner("glm-main", script).run_tools()
    assert result.status is SmokeStatus.FAIL
    assert any("没有发起任何工具调用" in note for note in result.notes)


def test_tools_stage_rejects_unregistered_tool_but_keeps_talking() -> None:
    """未注册的工具名必须被拒，但拒绝也要作为 tool result 回传给模型。"""
    script = Script(
        _tool_response("edit_file", json.dumps({"path": "notes.txt"})),
        _chat("那我换一个只读工具"),
    )
    result = _runner("glm-main", script).run_tools()

    assert result.status is SmokeStatus.FAIL
    assert len(script.requests) == 2
    assert "unregistered_tool" in script.bodies[1]["messages"][-1]["content"]


def test_tools_stage_reports_path_escape_without_leaking_content(tmp_path: Path) -> None:
    """模型伪造的越界路径由真实 WorkspacePathPolicy 拒绝。"""
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("越界内容", encoding="utf-8")
    script = Script(
        _tool_response("read_file", json.dumps({"path": str(outside)})),
        _chat("放弃"),
    )
    result = _runner("glm-main", script).run_tools()
    assert result.status is SmokeStatus.FAIL
    assert "越界内容" not in json.dumps(script.bodies, ensure_ascii=False)


def test_tools_stage_survives_multiple_tool_calls_in_one_turn() -> None:
    """一次返回两个 tool_calls：两条结果都要回传，而不是整轮作废。"""
    script = Script(
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "notes.txt"}),
                                    },
                                },
                                {
                                    "id": "call_b",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "missing.txt"}),
                                    },
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 500, "completion_tokens": 20},
            },
        ),
        _chat(f"结果是 {TOOL_MARKER}"),
    )
    result = _runner("glm-main", script).run_tools()

    assert result.status is SmokeStatus.PASS
    ids = [m.get("tool_call_id") for m in script.bodies[1]["messages"] if m["role"] == "tool"]
    assert ids == ["call_a", "call_b"]
    assert script.bodies[1]["messages"][-2]["role"] == "tool"


def test_tools_stage_does_not_replay_reasoning_into_history() -> None:
    """带 reasoning_content 的 tool_calls 响应：思考内容留在本地，不进第二轮请求体。"""
    reasoning = "内部推理不外传"
    first = httpx.Response(
        200,
        json={
            "model": "glm-4.7",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": reasoning,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "notes.txt"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 500, "completion_tokens": 20},
        },
    )
    script = Script(first, _chat(TOOL_MARKER))
    result = _runner("glm-main", script).run_tools()

    assert result.status is SmokeStatus.PASS
    assert reasoning not in json.dumps(script.bodies[1], ensure_ascii=False)


# --------------------------------------------------------------- 授权与拒绝


def test_dry_run_never_constructs_a_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry-run 连 httpx.Client 都不许建：建了就可能已经有 DNS 与连接。"""

    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("dry-run 里不应该构造 HTTP 客户端")

    monkeypatch.setattr("codepilot.llm.registry._default_client", explode)
    report = run_smoke(_config(), alias="glm-main", live=False)

    assert report.total_requests == 0
    assert all(item.status is SmokeStatus.NOT_RUN for item in report.stages)
    assert report.passed is False
    assert report.refusal is None


def test_live_without_cost_ceiling_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("被拒绝的 live 调用不应该发出请求")

    monkeypatch.setattr("codepilot.llm.registry._default_client", explode)
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    report = run_smoke(_config(), alias="glm-main", live=True, max_cost=None)

    assert report.refusal is not None and "--max-cost" in report.refusal
    assert report.total_requests == 0


def test_live_without_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺密钥必须报配置错误，并且一次外网请求都不发。"""
    import codepilot.llm.registry as registry_module

    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("缺密钥时不应该发出请求")

    monkeypatch.setattr(registry_module, "_default_client", explode)
    report = run_smoke(_config(), alias="glm-main", live=True, max_cost=0.05)

    assert report.refusal is not None and GLM_API_KEY_ENV in report.refusal
    assert all(item.status is SmokeStatus.NOT_RUN for item in report.stages)


def test_live_usd_alias_without_fx_rate_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """USD 单价 + CNY 预算 + 无汇率 = 拒绝真实调用，而不是记一个低估约七倍的数。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    report = run_smoke(_config(), alias="glm-5-zai", live=True, max_cost=0.05)
    assert report.refusal is not None and "汇率" in report.refusal


def test_live_with_matching_budget_currency_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """把预算币种改成 USD 就不需要汇率：报表与账单同一种货币。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    script = Script(_chat("收到"))
    _mock_registry_client(monkeypatch, script)

    report = run_smoke(
        _config(),
        alias="glm-5-zai",
        live=True,
        max_cost=0.05,
        stages=[SmokeStage.TEXT],
        budget_currency="USD",
    )
    assert report.refusal is None
    assert report.stages[0].status is SmokeStatus.PASS
    assert report.currency == "USD"


def test_budget_exhaustion_stops_remaining_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """费用触顶后不再派发后续阶段——预算必须对整条冒烟链生效，而不只是单次调用。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    script = Script(_chat("收到", prompt_tokens=1_000_000, completion_tokens=1))
    _mock_registry_client(monkeypatch, script)

    config = _config()
    expensive = config.models["glm-main"].model_copy(
        update={"price": ModelPrice(currency="CNY", input_per_mtok=900.0, output_per_mtok=1.0)}
    )
    config = config.model_copy(update={"models": {**config.models, "glm-main": expensive}})

    report = run_smoke(
        config,
        alias="glm-main",
        live=True,
        max_cost=0.001,
        stages=[SmokeStage.TEXT, SmokeStage.STRUCTURED],
    )
    assert report.stages[0].status is SmokeStatus.PASS
    assert report.stages[1].status is SmokeStatus.NOT_RUN
    assert "上限" in report.stages[1].detail
    assert len(script.requests) == 1, "预算耗尽后不该再发第二个阶段的请求"


def test_unpriced_alias_is_refused_for_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """价格未核实就不允许付费冒烟：UNKNOWN 的费用不能当预算花。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    config = _config()
    naked = config.models["glm-main"].model_copy(update={"price": None})
    config = config.model_copy(update={"models": {**config.models, "glm-main": naked}})

    report = run_smoke(config, alias="glm-main", live=True, max_cost=0.05)
    assert report.refusal is not None and "单价" in report.refusal


# --------------------------------------------------------------- 报告内容


def test_report_json_never_contains_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    script = Script(httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}))
    _mock_registry_client(monkeypatch, script)

    report = run_smoke(
        _config(), alias="glm-main", live=True, max_cost=1.0, stages=[SmokeStage.TEXT]
    )

    blob = json.dumps(report.as_dict(), ensure_ascii=False)
    assert KEY not in blob
    assert report.stages[0].status is SmokeStatus.FAIL
    assert report.cost_known is False, "失败调用的费用不能假装是确定的"


def test_usage_uncertain_is_surfaced_when_requests_are_unacknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时后服务端可能已经生成并计费：报告必须把这件事标成不确定，请求数照实累计。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    script = Script(httpx.ReadTimeout("timed out"), _chat("收到"))
    _mock_registry_client(monkeypatch, script)

    report = run_smoke(
        _config(), alias="glm-main", live=True, max_cost=1.0, stages=[SmokeStage.TEXT]
    )

    assert report.stages[0].status is SmokeStatus.PASS
    assert report.total_requests == 2
    assert report.usage_uncertain is True


def test_stage_selection_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, KEY)
    script = Script(_chat("收到"))
    _mock_registry_client(monkeypatch, script)

    report = run_smoke(
        _config(), alias="glm-main", live=True, max_cost=1.0, stages=[SmokeStage.TEXT]
    )
    assert [item.stage for item in report.stages] == ["text"]


def test_smoke_report_without_stages_is_not_a_pass() -> None:
    """空报告不能算通过：否则"一个阶段都没跑"也会返回 0。"""
    report = SmokeReport(
        provider="p",
        model_alias="a",
        model_id="m",
        base_url="https://x.test/v1",
        currency="CNY",
        live=True,
        stages=(),
    )
    assert report.passed is False
    assert report.total_requests == 0
