"""A-05/A-12: patch/report identity checks and path containment."""

from __future__ import annotations

import json
from typing import Any

from api_support import DEMO_ISSUE, HEADERS
from fastapi.testclient import TestClient

from codepilot.api.worker import ApiWorker
from codepilot.config import Settings


def create_and_run(client: TestClient, worker: ApiWorker) -> dict[str, Any]:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": DEMO_ISSUE},
    )
    assert response.status_code == 201
    worker.run_once()
    return response.json()


def test_patch_and_report_are_bound_to_the_run_identity(
    client: TestClient, worker: ApiWorker, api_settings: Settings
) -> None:
    created = create_and_run(client, worker)
    run_id = created["run_id"]
    run_dir = api_settings.artifacts_dir / "runs" / run_id

    patch_before = (run_dir / "patch_final.diff").read_text(encoding="utf-8")
    response = client.get(
        f"/api/v1/runs/{run_id}/patch?file=src/demo_calc/calc.py", headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["files_count"] == 1
    assert response.json()["identity_verified"] is True

    (run_dir / "patch_final.diff").write_text(patch_before + "\n# tampered\n", encoding="utf-8")
    tampered = client.get(f"/api/v1/runs/{run_id}/patch", headers=HEADERS)
    assert tampered.status_code == 409
    assert tampered.json()["error"]["code"] == "patch_identity_mismatch"
    (run_dir / "patch_final.diff").write_text(patch_before, encoding="utf-8")

    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    report["run_id"] = "run_someone_else"
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    cross_run = client.get(f"/api/v1/runs/{run_id}/report", headers=HEADERS)
    assert cross_run.status_code == 409
    assert cross_run.json()["error"]["code"] == "report_identity_mismatch"


def test_patch_and_report_are_not_available_before_finalize(
    client: TestClient, api_settings: Settings
) -> None:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": DEMO_ISSUE},
    )
    created = response.json()
    patch = client.get(f"/api/v1/runs/{created['run_id']}/patch", headers=HEADERS)
    assert patch.status_code == 404
    assert patch.json()["error"]["code"] == "patch_not_available"
    report = client.get(f"/api/v1/runs/{created['run_id']}/report", headers=HEADERS)
    assert report.status_code == 409
    assert report.json()["error"]["code"] == "report_not_ready"


def test_report_surface_does_not_expose_host_paths_or_the_api_key(
    client: TestClient, worker: ApiWorker, api_settings: Settings
) -> None:
    created = create_and_run(client, worker)
    response = client.get(f"/api/v1/runs/{created['run_id']}/report", headers=HEADERS)
    assert response.status_code == 200
    text = response.text
    assert "git_diff" not in response.json()["raw"]
    assert "test-key-0123456789abcdef" not in text
    assert str(api_settings.artifacts_dir) not in text
    assert str(api_settings.cache_dir) not in text


def test_patch_file_lookup_rejects_paths_outside_the_patch(
    client: TestClient, worker: ApiWorker
) -> None:
    created = create_and_run(client, worker)
    run_id = created["run_id"]
    for path in ("../report.json", "/etc/passwd", "src/../state_final.json"):
        response = client.get(
            f"/api/v1/runs/{run_id}/patch", params={"file": path}, headers=HEADERS
        )
        assert response.status_code == 400
    missing = client.get(
        f"/api/v1/runs/{run_id}/patch", params={"file": "src/missing.py"}, headers=HEADERS
    )
    assert missing.status_code == 404
