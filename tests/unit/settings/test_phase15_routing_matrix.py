"""Phase 15 routing matrix: defaults, overrides, protocols and fail-closed edges."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from codepilot.llm.anthropic_messages import AnthropicMessagesProvider
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.registry import LLMMode, LLMRegistry
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.llm.types import LLMRequest, Message
from codepilot.settings.credentials import CredentialVault
from codepilot.settings.models import (
    CredentialRef,
    ModelConfig,
    ModelPriceConfig,
    ProviderConfig,
)
from codepilot.settings.service import SettingsError, SettingsService
from codepilot.settings.store import SettingsStore


def _model(model_id: str, **overrides: Any) -> ModelConfig:
    base: dict[str, Any] = {
        "model_id": model_id,
        "display_name": model_id,
        "context_window": 32768,
        "max_output_tokens": 4096,
        "supports_tools": True,
        "supports_json_mode": True,
        "supports_temperature": True,
        "price": ModelPriceConfig(currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0),
    }
    base.update(overrides)
    return ModelConfig(**base)


def _provider(
    provider_id: str,
    models: list[ModelConfig],
    **overrides: Any,
) -> ProviderConfig:
    base: dict[str, Any] = {
        "provider_id": provider_id,
        "name": provider_id.title(),
        "protocol": "openai_compatible",
        "base_url": f"https://{provider_id}.test/v1",
        "credential": CredentialRef(kind="env", name="PHASE15_ROUTING_KEY"),
        "models": models,
    }
    base.update(overrides)
    return ProviderConfig(**base)


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SettingsService:
    monkeypatch.setenv("PHASE15_ROUTING_KEY", "sk-routing-secret")
    return SettingsService(
        SettingsStore(tmp_path / "settings.sqlite3"),
        CredentialVault(tmp_path / "settings"),
    )


def _leaf(provider: object) -> object:
    node = provider
    while isinstance(node, UsageTrackingProvider):
        node = node.inner
    return node


def _transport_factory(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"routing test must not dispatch HTTP: {request.url}")


def test_global_default_role_and_task_overrides_reach_the_live_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    document = service.create_provider(
        _provider("alpha", [_model("alpha-fast"), _model("alpha-strong")]),
        expected_version=0,
    )
    document = service.create_provider(
        _provider("beta", [_model("beta-only")]),
        expected_version=document.version,
    )
    document = service.update_section(
        "agents",
        {
            "default_provider_id": "alpha",
            "default_model_id": "alpha-fast",
            "roles": {
                "planner": {
                    "provider_id": "beta",
                    "model_id": "beta-only",
                    "reasoning": "inherit",
                },
                "coder": {
                    "provider_id": "alpha",
                    "model_id": "alpha-strong",
                    "reasoning": "inherit",
                },
            },
        },
        expected_version=document.version,
    )

    snapshot = service.build_live_snapshot()
    assert snapshot.roles["localizer"].provider_id == "alpha"
    assert snapshot.roles["localizer"].model_id == "alpha-fast"
    assert snapshot.roles["planner"].provider_id == "beta"
    assert snapshot.roles["planner"].model_id == "beta-only"
    assert snapshot.roles["coder"].provider_id == "alpha"
    assert snapshot.roles["coder"].model_id == "alpha-strong"
    assert snapshot.roles["reviewer"].model_id == "alpha-fast"
    assert snapshot.config_hash

    config = service.build_llm_config(snapshot)
    assert config.resolve_role("localizer").provider == "alpha"
    assert config.resolve_role("localizer").model == "alpha-fast"
    assert config.resolve_role("planner").provider == "beta"
    assert config.resolve_role("planner").model == "beta-only"
    assert config.resolve_role("coder").model == "alpha-strong"
    assert config.providers["alpha"].protocol == "openai_compatible"

    # A task-level selection overrides every role, but it must not rewrite the
    # previously sealed snapshot or the persisted settings.
    override = service.build_live_snapshot(provider_id="beta", model_id="beta-only")
    assert all(
        selection.provider_id == "beta" and selection.model_id == "beta-only"
        for selection in override.roles.values()
    )
    assert override.config_hash != snapshot.config_hash
    assert snapshot.roles["localizer"].model_id == "alpha-fast"
    override_config = service.build_llm_config(override)
    assert {override_config.resolve_role(role).model for role in override.roles} == {"beta-only"}

    changed = service.update_section(
        "agents",
        {
            "default_provider_id": "alpha",
            "default_model_id": "alpha-strong",
            "roles": {},
        },
        expected_version=service.document().version,
    )
    assert changed.version > document.version
    # Settings changes after sealing cannot alter what this task will dispatch.
    assert snapshot.roles["planner"].model_id == "beta-only"
    assert service.build_llm_config(snapshot).resolve_role("planner").model == "beta-only"

    # Removing the environment value is a resolver failure, not a silent fallback.
    monkeypatch.delenv("PHASE15_ROUTING_KEY", raising=False)
    with pytest.raises(SettingsError) as missing_value:
        service.build_llm_config(snapshot)
    assert missing_value.value.code == "credential_missing"
    monkeypatch.setenv("PHASE15_ROUTING_KEY", "sk-routing-secret")

    # Revoking the credential referenced by the frozen snapshot must fail closed
    # even though the old environment value is still present.
    service.delete_credential("alpha", expected_version=service.document().version)
    with pytest.raises(SettingsError) as caught:
        service.build_llm_config(snapshot)
    assert caught.value.code == "credential_missing"


def test_protocol_selection_comes_from_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    document = service.create_provider(
        _provider("alpha", [_model("alpha-model")]),
        expected_version=0,
    )
    document = service.create_provider(
        _provider(
            "claude",
            [_model("claude-model")],
            protocol="anthropic_messages",
            base_url="https://claude.test",
        ),
        expected_version=document.version,
    )
    service.update_section(
        "agents",
        {
            "default_provider_id": "alpha",
            "default_model_id": "alpha-model",
            "roles": {
                "reviewer": {
                    "provider_id": "claude",
                    "model_id": "claude-model",
                    "reasoning": "inherit",
                }
            },
        },
        expected_version=document.version,
    )
    config = service.build_llm_config(service.build_live_snapshot())
    assert config.providers["alpha"].protocol == "openai_compatible"
    assert config.providers["claude"].protocol == "anthropic_messages"

    registry = LLMRegistry(
        config,
        mode=LLMMode.LIVE,
        client_factory=lambda _spec, _key: httpx.Client(
            transport=httpx.MockTransport(_transport_factory)
        ),
    )
    try:
        assert isinstance(_leaf(registry.for_role("localizer")), OpenAICompatibleProvider)
        assert isinstance(_leaf(registry.for_role("reviewer")), AnthropicMessagesProvider)
    finally:
        registry.close()


class _FakeBedrockClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "bedrock route works"}],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 12, "outputTokens": 3},
        }

    def close(self) -> None:
        return None


def test_bedrock_snapshot_uses_the_deterministic_fake_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "codepilot.settings.service._protocol_available",
        lambda protocol: (
            protocol in {"openai_compatible", "anthropic_messages", "bedrock_converse"}
        ),
    )
    service = SettingsService(
        SettingsStore(tmp_path / "settings.sqlite3"),
        CredentialVault(tmp_path / "settings"),
    )
    document = service.create_provider(
        _provider(
            "bedrock",
            [_model("anthropic.claude-test")],
            protocol="bedrock_converse",
            base_url="",
            region="us-east-1",
            credential=CredentialRef(),
        ),
        expected_version=0,
    )
    service.update_section(
        "agents",
        {
            "default_provider_id": "bedrock",
            "default_model_id": "anthropic.claude-test",
            "roles": {},
        },
        expected_version=document.version,
    )
    snapshot = service.build_live_snapshot()
    assert snapshot.providers["bedrock"].protocol == "bedrock_converse"
    assert snapshot.providers["bedrock"].region == "us-east-1"
    config = service.build_llm_config(snapshot)
    assert config.resolve_role("planner").provider == "bedrock"

    client = _FakeBedrockClient()
    registry = LLMRegistry(
        config,
        mode=LLMMode.LIVE,
        bedrock_client_factory=lambda _spec: client,
    )
    try:
        response = registry.for_role("planner").complete(
            LLMRequest(messages=[Message.user("say ok")], max_output_tokens=32)
        )
    finally:
        registry.close()
    assert response.content == "bedrock route works"
    assert client.calls and client.calls[0]["modelId"] == "anthropic.claude-test"
    assert registry.usage.llm_calls == 1
    assert registry.usage.cost > 0
    assert sorted(registry.usage.by_role) == ["planner"]


def test_disabled_provider_keeps_config_completeness_separate_from_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    document = service.create_provider(
        _provider("alpha", [_model("alpha-model")]),
        expected_version=0,
    )
    disabled = service.update_provider(
        "alpha",
        _provider("alpha", [_model("alpha-model")], enabled=False),
        expected_version=document.version,
    )
    assert disabled.version > document.version
    model_view = service.provider_view(service.document().provider("alpha"))["models"][0]
    assert model_view["verification"]["config_complete"] is True
    assert model_view["verification"]["missing"] == []
    assert model_view["verification"]["routing"]["eligible"] is False
    assert "provider_disabled" in model_view["verification"]["routing"]["blockers"]


def test_missing_price_and_missing_model_fail_before_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    document = service.create_provider(
        _provider("alpha", [_model("alpha-model")]),
        expected_version=0,
    )
    service.update_section(
        "agents",
        {
            "default_provider_id": "alpha",
            "default_model_id": "alpha-model",
            "roles": {},
        },
        expected_version=document.version,
    )
    broken = service.upsert_model(
        "alpha",
        _model("alpha-model", price=ModelPriceConfig(currency="CNY")),
        original_model_id="alpha-model",
        expected_version=service.document().version,
    )
    assert broken.version > document.version
    with pytest.raises(SettingsError) as caught:
        service.build_live_snapshot()
    assert "price_missing" in str(caught.value)

    # Unavailable protocols fail before any HTTP client is built.
    missing = SettingsService(
        SettingsStore(tmp_path / "other.sqlite3"),
        CredentialVault(tmp_path / "other"),
    )
    missing.create_provider(
        _provider("alpha", [_model("alpha-model")]),
        expected_version=0,
    )
    missing.update_section(
        "agents",
        {
            "default_provider_id": "alpha",
            "default_model_id": "alpha-model",
            "roles": {},
        },
        expected_version=1,
    )
    monkeypatch.setattr("codepilot.settings.service._protocol_available", lambda _protocol: False)
    with pytest.raises(SettingsError) as unavailable:
        missing.build_live_snapshot()
    assert unavailable.value.code == "protocol_unavailable"
