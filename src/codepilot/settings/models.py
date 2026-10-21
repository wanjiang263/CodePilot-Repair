"""Typed settings document shared by the API, the worker and the dashboard.

The document never contains credential values. A provider stores a
``CredentialRef`` (``env`` variable name or vault id); plaintext only exists
inside :mod:`codepilot.settings.credentials` and the process that resolved it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "CATALOG_SOURCE",
    "DEFAULT_EXCHANGE_RATES",
    "PROTOCOLS",
    "ROUTABLE_ROLES",
    "AgentSettings",
    "CredentialRef",
    "ExecutionSettings",
    "GeneralSettings",
    "ModelConfig",
    "ModelPriceConfig",
    "ProviderConfig",
    "RoleRoute",
    "SettingsDocument",
    "WorkspaceSettings",
]

#: Model roles the graph can dispatch independently. Issue analysis and failure
#: classification are deterministic code paths, not model roles, so they are not
#: offered as routing targets.
ROUTABLE_ROLES: tuple[str, ...] = (
    "localizer",
    "planner",
    "coder",
    "reproducer",
    "diagnoser",
    "reviewer",
)

PROTOCOLS = Literal["openai_compatible", "anthropic_messages", "bedrock_converse"]

CATALOG_SOURCE = "builtin-catalog@2026-10-21"

DEFAULT_EXCHANGE_RATES: dict[str, float] = {"CNY": 1.0}

_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_MODEL_ID = re.compile(r"^[^\s]{1,200}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CredentialRef(_StrictModel):
    kind: Literal["none", "env", "vault"] = "none"
    name: str | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> CredentialRef:
        if self.kind == "none":
            if self.name is not None:
                raise ValueError("a none credential must not carry a name")
            return self
        if not self.name:
            raise ValueError("an env or vault credential requires a name")
        if self.kind == "env" and not _ENV_NAME.match(self.name):
            raise ValueError("environment credential names must look like API_KEY")
        if self.kind == "vault" and not re.match(r"^[a-f0-9]{32}$", self.name):
            raise ValueError("vault credential ids are 32 hex characters")
        return self

    @property
    def configured(self) -> bool:
        return self.kind != "none"


class ModelPriceConfig(_StrictModel):
    currency: str = "CNY"
    input_per_mtok: float | None = Field(default=None, ge=0)
    output_per_mtok: float | None = Field(default=None, ge=0)
    cached_input_per_mtok: float | None = Field(default=None, ge=0)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = value.upper()
        if not _CURRENCY.match(value):
            raise ValueError("currency must be a 3-letter ISO code")
        return value

    @property
    def complete(self) -> bool:
        return self.input_per_mtok is not None and self.output_per_mtok is not None


class ModelConfig(_StrictModel):
    model_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=120)
    context_window: int | None = Field(default=None, ge=1_000, le=20_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    supports_tools: bool = True
    supports_json_mode: bool = True
    supports_temperature: bool = True
    supports_thinking: bool | None = None
    price: ModelPriceConfig = Field(default_factory=ModelPriceConfig)
    source: Literal["builtin", "manual", "discovered"] = "manual"
    source_note: str | None = Field(default=None, max_length=300)
    favorite: bool = False

    @field_validator("model_id")
    @classmethod
    def _model_id(cls, value: str) -> str:
        if not _MODEL_ID.match(value):
            raise ValueError("model_id must not contain whitespace")
        return value

    @property
    def price_configured(self) -> bool:
        return self.price.complete

    @property
    def routing_ready(self) -> bool:
        return bool(
            self.context_window
            and self.max_output_tokens
            and self.supports_tools
            and self.price.complete
        )


class ProviderConfig(_StrictModel):
    provider_id: str
    name: str = Field(min_length=1, max_length=80)
    protocol: PROTOCOLS = "openai_compatible"
    base_url: str = Field(default="", max_length=500)
    enabled: bool = True
    custom: bool = False
    catalog_id: str | None = None
    credential: CredentialRef = Field(default_factory=CredentialRef)
    timeout_seconds: float = Field(default=60.0, ge=5.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    region: str | None = Field(default=None, max_length=32)
    aws_profile: str | None = Field(default=None, max_length=120)
    models: list[ModelConfig] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("provider_id")
    @classmethod
    def _provider_id(cls, value: str) -> str:
        if not _PROVIDER_ID.match(value):
            raise ValueError(
                "provider_id must be lowercase, start with a letter and contain only a-z, 0-9, - or _"
            )
        return value

    @model_validator(mode="after")
    def _unique_models(self) -> ProviderConfig:
        ids = [model.model_id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("a provider cannot contain the same model twice")
        if self.protocol == "bedrock_converse":
            if not self.region:
                raise ValueError("AWS Bedrock requires a region")
        elif not self.custom and not self.base_url:
            raise ValueError("this provider requires a base URL")
        if not self.custom and self.base_url and not self.base_url.startswith("https://"):
            raise ValueError("provider base URLs must use https")
        return self

    def model(self, model_id: str) -> ModelConfig | None:
        return next((item for item in self.models if item.model_id == model_id), None)

    @property
    def credential_configured(self) -> bool:
        return self.protocol == "bedrock_converse" or self.credential.configured


class RoleRoute(_StrictModel):
    provider_id: str | None = None
    model_id: str | None = None
    reasoning: Literal["inherit", "disabled"] = "inherit"

    @model_validator(mode="after")
    def _both_or_neither(self) -> RoleRoute:
        if (self.provider_id is None) != (self.model_id is None):
            raise ValueError("a role override needs both provider_id and model_id")
        return self

    @property
    def configured(self) -> bool:
        return self.provider_id is not None and self.model_id is not None


class GeneralSettings(_StrictModel):
    language: Literal["zh-CN", "en-US", "system"] = "system"
    theme: Literal["light", "dark", "system"] = "system"
    default_execution_mode: Literal["offline_fake", "docker", "live"] = "offline_fake"
    show_reasoning: bool = True
    activity_density: Literal["summary", "normal", "detailed"] = "normal"


class AgentSettings(_StrictModel):
    default_provider_id: str | None = None
    default_model_id: str | None = None
    roles: dict[str, RoleRoute] = Field(default_factory=dict)

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, value: dict[str, RoleRoute]) -> dict[str, RoleRoute]:
        unknown = sorted(set(value) - set(ROUTABLE_ROLES))
        if unknown:
            raise ValueError(f"unknown agent roles: {', '.join(unknown)}")
        return value

    @model_validator(mode="after")
    def _default_pair(self) -> AgentSettings:
        if (self.default_provider_id is None) != (self.default_model_id is None):
            raise ValueError("set both the default provider and model, or neither")
        return self


class ExecutionSettings(_StrictModel):
    budget_currency: str = "CNY"
    exchange_rates: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_EXCHANGE_RATES))
    max_iterations: int = Field(default=5, ge=1, le=10)
    max_test_runs: int = Field(default=12, ge=3, le=60)
    max_files_changed: int = Field(default=5, ge=1, le=20)
    max_lines_changed: int = Field(default=150, ge=1, le=2000)
    max_tool_calls_per_iteration: int = Field(default=30, ge=1, le=100)
    max_review_calls: int = Field(default=2, ge=1, le=5)
    max_runtime_seconds: int = Field(default=1800, ge=30, le=7200)
    max_total_tokens: int = Field(default=1_000_000, ge=10_000, le=10_000_000)
    max_cost: float | None = Field(default=None, ge=0, le=1000)
    max_parallel_tasks: int = Field(default=1, ge=1, le=4)

    @field_validator("budget_currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        value = value.upper()
        if not _CURRENCY.match(value):
            raise ValueError("currency must be a 3-letter ISO code")
        return value

    @field_validator("exchange_rates")
    @classmethod
    def _rates(cls, value: dict[str, float]) -> dict[str, float]:
        cleaned: dict[str, float] = {}
        for currency, rate in value.items():
            code = currency.upper()
            if not _CURRENCY.match(code):
                raise ValueError(f"invalid currency code: {currency}")
            if rate <= 0 or rate > 1000:
                raise ValueError("exchange rates must be positive and plausible")
            cleaned[code] = rate
        return cleaned


class WorkspaceSettings(_StrictModel):
    default_repository_kind: Literal["demo", "https"] = "demo"
    default_repository: str = Field(default="", max_length=2048)


class SettingsDocument(_StrictModel):
    version: int = Field(default=0, ge=0)
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    agents: AgentSettings = Field(default_factory=AgentSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    updated_at: datetime | None = None

    def provider(self, provider_id: str) -> ProviderConfig:
        try:
            return self.providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"unknown provider: {provider_id}") from exc

    def resolve_model(self, provider_id: str, model_id: str) -> tuple[ProviderConfig, ModelConfig]:
        provider = self.provider(provider_id)
        model = provider.model(model_id)
        if model is None:
            raise KeyError(f"provider {provider_id!r} has no model {model_id!r}")
        return provider, model

    @model_validator(mode="after")
    def _references(self) -> SettingsDocument:
        agents = self.agents
        if agents.default_provider_id and agents.default_model_id:
            if agents.default_provider_id not in self.providers:
                raise ValueError("the default model points to a provider that does not exist")
            self.resolve_model(agents.default_provider_id, agents.default_model_id)
        for role, route in agents.roles.items():
            if not route.configured:
                continue
            assert route.provider_id is not None and route.model_id is not None
            if route.provider_id not in self.providers:
                raise ValueError(f"role {role!r} points to a provider that does not exist")
            self.resolve_model(route.provider_id, route.model_id)
        return self

    def describe(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "general": self.general.model_dump(mode="json"),
            "agents": self.agents.model_dump(mode="json"),
            "execution": self.execution.model_dump(mode="json"),
            "workspace": self.workspace.model_dump(mode="json"),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
