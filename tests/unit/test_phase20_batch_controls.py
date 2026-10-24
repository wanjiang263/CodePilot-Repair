from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import l2cal10e_internal as internal  # noqa: E402

from codepilot.eval.assembly import ProviderBundle  # noqa: E402
from codepilot.llm.capabilities import build_request_body, prepare_messages  # noqa: E402
from codepilot.llm.config import load_llm_config  # noqa: E402
from codepilot.llm.types import LLMRequest, Message  # noqa: E402


def test_m3_disables_http_retries_and_sends_temperature_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def live(cls: type[ProviderBundle], config: Any, **kwargs: Any) -> Any:
        captured["config"] = config
        return config

    monkeypatch.setattr(ProviderBundle, "live", classmethod(live))
    monkeypatch.setattr(
        internal.smoke,
        "_load_llm_config",
        lambda _config: load_llm_config(ROOT / "configs/llm.yaml"),
    )
    monkeypatch.setenv("CODEPILOT_DISABLE_HTTP_RETRIES", "1")
    monkeypatch.setenv("CODEPILOT_REQUIRE_TEMPERATURE", "1")
    config = internal._provider_bundle(
        SimpleNamespace(live_llm=True, budget_currency="CNY", record_llm=False)
    )
    assert all(provider.max_retries == 0 for provider in config.providers.values())
    spec = config.models["glm-5.3-flash-main"]
    request = LLMRequest(messages=[Message.user("test")], temperature=0.0)
    body = build_request_body(request, spec, prepare_messages(request, spec))
    assert body["temperature"] == 0.0
    assert "seed" not in body
