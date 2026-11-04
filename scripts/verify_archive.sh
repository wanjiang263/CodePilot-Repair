#!/usr/bin/env sh
# Reproducible archive verifier entrypoint.
#
# The shared .venv is often a junction/symlink to the main checkout's venv, and
# its editable install pins `codepilot` to the main checkout's src. Force this
# checkout's src first so a worktree run sees the code being verified.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${PYTHON:-}" ]; then
    py=$PYTHON
elif [ -x "$root/.venv/Scripts/python.exe" ]; then
    py="$root/.venv/Scripts/python.exe"
elif [ -x "$root/.venv/bin/python" ]; then
    py="$root/.venv/bin/python"
else
    py=python
fi

PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec "$py" "$root/scripts/phase28_archive_verify.py" "$@"
