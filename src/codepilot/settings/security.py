"""Custom provider endpoint and connection-test guardrails.

Settings-created endpoints are user supplied, so they get a stricter client than
the built-in providers: HTTPS only, no redirects, no environment proxy, a size
cap, and a transport that resolves the host once and connects to the validated
address. TLS still verifies the original hostname through ``sni_hostname``.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

__all__ = [
    "MAX_PROVIDER_RESPONSE_BYTES",
    "ProviderSecurityError",
    "ResponseTooLargeError",
    "SecureJSONClient",
    "build_secure_httpx_client",
    "validate_provider_base_url",
]

MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024
_DEFAULT_PORTS = {"https": 443}


class ProviderSecurityError(ValueError):
    """A custom endpoint violates the SSRF / transport policy."""


class ResponseTooLargeError(ProviderSecurityError):
    """The remote response exceeded the bounded read limit."""


def validate_provider_base_url(value: str) -> str:
    """Validate syntax and immediate SSRF hazards without doing DNS.

    DNS is intentionally resolved at request time; doing it while editing a
    form would make saving dependent on network availability, and a cached
    answer would not protect the actual connection anyway.
    """
    raw = value.strip()
    if not raw:
        raise ProviderSecurityError("a base URL is required")
    if len(raw) > 500:
        raise ProviderSecurityError("the base URL is too long")
    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise ProviderSecurityError("custom provider URLs must use https")
    if parts.username or parts.password:
        raise ProviderSecurityError("credentials in provider URLs are not allowed")
    if parts.query or parts.fragment:
        raise ProviderSecurityError("provider URLs must not carry query strings or fragments")
    if not parts.hostname:
        raise ProviderSecurityError("the base URL has no host")
    if parts.port is not None and not (1 <= parts.port <= 65535):
        raise ProviderSecurityError("invalid port")
    _reject_literal(parts.hostname)
    return raw.rstrip("/")


def _reject_literal(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith(
            ".localhost"
        ):
            raise ProviderSecurityError("loopback endpoints are not allowed") from None
        return
    if not address.is_global:
        raise ProviderSecurityError("private, loopback and reserved addresses are not allowed")


def resolve_public_address(host: str) -> str:
    """Resolve once and require every answer to be globally routable."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProviderSecurityError(f"could not resolve provider host: {exc}") from exc
    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0])
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:  # pragma: no cover - getaddrinfo guarantees an address
            raise ProviderSecurityError("the provider host resolved to an invalid address") from exc
        if not address.is_global:
            raise ProviderSecurityError(
                "the provider host resolves to a private, loopback or reserved address"
            )
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise ProviderSecurityError("the provider host did not resolve")
    return addresses[0]


class _PinnedTransport(httpx.BaseTransport):
    """Connect to a validated address while keeping the original Host/SNI."""

    def __init__(self, *, resolver: Callable[[str], str]) -> None:
        self._resolver = resolver
        self._inner = httpx.HTTPTransport(trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        original_host = request.headers.get("host")
        address = self._resolver(host)
        request.url = request.url.copy_with(host=address)
        if original_host:
            request.headers["host"] = original_host
        request.extensions["sni_hostname"] = host
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> _PinnedTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def build_secure_httpx_client(
    *,
    base_url: str,
    timeout_seconds: float,
    transport: httpx.BaseTransport | None = None,
    resolver: Callable[[str], str] = resolve_public_address,
) -> httpx.Client:
    """HTTP client used by settings-created providers during live runs/tests."""
    return httpx.Client(
        base_url=base_url,
        transport=transport if transport is not None else _PinnedTransport(resolver=resolver),
        timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
        follow_redirects=False,
        trust_env=False,
    )


@dataclass(frozen=True, slots=True)
class BoundedResponse:
    status: int
    payload: Any
    latency_ms: int
    headers: dict[str, str]


class SecureJSONClient:
    """Small synchronous helper for model discovery and connection tests."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], str] = resolve_public_address,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
    ) -> None:
        self._client = httpx.Client(
            transport=transport if transport is not None else _PinnedTransport(resolver=resolver),
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._max_bytes = max_bytes

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float = 20.0,
    ) -> BoundedResponse:
        started = time.monotonic()
        with self._client.stream(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=timeout_seconds,
            follow_redirects=False,
        ) as response:
            if response.is_redirect:
                raise ProviderSecurityError("provider redirects are not followed")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > self._max_bytes:
                    raise ResponseTooLargeError("provider response exceeded the size limit")
            try:
                payload = httpx.Response(
                    response.status_code,
                    content=bytes(body),
                    headers=response.headers,
                ).json()
            except ValueError:
                payload = None
            return BoundedResponse(
                status=response.status_code,
                payload=payload,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                headers={key.lower(): value for key, value in response.headers.items()},
            )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SecureJSONClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
