"""真实冒烟脚本的结果判定；用假响应验证，不产生实际调用。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from codepilot.llm.config import ModelSpec, ProviderSpec
from codepilot.llm.types import LLMRequest, LLMResponse


@pytest.fixture
def smoke_module() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "llm_smoke.py"
    spec = importlib.util.spec_from_file_location("llm_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("content", "finish_reason", "expected"),
    [("收到", "stop", True), (None, "stop", False), ("  ", "stop", False), ("收", "length", False)],
)
def test_smoke_requires_complete_text_response(
    smoke_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
    finish_reason: str,
    expected: bool,
) -> None:
    response = LLMResponse(
        content=content,
        input_tokens=10,
        output_tokens=2,
        cost=0.0001,
        raw={"choices": [{"finish_reason": finish_reason}]},
    )
    calls: list[LLMRequest] = []
    closed: list[bool] = []

    class StubProvider:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def complete(self, request: LLMRequest) -> LLMResponse:
            calls.append(request)
            return response

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr("codepilot.config.read_secret_env", lambda name: "test-only-key")
    monkeypatch.setattr(smoke_module, "OpenAICompatibleProvider", StubProvider)
    provider = ProviderSpec(name="custom", base_url="https://custom.test/v1", api_key_env="KEY")
    model = ModelSpec(alias="main", provider="custom", model="custom-model")
    assert smoke_module._smoke(provider, model, {"CNY": 1.0}) is expected
    assert closed == [True]
    assert len(calls) == 1 and calls[0].max_output_tokens == 32
    output = capsys.readouterr().out
    assert "按配置估算费用" in output
    assert "test-only-key" not in output
    assert ("✓ 回复" in output) is expected
