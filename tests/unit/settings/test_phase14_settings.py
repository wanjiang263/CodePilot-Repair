"""Phase 14 settings, credentials, routing snapshots and SSRF controls."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from codepilot.settings.catalog import catalog_templates, template
from codepilot.settings.credentials import CredentialVault
from codepilot.settings.models import (
    CredentialRef,
    ModelConfig,
    ModelPriceConfig,
    ProviderConfig,
)
from codepilot.settings.security import (
    ProviderSecurityError,
    ResponseTooLargeError,
    SecureJSONClient,
    resolve_public_address,
    validate_provider_base_url,
)
from codepilot.settings.service import SettingsError, SettingsService
from codepilot.settings.store import SettingsStore, VersionConflictError


def _model(**overrides: Any) -> ModelConfig:
    base: dict[str, Any] = {
        "model_id": "test-model",
        "display_name": "Test Model",
        "context_window": 32768,
        "max_output_tokens": 4096,
        "supports_tools": True,
        "supports_json_mode": True,
        "supports_temperature": True,
        "price": ModelPriceConfig(currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0),
    }
    base.update(overrides)
    return ModelConfig(**base)


def _provider(provider_id: str = "mock", **overrides: Any) -> ProviderConfig:
    base: dict[str, Any] = {
        "provider_id": provider_id,
        "name": "Mock",
        "protocol": "openai_compatible",
        "base_url": "https://mock.example/v1",
        "credential": CredentialRef(kind="env", name="MOCK_API_KEY"),
        "models": [_model()],
    }
    base.update(overrides)
    return ProviderConfig(**base)


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SettingsService:
    monkeypatch.setenv("MOCK_API_KEY", "sk-test-secret-value")
    store = SettingsStore(tmp_path / "settings.sqlite3")
    vault = CredentialVault(tmp_path / "settings")
    return SettingsService(store, vault)


def test_catalog_marks_unverified_models_without_inventing_prices() -> None:
    names = {item.catalog_id for item in catalog_templates()}
    assert {
        "openai",
        "anthropic",
        "deepseek",
        "bigmodel",
        "moonshot",
        "qwen",
        "bedrock",
        "custom",
    } <= names
    openai = template("openai")
    assert openai.models
    assert all(not item.price.complete for item in openai.models)
    deepseek = template("deepseek")
    assert all(item.price.complete for item in deepseek.models)
    bedrock = template("bedrock")
    assert bedrock.protocol == "bedrock_converse"
    import importlib.util

    assert bedrock.available is (importlib.util.find_spec("boto3") is not None)


def test_provider_crud_and_version_conflict(service: SettingsService) -> None:
    document = service.create_provider(_provider(), expected_version=0)
    assert document.version == 1
    assert document.providers["mock"].name == "Mock"
    with pytest.raises(VersionConflictError):
        service.create_provider(_provider("other"), expected_version=0)

    updated = _provider()
    updated = updated.model_copy(update={"name": "Renamed"})
    document = service.update_provider("mock", updated, expected_version=1)
    assert document.providers["mock"].name == "Renamed"
    assert document.version == 2

    document = service.delete_provider("mock", expected_version=2)
    assert "mock" not in document.providers


def test_credential_vault_never_returns_plaintext(service: SettingsService) -> None:
    service.create_provider(_provider(credential=CredentialRef()), expected_version=0)
    document = service.set_credential(
        "mock", mode="value", value="sk-vault-secret-123", env_name=None, expected_version=1
    )
    provider = document.providers["mock"]
    assert provider.credential.kind == "vault"
    assert provider.credential.name is not None
    view = service.provider_view(provider)
    assert view["credential"]["configured"] is True
    assert view["credential"]["available"] is True
    assert "sk-vault-secret-123" not in json.dumps(view)
    credential_files = list((service.vault.credentials_dir).glob("*.cred"))
    assert len(credential_files) == 1
    assert b"sk-vault-secret-123" not in credential_files[0].read_bytes()

    document = service.delete_credential("mock", expected_version=document.version)
    assert document.providers["mock"].credential.kind == "none"
    assert not credential_files[0].exists()


def test_export_contains_references_not_secrets(service: SettingsService) -> None:
    service.create_provider(_provider(), expected_version=0)
    service.set_credential(
        "mock", mode="value", value="sk-export-secret", env_name=None, expected_version=1
    )
    exported = service.export_config()
    text = json.dumps(exported)
    assert "sk-export-secret" not in text
    assert exported["providers"]["mock"]["credential"]["kind"] == "vault"


def test_snapshot_freezes_models_and_never_contains_credentials(
    service: SettingsService,
) -> None:
    document = service.create_provider(_provider(), expected_version=0)
    document = service.update_section(
        "agents",
        {
            "default_provider_id": "mock",
            "default_model_id": "test-model",
            "roles": {
                "planner": {
                    "provider_id": "mock",
                    "model_id": "test-model",
                    "reasoning": "inherit",
                }
            },
        },
        expected_version=document.version,
    )
    snapshot = service.build_live_snapshot()
    assert snapshot.mode == "live"
    assert snapshot.config_hash
    assert set(snapshot.roles) == {
        "localizer",
        "planner",
        "coder",
        "reproducer",
        "diagnoser",
        "reviewer",
    }
    dumped = snapshot.model_dump_json()
    assert "sk-test-secret-value" not in dumped
    assert snapshot.providers["mock"].credential_kind == "env"

    config = service.build_llm_config(snapshot)
    assert config.resolve_role("coder").model == "test-model"
    assert config.providers["mock"].api_key() == "sk-test-secret-value"
    assert "sk-test-secret-value" not in config.providers["mock"].model_dump_json()


def test_live_admission_fails_closed_without_price(service: SettingsService) -> None:
    document = service.create_provider(
        _provider(models=[_model(price=ModelPriceConfig(currency="CNY"))]),
        expected_version=0,
    )
    document = service.update_section(
        "agents",
        {"default_provider_id": "mock", "default_model_id": "test-model", "roles": {}},
        expected_version=document.version,
    )
    with pytest.raises(SettingsError) as caught:
        service.build_live_snapshot()
    assert caught.value.code == "model_specs_incomplete"


def test_live_admission_rejects_unconvertible_currency(service: SettingsService) -> None:
    document = service.create_provider(
        _provider(
            models=[
                _model(
                    price=ModelPriceConfig(currency="USD", input_per_mtok=1.0, output_per_mtok=2.0)
                )
            ]
        ),
        expected_version=0,
    )
    service.update_section(
        "agents",
        {"default_provider_id": "mock", "default_model_id": "test-model", "roles": {}},
        expected_version=document.version,
    )
    with pytest.raises(SettingsError) as caught:
        service.build_live_snapshot()
    assert caught.value.code == "currency_rate_missing"


def test_role_routes_override_default(service: SettingsService) -> None:
    document = service.create_provider(_provider("one"), expected_version=0)
    document = service.create_provider(
        _provider("two", models=[_model(model_id="other-model")]),
        expected_version=document.version,
    )
    document = service.update_section(
        "agents",
        {
            "default_provider_id": "one",
            "default_model_id": "test-model",
            "roles": {
                "coder": {
                    "provider_id": "two",
                    "model_id": "other-model",
                    "reasoning": "inherit",
                }
            },
        },
        expected_version=document.version,
    )
    snapshot = service.build_live_snapshot()
    assert snapshot.roles["coder"].provider_id == "two"
    assert snapshot.roles["planner"].provider_id == "one"


def test_invalid_custom_base_urls_are_rejected() -> None:
    for url in (
        "http://api.example.com/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.8/v1",
        "https://api.example.com/v1?secret=x",
    ):
        with pytest.raises(ProviderSecurityError):
            validate_provider_base_url(url)
    assert validate_provider_base_url("https://api.example.com/v1") == "https://api.example.com/v1"


def test_resolver_rejects_private_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))],
    )
    with pytest.raises(ProviderSecurityError):
        resolve_public_address("internal.example.com")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert resolve_public_address("example.com") == "93.184.216.34"


def test_secure_client_rejects_redirects_and_oversized_responses() -> None:
    redirect = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://metadata.internal/"})
    )
    with SecureJSONClient(transport=redirect) as client, pytest.raises(ProviderSecurityError):
        client.request_json("GET", "https://api.example.com/v1/models")
    huge = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 200_000))
    with (
        SecureJSONClient(transport=huge, max_bytes=1024) as client,
        pytest.raises(ResponseTooLargeError),
    ):
        client.request_json("GET", "https://api.example.com/v1/models")


def test_connection_tests_use_provider_credential_and_discriminate_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_API_KEY", "sk-connection-secret")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )

    service = SettingsService(
        SettingsStore(tmp_path / "settings.sqlite3"),
        CredentialVault(tmp_path / "settings"),
        client_transport=httpx.MockTransport(handler),
    )
    service.create_provider(_provider(), expected_version=0)
    config_result = service.test_connection("mock", kind="config")
    assert config_result["ok"] is True
    assert seen == []
    models_result = service.test_connection("mock", kind="models")
    assert models_result["ok"] is True
    assert models_result["models"] == ["test-model"]
    with pytest.raises(SettingsError) as caught:
        service.test_connection("mock", kind="inference", model_id="test-model")
    assert caught.value.code == "cost_confirmation_required"
    inference = service.test_connection(
        "mock", kind="inference", model_id="test-model", confirm_cost=True
    )
    assert inference["ok"] is True
    assert inference["authenticated"] is True
    assert inference["tool_calling"] == "declared_not_verified"
    assert inference["usage"]["output_tokens"] == 1
    assert "sk-connection-secret" not in json.dumps(inference)
    assert all(
        request.headers.get("authorization") == "Bearer sk-connection-secret" for request in seen
    )
    posts = [request for request in seen if request.method == "POST"]
    assert posts and all(request.headers.get("x-codepilot-purpose") for request in posts)


def test_verification_status_distinguishes_routing_from_real_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOCK_API_KEY", "sk-connection-secret")
    fail = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        if fail:
            return httpx.Response(
                401, json={"error": {"message": "invalid key sk-connection-secret"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )

    service = SettingsService(
        SettingsStore(tmp_path / "settings.sqlite3"),
        CredentialVault(tmp_path / "settings"),
        client_transport=httpx.MockTransport(handler),
    )
    service.create_provider(_provider(), expected_version=0)

    view = service.provider_view(service.document().provider("mock"))
    model_view = view["models"][0]
    assert view["verification"]["routing_eligible"] == 1
    assert view["verification"]["inference_verified"] == 0
    assert model_view["verification"]["routing"]["eligible"] is True
    assert model_view["verification"]["inference"]["state"] == "not_tested"

    service.test_connection("mock", kind="config")
    view = service.provider_view(service.document().provider("mock"))
    assert view["verification"]["config_test"]["state"] == "ok"
    # A local config check must never be presented as a real inference record.
    assert view["verification"]["inference_verified"] == 0
    assert view["models"][0]["verification"]["inference"]["state"] == "not_tested"

    service.test_connection("mock", kind="models")
    view = service.provider_view(service.document().provider("mock"))
    assert view["verification"]["models_test"]["state"] == "ok"

    inference = service.test_connection(
        "mock", kind="inference", model_id="test-model", confirm_cost=True
    )
    assert inference["ok"] is True and inference["checked_at"]
    view = service.provider_view(service.document().provider("mock"))
    assert view["verification"]["inference_verified"] == 1
    status = view["models"][0]["verification"]["inference"]
    assert status["state"] == "verified"
    assert status["checked_at"] == inference["checked_at"]
    assert status["tool_calling"] == "declared_not_verified"

    # Editing the model invalidates the old evidence instead of silently reusing it.
    changed = _model(display_name="Changed")
    service.upsert_model(
        "mock", changed, original_model_id="test-model", expected_version=service.document().version
    )
    view = service.provider_view(service.document().provider("mock"))
    assert view["models"][0]["verification"]["inference"]["state"] == "stale"
    assert view["verification"]["inference_verified"] == 0

    # A failed real inference is a failure, not "verified" or "not tested".
    fail = True
    result = service.test_connection(
        "mock", kind="inference", model_id="test-model", confirm_cost=True
    )
    assert result["ok"] is False
    view = service.provider_view(service.document().provider("mock"))
    status = view["models"][0]["verification"]["inference"]
    assert status["state"] == "failed"
    assert "sk-connection-secret" not in json.dumps(view)


def test_routing_blockers_cover_credentials_capacity_and_currency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOCK_API_KEY", raising=False)
    service = SettingsService(
        SettingsStore(tmp_path / "settings.sqlite3"),
        CredentialVault(tmp_path / "settings"),
    )
    service.create_provider(_provider(), expected_version=0)
    view = service.provider_view(service.document().provider("mock"))
    model_view = view["models"][0]
    assert model_view["routing_ready"] is True
    assert model_view["verification"]["routing"]["eligible"] is False
    assert "credential_missing" in model_view["verification"]["routing"]["blockers"]

    monkeypatch.setenv("MOCK_API_KEY", "sk-test-secret-value")
    service.upsert_model(
        "mock",
        _model(
            context_window=4096,
            price=ModelPriceConfig(currency="USD", input_per_mtok=1, output_per_mtok=2),
        ),
        original_model_id="test-model",
        expected_version=service.document().version,
    )
    model_view = service.provider_view(service.document().provider("mock"))["models"][0]
    blockers = model_view["verification"]["routing"]["blockers"]
    assert "model_capacity_too_small" in blockers
    assert "currency_rate_missing" in blockers


def test_provider_deletion_is_blocked_while_routed(service: SettingsService) -> None:
    document = service.create_provider(_provider(), expected_version=0)
    document = service.update_section(
        "agents",
        {"default_provider_id": "mock", "default_model_id": "test-model", "roles": {}},
        expected_version=document.version,
    )
    with pytest.raises(SettingsError) as caught:
        service.delete_provider("mock", expected_version=document.version)
    assert caught.value.code == "provider_in_use"


def test_route_reasoning_disabled_requires_declared_support(service: SettingsService) -> None:
    document = service.create_provider(
        _provider(models=[_model(supports_thinking=None)]), expected_version=0
    )
    service.update_section(
        "agents",
        {
            "default_provider_id": "mock",
            "default_model_id": "test-model",
            "roles": {
                "coder": {
                    "provider_id": "mock",
                    "model_id": "test-model",
                    "reasoning": "disabled",
                }
            },
        },
        expected_version=document.version,
    )
    with pytest.raises(SettingsError) as caught:
        service.build_live_snapshot()
    assert caught.value.code == "reasoning_unsupported"


def test_route_with_thinking_support_is_serialized(service: SettingsService) -> None:
    document = service.create_provider(
        _provider(models=[_model(supports_thinking=True)]), expected_version=0
    )
    service.update_section(
        "agents",
        {
            "default_provider_id": "mock",
            "default_model_id": "test-model",
            "roles": {
                "coder": {
                    "provider_id": "mock",
                    "model_id": "test-model",
                    "reasoning": "disabled",
                }
            },
        },
        expected_version=document.version,
    )
    snapshot = service.build_live_snapshot()
    config = service.build_llm_config(snapshot)
    spec = config.resolve_role("coder")
    assert spec.extra_body["thinking"] == {"type": "disabled"}


def test_import_does_not_accept_vault_references(service: SettingsService) -> None:
    service.create_provider(_provider(), expected_version=0)
    exported = service.export_config()
    exported["providers"]["mock"]["credential"] = {"kind": "vault"}
    with pytest.raises(SettingsError):
        service.import_config(exported, expected_version=1)


def test_unavailable_protocol_is_rejected_before_dispatch(service: SettingsService) -> None:
    provider = ProviderConfig(
        provider_id="bedrock",
        name="Bedrock",
        protocol="bedrock_converse",
        region="us-east-1",
        models=[_model()],
    )
    document = service.create_provider(provider, expected_version=0)
    service.update_section(
        "agents",
        {"default_provider_id": "bedrock", "default_model_id": "test-model", "roles": {}},
        expected_version=document.version,
    )
    with pytest.raises(SettingsError) as caught:
        service.build_live_snapshot()
    assert caught.value.code in {"protocol_unavailable", "credential_missing"}


def test_task_override_beats_role_routes(service: SettingsService) -> None:
    document = service.create_provider(_provider("one"), expected_version=0)
    document = service.create_provider(
        _provider("two", models=[_model(model_id="other-model")]),
        expected_version=document.version,
    )
    document = service.update_section(
        "agents",
        {
            "default_provider_id": "one",
            "default_model_id": "test-model",
            "roles": {
                "coder": {
                    "provider_id": "one",
                    "model_id": "test-model",
                    "reasoning": "inherit",
                }
            },
        },
        expected_version=document.version,
    )
    snapshot = service.build_live_snapshot(provider_id="two", model_id="other-model")
    assert {selection.provider_id for selection in snapshot.roles.values()} == {"two"}
    assert {selection.model_id for selection in snapshot.roles.values()} == {"other-model"}
    assert snapshot.default_provider_id == "two"
    assert snapshot.default_model_id == "other-model"


def test_provider_creation_rejects_private_base_url_as_settings_error(
    service: SettingsService,
) -> None:
    with pytest.raises(SettingsError) as caught:
        service.create_provider(
            _provider(base_url="https://127.0.0.1/v1"),
            expected_version=0,
        )
    assert caught.value.code == "invalid_base_url"
