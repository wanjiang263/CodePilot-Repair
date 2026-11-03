"""Settings, provider, credential, routing and runtime-facts HTTP surface."""

from __future__ import annotations

import shutil
import stat
import time
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

import docker
from codepilot import __version__
from codepilot.api.routes import ApiContext
from codepilot.config import PROJECT_ROOT
from codepilot.settings.catalog import template
from codepilot.settings.models import (
    CredentialRef,
    ModelConfig,
    ProviderConfig,
    SettingsDocument,
)
from codepilot.settings.service import SettingsService
from codepilot.settings.store import VersionConflictError

__all__ = ["build_settings_router"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SectionUpdate(_StrictModel):
    version: int = Field(ge=0)
    values: dict[str, Any]


class ProviderCreate(_StrictModel):
    version: int = Field(ge=0)
    provider: ProviderConfig


class ProviderUpdate(_StrictModel):
    version: int = Field(ge=0)
    provider: ProviderConfig


class TemplateCreate(_StrictModel):
    version: int = Field(ge=0)
    catalog_id: str
    provider_id: str
    name: str | None = None
    base_url: str | None = None
    region: str | None = None
    api_key_env: str | None = None


class CredentialUpdate(_StrictModel):
    version: int = Field(ge=0)
    mode: Literal["value", "env"]
    value: str | None = Field(default=None, max_length=8192)
    env_name: str | None = Field(default=None, max_length=64)


class ModelUpsert(_StrictModel):
    version: int = Field(ge=0)
    original_model_id: str | None = None
    model: ModelConfig


class ConnectionTest(_StrictModel):
    kind: Literal["config", "models", "inference"] = "config"
    model_id: str | None = None
    confirm_cost: bool = False


class ImportRequest(_StrictModel):
    version: int = Field(ge=0)
    payload: dict[str, Any]


class CleanupRequest(_StrictModel):
    version: int = Field(ge=0)
    task_ids: list[str] = Field(min_length=1, max_length=50)
    confirmation: str


def _context(request: Request) -> ApiContext:
    return cast(ApiContext, request.app.state.api_context)


def _service(context: ApiContext) -> SettingsService:
    return context.settings_service


def _document_view(service: SettingsService, document: SettingsDocument) -> dict[str, Any]:
    return {
        **document.describe(),
        "secure_storage_available": service.vault.available,
        "providers": [service.provider_view(item) for item in document.providers.values()],
    }


def _git_commit() -> str | None:
    head = PROJECT_ROOT / ".git" / "HEAD"
    try:
        raw = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw.startswith("ref:"):
        ref = PROJECT_ROOT / ".git" / raw.partition(" ")[2].strip()
        try:
            return ref.read_text(encoding="utf-8").strip()[:12]
        except OSError:
            return None
    return raw[:12] or None


def _directory_size(path: Path, *, limit: int = 200_000) -> tuple[int, int]:
    total = 0
    files = 0
    if not path.exists():
        return 0, 0
    for item in path.rglob("*"):
        if files >= limit:
            break
        if item.is_file():
            try:
                total += item.stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def _docker_status() -> dict[str, Any]:
    started = time.monotonic()
    try:
        client = docker.from_env(timeout=2)
        try:
            client.ping()
            version = client.version()
        finally:
            client.close()
        return {
            "available": True,
            "server_version": version.get("Version") if isinstance(version, dict) else None,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": None,
        }
    except Exception as exc:  # pragma: no cover - daemon availability varies
        return {
            "available": False,
            "server_version": None,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def _force_rmtree(path: Path, *, attempts: int = 5) -> None:
    def onerror(func: Any, target: str, _exc: Any) -> None:
        try:
            Path(target).chmod(stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass

    # Windows anti-virus scanners hold freshly written files (git objects,
    # sqlite journals) without FILE_SHARE_DELETE for a few milliseconds, so
    # unlink/rmdir intermittently fail with sharing violations. Retry with
    # exponential backoff instead of turning a transient scan into an
    # ``artifact_cleanup_failed`` skip on the cleanup route.
    delay = 0.1
    for attempt in range(attempts):
        shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            return
        if attempt == attempts - 1:
            break
        time.sleep(delay)
        delay *= 2.0
    survivors = sorted(str(item.relative_to(path)) for item in path.rglob("*"))[:20]
    raise OSError(
        f"artifact directory could not be fully removed: {path.name}; survivors={survivors}"
    )


def build_settings_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/settings")
    def read_settings(request: Request) -> dict[str, Any]:
        context = _context(request)
        service = _service(context)
        return _document_view(service, service.document())

    @router.put("/settings/sections/{section}")
    def update_section(request: Request, section: str, body: SectionUpdate) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.update_section(section, body.values, expected_version=body.version)
        return _document_view(service, document)

    @router.get("/settings/catalog")
    def read_catalog(request: Request) -> list[dict[str, Any]]:
        return _service(_context(request)).catalog()

    @router.get("/settings/providers")
    def read_providers(request: Request) -> list[dict[str, Any]]:
        return _service(_context(request)).providers_view()

    @router.post("/settings/providers", status_code=status.HTTP_201_CREATED)
    def create_provider(request: Request, body: ProviderCreate) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.create_provider(body.provider, expected_version=body.version)
        return _document_view(service, document)

    @router.post("/settings/providers/from-template", status_code=status.HTTP_201_CREATED)
    def create_from_template(request: Request, body: TemplateCreate) -> dict[str, Any]:
        service = _service(_context(request))
        try:
            item = template(body.catalog_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "catalog_not_found", "message": str(exc)},
            ) from exc
        provider = ProviderConfig(
            provider_id=body.provider_id,
            name=body.name or item.name,
            protocol=item.protocol,
            base_url=body.base_url or item.base_url,
            catalog_id=item.catalog_id,
            custom=item.catalog_id == "custom",
            region=body.region,
            models=item.models,
            credential=CredentialRef(kind="env", name=body.api_key_env or item.api_key_env)
            if (body.api_key_env or item.api_key_env)
            else CredentialRef(),
        )
        document = service.create_provider(provider, expected_version=body.version)
        return _document_view(service, document)

    @router.put("/settings/providers/{provider_id}")
    def update_provider(request: Request, provider_id: str, body: ProviderUpdate) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.update_provider(
            provider_id, body.provider, expected_version=body.version
        )
        return _document_view(service, document)

    @router.delete("/settings/providers/{provider_id}")
    def delete_provider(
        request: Request,
        provider_id: str,
        version: int = Query(ge=0),
    ) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.delete_provider(provider_id, expected_version=version)
        return _document_view(service, document)

    @router.post("/settings/providers/{provider_id}/credential")
    def set_credential(
        request: Request, provider_id: str, body: CredentialUpdate
    ) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.set_credential(
            provider_id,
            mode=body.mode,
            value=body.value,
            env_name=body.env_name,
            expected_version=body.version,
        )
        return _document_view(service, document)

    @router.delete("/settings/providers/{provider_id}/credential")
    def delete_credential(
        request: Request,
        provider_id: str,
        version: int = Query(ge=0),
    ) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.delete_credential(provider_id, expected_version=version)
        return _document_view(service, document)

    @router.post("/settings/providers/{provider_id}/models", status_code=status.HTTP_201_CREATED)
    def upsert_model(request: Request, provider_id: str, body: ModelUpsert) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.upsert_model(
            provider_id,
            body.model,
            original_model_id=body.original_model_id,
            expected_version=body.version,
        )
        return _document_view(service, document)

    @router.put("/settings/providers/{provider_id}/models/{model_id:path}")
    def update_model(
        request: Request,
        provider_id: str,
        model_id: str,
        body: ModelUpsert,
    ) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.upsert_model(
            provider_id,
            body.model,
            original_model_id=model_id,
            expected_version=body.version,
        )
        return _document_view(service, document)

    @router.delete("/settings/providers/{provider_id}/models/{model_id:path}")
    def delete_model(
        request: Request,
        provider_id: str,
        model_id: str,
        version: int = Query(ge=0),
    ) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.delete_model(provider_id, model_id, expected_version=version)
        return _document_view(service, document)

    @router.get("/settings/models")
    def available_models(request: Request) -> list[dict[str, Any]]:
        return _service(_context(request)).available_models()

    @router.post("/settings/providers/{provider_id}/test")
    def test_provider(request: Request, provider_id: str, body: ConnectionTest) -> dict[str, Any]:
        service = _service(_context(request))
        return service.test_connection(
            provider_id,
            kind=body.kind,
            model_id=body.model_id,
            confirm_cost=body.confirm_cost,
        )

    @router.post("/settings/providers/{provider_id}/discover")
    def discover_models(request: Request, provider_id: str) -> dict[str, Any]:
        service = _service(_context(request))
        return service.discover_models(provider_id)

    @router.get("/settings/export")
    def export_settings(request: Request) -> dict[str, Any]:
        return _service(_context(request)).export_config()

    @router.post("/settings/import")
    def import_settings(request: Request, body: ImportRequest) -> dict[str, Any]:
        service = _service(_context(request))
        document = service.import_config(body.payload, expected_version=body.version)
        return _document_view(service, document)

    @router.get("/settings/audit")
    def audit(
        request: Request, limit: int = Query(default=40, ge=1, le=200)
    ) -> list[dict[str, Any]]:
        return _service(_context(request)).store.audit(limit=limit)

    @router.get("/settings/logs")
    def logs(
        request: Request, limit: int = Query(default=80, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        context = _context(request)
        items = context.database.recent_events(limit=limit)
        result: list[dict[str, Any]] = []
        for item in items:
            result.append(
                {
                    "seq": item.get("seq"),
                    "run_id": item.get("run_id"),
                    "type": context.content_scrubber.text(str(item.get("type") or "")),
                    "node": context.content_scrubber.text(str(item.get("node") or "")),
                    "status": context.content_scrubber.text(str(item.get("status") or "")),
                    "summary": context.content_scrubber.text(str(item.get("summary") or "")),
                    "error": context.content_scrubber.text(str(item.get("error") or "")),
                    "timestamp": item.get("timestamp"),
                }
            )
        return result

    @router.get("/settings/docs/{name:path}")
    def documentation(request: Request, name: str) -> dict[str, str]:
        context = _context(request)
        allowed = {
            "README.md": context.settings.readme_path,
            "architecture": context.settings.docs_dir / "02-实现方案.md",
            "agents": context.settings.docs_dir / "03-AGENTS.md",
            "security": context.settings.docs_dir / "06-security.md",
        }
        path = allowed.get(name)
        if path is None or not path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "document_not_found", "message": "documentation not found"},
            )
        return {"name": name, "content": path.read_text(encoding="utf-8")[:400_000]}

    @router.get("/settings/runtime")
    def runtime_facts(request: Request) -> dict[str, Any]:
        context = _context(request)
        service = _service(context)
        document = service.document()
        stats = context.database.queue_stats()
        run_dir = context.settings.runs_dir()
        artifact_bytes, artifact_files = _directory_size(run_dir)
        return {
            "product": "CodePilot-Repair",
            "version": __version__,
            "git_commit": _git_commit(),
            "python": None,
            "worker": {
                **stats,
                "lease_seconds": context.settings.api_worker_lease_seconds,
                "poll_seconds": context.settings.api_worker_poll_seconds,
                "execution_backend": context.settings.api_execution_backend,
            },
            "docker": _docker_status(),
            "event_sink": {
                "kind": "sqlite-archive",
                "path": "artifacts/api/api.sqlite3",
                "status": "available",
            },
            "checkpointer": {
                "kind": "langgraph-sqlite",
                "scope": "per-run artifacts/<run_id>/checkpoints.sqlite",
                "status": "available",
            },
            "storage": {
                "artifact_bytes": artifact_bytes,
                "artifact_files": artifact_files,
                "artifact_root": "artifacts/runs",
            },
            "secure_storage_available": service.vault.available,
            "settings_version": document.version,
            "providers": [service.config_check(provider_id) for provider_id in document.providers],
            "audit": service.store.audit(limit=20),
        }

    @router.post("/settings/cleanup")
    def cleanup(request: Request, body: CleanupRequest) -> dict[str, Any]:
        if body.confirmation != "DELETE":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "confirmation_required",
                    "message": "type DELETE to confirm artifact cleanup",
                },
            )
        context = _context(request)
        current = context.settings_service.document()
        if body.version != current.version:
            raise VersionConflictError(body.version, current.version)
        deleted: list[str] = []
        skipped: list[dict[str, str]] = []
        run_root = context.settings.runs_dir().resolve()
        for task_id in body.task_ids:
            row = context.database.get_by_task(task_id)
            if row is None:
                skipped.append({"task_id": task_id, "reason": "not_found"})
                continue
            target = (context.settings.artifacts_dir / row.artifact_rel).resolve()
            if run_root not in target.parents:
                skipped.append({"task_id": task_id, "reason": "unsafe_artifact_path"})
                continue
            if target.is_dir():
                try:
                    _force_rmtree(target)
                except OSError:
                    skipped.append({"task_id": task_id, "reason": "artifact_cleanup_failed"})
                    continue
            try:
                context.database.delete_finished_task(task_id)
            except KeyError:
                skipped.append({"task_id": task_id, "reason": "not_found"})
                continue
            except ValueError as exc:
                skipped.append({"task_id": task_id, "reason": str(exc)})
                continue
            deleted.append(task_id)
        return {"deleted": deleted, "skipped": skipped, "settings_version": current.version}

    return router
