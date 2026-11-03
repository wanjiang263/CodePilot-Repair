"""Phase32 static checks: the same command set Phase31 used, scoped to this phase.

Why a script: every check has to be reproducible and its exit code has to end up
in machine-readable form. The file list is derived from git (this phase's commits
plus the working tree), so the scope cannot silently shrink.

Scope honesty: mypy is run on ``src`` and ``scripts`` only (the project has never
type-checked ``tests``), and the credential scan inspects added lines only.

Usage: ``.venv/Scripts/python.exe scripts/phase32_static_checks.py``
Output: ``.codex-pytest-tmp/phase32/static-checks.json`` and
``docs/notes/phase32-static-checks.json``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".codex-pytest-tmp" / "phase32"
OUT = SCRATCH / "static-checks.json"
DOCS_JSON = ROOT / "docs" / "notes" / "phase32-static-checks.json"
BASE = "0dbddfeef327410aac292ef0e7df7182c5222f02"

CREDENTIAL = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def run(args: list[str]) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def changed_python_files() -> list[str]:
    _, committed = run(["git", "diff", "--name-only", BASE, "HEAD"])
    _, pending = run(["git", "status", "--porcelain=v1"])
    paths: set[str] = set()
    for line in committed.splitlines():
        if line.strip():
            paths.add(line.strip())
    for line in pending.splitlines():
        if len(line) < 4:
            continue
        tail = line[3:].strip()
        if " -> " in tail:
            tail = tail.split(" -> ", 1)[1]
        paths.add(tail)
    return sorted(path for path in paths if path.endswith(".py") and (ROOT / path).is_file())


def ast_parse(files: list[str]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    for relative in files:
        try:
            ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        except SyntaxError as exc:
            errors.append({"file": relative, "error": f"{exc.__class__.__name__}: {exc}"})
    return {
        "name": "AST parse",
        "files": files,
        "file_count": len(files),
        "exit_code": 0 if not errors else 1,
        "errors": errors,
    }


def logged(name: str, command: list[str], log_name: str) -> dict[str, Any]:
    code, output = run(command)
    log = SCRATCH / log_name
    log.write_text(output, encoding="utf-8", newline="\n")
    return {
        "name": name,
        "command": command,
        "exit_code": code,
        "log": str(log.relative_to(ROOT)),
        "output": output.strip()[:4000],
    }


def credential_scan() -> dict[str, Any]:
    _, diff = run(["git", "diff", "--unified=0", BASE, "HEAD", "--", "*.py", "*.md", "*.json"])
    _, pending = run(["git", "diff", "--unified=0", "--", "*.py", "*.md", "*.json"])
    added = [
        line[1:]
        for line in (diff + "\n" + pending).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    hits = [
        {"line": line[:160], "match": matched.group(0)[:24]}
        for line in added
        for matched in [CREDENTIAL.search(line)]
        if matched
    ]
    return {
        "name": "credential scan (added lines only)",
        "added_lines": len(added),
        "exit_code": 0 if not hits else 1,
        "hits": hits,
    }


MYPY_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): error: (?P<msg>.+)$", re.M)


def mypy_messages(output: str) -> dict[str, list[str]]:
    table: dict[str, list[str]] = {}
    for match in MYPY_LINE.finditer(output):
        name = match.group("path").replace("\\", "/")
        table.setdefault(name.rsplit("/", 1)[-1], []).append(match.group("msg").strip())
    return table


def mypy_changed(files: list[str], venv: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Gate on new mypy errors only; pre-existing ones must stay byte-identical to BASE."""
    scope = [name for name in files if name.startswith(("src/", "scripts/"))]
    exe = str(venv / "mypy.exe")
    common = ["--config-file", "pyproject.toml", "--no-pretty", "--hide-error-context"]
    _, output = run([exe, *common, "--no-error-summary", *scope]) if scope else (0, "")
    current = mypy_messages(output)
    scratch = SCRATCH / "mypy-base"
    scratch.mkdir(parents=True, exist_ok=True)
    by_base = {Path(name).name: name for name in scope}
    report: dict[str, Any] = {}
    for basename in sorted(current):
        tracked = by_base.get(basename)
        copy = scratch / basename
        blob = (
            subprocess.run(
                ["git", "show", f"{BASE}:{tracked}"], cwd=ROOT, capture_output=True, check=False
            )
            if tracked
            else None
        )
        if blob is None or blob.returncode != 0 or not blob.stdout:
            report[basename] = {
                "current_errors": len(current[basename]),
                "baseline_errors": None,
                "new_errors": current[basename],
                "baseline_error": "cannot materialise the BASE blob; treated as new errors",
            }
            continue
        copy.write_bytes(blob.stdout)
        _, before_out = run([exe, *common, str(copy)])
        before = sorted(msg for group in mypy_messages(before_out).values() for msg in group)
        now = sorted(current[basename])
        report[basename] = {
            "current_errors": len(now),
            "baseline_errors": len(before),
            "new_errors": now if now != before else [],
        }
    gating = {
        "name": "mypy (changed src/scripts, new errors vs BASE)",
        "scope": scope,
        "exit_code": 0 if all(not item["new_errors"] for item in report.values()) else 1,
        "pre_existing_files": report,
    }
    full_code, full_output = run([exe, *common, "src", "scripts"])
    informational = {
        "name": "mypy (src + scripts, informational)",
        "informational": True,
        "note": (
            "the repository has never type-checked every script; untouched files carry "
            "pre-existing errors, so this whole-directory count is recorded, not gated"
        ),
        "exit_code": full_code,
        "output": full_output.strip()[-2000:],
    }
    return gating, informational


def eol_check(files: list[str]) -> dict[str, Any]:
    offenders = []
    for relative in files:
        data = (ROOT / relative).read_bytes()
        if b"\r\n" in data:
            offenders.append({"file": relative, "crlf_lines": data.count(b"\r\n")})
    return {
        "name": "EOL (LF only)",
        "files": files,
        "file_count": len(files),
        "exit_code": 0 if not offenders else 1,
        "offenders": offenders,
    }


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    files = changed_python_files()
    venv = ROOT / ".venv" / "Scripts"
    checks: list[dict[str, Any]] = [ast_parse(files)]
    checks.append(logged("ruff check", [str(venv / "ruff.exe"), "check", *files], "ruff-check.log"))
    checks.append(
        logged(
            "ruff format --check",
            [str(venv / "ruff.exe"), "format", "--check", *files],
            "ruff-format.log",
        )
    )
    gating, informational = mypy_changed(files, venv)
    checks.append(gating)
    checks.append(informational)
    checks.append(logged("git diff --check", ["git", "diff", "--check", BASE], "diff-check.log"))
    checks.append(
        logged(
            "git diff --check (CRLF-aware)",
            [
                "git",
                "-c",
                "core.whitespace=cr-at-eol",
                "diff",
                "--check",
                BASE,
            ],
            "diff-check-crlf.log",
        )
    )
    checks.append(eol_check(files))
    checks.append(credential_scan())
    payload = {
        "tool": "phase32-static-final",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_commit": BASE,
        "files": files,
        "file_count": len(files),
        "checks": checks,
        "failed": [
            check["name"]
            for check in checks
            if check["exit_code"] != 0 and not check.get("informational")
        ],
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    OUT.write_text(text, encoding="utf-8", newline="\n")
    DOCS_JSON.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps({"files": len(files), "failed": payload["failed"]}, indent=2))
    return 0 if not payload["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
