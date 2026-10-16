"""可替换的传输层。

测试注入假的 ``sender``，所以这一层不需要网络——benchmark 的 fixture 必须能在
断网容器里执行（03 §13）。

发送协议是 ``(url, timeout, retries)`` 三个参数：重试次数由配置决定、由传输层
执行，所以它必须一路传下来。少传一个参数，传输层就会用自己那份默认值。
"""

from __future__ import annotations

from collections.abc import Callable


class Recorder:
    """记录每次调用并返回预设响应。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url: str, timeout: float, retries: int) -> str:
        self.calls.append((url, timeout, retries))
        if not self.responses:
            raise AssertionError("Recorder 的预设响应已用尽")
        return self.responses.pop(0)


def build_sender(*, responses: list[str]) -> Callable[[str, float, int], str]:
    """构造一个可注入的发送函数。"""
    return Recorder(responses=responses)
