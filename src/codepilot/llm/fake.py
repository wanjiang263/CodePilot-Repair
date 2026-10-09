"""``FakeLLM``：确定性、零网络、零费用的 provider（02 §12，L-11）。

单元测试里出现的所有 LLM 调用都应该是它。真实接口做测试有三个必然问题：
会花钱、会因网络抖动而随机失败、以及**输出不确定**——一个"有时过有时不过"
的测试比没有测试更糟，因为它会训练人忽略红色。

设计上刻意允许脚本化各种"坏"响应：非法 JSON、缺字段、抛异常。
错误处理分支只能靠喂坏数据来测，而真实模型很难按要求犯错。
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pydantic import BaseModel

from codepilot.llm.types import LLMRequest, LLMResponse, ToolCall

__all__ = ["FakeLLM", "Reply"]

#: 脚本的一项。字符串按 content 处理；dict / BaseModel 序列化成 JSON content；
#: 异常直接抛；可调用对象按请求生成下一项。
Reply = str | dict[str, Any] | BaseModel | Exception | LLMResponse | Callable[[LLMRequest], Any]


class FakeLLM:
    """按脚本返回响应的 provider。"""

    def __init__(
        self,
        replies: Iterable[Reply] | None = None,
        *,
        default: Reply | None = None,
        model_alias: str = "fake",
        cost_per_call: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self._queue: deque[Reply] = deque(replies or ())
        self._default = default
        self.model_alias = model_alias
        self.cost_per_call = cost_per_call
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

        self.calls: list[LLMRequest] = []
        """收到的每个请求，按顺序。断言"prompt 里到底放了什么"时用它。"""

        self.closed = False

    # ---------------------------------------------------------------- 脚本

    def queue(self, *replies: Reply) -> FakeLLM:
        """追加脚本项，返回自身以便链式调用。"""
        self._queue.extend(replies)
        return self

    @property
    def remaining(self) -> int:
        return len(self._queue)

    # ---------------------------------------------------------------- 调用

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self.closed:
            raise RuntimeError("FakeLLM 已关闭")
        self.calls.append(request)

        if self._queue:
            reply = self._queue.popleft()
        elif self._default is not None:
            reply = self._default
        else:
            raise AssertionError(
                f"FakeLLM 脚本已用尽，但仍收到第 {len(self.calls)} 次调用"
                f"（purpose={request.purpose!r}）。"
                "测试要么补足脚本项，要么给出 default。"
            )

        if callable(reply) and not isinstance(reply, (str, dict, BaseModel, LLMResponse)):
            reply = reply(request)

        if isinstance(reply, Exception):
            raise reply

        if isinstance(reply, LLMResponse):
            return reply

        if isinstance(reply, str):
            return self._wrap(content=reply)

        if isinstance(reply, BaseModel):
            return self._wrap(content=reply.model_dump_json())

        if isinstance(reply, dict):
            # 约定：带 "tool" 键的 dict 视为一次工具调用，否则视为 JSON 内容。
            # 这个约定让"喂一个非法 schema 的 dict"和"喂一次工具调用"都能写得短。
            if "tool" in reply and "arguments" in reply:
                arguments = reply["arguments"]
                if not isinstance(arguments, dict):
                    raise TypeError(
                        f"FakeLLM 的工具调用 arguments 必须是 dict，收到 {type(arguments).__name__}"
                    )
                return self._wrap(
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id=f"fake_{len(self.calls)}",
                            name=str(reply["tool"]),
                            arguments=arguments,
                        )
                    ],
                )
            return self._wrap(content=json.dumps(reply, ensure_ascii=False))

        raise TypeError(f"FakeLLM 不认识的脚本项类型: {type(reply).__name__}")

    def close(self) -> None:
        self.closed = True

    # ---------------------------------------------------------------- 辅助

    def _wrap(self, *, content: str | None, tool_calls: Sequence[ToolCall] = ()) -> LLMResponse:
        return LLMResponse(
            content=content,
            tool_calls=list(tool_calls),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost=self.cost_per_call,
            cost_known=True,
            model_alias=self.model_alias,
            latency_ms=0,
        )

    # ---------------------------------------------------------------- 断言辅助

    def last_request(self) -> LLMRequest:
        if not self.calls:
            raise AssertionError("FakeLLM 还没有收到任何调用")
        return self.calls[-1]

    def purposes(self) -> list[str]:
        """所有调用的 purpose，按顺序。用来断言流程走到了哪几步。"""
        return [call.purpose for call in self.calls]

    def prompt_texts(self) -> list[str]:
        """所有调用的消息正文，拼成一段文本。断言 prompt 内容时用。"""
        return ["\n".join(m.content or "" for m in call.messages) for call in self.calls]
