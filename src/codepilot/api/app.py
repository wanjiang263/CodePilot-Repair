"""FastAPI application for Phase 13.

Hard constraints carried from Phase 0:

1. Without a configured API key the service **refuses to start**; there is no
   unauthenticated mode.
2. The default bind address is loopback and CORS is not enabled. The React app is
   served same-origin (or through the Vite proxy) and authenticates with
   ``X-API-Key``.
3. Agent execution never happens in a request handler. ``POST /tasks`` only
   writes a durable queue row; ``codepilot worker`` executes the graph.

The API key is never logged, stored in artifacts or compiled into the frontend.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from codepilot.api.auth import (
    api_key_dependency,
    assert_api_key_configured,
    verify_api_key,
)
from codepilot.api.database import ApiDatabase
from codepilot.api.history import HistoryCatalog
from codepilot.api.routes import ApiContext, build_router
from codepilot.api.security import PathScrubber
from codepilot.api.settings_routes import build_settings_router
from codepilot.config import Settings, get_settings
from codepilot.settings.credentials import CredentialVault
from codepilot.settings.service import SettingsError, SettingsService
from codepilot.settings.store import SettingsStore, VersionConflictError

__all__ = [
    "api_key_dependency",
    "create_app",
    "verify_api_key",
]

log = logging.getLogger("codepilot.api")


class _OversizedBodyError(Exception):
    pass


class ApiMiddleware:
    """Trace IDs, request logging, body limit and conservative response headers."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        trace_id = uuid.uuid4().hex
        scope.setdefault("state", {})["trace_id"] = trace_id
        headers = MutableHeaders(scope=scope)
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.settings.api_max_body_bytes:
                    await _send_error(
                        send,
                        413,
                        "payload_too_large",
                        "Request body exceeds the configured limit.",
                        trace_id,
                    )
                    return
            except ValueError:
                await _send_error(
                    send, 400, "invalid_content_length", "Invalid Content-Length.", trace_id
                )
                return
        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.settings.api_max_body_bytes:
                    raise _OversizedBodyError
            return message

        status_code = 500
        started = time.perf_counter()

        async def traced_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                out = MutableHeaders(scope=message)
                out["X-Trace-ID"] = trace_id
                out["X-Content-Type-Options"] = "nosniff"
                out["Referrer-Policy"] = "no-referrer"
                if scope["path"].startswith("/api/"):
                    out["Cache-Control"] = "no-store"
            await send(message)

        path = str(scope.get("path") or "")
        try:
            await self.app(scope, limited_receive, traced_send)
        except _OversizedBodyError:
            await _send_error(
                send,
                413,
                "payload_too_large",
                "Request body exceeds the configured limit.",
                trace_id,
            )
        finally:
            log.info(
                "api.request",
                extra={
                    "method": scope.get("method"),
                    "path": path,
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "trace_id": trace_id,
                    "client": (scope.get("client") or ("",))[0],
                },
            )


async def _send_error(
    send: Send,
    status_code: int,
    code: str,
    message: str,
    trace_id: str,
) -> None:
    payload = json.dumps(
        {"error": {"code": code, "message": message, "trace_id": trace_id}},
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode()),
                (b"x-trace-id", trace_id.encode()),
                (b"x-content-type-options", b"nosniff"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _error_response(
    status_code: int,
    code: str,
    message: str,
    trace_id: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "trace_id": trace_id}},
        headers=headers,
    )


def create_app(
    settings: Settings | None = None,
    *,
    settings_service: SettingsService | None = None,
) -> FastAPI:
    """Build the API. Refuses to start without a sufficiently long API key."""
    resolved = settings if settings is not None else get_settings()
    assert_api_key_configured(resolved)

    database = ApiDatabase(
        resolved.resolved_api_queue_path,
        artifacts_dir=resolved.artifacts_dir,
    )
    history = HistoryCatalog(resolved)
    if settings_service is None:
        settings_store = SettingsStore(resolved.resolved_settings_path)
        vault = CredentialVault(
            resolved.resolved_settings_credentials_dir,
            master_key=(
                resolved.settings_master_key.get_secret_value()
                if resolved.settings_master_key is not None
                else None
            ),
        )
        settings_service = SettingsService(settings_store, vault)
    context = ApiContext(
        settings=resolved,
        database=database,
        history=history,
        scrubber=PathScrubber(resolved),
        content_scrubber=PathScrubber(resolved, scrub_absolute_paths=False),
        settings_service=settings_service,
    )

    app = FastAPI(
        title="CodePilot-Repair API",
        version="0.1.0",
        description=(
            "Repository-level autonomous repair agent: durable tasks, a separate "
            "worker, resumable SSE events, patches and read-only benchmark replay."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.api_context = context
    app.state.settings = resolved
    app.add_middleware(ApiMiddleware, settings=resolved)
    app.include_router(
        build_router(),
        dependencies=[Depends(api_key_dependency(resolved))],
    )
    app.include_router(
        build_settings_router(),
        dependencies=[Depends(api_key_dependency(resolved))],
    )

    @app.get("/health", dependencies=[Depends(api_key_dependency(resolved))])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(VersionConflictError)
    async def version_conflict(request: Request, exc: VersionConflictError) -> JSONResponse:
        trace_id = str(getattr(request.state, "trace_id", ""))
        return _error_response(
            409,
            "version_conflict",
            f"Settings changed elsewhere (current version {exc.actual}). Reload and retry.",
            trace_id,
        )

    @app.exception_handler(SettingsError)
    async def settings_error(request: Request, exc: SettingsError) -> JSONResponse:
        trace_id = str(getattr(request.state, "trace_id", ""))
        return _error_response(exc.status, exc.code, str(exc), trace_id)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        trace_id = str(getattr(request.state, "trace_id", ""))
        detail: Any = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code") or "http_error")
            message = str(detail.get("message") or "Request failed.")
        else:
            code = _code_for_status(exc.status_code)
            message = str(detail or "Request failed.")
        return _error_response(
            exc.status_code,
            code,
            message,
            trace_id,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        trace_id = str(getattr(request.state, "trace_id", ""))
        errors = [
            {
                "location": ".".join(str(part) for part in item.get("loc", ())),
                "message": str(item.get("msg") or "invalid value"),
                "type": str(item.get("type") or "value_error"),
            }
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "trace_id": trace_id,
                    "fields": errors,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        trace_id = str(getattr(request.state, "trace_id", ""))
        # Do not log the exception message or traceback: it may contain provider
        # error bodies or credentials. The trace id is enough to correlate with
        # the worker's own redacted logs.
        log.error(
            "api.unhandled",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
        )
        return _error_response(
            500,
            "internal_error",
            "Internal server error. Use the trace id when reporting this.",
            trace_id,
        )

    dist = Path(resolved.web_dist_dir)
    if dist.is_dir():
        app.mount("/app", StaticFiles(directory=dist, html=True), name="dashboard")

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse(url="/app/")

    return app


def _code_for_status(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        422: "validation_error",
    }.get(status_code, "http_error")
