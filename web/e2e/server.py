"""Isolated real API + offline worker for browser acceptance; never load .env."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uvicorn

from codepilot.api.app import create_app
from codepilot.api.database import ApiDatabase
from codepilot.api.worker import ApiWorker
from codepilot.config import Settings

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "browser-fixture-key-not-a-provider-secret"


def settings(scratch: Path) -> Settings:
    catalog = scratch / "benchmarks.yaml"
    if not catalog.exists():
        catalog.write_text("benchmarks: []\n", encoding="utf-8")
    return Settings(
        _env_file=None,
        api_key=API_KEY,
        artifacts_dir=scratch / "artifacts",
        api_queue_path=scratch / "queue.sqlite3",
        settings_path=scratch / "settings.sqlite3",
        api_execution_backend="fake",
        api_allow_live_llm=False,
        api_worker_poll_seconds=0.05,
        api_sse_poll_seconds=0.05,
        api_history_catalog=catalog,
        api_demo_root=ROOT / "demo",
        web_dist_dir=ROOT / "web/dist",
        llm_config=ROOT / "configs/llm.yaml",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker:
        resolved = settings(args.worker)
        database = ApiDatabase(
            resolved.resolved_api_queue_path, artifacts_dir=resolved.artifacts_dir
        )
        ApiWorker(settings=resolved, database=database).run_forever()
        return
    parent = ROOT / ".codex-pytest-tmp"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="browser-", dir=parent) as directory:
        scratch = Path(directory)
        resolved = settings(scratch)
        # Create the app/DB before starting the separate worker process.
        app = create_app(resolved)
        worker = subprocess.Popen([sys.executable, __file__, "--worker", str(scratch)], cwd=ROOT)
        try:
            for _ in range(600):
                if resolved.resolved_worker_health_path.exists():
                    break
                if worker.poll() is not None:
                    raise RuntimeError("Browser test worker exited before readiness")
                time.sleep(0.05)
            else:
                raise RuntimeError("Browser test worker did not become ready")
            uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)


if __name__ == "__main__":
    main()
