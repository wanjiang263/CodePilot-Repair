"""GLM 平台画像与运行期覆盖。

这里**没有任何 HTTP 逻辑**：请求仍然只由 ``OpenAICompatibleProvider`` 发出。
本模块只回答三个问题——一家 GLM 平台的地址是什么、密钥从哪个变量读、
以及"用环境变量换平台 / 换模型"这件事如何落到一份 :class:`LLMConfig` 上。

之所以值得单独一个文件：智谱有**两个**官方标准端点（Z.AI 国际站与 BigModel
中国站），密钥分属不同命名空间、可能互不通用。把它们表达成两个 provider 配置项
而不是 ``if provider == "zai"``，正是"接入新服务商只是往 YAML 里加一段配置"的
意思——本模块给出的只是那段配置的权威模板。

端点选择有一条红线：标准 API 端点、Coding Plan 端点、Anthropic / Responses 形态
端点的**认证命名空间不同**，不允许拿同一个 base_url 变量去试。下面的常量只列
官方标准 Chat Completions 地址。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from codepilot.llm.config import LLMConfig, ModelSpec, ProviderSpec

__all__ = [
    "DEFAULT_GLM_PLATFORM",
    "DEFAULT_TIMEOUT_SECONDS",
    "GLM_API_KEY_ENV",
    "GLM_ENV_KEYS",
    "GLM_PLATFORMS",
    "ZHIPU_API_KEY_ENV",
    "GlmConfigError",
    "apply_glm_env",
    "glm_platform",
    "glm_provider_spec",
    "glm_readiness",
]

#: 会被 :func:`apply_glm_env` 读取的环境变量。**只有出现任意一个时才会生效**，
#: 见该函数的说明。
GLM_ENV_KEYS: tuple[str, ...] = (
    "GLM_PLATFORM",
    "GLM_BASE_URL",
    "GLM_MODEL",
    "GLM_TIMEOUT_SECONDS",
    "GLM_MAX_RETRIES",
)

#: 首选密钥变量名。BigModel 站的历史配置里用的是 ``ZHIPU_API_KEY``；两家平台的密钥
#: 官方明确写了**互不通用**，所以 provider 配置里各自仍可指定自己的名字，
#: ``api_key_env_fallback`` 只是让改名前后的 ``.env`` 都能被读到——读密钥仍然只有
#: :meth:`ProviderSpec.api_key` 这一条路径。
GLM_API_KEY_ENV = "GLM_API_KEY"
ZHIPU_API_KEY_ENV = "ZHIPU_API_KEY"

DEFAULT_GLM_PLATFORM = "zai"

DEFAULT_TIMEOUT_SECONDS = 120.0
"""思考类模型的长响应比"一次普通 HTTP 往返"慢得多，30 秒会稳定超时。"""


@dataclass(frozen=True, slots=True)
class GlmPlatform:
    """一家 GLM 官方标准端点。"""

    name: str
    base_url: str
    currency: str
    """该站公开计价的币种。费用币种必须与账单币种一致，不能猜。"""

    docs_url: str


#: 只含 OpenAI 兼容的**标准** Chat Completions 端点（请求路径 ``<base>/chat/completions``）。
#: 刻意不含 ``/api/coding/paas/v4``（套餐端点）、``/api/anthropic``、``/api/v1``。
GLM_PLATFORMS: Mapping[str, GlmPlatform] = {
    "zai": GlmPlatform(
        name="zai",
        base_url="https://api.z.ai/api/paas/v4",
        currency="USD",
        docs_url="https://docs.z.ai/api-reference/introduction",
    ),
    "bigmodel": GlmPlatform(
        name="bigmodel",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        currency="CNY",
        docs_url="https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
    ),
}


class GlmConfigError(ValueError):
    """GLM 平台 / 端点 / 模型别名配置不合法。

    归 ``ValueError`` 而不归 LLM 层异常：这是一次**配置**错误，发生在构造 provider
    之前。把它做成 ``ProviderError`` 会让"环境变量名拼错"在报告里长得像"模型失败"。
    """


def glm_platform(name: str) -> GlmPlatform:
    """按平台名取画像；未知名字直接报错，不回退到默认平台。

    静默回退会让 ``GLM_PLATFORM=zaii`` 变成"打错了，但跑的是另一家"，而两家密钥
    与计价都不同，症状是一次看起来莫名其妙的 401。
    """
    normalized = name.strip().lower()
    platform = GLM_PLATFORMS.get(normalized)
    if platform is None:
        known = ", ".join(sorted(GLM_PLATFORMS))
        raise GlmConfigError(f"未知 GLM 平台 {name!r}；可用：{known}")
    return platform


def _reject_non_chat_completions_endpoint(base_url: str) -> None:
    """拒绝把套餐端点 / 其它协议形态端点当成标准 Chat Completions 使用。

    这不是洁癖，有两条来自官方文档与公告的依据（**注意：本轮没有实测过套餐端点**，
    结论来自文档，不是来自一次成功的调用）：

    1. ``/api/coding/paas/v4`` 按**套餐积分**计费，且官方公告写明调用 ``glm-4.7`` /
       ``glm-5.1`` 会被自动改写到别的模型上 —— 届时"这个模型 ID × 这份 token 单价"
       的费用口径整段失真，而报表上看不出任何异常。
    2. ``/api/anthropic``（Anthropic 形态）与 ``/api/v1``（Responses 形态）的请求体
       字段不同，用同一个客户端去试，症状是一堆难以归因的 400。
    """
    lowered = base_url.lower()
    for marker, hint in (
        (
            "/coding/",
            "这是 Coding Plan 套餐端点（按积分计费且会改写模型），请改用标准 API 端点",
        ),
        ("/anthropic", "这是 Anthropic 形态端点，不是 OpenAI 兼容的 Chat Completions"),
    ):
        if marker in lowered:
            raise GlmConfigError(f"GLM base_url {base_url!r} 不可用于标准 Chat Completions：{hint}")


def glm_provider_spec(
    platform: str,
    *,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
    max_concurrency: int = 4,
) -> ProviderSpec:
    """构造一个 GLM provider 配置项。"""
    profile = glm_platform(platform)
    selected_base_url = base_url or profile.base_url
    _reject_non_chat_completions_endpoint(selected_base_url)
    if max_retries is not None and max_retries < 0:
        raise GlmConfigError(f"GLM_MAX_RETRIES 不能为负，收到 {max_retries}")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise GlmConfigError(f"GLM_TIMEOUT_SECONDS 必须为正数，收到 {timeout_seconds}")
    return ProviderSpec(
        name=profile.name,
        base_url=selected_base_url,
        api_key_env=GLM_API_KEY_ENV,
        api_key_env_fallback=ZHIPU_API_KEY_ENV,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
        max_retries=max_retries,
    )


def _env(name: str, environ: Mapping[str, str] | None) -> str | None:
    source: Mapping[str, str] = environ if environ is not None else os.environ
    value = source.get(name)
    return value.strip() if value is not None and value.strip() else None


def _env_float(name: str, environ: Mapping[str, str] | None) -> float | None:
    raw = _env(name, environ)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise GlmConfigError(f"{name} 必须是数字（秒），收到 {raw!r}") from exc


def _env_int(name: str, environ: Mapping[str, str] | None) -> int | None:
    raw = _env(name, environ)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise GlmConfigError(f"{name} 必须是整数，收到 {raw!r}") from exc


def apply_glm_env(
    config: LLMConfig,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
) -> LLMConfig:
    """把 GLM 运行期覆盖写成一份**新**配置。

    显式参数优先于环境变量，环境变量优先于 YAML。覆盖只作用于 provider 归属等于所选
    平台的那些模型别名——判断依据是配置里的 provider 关系，不是别名或模型 ID 的字符串
    形状，因此不违反"业务代码不按 provider 名字分支"。

    没有任何 GLM 环境变量、也没有显式参数时**原样返回**：DeepSeek/Kimi 的运行不该
    因为这段代码存在而被悄悄多塞一个 provider，那会让 ``provider_config_hash`` 与
    历史批次对不上，而差异原因完全看不出来。

    返回新对象而不是原地修改：调用方会把这份配置同时用于 ``provider_config_hash`` 与
    ``model_identity``，就地改写会让"生效配置"和"被哈希的配置"分家——那正是不可复现
    运行的来源。
    """
    explicit = any(
        value is not None for value in (platform, model, base_url, timeout_seconds, max_retries)
    )
    source: Mapping[str, str] = environ if environ is not None else os.environ
    if not explicit and not any(_env(key, source) for key in GLM_ENV_KEYS):
        return config

    selected = platform or _env("GLM_PLATFORM", source) or DEFAULT_GLM_PLATFORM
    profile = glm_platform(selected)
    previous = config.providers.get(profile.name)

    # YAML 已有的 base_url 优先于本模块的模板地址：配置文件是这条信息的唯一出处，
    # 有人把它改成自建代理后，一次 GLM_MODEL 覆盖不该把代理地址悄悄换回官方地址。
    effective_base_url = (
        base_url
        or _env("GLM_BASE_URL", source)
        or (previous.base_url if previous else profile.base_url)
    )
    effective_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _env_float("GLM_TIMEOUT_SECONDS", source)
    )
    effective_retries = (
        max_retries if max_retries is not None else _env_int("GLM_MAX_RETRIES", source)
    )
    effective_model = model or _env("GLM_MODEL", source)

    # 没给覆盖时必须沿用 YAML 里已有的值，而不是回到本模块的默认值：否则
    # "只想换个模型 ID"的一次 GLM_MODEL 覆盖会顺带把 provider 的超时与重试
    # 次数抹掉，而配置 diff 里看不出这件事。
    provider = glm_provider_spec(
        profile.name,
        base_url=effective_base_url,
        timeout_seconds=(
            effective_timeout
            if effective_timeout is not None
            else (previous.timeout_seconds if previous else None)
        ),
        max_retries=(
            effective_retries
            if effective_retries is not None
            else (previous.max_retries if previous else None)
        ),
        max_concurrency=previous.max_concurrency if previous else 4,
    )

    models: dict[str, ModelSpec] = {
        alias: (
            spec.model_copy(update={"model": effective_model})
            if effective_model and spec.provider == profile.name
            else spec
        )
        for alias, spec in config.models.items()
    }
    return config.model_copy(
        update={"providers": {**config.providers, provider.name: provider}, "models": models}
    )


def glm_readiness(
    config: LLMConfig, alias: str, *, budget_currency: str | None = None
) -> str | None:
    """这个别名现在能不能真实调用；返回问题描述，``None`` 表示没有障碍。

    刻意不抛异常：调用方要把"缺密钥""模型 ID 还是占位符""单价未核实""币种没有汇率"
    如实报告成 NOT RUN，而不是翻译成一次看起来像模型能力失败的异常。

    ``budget_currency`` 是**本次运行**的费用币种。Z.AI 以 USD 计价：把预算币种改成
    USD 之后就不需要汇率，此时不该被配置里的默认 CNY 误判成"缺汇率"而拒绝放行。
    """
    target = budget_currency or config.budget_currency
    spec = config.models.get(alias)
    if spec is None:
        return f"模型别名 {alias!r} 不在配置中"
    provider = config.providers.get(spec.provider)
    if provider is None:
        return f"模型 {alias!r} 指向了未定义的 provider {spec.provider!r}"
    try:
        _reject_non_chat_completions_endpoint(provider.base_url)
    except GlmConfigError as exc:
        return str(exc)
    if not spec.model_id_configured:
        return f"模型别名 {alias!r} 的 model 字段仍是占位符"
    if provider.api_key() is None:
        names = [provider.api_key_env]
        if provider.api_key_env_fallback:
            names.append(provider.api_key_env_fallback)
        return f"环境变量 {' 或 '.join(names)} 均未设置（provider={provider.name}）"
    if not spec.price_configured or spec.price is None:
        return f"模型别名 {alias!r} 未配置单价：真实调用会产生无法归集的费用，live 冒烟拒绝执行"
    if spec.price.currency != target and spec.price.currency not in config.exchange_rates:
        return (
            f"模型别名 {alias!r} 的单价币种是 {spec.price.currency}，预算币种是 {target}，"
            "而 exchange_rates 没有声明该币种汇率；按 1.0 混算是错的，不接受。"
            f"要么声明汇率，要么用 --budget-currency {spec.price.currency}。"
        )
    return None
