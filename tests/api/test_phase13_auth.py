"""A-01/A-12: startup refusal, 401 coverage and request-size limits."""

from __future__ import annotations

from pathlib import Path

import pytest
from api_support import KEY, make_settings
from fastapi.testclient import TestClient

from codepilot.api.app import create_app
from codepilot.config import Settings


def test_refuses_to_start_without_key() -> None:
    with pytest.raises(RuntimeError, match="CODEPILOT_API_KEY"):
        create_app(Settings(api_key=None))


def test_refuses_blank_or_short_key() -> None:
    with pytest.raises(RuntimeError):
        create_app(Settings(api_key="   "))
    with pytest.raises(RuntimeError, match="太短"):
        create_app(Settings(api_key="short"))


def test_docs_are_not_public(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/health"),
        ("GET", "/api/v1/meta"),
        ("GET", "/api/v1/tasks"),
        ("GET", "/api/v1/tasks/task_missing"),
        ("GET", "/api/v1/runs/run_missing/events"),
        ("GET", "/api/v1/runs/run_missing/patch"),
        ("GET", "/api/v1/runs/run_missing/report"),
        ("POST", "/api/v1/runs/run_missing/cancel"),
        ("GET", "/api/v1/benchmarks/nope"),
        ("GET", "/api/v1/history"),
        ("GET", "/api/v1/openapi.json"),
    ],
)
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_every_protected_endpoint_requires_the_key(
    client: TestClient, method: str, path: str, headers: dict[str, str]
) -> None:
    response = client.request(method, path, headers=headers)
    assert response.status_code == 401
    assert KEY not in response.text


def test_health_with_key_is_minimal(client: TestClient) -> None:
    response = client.get("/health", headers={"X-API-Key": KEY})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert KEY not in response.text


def test_openapi_is_available_behind_auth(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json", headers={"X-API-Key": KEY})
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "CodePilot-Repair API"
    assert "/api/v1/tasks" in response.json()["paths"]


def test_request_body_limit_returns_413(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api_max_body_bytes=1024)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/tasks",
            headers={"X-API-Key": KEY, "Content-Type": "application/json"},
            content=b'{"issue":"' + b"x" * 2000 + b'"}',
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"


def test_validation_errors_do_not_echo_large_input(client: TestClient) -> None:
    payload = "sensitive-" + "x" * 30_000
    response = client.post(
        "/api/v1/tasks",
        headers={"X-API-Key": KEY},
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": payload,
        },
    )
    assert response.status_code == 422
    assert "sensitive-" not in response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_trace_id_is_returned_and_not_a_secret(client: TestClient) -> None:
    response = client.get("/health", headers={"X-API-Key": KEY})
    assert response.headers["x-trace-id"]
    assert KEY not in response.headers["x-trace-id"]
