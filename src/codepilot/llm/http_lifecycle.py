"""Request-owned cancellable HTTP I/O behind the synchronous provider API.

The bridge thread is always joined. It runs an event loop, not an abandoned
blocking request; cancellation awaits transport cleanup before returning.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from codepilot.net import sanitized_proxy_environment
from codepilot.observability.dispatch import current_dispatch_scope


class RequestCancelled(httpx.TransportError):
    """Cancellation after dispatch: usage may still be billed remotely."""


def send_request(
    client: httpx.Client,
    path: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: httpx.Timeout,
    deadline: float,
    trust_env: bool,
) -> httpx.Response:
    request = client.build_request("POST", path, json=body, headers=headers, timeout=timeout)
    scope = current_dispatch_scope()
    cancel = scope.cancel if scope else None
    if scope:
        deadline = min(deadline, time.monotonic() + scope.deadline.remaining(scope.reserve))
    # Keep the documented synchronous transport injection seam for offline
    # tests. Arbitrary blocking custom transports cannot promise cancellation.
    transport = client._transport
    if isinstance(transport, httpx.MockTransport):
        response = client.send(request)
        if cancel and cancel():
            raise RequestCancelled("cancelled after model dispatch", request=request)
        if time.monotonic() >= deadline:
            raise httpx.ReadTimeout("request total deadline exhausted", request=request)
        return response
    if not isinstance(transport, httpx.HTTPTransport):
        raise TypeError("Cancellable HTTP requires HTTPTransport or offline MockTransport")

    async def exchange() -> httpx.Response:
        with sanitized_proxy_environment():
            owned = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)
        async with (
            owned,
            owned.stream(
                request.method, request.url, content=request.content, headers=request.headers
            ) as response,
        ):
            content = await response.aread()
            # aread decodes content. Rebuilding must not decode it twice or
            # advertise the wire body's framing/length as the decoded size.
            clean = response.headers.copy()
            for key in ("content-encoding", "content-length", "transfer-encoding"):
                clean.pop(key, None)
            return httpx.Response(
                response.status_code, headers=clean, content=content, request=request
            )

    async def bounded() -> httpx.Response:
        task = asyncio.create_task(exchange())
        try:
            while True:
                if cancel and cancel():
                    raise RequestCancelled("cancelled after model dispatch", request=request)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise httpx.ReadTimeout("request total deadline exhausted", request=request)
                done, _ = await asyncio.wait({task}, timeout=min(0.025, remaining))
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return _run_joined(lambda: asyncio.run(bounded()))


def _run_joined(call: Callable[[], httpx.Response]) -> httpx.Response:
    # complete() can be called inside a node's already-running event loop.
    # Copy dispatch attribution into a private loop and join on EVERY outcome.
    context = contextvars.copy_context()
    result: list[httpx.Response] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(context.run(call))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, name="codepilot-http")
    worker.start()
    try:
        worker.join()
    finally:
        worker.join()
    if errors:
        raise errors[0]
    return result[0]
