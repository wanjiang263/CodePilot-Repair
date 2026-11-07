"""Strict incremental Chat Completions SSE assembly before usage settlement."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx


class SSECompletion:
    """Consume decoded SSE lines; an incomplete stream is never a success."""

    def __init__(self, on_delta: Callable[[str], None] | None = None) -> None:
        self.on_delta = on_delta
        self.data: list[str] = []
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.model: str | None = None
        self.response_id: str | None = None
        self.done = False
        self.events = 0
        self.deltas = 0
        self.size = 0

    def line(self, line: str) -> None:
        self.size += len(line.encode("utf-8"))
        if self.size > 16 * 1024 * 1024:
            raise httpx.RemoteProtocolError("SSE response exceeds 16 MiB")
        if not line:
            self._event()
        elif line.startswith("data:"):
            self.data.append(line[5:].removeprefix(" "))

    def _event(self) -> None:
        if not self.data:
            return
        text = "\n".join(self.data)
        self.data.clear()
        if self.done:
            raise httpx.RemoteProtocolError("SSE data after [DONE]")
        if text == "[DONE]":
            self.done = True
            return
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict) or raw.get("error"):
                raise ValueError("invalid event or provider error")
            self.events += 1
            self.response_id = raw.get("id") or self.response_id
            self.model = raw.get("model") or self.model
            if raw.get("usage") is not None:
                self.usage = raw["usage"]
            for choice in raw.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise ValueError("multiple choices are unsupported")
                delta = choice.get("delta") or {}
                for key, destination in (
                    ("content", self.content),
                    ("reasoning_content", self.reasoning),
                ):
                    value = delta.get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError("invalid delta type")
                        destination.append(value)
                        if key == "content" and value:
                            self.deltas += 1
                            if self.on_delta:
                                try:
                                    self.on_delta(value)
                                except Exception as exc:
                                    raise httpx.RemoteProtocolError("SSE observer failed") from exc
                for tool in delta.get("tool_calls") or []:
                    index = tool["index"]
                    if type(index) is not int or index < 0:
                        raise ValueError("invalid tool index")
                    target = self.tools.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    target["id"] += tool.get("id") or ""
                    function = tool.get("function") or {}
                    for key in ("name", "arguments"):
                        target["function"][key] += function.get(key) or ""
                if choice.get("finish_reason") is not None:
                    if not isinstance(choice["finish_reason"], str) or not choice["finish_reason"]:
                        raise ValueError("invalid finish reason")
                    self.finish_reason = choice["finish_reason"]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # Never echo a provider event: it can contain credentials or prompts.
            raise httpx.RemoteProtocolError("Malformed SSE completion event") from exc

    def finish(self) -> dict[str, Any]:
        self._event()
        if not self.done or not self.finish_reason:
            raise httpx.RemoteProtocolError("SSE completion ended before finish/[DONE]")
        if not isinstance(self.usage, dict) or any(
            type(self.usage.get(key)) is not int or self.usage[key] < 0
            for key in ("prompt_tokens", "completion_tokens")
        ):
            raise httpx.RemoteProtocolError("SSE completion has no acknowledged usage")
        return {
            "id": self.response_id,
            "model": self.model,
            "choices": [
                {
                    "message": {
                        "content": "".join(self.content) or None,
                        "reasoning_content": "".join(self.reasoning) or None,
                        "tool_calls": [self.tools[i] for i in sorted(self.tools)],
                    },
                    "finish_reason": self.finish_reason,
                }
            ],
            "usage": self.usage,
            "stream": {"events": self.events, "content_deltas": self.deltas, "done": self.done},
        }
