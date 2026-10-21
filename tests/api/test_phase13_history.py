"""A-04/A-05/A-07: read-only benchmark replay and artifact integrity."""

from __future__ import annotations

from pathlib import Path

import pytest
from api_support import HEADERS, make_settings, snapshot_tree, sse_payloads
from fastapi.testclient import TestClient

from codepilot.api.app import create_app
from codepilot.api.history import HistoryCatalog
from codepilot.config import PROJECT_ROOT


def history_client(tmp_path: Path, fixture: dict[str, object]) -> TestClient:
    settings = make_settings(tmp_path, api_history_catalog=fixture["catalog"])
    return TestClient(create_app(settings))


def test_history_replays_official_and_internal_verdicts_separately(
    tmp_path: Path, history_package: dict[str, object]
) -> None:
    before = snapshot_tree(history_package["package"])
    with history_client(tmp_path, history_package) as client:
        listing = client.get("/api/v1/history", headers=HEADERS)
        assert listing.status_code == 200
        items = listing.json()
        assert len(items) == 1
        assert items[0]["official_verdict"] == "RESOLVED"
        assert items[0]["verification_level"] == "budget_exhausted"
        assert items[0]["ui_status"] == "stopped"

        benchmark = client.get("/api/v1/benchmarks/history-fixture", headers=HEADERS)
        assert benchmark.status_code == 200
        payload = benchmark.json()
        assert payload["official"]["verdict"] == "RESOLVED"
        assert payload["official"]["harness_status"] == "EXECUTED"
        assert payload["internal"]["verification_level"] == "budget_exhausted"
        assert payload["status"] == "budget_exhausted"
        assert payload["stop_reason"] == "budget_test_runs"
        assert payload["ui_status"] == "stopped"
        assert all(check["ok"] for check in payload["integrity"]), payload["integrity"]
        assert payload["readonly"] is True

        report = client.get(f"/api/v1/runs/{history_package['run_id']}/report", headers=HEADERS)
        assert report.status_code == 200
        report_payload = report.json()
        assert report_payload["readonly"] is True
        assert report_payload["official"]["verdict"] == "RESOLVED"
        assert report_payload["internal"]["verification_level"] == "budget_exhausted"
        assert report_payload["raw"] == {}

        patch = client.get(f"/api/v1/runs/{history_package['run_id']}/patch", headers=HEADERS)
        assert patch.status_code == 200
        assert patch.json()["identity_verified"] is True
        assert patch.json()["files"][0]["path"] == "src/calc.py"

        with client.stream(
            "GET",
            f"/api/v1/runs/{history_package['run_id']}/events?after_seq=1",
            headers=HEADERS,
        ) as response:
            text = "".join(response.iter_text())
        events = [item for item in sse_payloads(text) if item.get("_event") == "repair"]
        assert [event["seq"] for event in events] == [2, 3]
        assert any(item.get("type") == "stream_end" for item in sse_payloads(text))

        cancelled = client.post(f"/api/v1/runs/{history_package['run_id']}/cancel", headers=HEADERS)
        assert cancelled.status_code == 409
        assert cancelled.json()["error"]["code"] == "readonly_replay"

        traversal = client.get(
            f"/api/v1/runs/{history_package['run_id']}/patch?file=../state_final.json",
            headers=HEADERS,
        )
        assert traversal.status_code == 400
        missing = client.get(
            f"/api/v1/runs/{history_package['run_id']}/patch?file=src/missing.py",
            headers=HEADERS,
        )
        assert missing.status_code == 404

    after = snapshot_tree(history_package["package"])
    assert after == before


def test_catalog_rejects_packages_without_a_runnable_run(tmp_path: Path) -> None:
    catalog = tmp_path / "empty.yaml"
    catalog.write_text("benchmarks: []\n", encoding="utf-8")
    settings = make_settings(tmp_path, api_history_catalog=catalog)
    catalog_model = HistoryCatalog(settings)
    assert catalog_model.entries == ()
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/history", headers=HEADERS).json() == []
        assert client.get("/api/v1/benchmarks/nope", headers=HEADERS).status_code == 404


@pytest.mark.skipif(
    not (PROJECT_ROOT / "artifacts/phase11-glm14-20261020/run-artifacts").is_dir(),
    reason="real GLM14 archived artifacts are not present on this checkout",
)
def test_real_glm14_artifact_replay_keeps_verdicts_separate(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        api_history_catalog=PROJECT_ROOT / "configs" / "benchmarks.yaml",
    )
    with TestClient(create_app(settings)) as client:
        entries = client.get("/api/v1/history", headers=HEADERS).json()
        assert len(entries) >= 4
        glm14 = next(item for item in entries if item["history_id"] == "glm14-marshmallow-1343")
        assert glm14["official_verdict"] == "RESOLVED"
        assert glm14["verification_level"] == "budget_exhausted"
        benchmark = client.get("/api/v1/benchmarks/glm14-marshmallow-1343", headers=HEADERS).json()
        assert benchmark["official"]["verdict"] == "RESOLVED"
        assert benchmark["internal"]["verification_level"] == "budget_exhausted"
        assert benchmark["stop_reason"] == "budget_test_runs"
        assert all(check["ok"] for check in benchmark["integrity"]), benchmark["integrity"]
