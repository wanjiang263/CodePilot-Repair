"""A-11: offline HTML export, escaping and read-only behavior."""

from __future__ import annotations

from pathlib import Path

from api_support import HEADERS, snapshot_tree
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from codepilot.api.reporting import build_patch_view, build_report_view, load_json
from codepilot.api.schemas import OfficialVerdictView
from codepilot.cli import app
from codepilot.html_report import render_html_report, write_html_report


def _synthetic_view(fixture: dict[str, object]):
    run_dir = Path(str(fixture["run_dir"]))
    state = load_json(run_dir / "state_final.json")
    report = load_json(run_dir / "report.json")
    view = build_report_view(
        run_id=str(fixture["run_id"]),
        task_id="instance-001",
        run_dir=run_dir,
        report=report,
        state=state,
        status="budget_exhausted",
        stop_reason="budget_test_runs",
        official=OfficialVerdictView(
            recorded=True,
            verdict="RESOLVED",
            resolved=True,
            status="SMOKE_COMPLETED_RESOLVED",
            harness_status="EXECUTED",
            authoritative=True,
        ),
        events_count=3,
        readonly=True,
    )
    patch = build_patch_view(
        run_id=str(fixture["run_id"]),
        task_id="instance-001",
        diff=str(fixture["patch"]),
        report=report,
        simulated=False,
    )
    return view, patch, state, report


def test_html_escapes_issue_diff_and_event_content(history_package: dict[str, object]) -> None:
    view, patch, _state, _report = _synthetic_view(history_package)
    malicious = "<script>alert('xss')</script>"
    view = view.model_copy(update={"issue": malicious, "failures": [malicious]})
    patch.files[0].diff = patch.files[0].diff.replace(
        "def add(a, b):", f"def add(a, b):  # {malicious}"
    )
    events = [
        {
            "seq": 1,
            "node": "plan",
            "type": "node_end",
            "status": "ok",
            "summary": malicious,
            "timestamp": "2026-10-20T13:31:51Z",
        }
    ]
    html = render_html_report(report=view, events=events, patch=patch)
    assert malicious not in html
    assert "&lt;script&gt;" in html
    assert "RESOLVED" in html
    assert "budget_test_runs" in html
    assert "budget_exhausted" in html
    assert "Unexecuted stages" in html
    assert "workflow" in html.lower()


def test_html_export_writes_outside_the_historical_package(
    tmp_path: Path, history_package: dict[str, object]
) -> None:
    before = snapshot_tree(Path(str(history_package["package"])))
    view, patch, _state, _report = _synthetic_view(history_package)
    output = tmp_path / "exports" / "report.html"
    written = write_html_report(report=view, events=[], patch=patch, output=output)
    assert written.is_file()
    text = written.read_text(encoding="utf-8")
    assert "CodePilot-Repair run" in text
    assert "Official" in text
    assert "Internal" in text
    assert snapshot_tree(Path(str(history_package["package"]))) == before


def test_cli_report_html_and_markdown(tmp_path: Path, history_package: dict[str, object]) -> None:
    run_dir = Path(str(history_package["run_dir"]))
    output = tmp_path / "cli-report.html"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["report", "--run-dir", str(run_dir), "--html", "--output", str(output)],
    )
    assert result.exit_code == 0, result.output
    assert output.is_file()
    text = output.read_text(encoding="utf-8")
    assert "official" in text.lower()
    assert "budget_test_runs" in text

    markdown = runner.invoke(app, ["report", "--run-dir", str(run_dir)])
    assert markdown.exit_code == 0
    assert "archived report" in markdown.output


def test_cli_report_history_run_uses_the_catalog(
    tmp_path: Path, history_package: dict[str, object]
) -> None:
    output = tmp_path / "history-report.html"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "report",
            str(history_package["run_id"]),
            "--run-dir",
            str(history_package["run_dir"]),
            "--html",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output.is_file()
    assert "RESOLVED" in output.read_text(encoding="utf-8")


def test_cli_report_rejects_a_directory_without_artifacts(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["report", "--run-dir", str(tmp_path)])
    assert result.exit_code != 0


def test_report_endpoint_reads_a_finalized_run(
    client: TestClient, worker, api_db, api_settings
) -> None:
    from api_support import DEMO_ISSUE

    created = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": DEMO_ISSUE},
    ).json()
    worker.run_once()
    response = client.get(f"/api/v1/runs/{created['run_id']}/report", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["internal"]["verification_level"] == "m2_verified"
    assert response.json()["official"]["recorded"] is False
