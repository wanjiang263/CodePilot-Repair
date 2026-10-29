"""Authorized, single-attempt GLM protocol and cancellation evidence."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from codepilot.llm.config import load_llm_config
from codepilot.llm.dispatch_budget import DispatchBudget, configure_dispatch_budget
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message, Role, ToolSpec
from codepilot.observability.dispatch import Deadline, DispatchScope, dispatch_scope


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["normal", "cancel"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root / "artifacts/phase27" / f"glm-{args.mode}"
    out.mkdir(exist_ok=False)
    load_dotenv(root / ".env")
    config = load_llm_config(root / "configs/phase22/llm.yaml")
    spec = config.models["glm-5.3-flash-main"]
    provider = config.providers[spec.provider]
    assert provider.name == "bigmodel" and spec.model == "glm-5.3-flash"
    assert provider.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert provider.max_retries == 0 and not provider.trust_env
    key = provider.api_key()
    assert key, "GLM credential unavailable"
    # Each probe segment can reserve <= 1 CNY, with <= 2 CNY across both.
    # The remaining round budget is passed separately to the Agent process.
    configure_dispatch_budget(DispatchBudget(1.0, out / "dispatch-ledger.json"))
    model = OpenAICompatibleProvider(
        spec=spec, provider=provider, api_key=key, policy=RetryPolicy(max_attempts=1)
    )
    records: list[dict[str, object]] = []

    def call(messages: list[Message], purpose: str, tools: list[ToolSpec] | None = None):
        start = time.monotonic()
        request = LLMRequest(
            messages=messages, tools=tools, purpose=purpose, max_output_tokens=1024
        )
        result = model.complete(request)
        records.append(
            {
                "purpose": purpose,
                "seconds": time.monotonic() - start,
                "request": request.model_dump(mode="json"),
                "response": result.model_dump(mode="json"),
            }
        )
        (out / "responses.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf8")
        assert result.raw and result.raw.get("model") == spec.model
        return result

    try:
        if args.mode == "normal":
            call([Message.user("Reply with only OK.")], "phase27_normal")
            messages = [
                Message.system("Call add exactly once for 17+25. Use its returned value."),
                Message.user("Use the add tool to calculate 17+25."),
            ]
            tools = [
                ToolSpec(
                    name="add",
                    description="Add two integers",
                    parameters={
                        "type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"],
                        "additionalProperties": False,
                    },
                )
            ]
            response = call(messages, "phase27_tool", tools)
            assert len(response.tool_calls) == 1 and response.tool_calls[0].is_valid
            tool = response.tool_calls[0]
            assert tool.name == "add" and tool.arguments == {"a": 17, "b": 25}
            messages += [
                Message(
                    role=Role.ASSISTANT, content=response.content, tool_calls=response.tool_calls
                ),
                Message.tool_result(tool.id, json.dumps({"sum": 42})),
            ]
            final = call(messages, "phase27_tool_result", tools)
            assert not final.tool_calls and "42" in (final.content or "")
        else:
            cancelled = threading.Event()
            timer = threading.Timer(0.25, cancelled.set)
            start = time.monotonic()
            timer.start()
            try:
                with dispatch_scope(
                    DispatchScope(Deadline(time.monotonic() + 10), cancel=cancelled.is_set)
                ):
                    call(
                        [Message.user("Explain binary search carefully in 400 words.")],
                        "phase27_cancel",
                    )
            except Exception as exc:
                records.append(
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "dispatched": getattr(exc, "dispatched", None),
                        "seconds": time.monotonic() - start,
                    }
                )
            finally:
                timer.cancel()
                timer.join()
            (out / "cancel-result.json").write_text(
                json.dumps(
                    {
                        "records": records,
                        "attempts": model.attempts,
                        "client_closed": model._client.is_closed,
                        "http_threads": [
                            t.name for t in threading.enumerate() if t.name == "codepilot-http"
                        ],
                        "request_query_supported": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf8",
            )
    finally:
        model.close()


if __name__ == "__main__":
    main()
