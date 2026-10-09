"""L-04 的证明：配置里允许留占位符，但 `check()` 必须把它们全部报出来。

真实配置文件（configs/llm.yaml）里现在就留着占位符，因为模型 ID 与单价
随服务商发版变化，写未经核实的数字比留空更危险。所以这个模块的重点不是
"配置完整"，而是"不完整时必须可见"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codepilot.llm.config import (
    KNOWN_ROLES,
    LLMConfig,
    ModelPrice,
    ModelSpec,
    ProviderSpec,
    is_placeholder,
    load_llm_config,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _config(**overrides: object) -> LLMConfig:
    base: dict[str, object] = {
        "providers": {
            "p1": {"name": "p1", "base_url": "https://x/v1", "api_key_env": "TEST_KEY_1"}
        },
        "models": {
            "m1": {
                "alias": "m1",
                "provider": "p1",
                "model": "real-model-id",
                "price": {
                    "currency": "CNY",
                    "input_per_mtok": 1.0,
                    "output_per_mtok": 2.0,
                },
            },
        },
        "roles": dict.fromkeys(KNOWN_ROLES, "m1"),
    }
    base.update(overrides)
    return LLMConfig.model_validate(base)


# --------------------------------------------------------------- 占位符识别


@pytest.mark.parametrize(
    "value",
    ["<官方模型 ID>", "<单价>", "", "   ", None],
)
def test_placeholders_recognised(value: object) -> None:
    assert is_placeholder(value) is True


@pytest.mark.parametrize("value", ["gpt-4o", "deepseek-chat", 0.5, 128000])
def test_real_values_not_placeholders(value: object) -> None:
    assert is_placeholder(value) is False


def test_placeholder_price_becomes_none() -> None:
    """YAML 里的 ``<单价>`` 会以字符串到达，不能让它把校验炸掉。"""
    price = ModelPrice.model_validate(
        {"currency": "CNY", "input_per_mtok": "<单价>", "output_per_mtok": None}
    )
    assert price.input_per_mtok is None
    assert price.is_complete is False


def test_price_needs_input_and_output() -> None:
    assert ModelPrice(input_per_mtok=1.0).is_complete is False
    assert ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0).is_complete is True


def test_cached_price_is_optional() -> None:
    """很多模型没有缓存计价，缺它不该被当成配置不完整。"""
    price = ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0)
    assert price.is_complete is True


# --------------------------------------------------------------- 体检报告


def test_check_reports_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_KEY_1", raising=False)
    report = _config().check()
    assert report.providers_missing_key == ["p1"]
    assert report.ready is False


def test_check_passes_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY_1", "sk-test")
    report = _config().check()
    assert report.providers_missing_key == []
    assert report.ready is True


def test_check_reports_unfilled_model_and_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY_1", "sk-test")
    config = _config(
        models={
            "m1": {
                "alias": "m1",
                "provider": "p1",
                "model": "<官方模型 ID>",
                "price": {
                    "currency": "CNY",
                    "input_per_mtok": "<单价>",
                    "output_per_mtok": "<单价>",
                },
            }
        }
    )
    report = config.check()
    assert report.models_missing_fields == {"m1": ["model", "price"]}
    assert report.ready is False


def test_check_reports_unmapped_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY_1", "sk-test")
    report = _config(roles={"coder": "m1"}).check()
    assert "planner" in report.roles_unmapped
    assert "coder" not in report.roles_unmapped


def test_check_reports_typo_in_role_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """拼错的角色名会让那个 Agent 悄悄用不上预期的模型，必须报出来。"""
    monkeypatch.setenv("TEST_KEY_1", "sk-test")
    report = _config(roles={**dict.fromkeys(KNOWN_ROLES, "m1"), "plannner": "m1"}).check()
    assert report.unknown_roles == ["plannner"]


def test_check_reports_dangling_model_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEY_1", "sk-test")
    report = _config(roles={**dict.fromkeys(KNOWN_ROLES, "m1"), "coder": "nope"}).check()
    assert report.unknown_model_refs == {"coder": "nope"}


def test_render_is_human_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_KEY_1", raising=False)
    text = _config().check().render()
    assert "p1" in text


# --------------------------------------------------------------- 角色解析


def test_resolve_role_returns_spec() -> None:
    spec = _config().resolve_role("coder")
    assert isinstance(spec, ModelSpec)
    assert spec.alias == "m1"


def test_resolve_unknown_role_raises() -> None:
    """不静默兜底到某个默认模型——那会让一次误配变成一次错误的实验数据。"""
    with pytest.raises(KeyError):
        _config(roles={}).resolve_role("coder")


def test_resolve_role_with_dangling_alias_raises() -> None:
    with pytest.raises(KeyError):
        _config(roles={"coder": "ghost"}).resolve_role("coder")


def test_provider_for_returns_provider_spec() -> None:
    provider = _config().provider_for(_config().resolve_role("coder"))
    assert isinstance(provider, ProviderSpec)
    assert provider.base_url == "https://x/v1"


# --------------------------------------------------------------- 加载


def test_load_injects_names_from_keys(tmp_path: Path) -> None:
    """YAML 里 provider 的名字是字典键，加载后要变成字段。"""
    path = tmp_path / "llm.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": {"alpha": {"base_url": "https://a/v1", "api_key_env": "A_KEY"}},
                "models": {"m": {"provider": "alpha", "model": "real"}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    config = load_llm_config(path)
    assert config.providers["alpha"].name == "alpha"
    assert config.models["m"].alias == "m"


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_llm_config(tmp_path / "nope.yaml")


def test_load_non_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "llm.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_llm_config(path)


def test_repository_config_file_loads() -> None:
    """仓库里那份 configs/llm.yaml 必须始终可解析。

    它随代码一起提交，坏掉的症状是所有测试一起失败。
    """
    config = load_llm_config(REPO_ROOT / "configs" / "llm.yaml")
    assert config.providers
    assert config.models
    assert set(config.roles) == KNOWN_ROLES
    for spec in config.models.values():
        assert spec.provider in config.providers
