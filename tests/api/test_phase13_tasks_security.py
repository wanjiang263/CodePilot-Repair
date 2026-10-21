"""A-02/A-03/A-09: task lifecycle and repository-source hardening."""

from __future__ import annotations

from pathlib import Path

import pytest
from api_support import DEMO_ISSUE, HEADERS, make_settings
from fastapi.testclient import TestClient

from codepilot.api.app import create_app
from codepilot.api.security import (
    RepositorySecurityError,
    validate_base_commit,
    validate_repository,
)
from codepilot.config import Settings


def test_valid_https_repository_is_normalized(api_settings: Settings) -> None:
    repository = validate_repository(
        kind="https",
        value="https://github.com/psf/requests",
        settings=api_settings,
    )
    assert repository.kind == "https"
    assert repository.value == "https://github.com/psf/requests"
    assert repository.local_path is None


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "/etc/passwd",
        "../repo",
        r"C:\Users\someone\repo",
        "ssh://github.com/psf/requests",
        "git@github.com:psf/requests.git",
        "http://github.com/psf/requests",
        "https://127.0.0.1/owner/repo",
        "https://localhost/owner/repo",
        "https://github.com.evil.example/owner/repo",
        "https://user:pass@github.com/owner/repo",
        "https://github.com/owner/repo?ref=main",
        "https://github.com/owner/repo#frag",
        "https://github.com/owner/../repo",
        "https://github.com/%2e%2e/repo",
        "https://github.com/owner/repo/",
        "https://github.com/",
        "https://github.com/owner",
        "https://github.com:8443/owner/repo",
    ],
)
def test_repository_sources_outside_the_policy_are_rejected(
    api_settings: Settings, value: str
) -> None:
    with pytest.raises(RepositorySecurityError):
        validate_repository(kind="https", value=value, settings=api_settings)


def test_demo_repository_is_a_server_side_mapping(api_settings: Settings) -> None:
    repository = validate_repository(kind="demo", value="mini_calc", settings=api_settings)
    assert repository.kind == "demo"
    assert repository.local_path is not None
    assert repository.local_path.is_dir()
    for bad in ("../mini_calc", "..", "mini_calc/../../x", "Mini_Calc"):
        with pytest.raises(RepositorySecurityError):
            validate_repository(kind="demo", value=bad, settings=api_settings)


@pytest.mark.parametrize("commit", ["HEAD", "deadbeef", "a" * 40])
def test_base_commit_accepts_head_and_hex(commit: str) -> None:
    assert validate_base_commit(commit)


@pytest.mark.parametrize("commit", ["main", "HEAD~1", "abc", "z" * 40, "deadbeef;rm"])
def test_base_commit_rejects_revisions(commit: str) -> None:
    with pytest.raises(RepositorySecurityError):
        validate_base_commit(commit)


def test_create_demo_task_returns_identity(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["task_id"].startswith("task_")
    assert data["run_id"].startswith("run_")
    assert data["status"] == "queued"
    assert data["ui_status"] == "queued"
    assert data["repository"] == "demo/mini_calc"
    assert data["events_count"] == 1

    detail = client.get(f"/api/v1/tasks/{data['task_id']}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["issue"] == DEMO_ISSUE
    assert detail.json()["budget"]["max_test_runs"] == 12

    listing = client.get("/api/v1/tasks", headers=HEADERS)
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["total"] == 1
    assert payload["items"][0]["task_id"] == data["task_id"]

    filtered = client.get("/api/v1/tasks?status=queued", headers=HEADERS)
    assert filtered.json()["total"] == 1
    empty = client.get("/api/v1/tasks?status=completed", headers=HEADERS)
    assert empty.json()["total"] == 0
    missing = client.get("/api/v1/tasks/task_missing", headers=HEADERS)
    assert missing.status_code == 404


def test_allowlisted_https_task_is_accepted_without_running(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "https", "value": "https://github.com/psf/requests"},
            "issue": DEMO_ISSUE,
        },
    )
    assert response.status_code == 201
    assert response.json()["repository"] == "github.com/psf/requests"


def test_rejected_repository_is_a_400(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "https", "value": "https://evil.example/owner/repo"},
            "issue": DEMO_ISSUE,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "repository_rejected"


def test_docker_mode_is_disabled_by_default(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "execution_mode": "docker",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "execution_mode_disabled"


def test_invalid_base_commit_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "base_commit": "main",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "repository_rejected"


def test_issue_length_is_validated(client: TestClient) -> None:
    too_short = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": "short"},
    )
    assert too_short.status_code == 422
    too_long = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": "x" * 20_001},
    )
    assert too_long.status_code == 422


def test_budget_overrides_are_bounded(client: TestClient) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "budget": {"max_iterations": 99},
        },
    )
    assert response.status_code == 422
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={
            "repository": {"kind": "demo", "value": "mini_calc"},
            "issue": DEMO_ISSUE,
            "budget": {"max_test_runs": 6, "max_runtime_seconds": 300},
        },
    )
    assert response.status_code == 201
    detail = client.get(f"/api/v1/tasks/{response.json()['task_id']}", headers=HEADERS)
    assert detail.json()["budget"]["max_test_runs"] == 6
    assert detail.json()["budget"]["max_runtime_seconds"] == 300


def test_api_restart_keeps_the_durable_queue(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as first:
        created = first.post(
            "/api/v1/tasks",
            headers=HEADERS,
            json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": DEMO_ISSUE},
        ).json()
    with TestClient(create_app(settings)) as restarted:
        detail = restarted.get(f"/api/v1/tasks/{created['task_id']}", headers=HEADERS)
        assert detail.status_code == 200
        assert detail.json()["run_id"] == created["run_id"]
        assert detail.json()["status"] == "queued"


def test_meta_exposes_no_paths_or_secrets(client: TestClient) -> None:
    response = client.get("/api/v1/meta", headers=HEADERS)
    assert response.status_code == 200
    text = response.text
    assert "demo/mini_calc" in text
    assert "C:\\\\" not in text and "/Users/" not in text and "/root/" not in text
    assert "test-key" not in text
