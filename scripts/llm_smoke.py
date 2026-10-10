"""真实 provider 连通性冒烟测试（L-15）。

**这个脚本会产生真实调用并产生费用。** 它不属于测试套件，不会被 pytest
收集，CI 里也永远不跑——所以它必须自己把话说明白，而不是等别人踩到。

单元测试用 ``FakeLLM`` / ``httpx.MockTransport`` 覆盖了逻辑正确性，但有三件
事只有真实接口能验证：

1. ``base_url`` 与路径拼接对不对（各家的 URL 结构确实不一样）。
2. 模型 ID 是否是当前有效的。
3. 服务商返回的 JSON 结构与解析代码的假设是否一致。

用法：

    uv run python scripts/llm_smoke.py                # 全部已配置的 provider
    uv run python scripts/llm_smoke.py deepseek       # 只测一个
    uv run python scripts/llm_smoke.py --yes          # 跳过确认（CI 不用，本地批量时用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codepilot.config import PROJECT_ROOT, get_settings
from codepilot.llm.config import LLMConfig, ModelSpec, ProviderSpec, load_llm_config
from codepilot.llm.errors import LLMError
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message

QUESTION = "只回复两个字：收到"

#: 与 ``RepairBudget.budget_currency`` 的默认值保持一致。
BUDGET_CURRENCY = "CNY"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", nargs="?", help="只测这一个 provider（默认全部）")
    parser.add_argument("--yes", action="store_true", help="跳过费用确认")
    return parser.parse_args()


def _candidates(config: LLMConfig, only: str | None) -> list[tuple[ProviderSpec, ModelSpec]]:
    """挑出可以测的 (provider, model) 组合，并说明其余为什么不能测。"""
    pairs: list[tuple[ProviderSpec, ModelSpec]] = []
    skipped: list[str] = []

    seen_providers: set[str] = set()
    for spec in config.models.values():
        provider = config.providers.get(spec.provider)
        if provider is None:
            skipped.append(f"{spec.alias}: provider {spec.provider!r} 未定义")
            continue
        if only and provider.name != only:
            continue
        if provider.name in seen_providers:
            continue
        if not provider.has_api_key():
            skipped.append(f"{provider.name}: 环境变量 {provider.api_key_env} 未设置")
            continue
        if not spec.model_id_configured:
            skipped.append(f"{spec.alias}: model 字段仍是占位符，请在 configs/llm.yaml 中填写")
            continue
        seen_providers.add(provider.name)
        pairs.append((provider, spec))

    for name in config.providers:
        if only and name != only:
            continue
        if name not in seen_providers and not any(s.startswith(f"{name}:") for s in skipped):
            skipped.append(f"{name}: 没有模型引用它")

    if skipped:
        print("跳过：")
        for line in skipped:
            print(f"  - {line}")
        print()

    return pairs


def _smoke(provider_spec: ProviderSpec, model_spec: ModelSpec, rates: dict[str, float]) -> bool:
    from codepilot.config import read_secret_env

    key = read_secret_env(provider_spec.api_key_env)
    assert key is not None  # _candidates 已过滤

    print(f"→ {provider_spec.name} / {model_spec.alias} ({model_spec.model})")
    print(f"  base_url = {provider_spec.base_url}")

    provider = OpenAICompatibleProvider(
        spec=model_spec,
        provider=provider_spec,
        api_key=key,
        policy=RetryPolicy(max_attempts=2),
        exchange_rates=rates,
    )
    try:
        response = provider.complete(
            LLMRequest(
                messages=[Message.user(QUESTION)],
                max_output_tokens=32,
                purpose="smoke",
            )
        )
    except LLMError as exc:
        print(f"  ✗ 失败: {exc}")
        if exc.body is not None:
            # 错误体里可能回显请求头，打印前先脱敏。
            from codepilot.observability.redaction import redact

            print(f"    响应体: {redact(exc.body)}")
        return False
    finally:
        provider.close()

    text = (response.content or "").strip().replace("\n", " ")
    choices = (response.raw or {}).get("choices") or []
    finish_reason = choices[0].get("finish_reason") if choices else None
    ok = bool(text) and finish_reason != "length"
    if ok:
        print(f"  ✓ 回复: {text[:80]!r}")
    else:
        print(f"  ✗ 未取得完整文本回复（finish_reason={finish_reason!r}）")
    cost_note = "" if response.cost_known else "  ⚠️ 未配置单价，费用不可信（cost_known=False）"
    print(
        f"    tokens: {response.input_tokens} in / {response.output_tokens} out, "
        f"按配置估算费用: {response.cost:.6f} {BUDGET_CURRENCY}{cost_note}"
    )
    if response.reasoning_content:
        print(f"    思考内容已单独捕获（{len(response.reasoning_content)} 字符）")
    return ok


def main() -> int:
    args = _parse_args()

    settings = get_settings()
    path = settings.resolved_llm_config
    if not path.exists():
        print(f"✗ 找不到配置文件: {path}", file=sys.stderr)
        return 1

    config = load_llm_config(path)
    pairs = _candidates(config, args.provider)

    if not pairs:
        print("没有可测试的 provider。请先：")
        print("  1. 在 .env 中设置对应的 API key（.env 不会被提交）")
        print("  2. 在 configs/llm.yaml 中填写模型 ID")
        print(f"  3. 用 `uv run codepilot config check` 复看还缺什么（配置位于 {PROJECT_ROOT}）")
        return 1

    print(f"即将对 {len(pairs)} 个 provider 发起**真实请求**，会产生少量费用。")
    if not args.yes:
        reply = input("继续？[y/N] ").strip().lower()
        if reply not in {"y", "yes"}:
            print("已取消。")
            return 130

    results = [_smoke(p, m, config.exchange_rates) for p, m in pairs]

    ok = sum(results)
    print()
    print(f"结果：{ok}/{len(results)} 成功。")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
