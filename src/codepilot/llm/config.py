"""LLM 配置（02 §5.3）。

为什么模型 ID 和单价只放在配置里，一个字都不写进代码：这两样东西随服务商
发版变化，写死在代码里意味着"换个模型要改代码、重跑测试、重新发版"。
更重要的是，代码里一旦出现具体模型名，就很难阻止有人写出
``if model == "deepseek-chat"`` 这种分支，而这正是 02 §5.3 明令禁止的。

配套的 ``check()`` 是关键：配置文件里允许留 ``<官方模型 ID>`` 这样的占位符，
但必须有一个命令能把"哪些还没填"一次说清楚。没有它，占位符会一路混进
benchmark，最后表现为"费用是 0，因为没配价格"。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from codepilot.config import read_secret_env

__all__ = [
    "KNOWN_ROLES",
    "ConfigReport",
    "LLMConfig",
    "ModelPrice",
    "ModelSpec",
    "ProviderSpec",
    "is_placeholder",
    "load_llm_config",
]

#: 02 §5.3 列出的 Agent 角色。未知角色名会出现在 ``ConfigReport.unknown_roles``
#: 里而不是被静默接受——一个拼错的角色名会让对应 Agent 悄悄用上默认模型。
KNOWN_ROLES: frozenset[str] = frozenset(
    {
        "issue_analyzer",
        "localizer",
        "planner",
        "coder",
        "reproducer",
        "diagnoser",
        "reviewer",
        "failure_classifier",
    }
)

_PLACEHOLDER_RE = re.compile(r"^\s*<[^>]*>\s*$")


def is_placeholder(value: Any) -> bool:
    """值是否是一个未填写的占位符。

    认两种形态：``<官方模型 ID>`` 这样的尖括号，以及空字符串 / None。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or bool(_PLACEHOLDER_RE.match(value))
    return False


def _placeholder_to_none(value: Any) -> Any:
    return None if is_placeholder(value) else value


class ModelPrice(BaseModel):
    """模型单价。全部是**每百万 token** 的价格，与各家官方文档口径一致。"""

    currency: str = "CNY"
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None
    cached_input_per_mtok: float | None = None

    @field_validator("input_per_mtok", "output_per_mtok", "cached_input_per_mtok", mode="before")
    @classmethod
    def _allow_placeholders(cls, value: Any) -> Any:
        return _placeholder_to_none(value)

    @property
    def is_complete(self) -> bool:
        """输入价与输出价都必须有，缓存价可以缺省（缺省时按输入价算）。"""
        return self.input_per_mtok is not None and self.output_per_mtok is not None


class ProviderSpec(BaseModel):
    """一家服务商的接入参数。密钥只存**环境变量名**。"""

    name: str
    base_url: str
    api_key_env: str

    max_concurrency: int = 4
    """每 provider 独立的并发上限，benchmark 并行跑任务时不会撞上服务商限流。"""

    timeout_seconds: float = 120.0
    extra_headers: dict[str, str] = Field(default_factory=dict)

    def has_api_key(self) -> bool:
        return read_secret_env(self.api_key_env) is not None


class ModelSpec(BaseModel):
    """项目内的模型别名及其能力。"""

    alias: str
    provider: str
    model: str
    context_window: int = 128_000

    supports_tools: bool = True
    supports_json_mode: bool = True
    supports_temperature: bool = True

    max_output_tokens: int = 4096
    price: ModelPrice | None = None

    @field_validator("model", mode="before")
    @classmethod
    def _model_id_placeholder(cls, value: Any) -> Any:
        return value if isinstance(value, str) else str(value)

    @property
    def model_id_configured(self) -> bool:
        return not is_placeholder(self.model)

    @property
    def price_configured(self) -> bool:
        return self.price is not None and self.price.is_complete

    def missing_fields(self) -> list[str]:
        """返回尚未核实的字段名，供 ``codepilot config check`` 输出。"""
        missing: list[str] = []
        if not self.model_id_configured:
            missing.append("model")
        if not self.price_configured:
            missing.append("price")
        return missing


class ConfigReport(BaseModel):
    """配置体检结果。

    ``ready`` 为 False 不等于"不能用"：缺价格的模型仍然可以跑，
    只是费用会记为 0 且 ``cost_known=False``。是否放行由调用方决定。
    """

    providers_missing_key: list[str] = Field(default_factory=list)
    models_missing_fields: dict[str, list[str]] = Field(default_factory=dict)
    roles_unmapped: list[str] = Field(default_factory=list)
    unknown_roles: list[str] = Field(default_factory=list)
    unknown_model_refs: dict[str, str] = Field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return not (
            self.providers_missing_key
            or self.models_missing_fields
            or self.roles_unmapped
            or self.unknown_model_refs
        )

    def render(self) -> str:
        """人能读的体检报告。"""
        lines: list[str] = []
        if self.providers_missing_key:
            lines.append("以下 provider 缺 API key（对应环境变量未设置）：")
            lines.extend(f"  - {name}" for name in self.providers_missing_key)
        if self.models_missing_fields:
            lines.append("以下模型有未核实字段（价格会记为 0，且标记 cost_known=False）：")
            for alias, fields in sorted(self.models_missing_fields.items()):
                lines.append(f"  - {alias}: {', '.join(fields)}")
        if self.unknown_model_refs:
            lines.append("以下角色指向了不存在的模型别名：")
            for role, alias in sorted(self.unknown_model_refs.items()):
                lines.append(f"  - {role} -> {alias}")
        if self.roles_unmapped:
            lines.append("以下已知角色未映射模型：")
            lines.extend(f"  - {role}" for role in self.roles_unmapped)
        if self.unknown_roles:
            lines.append("配置中出现未知角色名（可能是拼写错误）：")
            lines.extend(f"  - {role}" for role in self.unknown_roles)
        if not lines:
            lines.append("配置完整：provider、模型、角色映射均已填写。")
        return "\n".join(lines)


class LLMConfig(BaseModel):
    """``configs/llm.yaml`` 的完整内容。"""

    providers: dict[str, ProviderSpec] = Field(default_factory=dict)
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    roles: dict[str, str] = Field(default_factory=dict)

    allowed_providers: list[str] | None = None
    """白名单。``None`` 表示不限制；非空时未列出的 provider 拒绝调用（02 §5.3）。"""

    exchange_rates: dict[str, float] = Field(default_factory=dict)
    """币种 → 相对 ``budget_currency`` 的汇率。例如 ``{"USD": 7.1}`` 表示
    1 USD = 7.1 CNY。缺汇率的币种按 1.0 处理并会在报告里标注。"""

    def resolve_role(self, role: str) -> ModelSpec:
        """角色 → 模型。角色未映射时抛 KeyError，不静默兜底。"""
        alias = self.roles.get(role)
        if alias is None:
            raise KeyError(f"角色 {role!r} 未在 roles 中映射模型")
        spec = self.models.get(alias)
        if spec is None:
            raise KeyError(f"角色 {role!r} 指向了不存在的模型别名 {alias!r}")
        return spec

    def provider_for(self, spec: ModelSpec) -> ProviderSpec:
        provider = self.providers.get(spec.provider)
        if provider is None:
            raise KeyError(f"模型 {spec.alias!r} 指向了不存在的 provider {spec.provider!r}")
        return provider

    def check(self) -> ConfigReport:
        report = ConfigReport()

        for name, provider in sorted(self.providers.items()):
            if not provider.has_api_key():
                report.providers_missing_key.append(name)

        for alias, spec in sorted(self.models.items()):
            missing = spec.missing_fields()
            if missing:
                report.models_missing_fields[alias] = missing
            if spec.provider not in self.providers:
                report.unknown_model_refs[alias] = spec.provider

        report.unknown_roles = sorted(set(self.roles) - KNOWN_ROLES)
        report.roles_unmapped = sorted(KNOWN_ROLES - set(self.roles))
        for role, alias in sorted(self.roles.items()):
            if alias not in self.models:
                report.unknown_model_refs[role] = alias

        return report


def _inject_names(raw: dict[str, Any]) -> dict[str, Any]:
    """把 YAML 中作为字典键的名字写回对象内部。

    ``providers: {deepseek: {...}}`` 里的 ``deepseek`` 是键不是字段，
    但下游到处需要 ``provider.name``，所以在加载时补齐而不是让每个调用方
    自己记着带上下文。
    """
    data = dict(raw)

    providers = data.get("providers") or {}
    data["providers"] = {name: {**body, "name": name} for name, body in providers.items() if body}

    models = data.get("models") or {}
    data["models"] = {alias: {**body, "alias": alias} for alias, body in models.items() if body}

    return data


def load_llm_config(path: Path) -> LLMConfig:
    """从 YAML 加载 LLM 配置。

    文件不存在或不是合法 YAML 时抛异常——配置缺失必须立刻可见，
    退回到"内置默认配置"只会让人以为跑的是自己写的那份。
    """
    if not path.exists():
        raise FileNotFoundError(f"LLM 配置文件不存在: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"LLM 配置文件顶层必须是映射: {path}")

    return LLMConfig.model_validate(_inject_names(raw))
