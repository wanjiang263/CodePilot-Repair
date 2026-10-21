"""Built-in provider directory.

Model ids and numeric specifications are copied from the validated
``configs/llm.yaml`` where a source date exists. Providers without a verified
specification are listed as templates with explicit empty capability fields;
the UI must show them as incomplete rather than inventing prices.
"""

from __future__ import annotations

import importlib.util
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from codepilot.settings.models import CATALOG_SOURCE, ModelConfig, ModelPriceConfig

__all__ = ["ProviderTemplate", "catalog_templates", "template"]


class ProviderTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str
    name: str
    protocol: Literal["openai_compatible", "anthropic_messages", "bedrock_converse"]
    base_url: str = ""
    api_key_env: str | None = None
    auth_kind: Literal["api_key", "aws"] = "api_key"
    docs_url: str | None = None
    notes: str = ""
    models: list[ModelConfig] = Field(default_factory=list)
    source: str = CATALOG_SOURCE

    @property
    def available(self) -> bool:
        if self.protocol == "bedrock_converse":
            return importlib.util.find_spec("boto3") is not None
        return True


def _price(
    currency: str, input_per_mtok: float, output_per_mtok: float, cached: float | None = None
) -> ModelPriceConfig:
    return ModelPriceConfig(
        currency=currency,
        input_per_mtok=input_per_mtok,
        output_per_mtok=output_per_mtok,
        cached_input_per_mtok=cached,
    )


def _model(
    model_id: str,
    *,
    display: str = "",
    context: int | None = None,
    output: int | None = None,
    tools: bool = True,
    json_mode: bool = True,
    temperature: bool = True,
    thinking: bool | None = None,
    price: ModelPriceConfig | None = None,
    note: str | None = None,
) -> ModelConfig:
    return ModelConfig(
        model_id=model_id,
        display_name=display or model_id,
        context_window=context,
        max_output_tokens=output,
        supports_tools=tools,
        supports_json_mode=json_mode,
        supports_temperature=temperature,
        supports_thinking=thinking,
        price=price or ModelPriceConfig(),
        source="builtin",
        source_note=note or "Template only; verify capabilities and pricing with the provider.",
    )


_VERIFIED_DEEPSEEK = _model(
    "deepseek-flash",
    context=1_000_000,
    output=4096,
    thinking=True,
    price=_price("CNY", 2.0, 8.0, 0.04),
    note="configs/llm.yaml, DeepSeek pricing checked 2026-10-09 (peak tier).",
)
_VERIFIED_KIMI = _model(
    "kimi-k2.6",
    context=256_000,
    output=4096,
    temperature=False,
    thinking=True,
    price=_price("CNY", 6.5, 27.0, 1.1),
    note="configs/llm.yaml, Moonshot pricing checked 2026-10-09.",
)
_VERIFIED_GLM_47 = _model(
    "glm-4.7",
    context=200_000,
    output=4096,
    thinking=True,
    price=_price("CNY", 4.0, 16.0, 0.8),
    note="configs/llm.yaml, BigModel pricing checked 2026-10-17 (highest input tier).",
)
_VERIFIED_GLM_5 = _model(
    "glm-5",
    context=200_000,
    output=4096,
    temperature=False,
    thinking=False,
    price=_price("CNY", 6.0, 22.0, 1.5),
    note="configs/llm.yaml, BigModel pricing checked 2026-10-17.",
)
_VERIFIED_GLM_53 = _model(
    "glm-5.3",
    context=1_000_000,
    output=4096,
    temperature=False,
    thinking=False,
    price=_price("CNY", 8.0, 28.0, 2.0),
    note="configs/llm.yaml, BigModel pricing checked 2026-10-17.",
)
_VERIFIED_GLM_53_FLASH = _model(
    "glm-5.3-flash",
    context=1_000_000,
    output=4096,
    temperature=False,
    thinking=True,
    price=_price("CNY", 0.8, 2.8, 0.23),
    note="configs/llm.yaml, BigModel flash tier checked 2026-10-18.",
)
_ZAI_47 = _model(
    "glm-4.7",
    context=200_000,
    output=4096,
    thinking=True,
    price=_price("USD", 0.6, 2.2, 0.11),
    note="configs/llm.yaml, Z.AI USD pricing checked 2026-10-17.",
)
_ZAI_53 = _model(
    "glm-5.3",
    context=1_000_000,
    output=4096,
    temperature=False,
    thinking=False,
    price=_price("USD", 1.4, 4.4, 0.26),
    note="configs/llm.yaml, Z.AI USD pricing; availability smoke-tested 2026-10-17.",
)

_TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        catalog_id="openai",
        name="OpenAI",
        protocol="openai_compatible",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        docs_url="https://platform.openai.com/docs",
        notes="Verify model ids, context windows and prices before enabling a live run.",
        models=[
            _model("gpt-4.1", note="Model id template only; no verified price or context window."),
            _model(
                "gpt-4.1-mini", note="Model id template only; no verified price or context window."
            ),
        ],
    ),
    ProviderTemplate(
        catalog_id="anthropic",
        name="Anthropic",
        protocol="anthropic_messages",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        docs_url="https://docs.anthropic.com/en/api/messages",
        notes="Native Messages API. Tool calling is adapted without an OpenAI shim.",
        models=[
            _model(
                "claude-sonnet-4-5",
                note="Model id template only; no verified price or context window.",
            ),
            _model(
                "claude-opus-4-1",
                note="Model id template only; no verified price or context window.",
            ),
        ],
    ),
    ProviderTemplate(
        catalog_id="deepseek",
        name="DeepSeek",
        protocol="openai_compatible",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        docs_url="https://api-docs.deepseek.com",
        notes="Specifications copied from the validated repository configuration.",
        models=[_VERIFIED_DEEPSEEK],
    ),
    ProviderTemplate(
        catalog_id="zai",
        name="Z.AI",
        protocol="openai_compatible",
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="GLM_API_KEY",
        docs_url="https://docs.z.ai",
        notes="International endpoint. USD pricing requires an exchange rate for CNY budgets.",
        models=[_ZAI_47, _ZAI_53],
    ),
    ProviderTemplate(
        catalog_id="bigmodel",
        name="BigModel",
        protocol="openai_compatible",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="GLM_API_KEY",
        docs_url="https://docs.bigmodel.cn",
        notes="China endpoint. Uses the same GLM provider implementation validated in Phase 11.",
        models=[_VERIFIED_GLM_47, _VERIFIED_GLM_5, _VERIFIED_GLM_53, _VERIFIED_GLM_53_FLASH],
    ),
    ProviderTemplate(
        catalog_id="moonshot",
        name="Moonshot / Kimi",
        protocol="openai_compatible",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        docs_url="https://platform.moonshot.cn/docs",
        notes="Specifications copied from the validated repository configuration.",
        models=[_VERIFIED_KIMI],
    ),
    ProviderTemplate(
        catalog_id="qwen",
        name="Alibaba Cloud Model Studio / Qwen",
        protocol="openai_compatible",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        docs_url="https://help.aliyun.com/zh/model-studio/",
        notes="OpenAI-compatible mode. No verified pricing catalogue is bundled.",
        models=[
            _model(
                "qwen3-coder-plus",
                note="Model id template only; verify availability and pricing in Model Studio.",
            )
        ],
    ),
    ProviderTemplate(
        catalog_id="bedrock",
        name="AWS Bedrock",
        protocol="bedrock_converse",
        api_key_env=None,
        auth_kind="aws",
        docs_url="https://docs.aws.amazon.com/bedrock/latest/userguide/converse.html",
        notes=(
            "Uses the AWS SDK credential chain (environment, profile or instance role). "
            "An API key is never used to impersonate IAM. The optional boto3 extra is required."
        ),
        models=[],
    ),
    ProviderTemplate(
        catalog_id="custom",
        name="Custom provider",
        protocol="openai_compatible",
        base_url="",
        api_key_env=None,
        notes="Add an HTTPS endpoint that speaks OpenAI Chat Completions.",
        models=[],
    ),
)


def catalog_templates() -> list[ProviderTemplate]:
    return list(_TEMPLATES)


def template(catalog_id: str) -> ProviderTemplate:
    for item in _TEMPLATES:
        if item.catalog_id == catalog_id:
            return item
    raise KeyError(f"unknown provider template: {catalog_id}")
