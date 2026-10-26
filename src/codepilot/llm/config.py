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
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

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
    api_key_env: str = ""
    api_key_env_fallback: str | None = None
    """备用密钥环境变量名，用于改名过渡期。

    智谱的两家平台历史上分别用过 ``ZHIPU_API_KEY`` 与 ``GLM_API_KEY``。只认一个名字
    会让旧 ``.env`` 在升级后静默变成"provider 未配置"；两个都认又会让配置里出现歧义。
    显式的 fallback 让优先级写在配置里，读密钥的代码只有一条路径。
    """

    protocol: str = "openai_compatible"
    """``openai_compatible`` / ``anthropic_messages`` / ``bedrock_converse``.

    协议属于 provider，而不是模型：同一家服务商的模型共享端点语义。
    """

    region: str | None = None
    aws_profile: str | None = None

    api_key_value: SecretStr | None = Field(default=None, exclude=True, repr=False)
    """Runtime-only credential injected by the settings service.

    It is excluded from dumps and task snapshots. YAML/CLI configurations leave
    it unset and keep resolving credentials by environment variable name.
    """

    max_concurrency: int = 4
    """每 provider 独立的并发上限，benchmark 并行跑任务时不会撞上服务商限流。"""

    timeout_seconds: float = 120.0
    connect_timeout_seconds: float | None = Field(default=None, gt=0)
    write_timeout_seconds: float | None = Field(default=None, gt=0)
    pool_timeout_seconds: float | None = Field(default=None, gt=0)
    trust_env: bool = True
    max_retries: int | None = None
    """该 provider 的重试次数上限（不含首次请求）。

    放在 provider 而不是全局：限流策略是账号级的，各家给的配额窗口不一样，
    对一家合适的重试次数对另一家可能把一次任务的延迟乘以四。``None`` 表示
    沿用调用方传入的 ``RetryPolicy``。
    """

    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("protocol")
    @classmethod
    def _known_protocol(cls, value: str) -> str:
        allowed = {"openai_compatible", "anthropic_messages", "bedrock_converse"}
        if value not in allowed:
            raise ValueError(f"unsupported provider protocol: {value}")
        return value

    def api_key(self) -> str | None:
        """按运行时注入值 → ``api_key_env`` → fallback 的顺序读密钥。"""
        if self.api_key_value is not None:
            injected = self.api_key_value.get_secret_value().strip()
            if injected:
                return injected
        if not self.api_key_env:
            return None
        env_value = read_secret_env(self.api_key_env)
        if env_value is not None:
            return env_value
        if self.api_key_env_fallback:
            return read_secret_env(self.api_key_env_fallback)
        return None

    def has_api_key(self) -> bool:
        if self.protocol == "bedrock_converse":
            return True
        return self.api_key() is not None


class ModelSpec(BaseModel):
    """项目内的模型别名及其能力。"""

    alias: str
    provider: str
    model: str
    context_window: int = 128_000

    supports_tools: bool = True
    supports_json_mode: bool = True
    supports_temperature: bool = True

    supports_thinking: bool | None = None
    """该别名是否接受 ``thinking: {type: enabled|disabled}``。

    ``None`` 表示"按配置里的 ``extra_body`` 原样发送，能力未知"。它存在的理由不是
    方便，而是**兼容**：GLM 的 glm-4.7 与 GLM-5 系列对同一个参数的反应相反——一个
    收到 ``thinking`` 可能直接 400，另一个的思考模式无法关闭。这类差异只能由配置
    声明，代码里一旦出现 ``if model == ...`` 就变成全仓库找分支。
    """

    max_output_tokens: int = 4096
    price: ModelPrice | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """模型专属的请求参数，例如思考模式开关；直接合入请求体，不按厂商分支。"""

    @model_validator(mode="after")
    def _reject_thinking_when_unsupported(self) -> ModelSpec:
        if self.supports_thinking is False and "thinking" in self.extra_body:
            raise ValueError(
                f"模型别名 {self.alias!r} 声明 supports_thinking: false，"
                "但 extra_body 里仍带 thinking——两个字段会互相矛盾，请求体只能听一个"
            )
        return self

    @field_validator("extra_body")
    @classmethod
    def _protect_request_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "model",
            "messages",
            "max_tokens",
            "temperature",
            "tools",
            "tool_choice",
            "response_format",
            "stream",
        }
        conflicts = sorted(reserved.intersection(value))
        if conflicts:
            raise ValueError(f"extra_body 不可覆盖请求或能力字段: {', '.join(conflicts)}")
        return value

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
    currency_unmatched: dict[str, str] = Field(default_factory=dict)
    roles_unmapped: list[str] = Field(default_factory=list)
    unknown_roles: list[str] = Field(default_factory=list)
    unknown_model_refs: dict[str, str] = Field(default_factory=dict)
    budget_currency: str = "CNY"

    @property
    def ready(self) -> bool:
        return not (
            self.providers_missing_key
            or self.models_missing_fields
            or self.currency_unmatched
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
        if self.currency_unmatched:
            lines.append(
                "以下**已被角色使用**的模型，单价币种与预算币种不同且缺汇率"
                "（缺汇率时按 1.0 换算会把费用低估约一个数量级，因此视为配置不完整）："
            )
            for alias, currency in sorted(self.currency_unmatched.items()):
                lines.append(f"  - {alias}: 单价 {currency}，预算 {self.budget_currency}")
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

    budget_currency: str = "CNY"
    """费用与预算的主币种。Z.AI 以 USD 计价、BigModel 以 CNY 计价，混在一张报表里
    读出的总额是没有意义的，因此由配置显式决定，而不是由 provider 名字推断。"""

    def for_model(self, alias: str) -> LLMConfig:
        """Explicit experiment model selection overrides every default role."""
        if alias not in self.models:
            raise KeyError(f"unknown experiment model alias: {alias}")
        return self.model_copy(update={"roles": dict.fromkeys(self.roles, alias)})

    def resolve_role(self, role: str) -> ModelSpec:
        """角色 → 模型。角色未映射时抛 KeyError，不静默兜底。"""
        alias = self.roles.get(role)
        if alias is None:
            raise KeyError(f"角色 {role!r} 未在 roles 中映射模型")
        spec = self.models.get(alias)
        if spec is None:
            raise KeyError(f"角色 {role!r} 指向了不存在的模型别名 {alias!r}")
        return spec

    def bind_all_roles(self, alias: str) -> LLMConfig:
        """返回一份把**所有** Agent 角色都指向同一别名的新配置。

        用于 ``--llm-model`` 这类"这一整批都用同一个模型"的显式覆盖。返回新对象：
        就地改 roles 会让调用方手里的同一份配置在不同地方表现不同，而
        ``provider_config_hash`` 恰恰要求"生效配置"是一个确定的整体。
        """
        if alias not in self.models:
            known = ", ".join(sorted(self.models))
            raise KeyError(f"模型别名 {alias!r} 不在配置中；已知别名：{known}")
        roles = {**self.roles, **dict.fromkeys(KNOWN_ROLES, alias)}
        return self.model_copy(update={"roles": roles})

    def provider_for(self, spec: ModelSpec) -> ProviderSpec:
        provider = self.providers.get(spec.provider)
        if provider is None:
            raise KeyError(f"模型 {spec.alias!r} 指向了不存在的 provider {spec.provider!r}")
        return provider

    def check(self) -> ConfigReport:
        report = ConfigReport(budget_currency=self.budget_currency)

        for name, provider in sorted(self.providers.items()):
            if not provider.has_api_key():
                report.providers_missing_key.append(name)

        for alias, spec in sorted(self.models.items()):
            missing = spec.missing_fields()
            if missing:
                report.models_missing_fields[alias] = missing
            if spec.provider not in self.providers:
                report.unknown_model_refs[alias] = spec.provider

        # 只检查**正在被使用**的别名。声明一个 USD 价格但没有任何角色引用它，是
        # 一份待填写的清单；把它映射到角色上却缺汇率，才会真的把费用低估七倍。
        used_aliases = sorted(set(self.roles.values()) & set(self.models))
        for alias in used_aliases:
            price = self.models[alias].price
            if price is None or not price.is_complete:
                continue
            if price.currency != self.budget_currency and price.currency not in self.exchange_rates:
                report.currency_unmatched[alias] = price.currency

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


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """拒绝 YAML 里重复的映射键。

    PyYAML 默认让**后一份静默覆盖前一份**。在别的配置文件里这顶多是 annoying；
    在这个文件里它决定"这次跑的是哪个模型、按哪份单价计费"，而两份定义可以同时存在
    很久都不被发现——报表与冒烟只会说"我不知道"。重复别名是本轮开发中真实犯过的错，
    所以这里设了一道闸，而不是靠人记得检查。
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, str) and key in seen:
                raise ValueError(
                    f"LLM 配置里有重复的键 {key!r}（第 {key_node.start_mark.line + 1} 行）："
                    "后一份会静默覆盖前一份，请删掉其中一份"
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


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
    # 再过一遍带重复键检查的加载器：safe_load 已经确认语法合法，这里只关心键的唯一性。
    _NoDuplicateKeyLoader(path.read_text(encoding="utf-8")).get_single_data()

    return LLMConfig.model_validate(_inject_names(raw))
