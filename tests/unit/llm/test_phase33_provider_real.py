"""Phase33 T01: the --real channel of scripts/phase32_provider_matrix.py.

Every test here is hermetic: the network layer is replaced with
httpx.MockTransport / a fake bedrock client, so ZERO real requests and ZERO
cost are produced. What the fakes prove about the real code path:

1. budget + credentials + model id present -> the production providers
   (OpenAICompatibleProvider / AnthropicMessagesProvider / BedrockConverseProvider)
   are constructed and dispatch EXACTLY ONE request each;
2. any prerequisite missing -> that row (and only that row) is blocked with its
   own missing list and zero dispatched calls;
3. without --real the real rows are always blocked — a passing fake suite can
   never be written as real-verified;
4. a 5xx response is NOT retried (no hidden retry: one wire request);
5. a 200 without complete usage is NOT real-verified;
6. cost comes from the usage ledger + configs price table only; unknown price
   => cost_known=false, never an invented number;
7. the effective budget is min(PHASE32_REAL_BUDGET_CNY, 20) and exhaustion
   blocks the next provider before dispatch.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]

FULL_ENV = {
    "PHASE32_REAL_BUDGET_CNY": "1.0",
    "OPENAI_API_KEY": "test-openai-key",
    "OPENAI_BASE_URL": "https://openai.invalid/v1",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "AWS_ACCESS_KEY_ID": "test-aws-id",
    "AWS_SECRET_ACCESS_KEY": "test-aws-secret",
    "AWS_REGION": "us-east-1",
    "PHASE32_REAL_MODEL_OPENAI": "test-openai-model",
    "PHASE32_REAL_MODEL_ANTHROPIC": "test-anthropic-model",
    "PHASE32_REAL_MODEL_BEDROCK": "test-bedrock-model",
}
ALL_ENV_NAMES = (
    *FULL_ENV,
    "ANTHROPIC_BASE_URL",
    "AWS_DEFAULT_REGION",
    "CODEPILOT_DISPATCH_BUDGET_CNY",
    "CODEPILOT_DISPATCH_LEDGER",
)

OPENAI_JSON = {
    "id": "chatcmpl-test",
    "model": "test-openai-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "收到"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 0},
    },
}
ANTHROPIC_JSON = {
    "id": "msg-test",
    "model": "test-anthropic-model",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "收到"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 2},
}
BEDROCK_PAYLOAD = {
    "output": {"message": {"role": "assistant", "content": [{"text": "收到"}]}},
    "usage": {"inputTokens": 10, "outputTokens": 2},
    "stopReason": "end_turn",
}


@pytest.fixture
def matrix_module() -> types.ModuleType:
    """Load scripts/phase32_provider_matrix.py by path (scripts/ is not a package)."""
    path = ROOT / "scripts" / "phase32_provider_matrix.py"
    spec = importlib.util.spec_from_file_location("phase32_provider_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def stub_boto(monkeypatch: pytest.MonkeyPatch) -> None:
    """bedrock rows require boto3/botocore to be importable; stub them when absent.

    The fake bedrock client is injected, so the stubs are never used to talk to
    AWS — they only satisfy the row_missing() importability prerequisite.
    """
    for name in ("boto3", "botocore"):
        try:
            __import__(name)
        except ImportError:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


def apply_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = dict(FULL_ENV)
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def make_http_factory(
    counts: dict[str, list[httpx.Request]],
    responder: Callable[[str, httpx.Request], httpx.Response] | None = None,
) -> Callable[[Any, str], httpx.Client]:
    """Client factory matching the registry signature (ProviderSpec, api_key) -> Client."""

    def factory(pspec: Any, api_key: str) -> httpx.Client:
        key = pspec.name

        def handler(request: httpx.Request) -> httpx.Response:
            counts.setdefault(key, []).append(request)
            if responder is not None:
                return responder(key, request)
            if key == "anthropic":
                return httpx.Response(200, json=ANTHROPIC_JSON)
            return httpx.Response(200, json=OPENAI_JSON)

        return httpx.Client(transport=httpx.MockTransport(handler), base_url=pspec.base_url)

    return factory


class FakeBedrockClient:
    def __init__(self, payload: Any = None, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = BEDROCK_PAYLOAD if payload is None else payload
        self.exc = exc

    def converse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.payload


def run_channels(
    module: types.ModuleType,
    *,
    http_counts: dict[str, list[httpx.Request]] | None = None,
    responder: Callable[[str, httpx.Request], httpx.Response] | None = None,
    bedrock_client: FakeBedrockClient | None = None,
    llm_config_path: Path | None = None,
    model_overrides: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    counts = http_counts if http_counts is not None else {}
    return module.run_real_channels(
        module.budget(),
        llm_config_path=llm_config_path or module.DEFAULT_LLM_CONFIG,
        model_overrides=model_overrides or {},
        http_client_factory=make_http_factory(counts, responder),
        bedrock_client_factory=(lambda pspec: bedrock_client or FakeBedrockClient()),
    )


def rows_by_provider(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["provider"]: row for row in section["rows"]}


# ---------------------------------------------------------------------------
# 1. real code path: exactly one dispatch per provider when everything is present
# ---------------------------------------------------------------------------


def test_real_channel_dispatches_exactly_one_request_per_provider(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_env(monkeypatch)
    counts: dict[str, list[httpx.Request]] = {}
    bedrock = FakeBedrockClient()
    section = run_channels(matrix_module, http_counts=counts, bedrock_client=bedrock)

    assert section["real_requests"] == 3
    assert len(counts["openai-compatible"]) == 1
    assert len(counts["anthropic"]) == 1
    assert len(bedrock.calls) == 1
    rows = rows_by_provider(section)
    for key in ("openai-compatible", "anthropic", "bedrock"):
        assert rows[key]["status"] == "real-verified", rows[key]
        assert rows[key]["real_requests"] == 1
        assert rows[key]["missing"] == []
        assert rows[key]["input_tokens"] == 10
        assert rows[key]["output_tokens"] == 2
    # wire paths prove the production providers were used
    assert counts["openai-compatible"][0].url.path == "/v1/chat/completions"
    assert counts["anthropic"][0].url.path == "/v1/messages"
    # the probe models have no price in configs/llm.yaml -> cost stays unknown,
    # never invented
    assert section["real_cost_cny"] == 0.0
    assert section["cost_known"] is False
    assert section["cost_is_lower_bound"] is True
    for key in ("openai-compatible", "anthropic", "bedrock"):
        assert rows[key]["cost_known"] is False
        assert rows[key]["cost_cny"] == 0.0
    assert section["llm_calls"] == 3


# ---------------------------------------------------------------------------
# 2. default mode: fake results are NEVER written as real-verified
# ---------------------------------------------------------------------------


def test_default_mode_blocks_every_real_row_even_with_full_env(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_env(monkeypatch)
    section = matrix_module.blocked_real_provider(
        matrix_module.budget(), model_overrides={}, real_requested=False
    )
    assert section["real_requests"] == 0
    assert section["real_cost_cny"] == 0.0
    assert len(section["rows"]) == 3
    for row in section["rows"]:
        assert row["status"] == "blocked"
        assert row["status"] != "real-verified"
        assert row["real_requests"] == 0
        assert row["missing"][0].startswith("--real flag")


def test_real_flag_defaults_to_false(matrix_module: types.ModuleType) -> None:
    args = matrix_module.parse_args([])
    assert args.real is False
    assert matrix_module.parse_args(["--real"]).real is True


# ---------------------------------------------------------------------------
# 3. missing prerequisites: per-row blocked with zero calls
# ---------------------------------------------------------------------------


def test_missing_budget_blocks_all_rows_with_zero_calls(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {k: v for k, v in FULL_ENV.items() if k != "PHASE32_REAL_BUDGET_CNY"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no request may be dispatched without budget")

    section = matrix_module.run_real_channels(
        matrix_module.budget(),
        llm_config_path=matrix_module.DEFAULT_LLM_CONFIG,
        model_overrides={},
        http_client_factory=explode,
        bedrock_client_factory=explode,
    )
    assert section["real_requests"] == 0
    assert section["llm_calls"] == 0
    for row in section["rows"]:
        assert row["status"] == "blocked"
        assert "PHASE32_REAL_BUDGET_CNY > 0" in row["missing"]


def test_missing_credentials_block_only_their_own_row(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # budget + anthropic only; openai and bedrock credentials/model ids absent
    for key in (
        "PHASE32_REAL_BUDGET_CNY",
        "ANTHROPIC_API_KEY",
        "PHASE32_REAL_MODEL_ANTHROPIC",
    ):
        monkeypatch.setenv(key, FULL_ENV[key])
    counts: dict[str, list[httpx.Request]] = {}
    section = run_channels(matrix_module, http_counts=counts)

    rows = rows_by_provider(section)
    assert rows["anthropic"]["status"] == "real-verified"
    assert rows["anthropic"]["missing"] == []
    assert len(counts["anthropic"]) == 1
    assert "openai-compatible" not in counts

    assert rows["openai-compatible"]["status"] == "blocked"
    assert rows["openai-compatible"]["real_requests"] == 0
    openai_missing = rows["openai-compatible"]["missing"]
    assert "OPENAI_API_KEY" in openai_missing
    assert "OPENAI_BASE_URL" in openai_missing
    assert "PHASE32_REAL_MODEL_OPENAI" in openai_missing
    assert not any("ANTHROPIC" in item for item in openai_missing)
    assert not any("AWS" in item for item in openai_missing)

    assert rows["bedrock"]["status"] == "blocked"
    assert rows["bedrock"]["real_requests"] == 0
    bedrock_missing = rows["bedrock"]["missing"]
    assert "AWS_ACCESS_KEY_ID" in bedrock_missing
    assert "AWS_SECRET_ACCESS_KEY" in bedrock_missing
    assert "AWS_REGION or AWS_DEFAULT_REGION" in bedrock_missing
    assert "PHASE32_REAL_MODEL_BEDROCK" in bedrock_missing
    assert not any("OPENAI" in item for item in bedrock_missing)

    # per-row lists are independent objects, not one shared list (the D1 defect)
    assert openai_missing is not bedrock_missing
    assert openai_missing != bedrock_missing
    assert section["real_requests"] == 1


def test_blocked_rows_have_independent_missing_lists(
    matrix_module: types.ModuleType,
) -> None:
    # nothing in the environment at all
    section = matrix_module.blocked_real_provider(
        matrix_module.budget(), model_overrides={}, real_requested=True
    )
    rows = section["rows"]
    assert len(rows) == 3
    lists = [row["missing"] for row in rows]
    assert lists[0] is not lists[1] and lists[1] is not lists[2]
    assert len(set(map(tuple, lists))) == 3
    for missing in lists:
        assert "PHASE32_REAL_BUDGET_CNY > 0" in missing
    assert any(item == "OPENAI_API_KEY" for item in lists[0])
    assert any(item == "ANTHROPIC_API_KEY" for item in lists[1])
    assert any(item == "AWS_ACCESS_KEY_ID" for item in lists[2])


# ---------------------------------------------------------------------------
# 4. failure honesty: no hidden retry, no verified-without-usage
# ---------------------------------------------------------------------------


def test_retryable_5xx_dispatches_exactly_one_request(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "PHASE32_REAL_BUDGET_CNY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "PHASE32_REAL_MODEL_OPENAI",
    ):
        monkeypatch.setenv(key, FULL_ENV[key])
    counts: dict[str, list[httpx.Request]] = {}

    def responder(key: str, request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    section = run_channels(matrix_module, http_counts=counts, responder=responder)
    row = rows_by_provider(section)["openai-compatible"]
    assert row["status"] == "real-failed"
    assert row["real_requests"] == 1
    assert len(counts["openai-compatible"]) == 1  # 503 is retryable — and NOT retried
    assert row["error"]["status"] == 503
    assert row["cost_known"] is False


def test_zero_usage_response_is_not_real_verified(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "PHASE32_REAL_BUDGET_CNY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "PHASE32_REAL_MODEL_OPENAI",
    ):
        monkeypatch.setenv(key, FULL_ENV[key])
    zero_usage = dict(OPENAI_JSON)
    zero_usage["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}

    def responder(key: str, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=zero_usage)

    section = run_channels(matrix_module, responder=responder)
    row = rows_by_provider(section)["openai-compatible"]
    assert row["status"] == "real-failed"
    assert row["status"] != "real-verified"
    assert row["real_requests"] == 1
    assert "usage" in row["failure_reason"]


def test_bedrock_error_is_recorded_with_status(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "PHASE32_REAL_BUDGET_CNY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "PHASE32_REAL_MODEL_BEDROCK",
    ):
        monkeypatch.setenv(key, FULL_ENV[key])

    class AccessDenied(Exception):  # noqa: N818 - 模拟 botocore errorfactory 的真实异常类名
        def __init__(self) -> None:
            super().__init__("AccessDeniedException")
            self.response = {
                "Error": {"Code": "AccessDeniedException"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            }

    bedrock = FakeBedrockClient(exc=AccessDenied())
    section = run_channels(matrix_module, bedrock_client=bedrock)
    row = rows_by_provider(section)["bedrock"]
    assert row["status"] == "real-failed"
    assert row["real_requests"] == 1
    assert len(bedrock.calls) == 1
    assert row["error"]["status"] == 403
    assert section["real_requests"] == 1


# ---------------------------------------------------------------------------
# 5. money: ledger + price table only, capped budget enforced before dispatch
# ---------------------------------------------------------------------------


PRICED_CONFIG = """providers:
  probe-openai:
    base_url: https://openai.invalid/v1
    protocol: openai_compatible
  probe-anthropic:
    base_url: https://api.anthropic.com
    protocol: anthropic_messages
models:
  probe-openai-model:
    provider: probe-openai
    model: test-openai-model
    price:
      currency: CNY
      input_per_mtok: 1000.0
      output_per_mtok: 2000.0
  probe-anthropic-model:
    provider: probe-anthropic
    model: test-anthropic-model
    price:
      currency: CNY
      input_per_mtok: 1000.0
      output_per_mtok: 2000.0
roles:
  coder: probe-openai-model
exchange_rates:
  CNY: 1.0
"""


def write_priced_config(tmp_path: Path) -> Path:
    path = tmp_path / "llm.yaml"
    path.write_text(PRICED_CONFIG, encoding="utf-8")
    return path


def test_known_price_cost_comes_from_usage_ledger(
    matrix_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key in (
        "PHASE32_REAL_BUDGET_CNY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "PHASE32_REAL_MODEL_OPENAI",
    ):
        monkeypatch.setenv(key, FULL_ENV[key])
    config = write_priced_config(tmp_path)
    counts: dict[str, list[httpx.Request]] = {}
    section = run_channels(matrix_module, http_counts=counts, llm_config_path=config)
    row = rows_by_provider(section)["openai-compatible"]
    assert row["status"] == "real-verified"
    # 10 input * 1000 CNY/Mtok + 2 output * 2000 CNY/Mtok = 0.014 CNY
    assert row["cost_cny"] == pytest.approx(0.014)
    assert row["cost_known"] is True
    assert section["real_cost_cny"] == pytest.approx(0.014)
    assert section["cost_known"] is True
    assert section["cost_is_lower_bound"] is False


def test_exhausted_budget_blocks_next_provider_before_dispatch(
    matrix_module: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    apply_env(monkeypatch, PHASE32_REAL_BUDGET_CNY="0.014")
    # drop bedrock so the sequence is openai (spends 0.014) -> anthropic (blocked)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.delenv("PHASE32_REAL_MODEL_BEDROCK")
    config = write_priced_config(tmp_path)
    counts: dict[str, list[httpx.Request]] = {}
    section = run_channels(matrix_module, http_counts=counts, llm_config_path=config)
    rows = rows_by_provider(section)
    assert rows["openai-compatible"]["status"] == "real-verified"
    assert rows["anthropic"]["status"] == "blocked"
    assert rows["anthropic"]["real_requests"] == 0
    assert any("exhausted" in item for item in rows["anthropic"]["missing"])
    assert "anthropic" not in counts
    assert section["real_requests"] == 1
    assert section["real_cost_cny"] == pytest.approx(0.014)


def test_budget_is_capped_at_20_cny(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHASE32_REAL_BUDGET_CNY", "50")
    plan = matrix_module.budget()
    assert plan["budget_cny"] == 50.0
    assert plan["cap_cny"] == 20.0
    assert plan["effective_budget_cny"] == 20.0
    assert plan["capped"] is True
    assert plan["real_requests_allowed"] is True
    # the only budget variable in the script remains PHASE32_REAL_BUDGET_CNY
    assert matrix_module.BUDGET_ENV == "PHASE32_REAL_BUDGET_CNY"
    assert matrix_module.BUDGET_CAP_CNY == 20.0


# ---------------------------------------------------------------------------
# 6. Phase34 T03: control flow propagates; the presence map feeds the gate
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_is_not_swallowed(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in (
        "PHASE32_REAL_BUDGET_CNY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "PHASE32_REAL_MODEL_BEDROCK",
    ):
        monkeypatch.setenv(key, FULL_ENV[key])

    class InterruptingBedrock(FakeBedrockClient):
        def converse(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_channels(matrix_module, bedrock_client=InterruptingBedrock())


def test_row_gate_consumes_credentials_present(
    matrix_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_env(monkeypatch)  # the environment itself claims every credential exists
    names = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )
    absent = dict.fromkeys(names, False)
    absent.update({"boto3_installed": False, "botocore_installed": False})
    monkeypatch.setattr(matrix_module, "credentials_present", lambda: dict(absent))

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credentials map says absent; nothing may dispatch")

    section = matrix_module.run_real_channels(
        matrix_module.budget(),
        llm_config_path=matrix_module.DEFAULT_LLM_CONFIG,
        model_overrides={},
        http_client_factory=explode,
        bedrock_client_factory=explode,
    )
    assert section["real_requests"] == 0
    assert section["real_cost_cny"] == 0.0
    rows = rows_by_provider(section)
    for row in rows.values():
        assert row["status"] == "blocked"
        assert row["real_requests"] == 0
    assert "OPENAI_API_KEY" in rows["openai-compatible"]["missing"]
    assert "ANTHROPIC_API_KEY" in rows["anthropic"]["missing"]
    assert "AWS_ACCESS_KEY_ID" in rows["bedrock"]["missing"]
    assert "boto3 importable" in rows["bedrock"]["missing"]
