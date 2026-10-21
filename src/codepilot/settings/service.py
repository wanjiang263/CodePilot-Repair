"""Settings service: validation, snapshots, connection tests and live config."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from codepilot.config import read_secret_env
from codepilot.llm.config import LLMConfig, ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.errors import LLMError
from codepilot.llm.registry import LLMMode, LLMRegistry
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message
from codepilot.observability.redaction import redact_text
from codepilot.settings.catalog import catalog_templates
from codepilot.settings.credentials import (
    CredentialError,
    CredentialVault,
    SecretNotFoundError,
)
from codepilot.settings.models import (
    ROUTABLE_ROLES,
    AgentSettings,
    CredentialRef,
    ExecutionSettings,
    GeneralSettings,
    ModelConfig,
    ModelPriceConfig,
    ProviderConfig,
    SettingsDocument,
    WorkspaceSettings,
)
from codepilot.settings.security import (
    ProviderSecurityError,
    SecureJSONClient,
    build_secure_httpx_client,
    resolve_public_address,
    validate_provider_base_url,
)
from codepilot.settings.store import SettingsStore, VersionConflictError

__all__ = [
    "FrozenModel",
    "FrozenProvider",
    "RoleSelection",
    "SettingsError",
    "SettingsService",
    "TaskModelSnapshot",
]

_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "general": GeneralSettings,
    "agents": AgentSettings,
    "execution": ExecutionSettings,
    "workspace": WorkspaceSettings,
}

_TEST_MAX_OUTPUT_TOKENS = 16
_TOOL_ROLES = frozenset({"planner", "coder"})
_PROTOCOL_EXTRA = "bedrock"


class SettingsError(RuntimeError):
    def __init__(self, message: str, *, code: str = "settings_error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class FrozenProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    name: str
    protocol: str
    base_url: str
    timeout_seconds: float
    max_retries: int
    max_concurrency: int
    region: str | None = None
    aws_profile: str | None = None
    credential_kind: Literal["none", "env", "vault"] = "none"
    credential_name: str | None = None


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    display_name: str
    context_window: int | None
    max_output_tokens: int | None
    supports_tools: bool
    supports_json_mode: bool
    supports_temperature: bool
    supports_thinking: bool | None
    price: ModelPriceConfig


class RoleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    provider_id: str
    model_id: str
    reasoning: Literal["inherit", "disabled"] = "inherit"


class TaskModelSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["fake", "live"]
    config_hash: str = ""
    config_version: int = 0
    budget_currency: str = "CNY"
    exchange_rates: dict[str, float] = Field(default_factory=dict)
    default_provider_id: str | None = None
    default_model_id: str | None = None
    roles: dict[str, RoleSelection] = Field(default_factory=dict)
    providers: dict[str, FrozenProvider] = Field(default_factory=dict)
    models: dict[str, FrozenModel] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def seal(self) -> TaskModelSnapshot:
        payload = self.model_dump(mode="json", exclude={"config_hash", "created_at"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self.model_copy(update={"config_hash": digest})

    @property
    def model_identity(self) -> str:
        if self.mode == "fake":
            return "FakeLLM"
        if not self.roles:
            return ""
        return "/".join(
            f"{role}={selection.model_id}" for role, selection in sorted(self.roles.items())
        )


def _model_key(provider_id: str, model_id: str) -> str:
    return f"{provider_id}/{model_id}"


class SettingsService:
    def __init__(
        self,
        store: SettingsStore,
        vault: CredentialVault,
        *,
        client_transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], str] = resolve_public_address,
        bedrock_client_factory: Callable[[ProviderSpec], Any] | None = None,
    ) -> None:
        self.store = store
        self.vault = vault
        self._transport = client_transport
        self._resolver = resolver
        self._bedrock_client_factory = bedrock_client_factory

    # ---------------------------------------------------------------- document
    def document(self) -> SettingsDocument:
        return self.store.load()

    def update_section(
        self, section: str, payload: dict[str, Any], *, expected_version: int
    ) -> SettingsDocument:
        model = _SECTION_MODELS.get(section)
        if model is None:
            raise SettingsError(
                f"unknown settings section: {section}", code="unknown_section", status=404
            )
        try:
            parsed = model.model_validate(payload)
        except ValueError as exc:
            raise SettingsError(str(exc), code="validation_error") from exc
        current = self.store.load()
        updated = _validate_document(
            {
                **current.model_dump(),
                section: parsed.model_dump(mode="json"),
            }
        )
        return self.store.save(
            updated,
            expected_version=expected_version,
            action=f"update.{section}",
            target=section,
            summary=f"updated {section} settings",
        )

    # --------------------------------------------------------------- providers
    def provider_view(self, provider: ProviderConfig) -> dict[str, Any]:
        document = self.document()
        available, available_reason = self._credential_available(provider)
        data = provider.model_dump(mode="json", exclude={"credential"})
        data["credential"] = {
            "kind": provider.credential.kind,
            "configured": provider.credential.configured,
            "available": available,
            "reason": available_reason,
            "env_name": provider.credential.name if provider.credential.kind == "env" else None,
            "updated_at": provider.credential.updated_at.isoformat()
            if provider.credential.updated_at
            else None,
        }
        data["protocol_supported"] = _protocol_available(provider.protocol)
        data["protocol_reason"] = _protocol_reason(provider.protocol)
        tests = self.store.connection_tests(provider.provider_id)
        data["models"] = [
            self.model_view(provider, model, document=document, tests=tests)
            for model in provider.models
        ]
        fingerprint = self._connection_fingerprint(provider)
        data["verification"] = {
            "routing_eligible": sum(
                1
                for model in provider.models
                if not self._routing_blockers(document, provider, model)
            ),
            "inference_verified": sum(
                1
                for model in provider.models
                if self._inference_status(
                    provider,
                    model,
                    next(
                        (
                            test
                            for test in tests
                            if test["kind"] == "inference" and test["model_id"] == model.model_id
                        ),
                        None,
                    ),
                )["state"]
                == "verified"
            ),
            "config_test": _test_summary(_latest_test(tests, "config"), fingerprint),
            "models_test": _test_summary(_latest_test(tests, "models"), fingerprint),
        }
        data["editable"] = provider.model_dump(
            mode="json", exclude={"credential", "created_at", "updated_at"}
        )
        return data

    def model_view(
        self,
        provider: ProviderConfig,
        model: ModelConfig,
        *,
        document: SettingsDocument | None = None,
        tests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        document = document or self.document()
        tests = tests if tests is not None else self.store.connection_tests(provider.provider_id)
        inference_record = next(
            (
                test
                for test in tests
                if test["kind"] == "inference" and test["model_id"] == model.model_id
            ),
            None,
        )
        blockers = self._model_blockers(provider, model)
        spec_blockers = self._spec_blockers(model)
        routing_blockers = self._routing_blockers(document, provider, model)
        available, available_reason = self._credential_available(provider)
        data = model.model_dump(mode="json")
        # Kept for compatibility: this only describes the model spec, not a live route.
        data["routing_ready"] = model.routing_ready
        data["blockers"] = blockers
        data["verification"] = {
            "config_complete": not spec_blockers,
            "missing": spec_blockers,
            "credential": {
                "available": available,
                "reason": available_reason,
            },
            "protocol": {
                "available": _protocol_available(provider.protocol),
                "reason": _protocol_reason(provider.protocol),
            },
            "routing": {
                "eligible": not routing_blockers,
                "blockers": routing_blockers,
            },
            "inference": self._inference_status(provider, model, inference_record),
            "tool_calling": _tool_status(model),
        }
        return data

    def providers_view(self) -> list[dict[str, Any]]:
        document = self.document()
        return [self.provider_view(item) for item in document.providers.values()]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                **item.model_dump(mode="json"),
                "available": item.available,
                "protocol_supported": _protocol_available(item.protocol),
                "auth_kind": item.auth_kind,
            }
            for item in catalog_templates()
        ]

    def create_provider(
        self, provider: ProviderConfig, *, expected_version: int
    ) -> SettingsDocument:
        document = self.document()
        if provider.provider_id in document.providers:
            raise SettingsError(
                "a provider with this id already exists", code="provider_exists", status=409
            )
        _validate_provider_endpoint(provider)
        if provider.credential.kind == "vault":
            raise SettingsError(
                "vault credentials must be created through the credential endpoint",
                code="credential_invalid",
            )
        now = datetime.now(UTC)
        created = provider.model_copy(update={"created_at": now, "updated_at": now})
        document.providers[created.provider_id] = created
        return self._save(
            document,
            expected_version,
            "provider.create",
            created.provider_id,
            f"created provider {created.provider_id}",
        )

    def update_provider(
        self, provider_id: str, provider: ProviderConfig, *, expected_version: int
    ) -> SettingsDocument:
        document = self.document()
        existing = document.providers.get(provider_id)
        if existing is None:
            raise SettingsError("provider not found", code="provider_not_found", status=404)
        if provider.provider_id != provider_id:
            raise SettingsError("provider_id is immutable", code="provider_id_immutable")
        _validate_provider_endpoint(provider)
        updated = provider.model_copy(
            update={
                "created_at": existing.created_at,
                "updated_at": datetime.now(UTC),
                "credential": existing.credential,
            }
        )
        document.providers[provider_id] = updated
        # References may become invalid if a model was removed or renamed.
        _validate_document(document.model_dump())
        return self._save(
            document,
            expected_version,
            "provider.update",
            provider_id,
            f"updated provider {provider_id}",
        )

    def delete_provider(self, provider_id: str, *, expected_version: int) -> SettingsDocument:
        document = self.document()
        provider = document.providers.get(provider_id)
        if provider is None:
            raise SettingsError("provider not found", code="provider_not_found", status=404)
        refs = _provider_references(document, provider_id)
        if refs:
            raise SettingsError(
                "remove the agent routing that points at this provider first: " + ", ".join(refs),
                code="provider_in_use",
                status=409,
            )
        if provider.credential.kind == "vault" and provider.credential.name:
            self.vault.delete(provider.credential.name)
        del document.providers[provider_id]
        saved = self._save(
            document,
            expected_version,
            "provider.delete",
            provider_id,
            f"deleted provider {provider_id}",
        )
        self.store.delete_connection_tests(provider_id)
        return saved

    def set_credential(
        self,
        provider_id: str,
        *,
        mode: Literal["value", "env"],
        value: str | None,
        env_name: str | None,
        expected_version: int,
    ) -> SettingsDocument:
        document = self.document()
        provider = document.providers.get(provider_id)
        if provider is None:
            raise SettingsError("provider not found", code="provider_not_found", status=404)
        if provider.protocol == "bedrock_converse":
            raise SettingsError(
                "AWS Bedrock uses the AWS credential chain, not a provider API key",
                code="credential_not_applicable",
            )
        old = provider.credential
        if mode == "value":
            if not value or not value.strip():
                raise SettingsError("credential value is empty", code="credential_empty")
            try:
                credential_id = self.vault.put(value)
            except CredentialError as exc:
                raise SettingsError(
                    "secure credential storage is unavailable; use an environment variable reference",
                    code="secure_storage_unavailable",
                    status=503,
                ) from exc
            new_ref = CredentialRef(kind="vault", name=credential_id, updated_at=datetime.now(UTC))
        else:
            name = (env_name or "").strip()
            if not name:
                raise SettingsError("environment variable name is empty", code="credential_empty")
            try:
                new_ref = CredentialRef(kind="env", name=name, updated_at=datetime.now(UTC))
            except ValueError as exc:
                raise SettingsError(str(exc), code="credential_invalid") from exc
        if old.kind == "vault" and old.name and old.name != new_ref.name:
            self.vault.delete(old.name)
        document.providers[provider_id] = provider.model_copy(
            update={"credential": new_ref, "updated_at": datetime.now(UTC)}
        )
        return self._save(
            document,
            expected_version,
            "credential.replace",
            provider_id,
            f"credential source set to {new_ref.kind} for {provider_id}",
        )

    def delete_credential(self, provider_id: str, *, expected_version: int) -> SettingsDocument:
        document = self.document()
        provider = document.providers.get(provider_id)
        if provider is None:
            raise SettingsError("provider not found", code="provider_not_found", status=404)
        if provider.credential.kind == "vault" and provider.credential.name:
            self.vault.delete(provider.credential.name)
        document.providers[provider_id] = provider.model_copy(
            update={"credential": CredentialRef(), "updated_at": datetime.now(UTC)}
        )
        return self._save(
            document,
            expected_version,
            "credential.revoke",
            provider_id,
            f"credential revoked for {provider_id}",
        )

    # ------------------------------------------------------------------- models
    def upsert_model(
        self,
        provider_id: str,
        model: ModelConfig,
        *,
        original_model_id: str | None,
        expected_version: int,
    ) -> SettingsDocument:
        document = self.document()
        provider = document.providers.get(provider_id)
        if provider is None:
            raise SettingsError("provider not found", code="provider_not_found", status=404)
        models = list(provider.models)
        existing_index = next(
            (index for index, item in enumerate(models) if item.model_id == original_model_id),
            None,
        )
        if original_model_id is not None and existing_index is None:
            raise SettingsError("model not found", code="model_not_found", status=404)
        if model.model_id != original_model_id and provider.model(model.model_id) is not None:
            raise SettingsError(
                "a model with this id already exists", code="model_exists", status=409
            )
        if existing_index is None:
            models.append(model)
        else:
            models[existing_index] = model
        document.providers[provider_id] = provider.model_copy(
            update={"models": models, "updated_at": datetime.now(UTC)}
        )
        if original_model_id and original_model_id != model.model_id:
            _retarget_model(document.agents, provider_id, original_model_id, model.model_id)
        _validate_document(document.model_dump())
        return self._save(
            document,
            expected_version,
            "model.upsert",
            f"{provider_id}/{model.model_id}",
            f"upserted model {provider_id}/{model.model_id}",
        )

    def delete_model(
        self, provider_id: str, model_id: str, *, expected_version: int
    ) -> SettingsDocument:
        document = self.document()
        provider = document.providers.get(provider_id)
        if provider is None:
            raise SettingsError("provider not found", code="provider_not_found", status=404)
        if provider.model(model_id) is None:
            raise SettingsError("model not found", code="model_not_found", status=404)
        refs = _model_references(document, provider_id, model_id)
        if refs:
            raise SettingsError(
                "remove the agent routing that points at this model first: " + ", ".join(refs),
                code="model_in_use",
                status=409,
            )
        document.providers[provider_id] = provider.model_copy(
            update={
                "models": [item for item in provider.models if item.model_id != model_id],
                "updated_at": datetime.now(UTC),
            }
        )
        saved = self._save(
            document,
            expected_version,
            "model.delete",
            f"{provider_id}/{model_id}",
            f"deleted model {provider_id}/{model_id}",
        )
        self.store.delete_connection_tests(provider_id, model_id)
        return saved

    # --------------------------------------------------------------- resolution
    def available_models(self) -> list[dict[str, Any]]:
        document = self.document()
        result: list[dict[str, Any]] = []
        for provider in document.providers.values():
            _available, reason = self._credential_available(provider)
            tests = self.store.connection_tests(provider.provider_id)
            for model in provider.models:
                blockers = self._routing_blockers(document, provider, model)
                inference = self._inference_status(
                    provider,
                    model,
                    _latest_inference(tests, model.model_id),
                )
                result.append(
                    {
                        "provider_id": provider.provider_id,
                        "provider_name": provider.name,
                        "protocol": provider.protocol,
                        "model_id": model.model_id,
                        "display_name": model.display_name,
                        "context_window": model.context_window,
                        "max_output_tokens": model.max_output_tokens,
                        "supports_tools": model.supports_tools,
                        "supports_json_mode": model.supports_json_mode,
                        "supports_thinking": model.supports_thinking,
                        "price": model.price.model_dump(mode="json"),
                        "currency": model.price.currency,
                        "ready": not blockers,
                        "blockers": blockers,
                        "credential_reason": reason,
                        "inference_verified": inference["state"] == "verified",
                        "inference_state": inference["state"],
                    }
                )
        return result

    def build_live_snapshot(
        self,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> TaskModelSnapshot:
        document = self.document()
        agents = document.agents
        if (provider_id is None) != (model_id is None):
            raise SettingsError("select both provider and model", code="model_selection_invalid")
        if provider_id is None and (
            agents.default_provider_id is None or agents.default_model_id is None
        ):
            raise SettingsError(
                "set a global default provider and model before starting a live run",
                code="default_model_missing",
            )
        override = (provider_id, model_id) if provider_id and model_id else None
        selections: dict[str, RoleSelection] = {}
        for role in ROUTABLE_ROLES:
            route = agents.roles.get(role)
            if override is not None:
                chosen_provider, chosen_model = override
                reasoning = route.reasoning if route else "inherit"
            elif route is not None and route.configured:
                assert route.provider_id is not None and route.model_id is not None
                chosen_provider, chosen_model = route.provider_id, route.model_id
                reasoning = route.reasoning
            else:
                if agents.default_provider_id is None or agents.default_model_id is None:
                    raise SettingsError(
                        f"role {role} has no route and no global default exists",
                        code="default_model_missing",
                    )
                chosen_provider, chosen_model = (
                    agents.default_provider_id,
                    agents.default_model_id,
                )
                reasoning = route.reasoning if route else "inherit"
            self._admit_model(document, role, chosen_provider, chosen_model, reasoning)
            selections[role] = RoleSelection(
                role=role,
                provider_id=chosen_provider,
                model_id=chosen_model,
                reasoning=reasoning,
            )

        frozen_providers: dict[str, FrozenProvider] = {}
        frozen_models: dict[str, FrozenModel] = {}
        for selection in selections.values():
            provider, model = document.resolve_model(selection.provider_id, selection.model_id)
            frozen_providers.setdefault(
                provider.provider_id,
                FrozenProvider(
                    provider_id=provider.provider_id,
                    name=provider.name,
                    protocol=provider.protocol,
                    base_url=provider.base_url,
                    timeout_seconds=provider.timeout_seconds,
                    max_retries=provider.max_retries,
                    max_concurrency=provider.max_concurrency,
                    region=provider.region,
                    aws_profile=provider.aws_profile,
                    credential_kind=provider.credential.kind,
                    credential_name=provider.credential.name,
                ),
            )
            key = _model_key(provider.provider_id, model.model_id)
            frozen_models[key] = FrozenModel(
                provider_id=provider.provider_id,
                model_id=model.model_id,
                display_name=model.display_name,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                supports_tools=model.supports_tools,
                supports_json_mode=model.supports_json_mode,
                supports_temperature=model.supports_temperature,
                supports_thinking=model.supports_thinking,
                price=model.price,
            )
        snapshot = TaskModelSnapshot(
            mode="live",
            config_version=document.version,
            budget_currency=document.execution.budget_currency,
            exchange_rates=document.execution.exchange_rates,
            default_provider_id=(
                override[0] if override is not None else agents.default_provider_id
            ),
            default_model_id=(override[1] if override is not None else agents.default_model_id),
            roles=selections,
            providers=frozen_providers,
            models=frozen_models,
        )
        return snapshot.seal()

    def fake_snapshot(self) -> TaskModelSnapshot:
        document = self.document()
        return TaskModelSnapshot(
            mode="fake",
            config_version=document.version,
            budget_currency=document.execution.budget_currency,
            exchange_rates=document.execution.exchange_rates,
        ).seal()

    def build_llm_config(self, snapshot: TaskModelSnapshot) -> LLMConfig:
        if snapshot.mode != "live":
            raise SettingsError(
                "a fake snapshot cannot build live providers", code="snapshot_not_live"
            )
        current = self.document()
        providers: dict[str, ProviderSpec] = {}
        for provider_id, frozen in snapshot.providers.items():
            # The snapshot freezes the model configuration, but credential
            # revocation/rotation is a security boundary: an old task must not
            # keep using a reference the operator has since removed or replaced.
            live_provider = current.providers.get(provider_id)
            if live_provider is None:
                raise SettingsError(
                    f"provider {provider_id} referenced by this task no longer exists",
                    code="provider_missing",
                )
            live_ref = (live_provider.credential.kind, live_provider.credential.name)
            frozen_ref = (frozen.credential_kind, frozen.credential_name)
            if live_ref != frozen_ref:
                raise SettingsError(
                    f"the credential reference for {provider_id} changed after the task "
                    "was created; create a new task to use the new credential",
                    code="credential_missing"
                    if not live_provider.credential.configured
                    else "credential_changed",
                )
            api_key = self._resolve_frozen_credential(frozen)
            providers[provider_id] = ProviderSpec(
                name=provider_id,
                base_url=frozen.base_url,
                api_key_env="",
                protocol=frozen.protocol,
                region=frozen.region,
                aws_profile=frozen.aws_profile,
                api_key_value=SecretStr(api_key) if api_key else None,
                max_concurrency=frozen.max_concurrency,
                timeout_seconds=frozen.timeout_seconds,
                max_retries=frozen.max_retries,
            )
        models: dict[str, ModelSpec] = {}
        aliases: dict[tuple[str, str, str], str] = {}
        role_aliases: dict[str, str] = {}
        for role, selection in snapshot.roles.items():
            key = _model_key(selection.provider_id, selection.model_id)
            frozen_model = snapshot.models[key]
            reasoning = selection.reasoning
            alias_key = (selection.provider_id, selection.model_id, reasoning)
            alias = aliases.get(alias_key)
            if alias is None:
                alias = f"{selection.provider_id}::{selection.model_id}::{reasoning}"
                aliases[alias_key] = alias
                extra_body: dict[str, Any] = {}
                if reasoning == "disabled" and frozen_model.supports_thinking:
                    extra_body["thinking"] = {"type": "disabled"}
                models[alias] = ModelSpec(
                    alias=alias,
                    provider=selection.provider_id,
                    model=selection.model_id,
                    context_window=frozen_model.context_window or 128_000,
                    supports_tools=frozen_model.supports_tools,
                    supports_json_mode=frozen_model.supports_json_mode,
                    supports_temperature=frozen_model.supports_temperature,
                    supports_thinking=frozen_model.supports_thinking,
                    max_output_tokens=frozen_model.max_output_tokens or 4096,
                    price=_to_price(frozen_model.price),
                    extra_body=extra_body,
                )
            role_aliases[role] = alias
        return LLMConfig(
            providers=providers,
            models=models,
            roles=role_aliases,
            exchange_rates=snapshot.exchange_rates,
            budget_currency=snapshot.budget_currency,
        )

    # --------------------------------------------------------------- connection
    def config_check(self, provider_id: str, model_id: str | None = None) -> dict[str, Any]:
        document = self.document()
        provider = document.providers.get(provider_id)
        if provider is None:
            raise SettingsError("provider not found", code="provider_not_found", status=404)
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        add("provider_enabled", provider.enabled, "enabled" if provider.enabled else "disabled")
        if provider.protocol == "bedrock_converse":
            add("region", bool(provider.region), provider.region or "missing region")
            add(
                "aws_sdk",
                _protocol_available(provider.protocol),
                _protocol_reason(provider.protocol),
            )
        else:
            try:
                validate_provider_base_url(provider.base_url)
                add("base_url", True, "valid https endpoint")
            except ProviderSecurityError as exc:
                add("base_url", False, str(exc))
            available, reason = self._credential_available(provider)
            add("credential", available, reason)
        if model_id is not None:
            model = provider.model(model_id)
            if model is None:
                add("model", False, "model not found")
            else:
                add("model", True, model.model_id)
                for blocker in self._model_blockers(provider, model):
                    add(blocker, False, _blocker_text(blocker))
                if not self._model_blockers(provider, model):
                    add("model_specs", True, "capabilities and pricing are complete")
        ok = all(item["ok"] for item in checks)
        return {"kind": "config", "ok": ok, "checks": checks}

    def test_connection(
        self,
        provider_id: str,
        *,
        kind: Literal["config", "models", "inference"],
        model_id: str | None = None,
        confirm_cost: bool = False,
    ) -> dict[str, Any]:
        config_result = self.config_check(provider_id, model_id)
        if kind == "config":
            return self._record_test(provider_id, model_id, "config", config_result)
        # The local configuration check really ran, so it is recorded even when the
        # requested network test is refused or fails later.
        self._record_test(provider_id, model_id, "config", config_result)
        if not config_result["ok"]:
            return config_result
        if kind == "models":
            return self._record_test(provider_id, None, "models", self._test_models(provider_id))
        if not model_id:
            raise SettingsError("inference test requires a model", code="model_required")
        if not confirm_cost:
            raise SettingsError(
                "an inference test can incur provider charges and must be confirmed",
                code="cost_confirmation_required",
            )
        return self._record_test(
            provider_id, model_id, "inference", self._test_inference(provider_id, model_id)
        )

    def discover_models(self, provider_id: str) -> dict[str, Any]:
        config_result = self.config_check(provider_id)
        if not config_result["ok"]:
            return self._record_test(provider_id, None, "config", config_result)
        self._record_test(provider_id, None, "config", config_result)
        return self._record_test(provider_id, None, "models", self._test_models(provider_id))

    def _test_models(self, provider_id: str) -> dict[str, Any]:
        document = self.document()
        provider = document.provider(provider_id)
        started = time.monotonic()
        try:
            ids = self._fetch_model_ids(provider)
        except (ProviderSecurityError, CredentialError, LLMError, httpx.HTTPError) as exc:
            return {
                "kind": "models",
                "ok": False,
                "checks": [
                    *self.config_check(provider_id)["checks"],
                    {"name": "model_catalog", "ok": False, "detail": _safe_error(exc)},
                ],
                "models": [],
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # pragma: no cover - defensive boundary
            return {
                "kind": "models",
                "ok": False,
                "checks": [{"name": "model_catalog", "ok": False, "detail": _safe_error(exc)}],
                "models": [],
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        return {
            "kind": "models",
            "ok": True,
            "checks": [
                *self.config_check(provider_id)["checks"],
                {"name": "model_catalog", "ok": True, "detail": f"{len(ids)} model(s) returned"},
            ],
            "models": ids,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    def _fetch_model_ids(self, provider: ProviderConfig) -> list[str]:
        if provider.protocol == "bedrock_converse":
            client = self._bedrock_runtime(provider)
            response = client.list_foundation_models()
            summaries = response.get("modelSummaries") if isinstance(response, dict) else None
            ids: list[str] = []
            for item in summaries or []:
                if isinstance(item, dict) and item.get("modelId"):
                    ids.append(str(item["modelId"]))
            return ids[:200]
        credential = self._resolve_credential(provider)
        if provider.protocol == "anthropic_messages":
            base = provider.base_url.rstrip("/")
            url = base + ("/models" if base.endswith("/v1") else "/v1/models")
            headers = {
                "x-api-key": credential,
                "anthropic-version": "2023-06-01",
            }
        else:
            url = provider.base_url.rstrip("/") + "/models"
            headers = {"Authorization": f"Bearer {credential}"}
        validate_provider_base_url(provider.base_url)
        with SecureJSONClient(
            transport=self._transport,
            resolver=self._resolver,
        ) as client:
            response = client.request_json(
                "GET",
                url,
                headers=headers,
                timeout_seconds=min(provider.timeout_seconds, 30.0),
            )
        if response.status >= 400:
            raise ProviderSecurityError(f"model discovery returned HTTP {response.status}")
        payload = response.payload
        data = payload.get("data") if isinstance(payload, dict) else None
        ids = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    ids.append(item["id"])
        return ids[:200]

    def _test_inference(self, provider_id: str, model_id: str) -> dict[str, Any]:
        document = self.document()
        provider = document.resolve_model(provider_id, model_id)[0]
        model = provider.model(model_id)
        assert model is not None
        started = time.monotonic()
        config = self._single_model_config(provider, model)
        registry = LLMRegistry(
            config,
            mode=LLMMode.LIVE,
            budget_currency=document.execution.budget_currency,
            policy=RetryPolicy(max_attempts=1),
            client_factory=self._registry_client,
            bedrock_client_factory=self._bedrock_client_factory,
        )
        try:
            response = registry.for_role("coder").complete(
                LLMRequest(
                    messages=[
                        Message.system("You are a connection test. Reply with the single word OK."),
                        Message.user("Reply with OK."),
                    ],
                    temperature=0.0,
                    max_output_tokens=_TEST_MAX_OUTPUT_TOKENS,
                    purpose="connection_test",
                )
            )
        except Exception as exc:
            return {
                "kind": "inference",
                "ok": False,
                "authenticated": False,
                "checks": [
                    *self.config_check(provider_id, model_id)["checks"],
                    {"name": "inference", "ok": False, "detail": _safe_error(exc)},
                ],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "tool_calling": _tool_status(model),
            }
        finally:
            registry.close()
        latency = int((time.monotonic() - started) * 1000)
        return {
            "kind": "inference",
            "ok": True,
            "authenticated": True,
            "checks": [
                *self.config_check(provider_id, model_id)["checks"],
                {"name": "authentication", "ok": True, "detail": "request accepted"},
                {"name": "protocol", "ok": True, "detail": provider.protocol},
                {"name": "inference", "ok": True, "detail": "bounded test request completed"},
            ],
            "model_id": model_id,
            "model_echo": response.model_alias,
            "finish_reason": response.finish_reason,
            "latency_ms": latency,
            "usage": {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cached_input_tokens": response.cached_input_tokens,
            },
            "cost": response.cost,
            "cost_known": response.cost_known,
            "currency": document.execution.budget_currency,
            "tool_calling": _tool_status(model),
        }

    def _single_model_config(self, provider: ProviderConfig, model: ModelConfig) -> LLMConfig:
        api_key = (
            None if provider.protocol == "bedrock_converse" else self._resolve_credential(provider)
        )
        provider_spec = ProviderSpec(
            name=provider.provider_id,
            base_url=provider.base_url,
            api_key_env="",
            protocol=provider.protocol,
            region=provider.region,
            aws_profile=provider.aws_profile,
            api_key_value=SecretStr(api_key) if api_key else None,
            max_concurrency=1,
            timeout_seconds=min(provider.timeout_seconds, 30.0),
            max_retries=provider.max_retries,
        )
        alias = "__connection_test__"
        model_spec = ModelSpec(
            alias=alias,
            provider=provider.provider_id,
            model=model.model_id,
            context_window=model.context_window or 8192,
            supports_tools=model.supports_tools,
            supports_json_mode=model.supports_json_mode,
            supports_temperature=model.supports_temperature,
            supports_thinking=model.supports_thinking,
            max_output_tokens=min(
                model.max_output_tokens or _TEST_MAX_OUTPUT_TOKENS, _TEST_MAX_OUTPUT_TOKENS
            ),
            price=_to_price(model.price),
        )
        document = self.document()
        return LLMConfig(
            providers={provider.provider_id: provider_spec},
            models={alias: model_spec},
            roles=dict.fromkeys(ROUTABLE_ROLES, alias),
            exchange_rates=document.execution.exchange_rates,
            budget_currency=document.execution.budget_currency,
        )

    def _registry_client(self, provider: ProviderSpec, api_key: str | None) -> httpx.Client:
        del api_key
        return build_secure_httpx_client(
            base_url=provider.base_url,
            timeout_seconds=provider.timeout_seconds,
            transport=self._transport,
            resolver=self._resolver,
        )

    def registry_client(self, provider: ProviderSpec, api_key: str | None) -> httpx.Client:
        """Public client factory used by the worker's live ProviderBundle."""
        return self._registry_client(provider, api_key)

    @property
    def bedrock_client_factory(self) -> Callable[[ProviderSpec], Any] | None:
        return self._bedrock_client_factory

    def _bedrock_runtime(self, provider: ProviderConfig) -> Any:
        if self._bedrock_client_factory is not None:
            return self._bedrock_client_factory(
                ProviderSpec(
                    name=provider.provider_id,
                    base_url="",
                    protocol="bedrock_converse",
                    region=provider.region,
                    aws_profile=provider.aws_profile,
                    timeout_seconds=provider.timeout_seconds,
                    max_retries=provider.max_retries,
                )
            )
        from codepilot.llm.bedrock_converse import _build_client

        return _build_client(
            ProviderSpec(
                name=provider.provider_id,
                base_url="",
                protocol="bedrock_converse",
                region=provider.region,
                aws_profile=provider.aws_profile,
                timeout_seconds=provider.timeout_seconds,
                max_retries=provider.max_retries,
            )
        )

    # ------------------------------------------------------------------ export
    def export_config(self) -> dict[str, Any]:
        document = self.document()
        providers: dict[str, Any] = {}
        for provider_id, provider in document.providers.items():
            data = provider.model_dump(mode="json", exclude={"credential"})
            data["credential"] = {
                "kind": provider.credential.kind,
                "env_name": provider.credential.name if provider.credential.kind == "env" else None,
            }
            providers[provider_id] = data
        return {
            "schema_version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "settings_version": document.version,
            "general": document.general.model_dump(mode="json"),
            "agents": document.agents.model_dump(mode="json"),
            "execution": document.execution.model_dump(mode="json"),
            "workspace": document.workspace.model_dump(mode="json"),
            "providers": providers,
        }

    def import_config(self, payload: dict[str, Any], *, expected_version: int) -> SettingsDocument:
        if int(payload.get("schema_version") or 0) != 1:
            raise SettingsError("unsupported settings export schema", code="import_schema")
        providers_payload = payload.get("providers") or {}
        if not isinstance(providers_payload, dict):
            raise SettingsError("providers must be an object", code="import_invalid")
        providers: dict[str, ProviderConfig] = {}
        for provider_id, raw in providers_payload.items():
            if not isinstance(raw, dict):
                raise SettingsError("provider entries must be objects", code="import_invalid")
            body = dict(raw)
            body.pop("provider_id", None)
            credential = body.pop("credential", None) or {}
            provider = ProviderConfig.model_validate({**body, "provider_id": provider_id})
            kind = credential.get("kind") if isinstance(credential, dict) else "none"
            if kind == "env":
                name = credential.get("env_name")
                if not isinstance(name, str):
                    raise SettingsError("an env credential needs env_name", code="import_invalid")
                provider = provider.model_copy(
                    update={"credential": CredentialRef(kind="env", name=name)}
                )
            elif kind == "vault":
                raise SettingsError(
                    "exports should not contain vault references; re-enter the credential after import",
                    code="import_credential",
                )
            else:
                provider = provider.model_copy(update={"credential": CredentialRef()})
            _validate_provider_endpoint(provider)
            providers[provider_id] = provider
        document = _validate_document(
            {
                "general": payload.get("general") or {},
                "agents": payload.get("agents") or {},
                "execution": payload.get("execution") or {},
                "workspace": payload.get("workspace") or {},
                "providers": {
                    key: value.model_dump(mode="json") for key, value in providers.items()
                },
            }
        )
        return self.store.save(
            document,
            expected_version=expected_version,
            action="settings.import",
            target="document",
            summary=f"imported {len(providers)} provider(s)",
        )

    # ------------------------------------------------------------------ private
    def _save(
        self,
        document: SettingsDocument,
        expected_version: int,
        action: str,
        target: str,
        summary: str,
    ) -> SettingsDocument:
        try:
            return self.store.save(
                document,
                expected_version=expected_version,
                action=action,
                target=target,
                summary=summary,
            )
        except VersionConflictError:
            raise

    def _credential_available(self, provider: ProviderConfig) -> tuple[bool, str]:
        if provider.protocol == "bedrock_converse":
            available = _protocol_available(provider.protocol)
            return (
                available,
                "AWS SDK credential chain" if available else _protocol_reason(provider.protocol),
            )
        ref = provider.credential
        if ref.kind == "none" or not ref.name:
            return False, "not configured"
        if ref.kind == "env":
            if read_secret_env(ref.name):
                return True, f"environment variable {ref.name}"
            return False, f"environment variable {ref.name} is not set"
        if not self.vault.exists(ref.name):
            return False, "stored credential is missing"
        try:
            self.vault.get(ref.name)
        except CredentialError:
            return False, "stored credential cannot be decrypted"
        return True, "encrypted vault"

    def _resolve_credential(self, provider: ProviderConfig) -> str:
        ref = provider.credential
        if ref.kind == "env" and ref.name:
            value = read_secret_env(ref.name)
            if not value:
                raise SettingsError(
                    f"environment variable {ref.name} is not set",
                    code="credential_missing",
                )
            return value
        if ref.kind == "vault" and ref.name:
            try:
                return self.vault.get(ref.name)
            except SecretNotFoundError as exc:
                raise SettingsError(
                    "the stored credential no longer exists",
                    code="credential_missing",
                ) from exc
            except CredentialError as exc:
                raise SettingsError(
                    "the stored credential cannot be decrypted",
                    code="credential_unavailable",
                ) from exc
        raise SettingsError("provider credential is not configured", code="credential_missing")

    def _resolve_frozen_credential(self, frozen: FrozenProvider) -> str | None:
        if frozen.protocol == "bedrock_converse":
            return None
        if frozen.credential_kind == "env" and frozen.credential_name:
            value = read_secret_env(frozen.credential_name)
            if not value:
                raise SettingsError(
                    f"environment variable {frozen.credential_name} is not set",
                    code="credential_missing",
                )
            return value
        if frozen.credential_kind == "vault" and frozen.credential_name:
            try:
                return self.vault.get(frozen.credential_name)
            except CredentialError as exc:
                raise SettingsError(
                    "the frozen provider credential is unavailable",
                    code="credential_missing",
                ) from exc
        raise SettingsError(
            f"provider {frozen.provider_id} has no credential", code="credential_missing"
        )

    def _spec_blockers(self, model: ModelConfig) -> list[str]:
        blockers: list[str] = []
        if not model.context_window:
            blockers.append("context_window_missing")
        if not model.max_output_tokens:
            blockers.append("max_output_tokens_missing")
        if not model.supports_tools:
            blockers.append("tool_calling_unsupported")
        if not model.price.complete:
            blockers.append("price_missing")
        return blockers

    def _model_blockers(self, provider: ProviderConfig, model: ModelConfig) -> list[str]:
        blockers = self._spec_blockers(model)
        if not provider.enabled:
            blockers.append("provider_disabled")
        return blockers

    def _routing_blockers(
        self, document: SettingsDocument, provider: ProviderConfig, model: ModelConfig
    ) -> list[str]:
        """Everything that would make ``_admit_model`` fail for this model.

        This is deliberately stricter than ``ModelConfig.routing_ready``: that
        property only describes the model spec. The dashboard must not present
        spec completeness as a proven live route.
        """
        blockers = set(self._model_blockers(provider, model))
        if not _protocol_available(provider.protocol):
            blockers.add("protocol_unavailable")
        available, _reason = self._credential_available(provider)
        if not available:
            blockers.add("credential_missing")
        if (model.context_window is not None and model.context_window < 8192) or (
            model.max_output_tokens is not None and model.max_output_tokens < 1024
        ):
            blockers.add("model_capacity_too_small")
        currency = document.execution.budget_currency
        if (
            model.price.complete
            and model.price.currency != currency
            and model.price.currency not in document.execution.exchange_rates
        ):
            blockers.add("currency_rate_missing")
        return sorted(blockers)

    def _connection_fingerprint(
        self, provider: ProviderConfig, model: ModelConfig | None = None
    ) -> str:
        payload: dict[str, Any] = {
            "provider": {
                "protocol": provider.protocol,
                "base_url": provider.base_url,
                "enabled": provider.enabled,
                "region": provider.region,
                "aws_profile": provider.aws_profile,
                "credential": {
                    "kind": provider.credential.kind,
                    "name": provider.credential.name,
                    "updated_at": provider.credential.updated_at.isoformat()
                    if provider.credential.updated_at
                    else None,
                },
            }
        }
        if model is not None:
            payload["model"] = model.model_dump(mode="json")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _record_test(
        self,
        provider_id: str,
        model_id: str | None,
        kind: Literal["config", "models", "inference"],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        document = self.document()
        provider = document.provider(provider_id)
        model = None
        if kind == "inference" and model_id is not None:
            model = document.resolve_model(provider_id, model_id)[1]
        fingerprint = self._connection_fingerprint(provider, model)
        detail = {
            key: value for key, value in result.items() if key not in {"checked_at", "fingerprint"}
        }
        checked_at = self.store.record_connection_test(
            provider_id=provider_id,
            model_id=model_id,
            kind=kind,
            ok=bool(result.get("ok")),
            fingerprint=fingerprint,
            detail=detail,
        )
        return {**result, "checked_at": checked_at, "fingerprint": fingerprint}

    def _inference_status(
        self,
        provider: ProviderConfig,
        model: ModelConfig,
        record: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if record is None:
            return {
                "state": "not_tested",
                "checked_at": None,
                "detail": None,
                "latency_ms": None,
                "model_echo": None,
                "tool_calling": _tool_status(model),
            }
        detail = record.get("detail") or {}
        expected = self._connection_fingerprint(provider, model)
        if record.get("fingerprint") != expected:
            return {
                "state": "stale",
                "checked_at": record.get("checked_at"),
                "detail": "configuration or credential reference changed after the test",
                "latency_ms": detail.get("latency_ms"),
                "model_echo": detail.get("model_echo"),
                "tool_calling": _tool_status(model),
            }
        state = "verified" if record.get("ok") else "failed"
        failure = None
        if state == "failed":
            failure = next(
                (
                    str(check.get("detail"))
                    for check in detail.get("checks", [])
                    if isinstance(check, dict) and not check.get("ok")
                ),
                "the inference test did not complete",
            )
        return {
            "state": state,
            "checked_at": record.get("checked_at"),
            "detail": failure,
            "latency_ms": detail.get("latency_ms"),
            "model_echo": detail.get("model_echo"),
            "tool_calling": _tool_status(model),
        }

    def _admit_model(
        self,
        document: SettingsDocument,
        role: str,
        provider_id: str,
        model_id: str,
        reasoning: str,
    ) -> None:
        try:
            provider, model = document.resolve_model(provider_id, model_id)
        except KeyError as exc:
            raise SettingsError(str(exc), code="model_not_found") from exc
        if not provider.enabled:
            raise SettingsError(f"provider {provider_id} is disabled", code="provider_disabled")
        if not _protocol_available(provider.protocol):
            raise SettingsError(
                f"provider {provider_id} protocol is unavailable: {_protocol_reason(provider.protocol)}",
                code="protocol_unavailable",
            )
        available, reason = self._credential_available(provider)
        if not available:
            raise SettingsError(
                f"provider {provider_id} is not authenticated: {reason}",
                code="credential_missing",
            )
        blockers = self._model_blockers(provider, model)
        if blockers:
            raise SettingsError(
                f"model {provider_id}/{model_id} is incomplete: {', '.join(blockers)}",
                code="model_specs_incomplete",
            )
        assert model.context_window is not None and model.max_output_tokens is not None
        if model.context_window < 8192 or model.max_output_tokens < 1024:
            raise SettingsError(
                "live routing requires at least an 8k context window and 1k output tokens",
                code="model_capacity_too_small",
            )
        if role in _TOOL_ROLES and not model.supports_tools:
            raise SettingsError(
                f"role {role} requires a model with tool calling",
                code="tool_calling_unsupported",
            )
        price = model.price
        if not price.complete:
            raise SettingsError(
                f"model {provider_id}/{model_id} has no complete price; live runs fail closed",
                code="price_missing",
            )
        currency = document.execution.budget_currency
        if price.currency != currency and price.currency not in document.execution.exchange_rates:
            raise SettingsError(
                f"model price currency {price.currency} has no exchange rate to {currency}",
                code="currency_rate_missing",
            )
        if reasoning == "disabled" and model.supports_thinking is not True:
            raise SettingsError(
                f"model {provider_id}/{model_id} does not declare reasoning support",
                code="reasoning_unsupported",
            )


def _latest_test(tests: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((test for test in tests if test.get("kind") == kind), None)


def _latest_inference(tests: list[dict[str, Any]], model_id: str) -> dict[str, Any] | None:
    return next(
        (
            test
            for test in tests
            if test.get("kind") == "inference" and test.get("model_id") == model_id
        ),
        None,
    )


def _test_summary(record: dict[str, Any] | None, fingerprint: str) -> dict[str, Any]:
    if record is None:
        return {"state": "not_tested", "checked_at": None, "detail": None, "latency_ms": None}
    detail = record.get("detail") or {}
    state = "ok" if record.get("ok") else "failed"
    if record.get("fingerprint") != fingerprint:
        state = "stale"
    failure = None
    if state == "failed":
        failure = next(
            (
                str(check.get("detail"))
                for check in detail.get("checks", [])
                if isinstance(check, dict) and not check.get("ok")
            ),
            "the test did not complete",
        )
    return {
        "state": state,
        "checked_at": record.get("checked_at"),
        "detail": failure,
        "latency_ms": detail.get("latency_ms"),
    }


def _validate_provider_endpoint(provider: ProviderConfig) -> None:
    if provider.protocol == "bedrock_converse":
        return
    try:
        validate_provider_base_url(provider.base_url)
    except ProviderSecurityError as exc:
        raise SettingsError(str(exc), code="invalid_base_url") from exc


def _to_price(config: ModelPriceConfig) -> ModelPrice | None:
    if not config.complete:
        return None
    return ModelPrice(
        currency=config.currency,
        input_per_mtok=config.input_per_mtok,
        output_per_mtok=config.output_per_mtok,
        cached_input_per_mtok=config.cached_input_per_mtok,
    )


def _protocol_available(protocol: str) -> bool:
    if protocol == "bedrock_converse":
        return importlib.util.find_spec("boto3") is not None
    return protocol in {"openai_compatible", "anthropic_messages"}


def _protocol_reason(protocol: str) -> str:
    if protocol == "bedrock_converse":
        return (
            "boto3 is installed"
            if importlib.util.find_spec("boto3") is not None
            else "install the optional bedrock extra to enable the AWS SDK adapter"
        )
    if protocol in {"openai_compatible", "anthropic_messages"}:
        return "native adapter available"
    return "unsupported protocol"


def _tool_status(model: ModelConfig) -> str:
    if not model.supports_tools:
        return "not_supported"
    return "declared_not_verified"


def _blocker_text(blocker: str) -> str:
    return {
        "context_window_missing": "context window is not configured",
        "max_output_tokens_missing": "max output tokens are not configured",
        "tool_calling_unsupported": "the model does not declare tool calling",
        "price_missing": "input and output prices are required for live runs",
        "provider_disabled": "provider is disabled",
    }.get(blocker, blocker)


def _safe_error(exc: BaseException) -> str:
    message = str(exc)
    lowered = message.lower()
    for marker in ("x-api-key", "authorization", "bearer "):
        if marker in lowered:
            return "provider rejected the request; response body withheld"
    return redact_text(message)[:500] or type(exc).__name__


def _provider_references(document: SettingsDocument, provider_id: str) -> list[str]:
    refs: list[str] = []
    if document.agents.default_provider_id == provider_id:
        refs.append("default model")
    for role, route in document.agents.roles.items():
        if route.provider_id == provider_id:
            refs.append(role)
    return refs


def _model_references(document: SettingsDocument, provider_id: str, model_id: str) -> list[str]:
    refs: list[str] = []
    agents = document.agents
    if agents.default_provider_id == provider_id and agents.default_model_id == model_id:
        refs.append("default model")
    for role, route in agents.roles.items():
        if route.provider_id == provider_id and route.model_id == model_id:
            refs.append(role)
    return refs


def _retarget_model(
    agents: AgentSettings, provider_id: str, old_model_id: str, new_model_id: str
) -> None:
    if agents.default_provider_id == provider_id and agents.default_model_id == old_model_id:
        agents.default_model_id = new_model_id
    for route in agents.roles.values():
        if route.provider_id == provider_id and route.model_id == old_model_id:
            route.model_id = new_model_id


def _validate_document(payload: dict[str, Any]) -> SettingsDocument:
    try:
        return SettingsDocument.model_validate(payload)
    except ValueError as exc:
        raise SettingsError(
            f"settings reference validation failed: {exc}",
            code="reference_invalid",
        ) from exc
