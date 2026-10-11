"""Capture exact acceptance CLI stdout/stderr separately, without masking exits."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

directory = Path(__file__).resolve().parent
label, *command = sys.argv[1:]
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
process = subprocess.run(
    command,
    capture_output=True,
    env={**os.environ, "PYTHONIOENCODING": "utf-8", "UV_OFFLINE": "1", "UV_NO_SYNC": "1"},
)
for stream, data in (("stdout", process.stdout), ("stderr", process.stderr)):
    (directory / f"{label}.{stream}").write_bytes(data)
(directory / f"{label}.json").write_text(
    json.dumps(
        {
            "command": command,
            "exit_code": process.returncode,
            "environment": {"UV_OFFLINE": "1", "UV_NO_SYNC": "1", "PYTHONIOENCODING": "utf-8"},
            "stdout_sha256": hashlib.sha256(process.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(process.stderr).hexdigest(),
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
print(process.stdout.decode("utf-8", errors="replace"), end="")
if process.returncode:
    print(process.stderr.decode("utf-8", errors="replace"), file=sys.stderr, end="")
sys.exit(process.returncode)
