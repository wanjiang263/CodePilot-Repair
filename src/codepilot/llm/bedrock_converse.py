"""AWS Bedrock Converse adapter.

Bedrock is not an API-key provider. Authentication always goes through the AWS
SDK credential chain (environment, shared profile or instance role); a stored
provider credential is never mapped onto IAM. The optional ``bedrock`` extra
installs boto3. Tests inject a deterministic fake runtime client.
"""

from __future__ import annotations

import json
from typing import Any

from codepilot.llm.capabilities import prepare_messages
from codepilot.llm.config import ModelSpec, ProviderSpec
from codepilot.llm.cost import compute_cost
from codepilot.llm.errors import (
    ContextTooLongError,
    LLMError,
    ProviderError,
    ProviderNotConfiguredError,
    RateLimitError,
)
from codepilot.llm.types import LLMRequest, LLMResponse, Message, Role, ToolCall
from codepilot.observability.redaction import REDACTED

__all__ = ["BedrockConverseProvider"]


class BedrockConverseProvider:
    def __init__(
        self,
        *,
        spec: ModelSpec,
        provider: ProviderSpec,
        client: Any = None,
        exchange_rates: dict[str, float] | None = None,
        budget_currency: str = "CNY",
    ) -> None:
        if not provider.region:
            raise ProviderNotConfiguredError(
                "AWS Bedrock 需要 region", provider=provider.name, model_alias=spec.alias
            )
        self.spec = spec
        self.provider = provider
        self.exchange_rates = exchange_rates or {}
        self.budget_currency = budget_currency
        self._client = client if client is not None else _build_client(provider)
        self._api_key = ""

    def complete(self, request: LLMRequest) -> LLMResponse:
        body = build_converse_request(request, self.spec)
        try:
            response = self._client.converse(**body)
        except Exception as exc:
            raise _translate(exc, self.provider.name, self.spec.alias) from exc
        return self._parse(response)

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> BedrockConverseProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _parse(self, response: Any) -> LLMResponse:
        if not isinstance(response, dict):
            raise ProviderError(
                "Bedrock 响应不是对象",
                provider=self.provider.name,
                model_alias=self.spec.alias,
            )
        output = response.get("output") or {}
        message = output.get("message") if isinstance(output, dict) else None
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            raise ProviderError(
                "Bedrock 响应缺少 output.message.content",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=response,
            )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            tool_use = block.get("toolUse")
            if isinstance(tool_use, dict):
                arguments = tool_use.get("input")
                tool_calls.append(
                    ToolCall(
                        id=str(tool_use.get("toolUseId") or ""),
                        name=str(tool_use.get("name") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                        raw_arguments=json.dumps(arguments, ensure_ascii=False)
                        if isinstance(arguments, dict)
                        else None,
                    )
                )
        usage = response.get("usage") or {}
        input_tokens = int(usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("outputTokens") or 0)
        cached_tokens = int(usage.get("cacheReadInputTokens") or 0)
        breakdown = compute_cost(
            price=self.spec.price,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            target_currency=self.budget_currency,
            exchange_rates=self.exchange_rates,
        )
        return LLMResponse(
            content="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            cost=breakdown.amount,
            cost_known=breakdown.known,
            model_alias=self.spec.alias,
            finish_reason=response.get("stopReason"),
            raw=response if isinstance(response, dict) else None,
        )


def build_converse_request(request: LLMRequest, spec: ModelSpec) -> dict[str, Any]:
    messages = prepare_messages(request, spec)
    system = [
        {"text": message.content or ""}
        for message in messages
        if message.role is Role.SYSTEM and message.content
    ]
    body: dict[str, Any] = {
        "modelId": spec.model,
        "messages": _encode_messages(messages),
        "inferenceConfig": {"maxTokens": request.max_output_tokens},
    }
    if spec.supports_temperature:
        body["inferenceConfig"]["temperature"] = request.temperature
    if system:
        body["system"] = system
    if request.tools and spec.supports_tools:
        body["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": {"json": tool.parameters},
                    }
                }
                for tool in request.tools
            ],
            "toolChoice": {"auto": {}},
        }
    return body


def _encode_messages(messages: list[Message]) -> list[dict[str, Any]]:
    encoded: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            continue
        if message.role is Role.TOOL:
            block = {
                "toolResult": {
                    "toolUseId": message.tool_call_id or "",
                    "content": [{"text": message.content or ""}],
                }
            }
            if encoded and encoded[-1]["role"] == "user":
                encoded[-1]["content"].append(block)
            else:
                encoded.append({"role": "user", "content": [block]})
            continue
        if message.role is Role.ASSISTANT:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    }
                )
            encoded.append({"role": "assistant", "content": blocks or [{"text": ""}]})
            continue
        encoded.append({"role": "user", "content": [{"text": message.content or ""}]})
    return encoded


def _build_client(provider: ProviderSpec) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ProviderNotConfiguredError(
            "AWS Bedrock 适配器需要安装可选依赖：uv sync --extra bedrock",
            provider=provider.name,
        ) from exc
    config = Config(
        read_timeout=provider.timeout_seconds,
        connect_timeout=min(10.0, provider.timeout_seconds),
        retries={"max_attempts": (provider.max_retries or 2) + 1, "mode": "standard"},
    )
    session = boto3.Session(
        profile_name=provider.aws_profile,
        region_name=provider.region,
    )
    return session.client("bedrock-runtime", config=config)


def _translate(exc: Exception, provider: str, alias: str) -> LLMError:
    text = REDACTED if "aws_secret_access_key" in str(exc).lower() else str(exc)
    response = getattr(exc, "response", None)
    status = None
    code = ""
    if isinstance(response, dict):
        metadata = response.get("ResponseMetadata") or {}
        status = metadata.get("HTTPStatusCode")
        code = str((response.get("Error") or {}).get("Code") or "")
    if code in {"ThrottlingException", "TooManyRequestsException"} or status == 429:
        return RateLimitError(
            "AWS Bedrock 触发限流", provider=provider, model_alias=alias, status=429
        )
    if code in {"ValidationException"} and "context" in text.lower():
        return ContextTooLongError(
            "上下文超限", provider=provider, model_alias=alias, body=text[:2000]
        )
    return ProviderError(
        f"Bedrock 调用失败: {text[:500]}",
        provider=provider,
        model_alias=alias,
        status=status,
    )
