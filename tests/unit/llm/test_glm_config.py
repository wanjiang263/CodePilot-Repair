"""GLM 平台画像、运行期覆盖与币种闸门（纯配置层，零网络）。

这一份文件证明的是"接一家服务商只是加配置"这件事真的成立：没有一个字节的请求
发出，也没有一行 ``if provider == "glm"``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from codepilot.llm.config import KNOWN_ROLES, LLMConfig, ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.errors import RateLimitError
from codepilot.llm.glm import (
    GLM_API_KEY_ENV,
    GLM_PLATFORMS,
    ZHIPU_API_KEY_ENV,
    GlmConfigError,
    apply_glm_env,
    glm_platform,
    glm_provider_spec,
    glm_readiness,
)
from codepilot.llm.registry import LLMRegistry, transport_provider
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.filterwarnings("ignore")


def _config(**overrides: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "providers": {
            "bigmodel": {
                "name": "bigmodel",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key_env": GLM_API_KEY_ENV,
                "max_retries": 2,
            },
            "zai": {
                "name": "zai",
                "base_url": "https://api.z.ai/api/paas/v4",
                "api_key_env": GLM_API_KEY_ENV,
            },
        },
        "models": {
            "glm-4.7-main": {
                "alias": "glm-4.7-main",
                "provider": "bigmodel",
                "model": "glm-4.7",
                "price": {"currency": "CNY", "input_per_mtok": 4.0, "output_per_mtok": 16.0},
            },
            "glm-5-zai": {
                "alias": "glm-5-zai",
                "provider": "zai",
                "model": "glm-5",
                "price": {"currency": "USD", "input_per_mtok": 1.0, "output_per_mtok": 3.2},
            },
        },
        "roles": dict.fromkeys(KNOWN_ROLES, "glm-4.7-main"),
        "exchange_rates": {"CNY": 1.0},
        "budget_currency": "CNY",
    }
    base.update(overrides)
    return LLMConfig.model_validate(base)


# --------------------------------------------------------------- 平台画像


def test_two_official_standard_endpoints_are_configured() -> None:
    assert GLM_PLATFORMS["zai"].base_url == "https://api.z.ai/api/paas/v4"
    assert GLM_PLATFORMS["bigmodel"].base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert GLM_PLATFORMS["zai"].currency == "USD"
    assert GLM_PLATFORMS["bigmodel"].currency == "CNY"


def test_platform_name_is_case_insensitive_and_trimmed() -> None:
    assert glm_platform("  Z.AI ".replace(".", "")).name == "zai"
    assert glm_platform("BigModel").name == "bigmodel"


def test_unknown_platform_fails_instead_of_falling_back() -> None:
    """拼错平台名不能"退到默认那家"：两家密钥与计价都不同，症状会是一句 401。"""
    with pytest.raises(GlmConfigError, match="未知 GLM 平台"):
        glm_platform("zaii")


@pytest.mark.parametrize(
    "url",
    [
        "https://open.bigmodel.cn/api/coding/paas/v4",
        "https://api.z.ai/api/coding/paas/v4",
        "https://api.z.ai/api/anthropic",
    ],
)
def test_non_chat_completions_endpoints_are_refused(url: str) -> None:
    """套餐端点按积分计费且会改写模型；Anthropic 形态请求体不同。

    把它们接进来不会报错，但会让"模型 ID × token 单价"的费用口径整段失真，
    而报表上看不出任何异常。
    """
    with pytest.raises(GlmConfigError, match="不可用于标准 Chat Completions"):
        glm_provider_spec("zai", base_url=url)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_retries": -1}, "不能为负"),
        ({"timeout_seconds": 0}, "必须为正数"),
    ],
)
def test_invalid_knobs_rejected(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(GlmConfigError, match=message):
        glm_provider_spec("zai", **kwargs)


def test_provider_spec_uses_both_key_variable_names() -> None:
    spec = glm_provider_spec("bigmodel")
    assert spec.api_key_env == GLM_API_KEY_ENV
    assert spec.api_key_env_fallback == ZHIPU_API_KEY_ENV


@pytest.fixture
def isolated_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把密钥来源限制在"进程环境变量"这一条上。

    ``read_secret_env`` 会回落到项目根的 ``.env``。开发机上那份文件里真的有 GLM
    密钥，不回落的测试会在本地全绿、在 CI（没有 .env）失败，或者反过来。这里把
    PROJECT_ROOT 指到 tmp，两条来源都变成"由测试说了算"。
    """
    import codepilot.config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv(GLM_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ZHIPU_API_KEY_ENV, raising=False)
    app_config._dotenv_values.cache_clear()


def test_provider_api_key_prefers_primary_then_fallback(
    isolated_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ZHIPU_API_KEY_ENV, "legacy-key")
    spec = glm_provider_spec("bigmodel")
    assert spec.api_key() == "legacy-key"

    monkeypatch.setenv(GLM_API_KEY_ENV, "primary-key")
    assert spec.api_key() == "primary-key"
    assert spec.has_api_key() is True


# --------------------------------------------------------------- 运行期覆盖


def test_no_env_means_untouched_config() -> None:
    """一个 GLM 变量都没设时，配置对象必须原样返回。

    DeepSeek/Kimi 的运行不应该因为这段代码存在而被悄悄多塞一个 provider——那会让
    provider_config_hash 与历史批次对不上，而差异原因完全看不出来。
    """
    config = _config()
    assert apply_glm_env(config, environ={}) is config


def test_platform_selection_targets_that_provider_only() -> None:
    config = _config()
    result = apply_glm_env(config, environ={"GLM_PLATFORM": "zai", "GLM_MODEL": "glm-4.6"})
    assert result.models["glm-5-zai"].model == "glm-4.6"
    assert result.models["glm-4.7-main"].model == "glm-4.7"
    assert result.providers["zai"].base_url == "https://api.z.ai/api/paas/v4"


def test_bigmodel_platform_leaves_zai_alone() -> None:
    config = _config()
    result = apply_glm_env(config, environ={"GLM_PLATFORM": "bigmodel", "GLM_MODEL": "glm-5"})
    assert result.models["glm-4.7-main"].model == "glm-5"
    assert result.models["glm-5-zai"].model == "glm-5"  # 原值


def test_base_url_and_knobs_are_overridden() -> None:
    config = _config()
    result = apply_glm_env(
        config,
        environ={
            "GLM_PLATFORM": "zai",
            "GLM_BASE_URL": "https://mirror.example.com/api/paas/v4",
            "GLM_TIMEOUT_SECONDS": "33.5",
            "GLM_MAX_RETRIES": "5",
        },
    )
    zai = result.providers["zai"]
    assert zai.base_url == "https://mirror.example.com/api/paas/v4"
    assert zai.timeout_seconds == 33.5
    assert zai.max_retries == 5


def test_model_override_does_not_clobber_existing_yaml_knobs() -> None:
    """只换模型 ID 时，YAML 里的超时/重试/自建地址都必须原样保留。

    否则"换个模型试一下"会顺带把 provider 的超时抹回默认值，而配置 diff 里看不出
    这件事发生过。
    """
    config = _config()
    result = apply_glm_env(config, environ={"GLM_PLATFORM": "bigmodel", "GLM_MODEL": "glm-5"})
    bigmodel = result.providers["bigmodel"]
    assert bigmodel.max_retries == 2
    assert bigmodel.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert bigmodel.timeout_seconds == 120.0


def test_missing_provider_is_injected_from_profile() -> None:
    config = _config(providers={"bigmodel": _config().providers["bigmodel"].model_dump()})
    result = apply_glm_env(config, environ={"GLM_PLATFORM": "zai"})
    assert result.providers["zai"].base_url == "https://api.z.ai/api/paas/v4"


@pytest.mark.parametrize(
    ("name", "value"),
    [("GLM_TIMEOUT_SECONDS", "soon"), ("GLM_MAX_RETRIES", "two")],
)
def test_non_numeric_env_fails_loudly(name: str, value: str) -> None:
    with pytest.raises(GlmConfigError, match="必须是"):
        apply_glm_env(_config(), environ={name: value})


def test_override_returns_new_object() -> None:
    config = _config()
    result = apply_glm_env(config, environ={"GLM_PLATFORM": "zai", "GLM_MODEL": "other"})
    assert config.models["glm-5-zai"].model == "glm-5"
    assert result is not config


# --------------------------------------------------------------- 角色绑定


def test_bind_all_roles_requires_existing_alias() -> None:
    config = _config()
    bound = config.bind_all_roles("glm-5-zai")
    assert set(bound.roles.values()) == {"glm-5-zai"}
    with pytest.raises(KeyError, match="不在配置中"):
        config.bind_all_roles("typo")


# --------------------------------------------------------------- 思考能力字段


def test_supports_thinking_false_strips_thinking_from_body() -> None:
    from codepilot.llm.capabilities import build_request_body
    from codepilot.llm.types import LLMRequest, Message

    spec = ModelSpec(
        alias="m",
        provider="p",
        model="glm-5",
        supports_thinking=False,
        extra_body={"reasoning_effort": "high"},
    )
    body = build_request_body(LLMRequest(messages=[Message.user("hi")]), spec, [])
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_declaring_thinking_while_unsupported_is_a_config_error() -> None:
    """两个字段互相矛盾时必须在加载期报错，而不是让请求体"听其中一个"。"""
    with pytest.raises(Exception, match="supports_thinking"):
        ModelSpec(
            alias="m",
            provider="p",
            model="glm-4.7",
            supports_thinking=False,
            extra_body={"thinking": {"type": "disabled"}},
        )


# --------------------------------------------------------------- 币种闸门


def test_usd_price_without_rate_blocks_a_used_alias() -> None:
    """已被角色使用的 USD 模型缺汇率 = 配置不完整。

    ``convert()`` 缺汇率时按 1.0 处理：USD 单价乘 1.0 记进 CNY 预算会把费用低估约
    七倍，而 cost_known 仍然是 True。这种数不能放行。
    """
    config = _config().bind_all_roles("glm-5-zai")
    report = config.check()
    assert report.currency_unmatched == {"glm-5-zai": "USD"}
    assert report.ready is False
    assert "币种" in report.render()


def test_usd_price_is_fine_once_rate_declared() -> None:
    config = _config(exchange_rates={"CNY": 1.0, "USD": 7.0}).bind_all_roles("glm-5-zai")
    assert config.check().currency_unmatched == {}


def test_unused_usd_alias_is_only_a_pending_entry() -> None:
    """声明了 USD 价格但没有任何角色引用它 → 不阻塞。

    待填写的清单和正在花的钱是两件事，混在一起会让人把 config check 当噪音忽略。
    """
    assert _config().check().currency_unmatched == {}


def test_same_currency_needs_no_rate() -> None:
    assert _config().check().currency_unmatched == {}


# --------------------------------------------------------------- 冒烟前置条件


def test_readiness_blocks_missing_price(
    isolated_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, "k")
    config = _config()
    naked = config.model_copy(
        update={
            "models": {
                **config.models,
                "glm-noprice": ModelSpec(alias="glm-noprice", provider="zai", model="glm-5"),
            }
        }
    )
    assert "未配置单价" in (glm_readiness(naked, "glm-noprice") or "")


def test_readiness_blocks_missing_key(isolated_keys: None) -> None:
    message = glm_readiness(_config(), "glm-4.7-main")
    assert message is not None and GLM_API_KEY_ENV in message


def test_readiness_blocks_coding_plan_endpoint(
    isolated_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, "k")
    config = _config()
    broken = config.model_copy(
        update={
            "providers": {
                **config.providers,
                "bigmodel": ProviderSpec(
                    name="bigmodel",
                    base_url="https://open.bigmodel.cn/api/coding/paas/v4",
                    api_key_env=GLM_API_KEY_ENV,
                ),
            }
        }
    )
    assert "Coding Plan" in (glm_readiness(broken, "glm-4.7-main") or "")


def test_readiness_passes_for_a_fully_configured_cny_alias(
    isolated_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GLM_API_KEY_ENV, "k")
    assert glm_readiness(_config(), "glm-4.7-main") is None


# --------------------------------------------------------------- provider 重试次数


def _counting_transport(
    responses: list[httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """返回 transport 与**它自己**记录的请求列表。

    曾经在这里再造一个空列表给 factory 用，结果 factory 根本不知道请求是什么，
    断言永远是 0 —— 一个"永远通过的计数断言"比没有断言更糟。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0) if responses else httpx.Response(429, json={})

    return httpx.MockTransport(handler), seen


def _registry_with_client(config: LLMConfig, transport: httpx.MockTransport) -> LLMRegistry:
    def factory(provider_spec: Any, api_key: str) -> httpx.Client:
        del api_key
        return httpx.Client(transport=transport, base_url=provider_spec.base_url)

    return LLMRegistry(config, client_factory=factory, policy=RetryPolicy(max_attempts=3))


def test_provider_max_retries_caps_real_http_attempts(
    isolated_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_retries: 2`` 意味着最多 3 次 HTTP —— 这是行为，不是字段读起来对不对。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, "k")
    config = _config()
    transport, seen = _counting_transport([])
    registry = _registry_with_client(config, transport)
    with pytest.raises(RateLimitError):
        registry.complete("coder", LLMRequest(messages=[Message.user("hi")]))
    assert len(seen) == 3  # bigmodel 的 max_retries=2 → 首次 + 2 次重试


def test_registry_keeps_global_policy_when_provider_silent(
    isolated_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider 没写 max_retries 时沿用调用方传入的全局策略。"""
    monkeypatch.setenv(GLM_API_KEY_ENV, "k")
    config = _config()
    raw = config.model_dump()
    raw["providers"]["bigmodel"]["max_retries"] = None
    transport, seen = _counting_transport([])
    registry = _registry_with_client(LLMConfig.model_validate(raw), transport)
    with pytest.raises(RateLimitError):
        registry.complete("coder", LLMRequest(messages=[Message.user("hi")]))
    assert len(seen) == 3  # 全局 RetryPolicy(max_attempts=3)


def test_registry_backfills_missing_provider_from_profile() -> None:
    """YAML 里没写 zai 时，GLM_PLATFORM=zai 仍然能拿到官方地址与密钥变量名。"""
    config = _config(providers={})
    result = apply_glm_env(config, environ={"GLM_PLATFORM": "zai", "GLM_MODEL": "glm-5"})
    assert result.providers["zai"].api_key_env == GLM_API_KEY_ENV
    assert result.providers["zai"].base_url == "https://api.z.ai/api/paas/v4"


def test_transport_provider_unwraps_decorators() -> None:
    from codepilot.llm.openai_compatible import OpenAICompatibleProvider
    from codepilot.llm.tracking import UsageTrackingProvider
    from codepilot.schemas.usage import Usage

    inner = OpenAICompatibleProvider(
        spec=ModelSpec(alias="a", provider="p", model="m"),
        provider=ProviderSpec(name="p", base_url="https://p.test/v1", api_key_env="P"),
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    chain = UsageTrackingProvider(inner, usage=Usage())
    assert transport_provider(chain) is inner


def test_transport_provider_rejects_chain_without_http_layer() -> None:
    from codepilot.llm.fake import FakeLLM

    with pytest.raises(TypeError, match="OpenAICompatibleProvider"):
        transport_provider(FakeLLM(["x"]))


# --------------------------------------------------------------- 仓库配置本身


def test_repository_yaml_declares_both_glm_platforms() -> None:
    from codepilot.llm.config import load_llm_config

    config = load_llm_config(REPO_ROOT / "configs" / "llm.yaml")
    assert {"bigmodel", "zai"} <= set(config.providers)


def test_repository_glm_aliases_are_complete_and_conservative() -> None:
    from codepilot.llm.config import load_llm_config

    config = load_llm_config(REPO_ROOT / "configs" / "llm.yaml")
    glm = {alias: spec for alias, spec in config.models.items() if spec.provider in GLM_PLATFORMS}
    assert glm, "configs/llm.yaml 必须已经带上 GLM 别名"
    for alias, spec in glm.items():
        assert spec.model_id_configured, alias
        assert spec.price is not None and spec.price.is_complete, alias
        expected = "USD" if spec.provider == "zai" else "CNY"
        assert spec.price.currency == expected, f"{alias} 的币种应当跟着平台走"
        # 分档计价一律取公开最高档：预算估算宁可偏高，也不要提前停在一个假的上限上。
        assert spec.price.input_per_mtok is not None
    assert {spec.model for spec in glm.values()} >= {"glm-4.7", "glm-5"}


def test_repository_roles_still_point_at_deepseek() -> None:
    """接入 GLM 不应该顺手把默认角色改到一家付费的新服务商上。"""
    from codepilot.llm.config import load_llm_config

    config = load_llm_config(REPO_ROOT / "configs" / "llm.yaml")
    assert set(config.roles.values()) == {"deepseek-chat"}


def test_model_price_complete_property() -> None:
    assert ModelPrice(currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0).is_complete
    assert not ModelPrice(currency="CNY", input_per_mtok=1.0).is_complete
