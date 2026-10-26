"""One-shot foreground supervisor retained by the caller's execution session."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/phase22"
SUFFIX = "-r3" if "--segment3" in sys.argv else "-r2" if "--segment2" in sys.argv else ""
RUN_SUFFIX = SUFFIX + (
    "-indexed" if "--indexed" in sys.argv else "-restart" if "--restart" in sys.argv else ""
)


def record(payload: dict) -> None:
    stage = OUT / f"supervisor{RUN_SUFFIX}.tmp"
    stage.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    stage.replace(OUT / f"supervisor{RUN_SUFFIX}.json")


def main() -> int:
    with (OUT / f"launch{RUN_SUFFIX}.json").open("x", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "supervisor_pid": os.getpid(),
                    "retry": False,
                    "lifetime": "caller_retained_execution_session",
                }
            )
        )
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "DOCKER_HOST": "tcp://172.26.124.107:23750",
        "NO_PROXY": "172.26.124.107,localhost,127.0.0.1",
        "no_proxy": "172.26.124.107,localhost,127.0.0.1",
    }
    # Explicit frozen YAML owns model route/timeouts; inherited GLM overrides
    # must never silently change this paid segment.
    for key in (
        "GLM_PLATFORM",
        "GLM_BASE_URL",
        "GLM_MODEL",
        "GLM_TIMEOUT_SECONDS",
        "GLM_MAX_RETRIES",
        "CODEPILOT_INDEX_SNAPSHOT",
    ):
        env.pop(key, None)
    command = [
        sys.executable,
        str(ROOT / "scripts/m3_batch.py"),
        "--catalog",
        f"configs/phase22/catalog{SUFFIX}.json",
        "--config",
        f"configs/phase22/config{SUFFIX}.json",
        "--artifact-root",
        f"artifacts/phase22/live{RUN_SUFFIX}",
        "--live",
    ]
    with (OUT / f"live{RUN_SUFFIX}.log").open("xb") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        record(
            {
                "supervisor_pid": os.getpid(),
                "batch_pid": process.pid,
                "status": "running",
                "exit_code": None,
                "log": f"artifacts/phase22/live{RUN_SUFFIX}.log",
            }
        )
        code = process.wait()
        record(
            {
                "supervisor_pid": os.getpid(),
                "batch_pid": process.pid,
                "status": "exited",
                "exit_code": code,
                "log": f"artifacts/phase22/live{RUN_SUFFIX}.log",
            }
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
