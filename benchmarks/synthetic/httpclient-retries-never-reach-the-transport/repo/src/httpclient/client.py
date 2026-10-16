"""客户端：把配置与传输拼起来。"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urljoin

from httpclient.config import Config

Sender = Callable[[str, float, int], str]


class Client:
    """一个最小的 HTTP 客户端。"""

    def __init__(self, config: Config, *, sender: Sender) -> None:
        self.config = config
        self.sender = sender

    def request(self, path: str) -> str:
        """发一次请求。超时与重试次数都取自配置，不在这里重写。"""
        url = urljoin(self.config.base_url, path)
        return self.sender(url, self.config.timeout)

    def request_with_retries(self, path: str) -> str:
        """按配置的重试次数重发，直到成功。"""
        attempts = self.config.retries + 1
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self.request(path)
            except Exception as exc:  # 传输层抛什么异常不由本库规定
                last = exc
        assert last is not None
        raise last


def new_client(*, base_url: str, responses: list[str], **kwargs: object) -> Client:
    """便捷构造：注入一个回放 ``responses`` 的 sender，并把其余配置转给 :class:`Config`。"""
    from httpclient.transport import build_sender

    config = Config(base_url=base_url, **kwargs)  # type: ignore[arg-type]
    return Client(config, sender=build_sender(responses=list(responses)))
