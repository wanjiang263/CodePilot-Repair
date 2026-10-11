"""Record an actual local command, its output and exit code without masking failure."""

import json
import os
import subprocess
import sys
from pathlib import Path

directory = Path(__file__).resolve().parent
label, *command = sys.argv[1:]
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
environment = dict(os.environ, PYTHONIOENCODING="utf-8")
with (directory / f"{label}.log").open("w", encoding="utf-8") as log:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log.write(line)
        log.flush()
        print(line, end="", flush=True)
    code = process.wait()
(directory / f"{label}.json").write_text(
    json.dumps({"command": command, "exit_code": code}, indent=2) + "\n", encoding="utf-8"
)
sys.exit(code)
