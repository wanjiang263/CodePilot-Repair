"""One-shot detached supervisor. No retry or automatic recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/phase22"
SUFFIX = "-r2" if "--segment2" in sys.argv else ""


def record(payload: dict) -> None:
    stage = OUT / f"supervisor{SUFFIX}.tmp"
    stage.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    stage.replace(OUT / f"supervisor{SUFFIX}.json")


def main() -> int:
    if "--worker" not in sys.argv:
        with (OUT / f"launch{SUFFIX}.json").open("x", encoding="utf-8") as handle:
            handle.write(json.dumps({"launcher_pid": os.getpid(), "retry": False}))
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                *(["--segment2"] if SUFFIX else []),
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        print(json.dumps({"supervisor_pid": process.pid}))
        return 0
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
        f"artifacts/phase22/live{SUFFIX}",
        "--live",
    ]
    with (OUT / f"live{SUFFIX}.log").open("xb") as log:
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
                "log": "artifacts/phase22/live.log",
            }
        )
        code = process.wait()
        record(
            {
                "supervisor_pid": os.getpid(),
                "batch_pid": process.pid,
                "status": "exited",
                "exit_code": code,
                "log": "artifacts/phase22/live.log",
            }
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
