"""Phase 14 Settings API, credential masking, live admission and routing E2E."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_support import DEMO_ISSUE, HEADERS, KEY, make_settings
from fastapi.testclient import TestClient

from codepilot.api.app import create_app
from codepilot.api.demo import _DemoScript
from codepilot.api.worker import ApiWorker
from codepilot.config import Settings
from codepilot.llm.types import LLMRequest, Message, Role
from codepilot.settings.credentials import CredentialVault
from codepilot.settings.service import SettingsService
from codepilot.settings.store import SettingsStore

_SECRET = "sk-live-secret-0123456789"


def _model(model_id: str) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "display_name": model_id,
        "context_window": 32768,
        "max_output_tokens": 4096,
        "supports_tools": True,
        "supports_json_mode": True,
        "supports_temperature": True,
        "price": {
            "currency": "CNY",
            "input_per_mtok": 1.0,
            "output_per_mtok": 2.0,
            "cached_input_per_mtok": 0.1,
        },
    }


def _provider(provider_id: str, model_id: str) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "name": provider_id.title(),
        "protocol": "openai_compatible",
        "base_url": f"https://{provider_id}.test/v1",
        "credential": {"kind": "env", "name": "PHASE14_TEST_KEY"},
        "models": [_model(model_id)],
    }


def _live_environment(tmp_path: Path) -> tuple[Settings, SettingsService, TestClient]:
    settings = make_settings(
        tmp_path,
        api_execution_backend="fake",
        api_live_allow_simulated_sandbox=True,
        api_max_live_cost=10.0,
    )
    service = SettingsService(
        SettingsStore(settings.resolved_settings_path),
        CredentialVault(settings.resolved_settings_credentials_dir),
    )
    client = TestClient(create_app(settings, settings_service=service))
    return settings, service, client


def _configure(client: TestClient, service: SettingsService) -> None:
    response = client.post(
        "/api/v1/settings/providers",
        headers=HEADERS,
        json={"version": 0, "provider": _provider("alpha", "alpha-model")},
    )
    assert response.status_code == 201, response.text
    version = response.json()["version"]
    response = client.post(
        "/api/v1/settings/providers",
        headers=HEADERS,
        json={"version": version, "provider": _provider("beta", "beta-model")},
    )
    assert response.status_code == 201, response.text
    version = response.json()["version"]
    response = client.put(
        "/api/v1/settings/sections/agents",
        headers=HEADERS,
        json={
            "version": version,
            "values": {
                "default_provider_id": "alpha",
                "default_model_id": "alpha-model",
                "roles": {
                    "planner": {
                        "provider_id": "beta",
                        "model_id": "beta-model",
                        "reasoning": "inherit",
                    },
                    "coder": {
                        "provider_id": "beta",
                        "model_id": "beta-model",
                        "reasoning": "inherit",
                    },
                    "reviewer": {
                        "provider_id": "beta",
                        "model_id": "beta-model",
                        "reasoning": "inherit",
                    },
                },
            },
        },
    )
    assert response.status_code == 200, response.text


def test_settings_api_requires_auth_and_never_returns_secret(tmp_path: Path) -> None:
    _settings, _service, client = _live_environment(tmp_path)
    assert client.get("/api/v1/settings").status_code == 401

    response = client.post(
        "/api/v1/settings/providers",
        headers=HEADERS,
        json={"version": 0, "provider": _provider("alpha", "alpha-model")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 1
    assert _SECRET not in response.text

    response = client.post(
        "/api/v1/settings/providers/alpha/credential",
        headers=HEADERS,
        json={"version": 1, "mode": "value", "value": _SECRET},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    credential = payload["providers"][0]["credential"]
    assert credential["kind"] == "vault"
    assert credential["configured"] is True
    assert _SECRET not in response.text
    assert "name" not in credential or credential.get("name") is None

    # The export path is convenience-only and must remove credential material.
    exported = client.get("/api/v1/settings/export", headers=HEADERS)
    assert exported.status_code == 200
    assert _SECRET not in exported.text
    assert exported.json()["providers"]["alpha"]["credential"]["kind"] == "vault"

    # Stale writes are rejected rather than silently overwriting newer settings.
    conflict = client.post(
        "/api/v1/settings/providers",
        headers=HEADERS,
        json={"version": 0, "provider": _provider("stale", "m")},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "version_conflict"


def test_settings_catalog_and_template_creation(tmp_path: Path) -> None:
    _settings, _service, client = _live_environment(tmp_path)
    catalog = client.get("/api/v1/settings/catalog", headers=HEADERS)
    assert catalog.status_code == 200
    ids = {item["catalog_id"] for item in catalog.json()}
    assert "deepseek" in ids and "anthropic" in ids and "bedrock" in ids
    response = client.post(
        "/api/v1/settings/providers/from-template",
        headers=HEADERS,
        json={
            "version": 0,
            "catalog_id": "deepseek",
            "provider_id": "deepseek-main",
            "api_key_env": "DEEPSEEK_API_KEY",
        },
    )
    assert response.status_code == 201, response.text
    provider = response.json()["providers"][0]
    assert provider["catalog_id"] == "deepseek"
    assert provider["models"][0]["routing_ready"] is True


def test_live_task_freezes_routes_and_worker_observes_provider_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHASE14_TEST_KEY", _SECRET)
    settings, service, client = _live_environment(tmp_path)
    _configure(client, service)

    meta = client.get("/api/v1/meta", headers=HEADERS).json()
    assert meta["live_llm_enabled"] is True
    assert any(item["ready"] for item in meta["live_models"])
    assert "live" in meta["execution_modes"]

    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "execution_mode": "live",
            "budget": {"max_cost": 1.0},
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["llm_mode"] == "live"
    assert task["provider_id"] == "alpha"
    assert task["model_id"] == "alpha-model"
    assert task["config_hash"]
    assert task["simulated_llm"] is False
    task_id = task["task_id"]
    run_id = task["run_id"]
    frozen_hash = task["config_hash"]

    # Changing settings after creation must not rewrite the already-frozen task.
    version = client.get("/api/v1/settings", headers=HEADERS).json()["version"]
    changed = client.put(
        "/api/v1/settings/sections/agents",
        headers=HEADERS,
        json={
            "version": version,
            "values": {
                "default_provider_id": "beta",
                "default_model_id": "beta-model",
                "roles": {},
            },
        },
    )
    assert changed.status_code == 200, changed.text
    reread = client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS).json()
    assert reread["config_hash"] == frozen_hash
    assert reread["provider_id"] == "alpha"

    script = _DemoScript()
    observed: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        purpose = request.headers.get("x-codepilot-purpose", "generic")
        host = request.url.host
        observed.append((host, purpose))
        body = json.loads(request.content.decode("utf-8"))
        messages = [
            Message(
                role=Role(item.get("role", "user")),
                content=item.get("content") or "",
            )
            for item in body.get("messages", [])
        ]
        reply = script._reply(LLMRequest(messages=messages, purpose=purpose))
        if isinstance(reply, dict) and "tool" in reply:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{len(observed)}",
                        "type": "function",
                        "function": {
                            "name": reply["tool"],
                            "arguments": json.dumps(reply["arguments"]),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": json.dumps(reply, ensure_ascii=False),
            }
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "model": "mock-live",
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 24},
            },
        )

    service._transport = httpx.MockTransport(handler)  # test transport seam
    worker = ApiWorker(
        settings=settings,
        database=client.app.state.api_context.database,
        settings_service=service,
    )
    assert worker.run_once() is not None

    completed = client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS).json()
    assert completed["ui_status"] == "completed", completed
    assert completed["simulated_llm"] is False
    assert completed["simulated_sandbox"] is True
    assert completed["patch_available"] is True

    hosts = {host for host, _purpose in observed}
    assert hosts == {"alpha.test", "beta.test"}
    purposes = {purpose for _host, purpose in observed}
    assert {"localizer_files", "planner", "coder", "reviewer"} <= purposes
    assert all(
        host == "beta.test"
        for host, purpose in observed
        if purpose in {"planner", "coder", "reviewer"}
    )
    assert all(
        host == "alpha.test" for host, purpose in observed if purpose.startswith("localizer")
    )

    report = client.get(f"/api/v1/runs/{run_id}/report", headers=HEADERS)
    assert report.status_code == 200
    assert report.json()["summary"]["simulated_llm"] is False
    assert report.json()["summary"]["llm_mode"] == "live"
    assert report.json()["summary"]["config_hash"] == frozen_hash

    # Secrets and API auth keys must not reach artifacts or archived API events.
    for path in settings.artifacts_dir.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert _SECRET.encode() not in raw, path
            assert KEY.encode() not in raw, path
    with client.app.state.api_context.database.connect() as connection:
        rows = connection.execute("SELECT payload_json FROM api_events").fetchall()
    assert all(_SECRET not in row[0] for row in rows)


def test_live_task_uses_native_anthropic_messages_in_the_m2_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "sk-ant-phase15-0123456789"
    monkeypatch.setenv("PHASE15_ANTHROPIC_KEY", key)
    settings, service, client = _live_environment(tmp_path)
    provider = {
        "provider_id": "claude",
        "name": "Claude",
        "protocol": "anthropic_messages",
        "base_url": "https://claude.test",
        "credential": {"kind": "env", "name": "PHASE15_ANTHROPIC_KEY"},
        "models": [_model("claude-test-model")],
    }
    created = client.post(
        "/api/v1/settings/providers",
        headers=HEADERS,
        json={"version": 0, "provider": provider},
    )
    assert created.status_code == 201, created.text
    configured = client.put(
        "/api/v1/settings/sections/agents",
        headers=HEADERS,
        json={
            "version": created.json()["version"],
            "values": {
                "default_provider_id": "claude",
                "default_model_id": "claude-test-model",
                "roles": {},
            },
        },
    )
    assert configured.status_code == 200, configured.text

    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "execution_mode": "live",
            "budget": {"max_cost": 1.0},
        },
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["task_id"]
    run_id = response.json()["run_id"]

    script = _DemoScript()
    observed: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        purpose = request.headers.get("x-codepilot-purpose", "generic")
        observed.append((request.url.path, purpose, str(request.url)))
        assert request.headers.get("x-api-key") == key
        assert request.headers.get("anthropic-version") == "2023-06-01"
        assert "authorization" not in request.headers
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "claude-test-model"
        assert body["max_tokens"] > 0
        texts: list[str] = []
        for message in body.get("messages", []):
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        texts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_result":
                        result = block.get("content")
                        texts.append(result if isinstance(result, str) else json.dumps(result))
        reply = script._reply(
            LLMRequest(messages=[Message.user("\n".join(texts))], purpose=purpose)
        )
        usage = {"input_tokens": 120, "output_tokens": 24, "cache_read_input_tokens": 0}
        if isinstance(reply, dict) and "tool" in reply:
            content = [
                {
                    "type": "tool_use",
                    "id": f"toolu_{len(observed)}",
                    "name": reply["tool"],
                    "input": reply["arguments"],
                }
            ]
            stop_reason = "tool_use"
        else:
            content = [{"type": "text", "text": json.dumps(reply, ensure_ascii=False)}]
            stop_reason = "end_turn"
        return httpx.Response(
            200,
            json={
                "id": f"msg_{len(observed)}",
                "type": "message",
                "role": "assistant",
                "model": "claude-test-model",
                "content": content,
                "stop_reason": stop_reason,
                "usage": usage,
            },
        )

    service._transport = httpx.MockTransport(handler)
    worker = ApiWorker(
        settings=settings,
        database=client.app.state.api_context.database,
        settings_service=service,
    )
    assert worker.run_once() is not None

    completed = client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS).json()
    assert completed["ui_status"] == "completed", completed
    assert completed["patch_available"] is True
    assert completed["provider_id"] == "claude"
    assert completed["model_id"] == "claude-test-model"
    # Every role went through the native Messages adapter, not an OpenAI shim.
    assert observed and all(path.endswith("/v1/messages") for path, _purpose, _url in observed)
    purposes = {purpose for _path, purpose, _url in observed}
    assert {"localizer_files", "planner", "coder", "reviewer"} <= purposes
    report = client.get(f"/api/v1/runs/{run_id}/report", headers=HEADERS)
    assert report.status_code == 200
    assert report.json()["summary"]["llm_mode"] == "live"
    assert report.json()["summary"]["config_hash"] == completed["config_hash"]
    for path in settings.artifacts_dir.rglob("*"):
        if path.is_file():
            assert key.encode() not in path.read_bytes(), path


def test_live_task_requires_cost_cap_and_complete_model_specs(tmp_path: Path) -> None:
    _settings, service, client = _live_environment(tmp_path)
    _configure(client, service)
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "execution_mode": "live",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "live_cost_cap_required"

    # Remove the only complete price and ensure the task is refused before queueing.
    version = client.get("/api/v1/settings", headers=HEADERS).json()["version"]
    provider = client.get("/api/v1/settings/providers", headers=HEADERS).json()[0]
    provider = provider["editable"]
    provider["models"][0]["price"] = {"currency": "CNY"}
    updated = client.put(
        "/api/v1/settings/providers/alpha",
        headers=HEADERS,
        json={"version": version, "provider": provider},
    )
    assert updated.status_code == 200, updated.text
    version = updated.json()["version"]
    # Detach routes so the incomplete provider is not blocked by role validation.
    client.put(
        "/api/v1/settings/sections/agents",
        headers=HEADERS,
        json={
            "version": version,
            "values": {"default_provider_id": None, "default_model_id": None, "roles": {}},
        },
    )
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "execution_mode": "live",
            "budget": {"max_cost": 1.0},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {
        "default_model_missing",
        "model_specs_incomplete",
    }


def test_connection_test_endpoint_distinguishes_kinds(tmp_path: Path) -> None:
    monkeypatch_key = "sk-endpoint-secret"
    import os

    os.environ["PHASE14_TEST_KEY"] = monkeypatch_key
    settings = make_settings(tmp_path)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "alpha-model"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    service = SettingsService(
        SettingsStore(settings.resolved_settings_path),
        CredentialVault(settings.resolved_settings_credentials_dir),
        client_transport=httpx.MockTransport(handler),
    )
    with TestClient(create_app(settings, settings_service=service)) as client:
        client.post(
            "/api/v1/settings/providers",
            headers=HEADERS,
            json={"version": 0, "provider": _provider("alpha", "alpha-model")},
        )
        config_only = client.post(
            "/api/v1/settings/providers/alpha/test",
            headers=HEADERS,
            json={"kind": "config"},
        )
        assert config_only.status_code == 200
        assert config_only.json()["ok"] is True
        assert seen == []
        after_config = client.get("/api/v1/settings", headers=HEADERS).json()["providers"][0]
        assert after_config["verification"]["config_test"]["state"] == "ok"
        assert after_config["verification"]["inference_verified"] == 0
        assert after_config["models"][0]["verification"]["inference"]["state"] == "not_tested"
        models = client.post(
            "/api/v1/settings/providers/alpha/test",
            headers=HEADERS,
            json={"kind": "models"},
        )
        assert models.status_code == 200
        assert models.json()["models"] == ["alpha-model"]
        denied = client.post(
            "/api/v1/settings/providers/alpha/test",
            headers=HEADERS,
            json={"kind": "inference", "model_id": "alpha-model"},
        )
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "cost_confirmation_required"
        inference = client.post(
            "/api/v1/settings/providers/alpha/test",
            headers=HEADERS,
            json={
                "kind": "inference",
                "model_id": "alpha-model",
                "confirm_cost": True,
            },
        )
        assert inference.status_code == 200, inference.text
        payload = inference.json()
        assert payload["ok"] is True
        assert payload["tool_calling"] == "declared_not_verified"
        assert payload["checked_at"]
        assert monkeypatch_key not in inference.text
        assert all(
            request.headers.get("authorization") == f"Bearer {monkeypatch_key}" for request in seen
        )
        after_inference = client.get("/api/v1/settings", headers=HEADERS).json()["providers"][0]
        assert after_inference["verification"]["models_test"]["state"] == "ok"
        assert after_inference["verification"]["inference_verified"] == 1
        status = after_inference["models"][0]["verification"]["inference"]
        assert status["state"] == "verified"
        assert status["checked_at"] == payload["checked_at"]


def test_runtime_logs_docs_and_scoped_cleanup(tmp_path: Path) -> None:
    settings, service, client = _live_environment(tmp_path)
    created = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": DEMO_ISSUE},
    )
    assert created.status_code == 201, created.text
    task = created.json()
    worker = ApiWorker(
        settings=settings,
        database=client.app.state.api_context.database,
        settings_service=service,
    )
    assert worker.run_once() is not None

    runtime = client.get("/api/v1/settings/runtime", headers=HEADERS)
    assert runtime.status_code == 200, runtime.text
    facts = runtime.json()
    assert facts["settings_version"] >= 0
    assert facts["storage"]["artifact_files"] > 0

    logs = client.get("/api/v1/settings/logs", headers=HEADERS)
    assert logs.status_code == 200
    assert any(entry["type"] for entry in logs.json())

    docs = client.get("/api/v1/settings/docs/README.md", headers=HEADERS)
    assert docs.status_code == 200
    assert "CodePilot" in docs.json()["content"]
    assert client.get("/api/v1/settings/docs/secrets.txt", headers=HEADERS).status_code == 404

    version = client.get("/api/v1/settings", headers=HEADERS).json()["version"]
    denied = client.post(
        "/api/v1/settings/cleanup",
        headers=HEADERS,
        json={"version": version, "task_ids": [task["task_id"]], "confirmation": "no"},
    )
    assert denied.status_code == 400
    cleanup = client.post(
        "/api/v1/settings/cleanup",
        headers=HEADERS,
        json={"version": version, "task_ids": [task["task_id"]], "confirmation": "DELETE"},
    )
    assert cleanup.status_code == 200, cleanup.text
    assert cleanup.json()["deleted"] == [task["task_id"]]
    assert client.get(f"/api/v1/tasks/{task['task_id']}", headers=HEADERS).status_code == 404
    assert not (settings.artifacts_dir / "runs" / task["run_id"]).exists()
