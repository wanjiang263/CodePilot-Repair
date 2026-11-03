"""Static checks: reproducible command set, machine-readable exit codes.

Phase33 changes (T07 / D6):
- parameterised: --base / --scratch / --docs-out / --tool, so a phase33 run
  never overwrites phase32 docs (defaults keep the phase32 behaviour).
- mypy errors are keyed by repo-relative FULL path; the old basename
  aggregation collided on same-named files (D6). Baseline blobs are mirrored
  into <scratch>/mypy-base/<relpath> to keep paths collision-free.
- mypy runs with --follow-imports=silent per phase33 spec (scope = changed
  src/scripts files only); the whole-tree mypy run stays informational debt.
- added: locale JSON parse + key-parity check (web/src/locales/*/index.json).
- added: web/src scope section — if base..HEAD + pending touch web/src, the
  frontend gates run (tsc/eslint/vitest/build); otherwise the empty
  git diff --name-only output is recorded and the section is a documented
  conditional skip, never a fake pass.
- untracked files are enumerated via git ls-files --others --exclude-standard
  so new files inside new directories cannot escape the file list.

Usage (phase33):
.venv/Scripts/python.exe scripts/phase32_static_checks.py
  --base b4825911b8e2880adfe6177b9aca6f4c35bf15b1
  --scratch .codex-pytest-tmp/phase33
  --docs-out docs/notes/phase33-static-checks.json
  --tool phase33-static-final
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PHASE32_BASE = "0dbddfeef327410aac292ef0e7df7182c5222f02"

CREDENTIAL = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


class Cfg:
    """Run configuration; defaults reproduce the phase32 invocation."""

    base: str = PHASE32_BASE
    scratch: Path = ROOT / ".codex-pytest-tmp" / "phase32"
    out: Path = ROOT / ".codex-pytest-tmp" / "phase32" / "static-checks.json"
    docs_out: Path = ROOT / "docs" / "notes" / "phase32-static-checks.json"
    tool: str = "phase32-static-final"


CFG = Cfg()


def run(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        args,
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def changed_python_files() -> list[str]:
    _, committed = run(["git", "diff", "--name-only", CFG.base, "HEAD"])
    _, pending = run(["git", "status", "--porcelain=v1"])
    _, untracked = run(["git", "ls-files", "--others", "--exclude-standard"])
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
    for line in untracked.splitlines():
        if line.strip():
            paths.add(line.strip())
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


def logged(name: str, command: list[str], log_name: str, cwd: Path | None = None) -> dict[str, Any]:
    code, output = run(command, cwd=cwd)
    log = CFG.scratch / log_name
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(output, encoding="utf-8", newline="\n")
    return {
        "name": name,
        "command": command,
        "exit_code": code,
        "log": str(log.relative_to(ROOT)).replace("\\", "/"),
        "output": output.strip()[:4000],
    }


def credential_scan() -> dict[str, Any]:
    _, diff = run(["git", "diff", "--unified=0", CFG.base, "HEAD", "--", "*.py", "*.md", "*.json"])
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
    """Key errors by the FULL reported path (normalised), never by basename (D6)."""
    table: dict[str, list[str]] = {}
    for match in MYPY_LINE.finditer(output):
        name = match.group("path").replace("\\", "/")
        table.setdefault(name, []).append(match.group("msg").strip())
    return table


def mypy_changed(files: list[str], venv: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Gate on new mypy errors only; pre-existing ones must match the BASE blob."""
    scope = [name for name in files if name.startswith(("src/", "scripts/"))]
    exe = str(venv / "mypy.exe")
    common = [
        "--config-file",
        "pyproject.toml",
        "--follow-imports=silent",
        "--no-pretty",
        "--hide-error-context",
    ]
    _, output = run([exe, *common, "--no-error-summary", *scope]) if scope else (0, "")
    current = mypy_messages(output)
    scratch = CFG.scratch / "mypy-base"
    scratch.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    for relpath in sorted(current):
        tracked = (
            relpath if relpath in scope else next((s for s in scope if relpath.endswith(s)), None)
        )
        copy = scratch / (tracked or relpath)
        blob = (
            subprocess.run(
                ["git", "show", f"{CFG.base}:{tracked}"], cwd=ROOT, capture_output=True, check=False
            )
            if tracked
            else None
        )
        if blob is None or blob.returncode != 0 or not blob.stdout:
            report[relpath] = {
                "current_errors": len(current[relpath]),
                "baseline_errors": None,
                "new_errors": current[relpath],
                "baseline_error": "cannot materialise the BASE blob (new or renamed file); treated as new errors",
            }
            continue
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(blob.stdout)
        _, before_out = run([exe, *common, str(copy.relative_to(ROOT)).replace("\\", "/")])
        before = sorted(msg for group in mypy_messages(before_out).values() for msg in group)
        now = sorted(current[relpath])
        report[relpath] = {
            "current_errors": len(now),
            "baseline_errors": len(before),
            "new_errors": now if now != before else [],
        }
    gating = {
        "name": "mypy (changed src/scripts, new errors vs BASE, full-path keys)",
        "scope": scope,
        "exit_code": 0 if all(not item["new_errors"] for item in report.values()) else 1,
        "pre_existing_files": report,
    }
    full_code, full_output = run([exe, *common, "src", "scripts"])
    informational = {
        "name": "mypy (src + scripts, informational debt, not gated)",
        "informational": True,
        "note": (
            "the repository has never type-checked every script; untouched files carry "
            "pre-existing errors, so this whole-directory count is recorded as debt, not gated"
        ),
        "exit_code": full_code,
        "output": full_output.strip()[-2000:],
    }
    return gating, informational


def flatten_keys(obj: Any, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            keys.add(path)
            keys |= flatten_keys(v, path)
    return keys


def locale_check() -> dict[str, Any]:
    """Parse every locale JSON and compare flattened key sets across locales."""
    loc_root = ROOT / "web" / "src" / "locales"
    entries = sorted(loc_root.glob("*/index.json")) if loc_root.is_dir() else []
    if not entries:
        return {
            "name": "locale JSON parse",
            "exit_code": 0,
            "files": [],
            "note": "no locale files found",
        }
    parsed: dict[str, set[str]] = {}
    errors: list[dict[str, str]] = []
    for path in entries:
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        try:
            parsed[rel] = flatten_keys(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors.append({"file": rel, "error": str(exc)})
    keysets = list(parsed.values())
    parity: dict[str, Any] = {"equal": all(s == keysets[0] for s in keysets) if keysets else True}
    if keysets and not parity["equal"]:
        union = set().union(*keysets)
        parity["missing"] = {
            rel: sorted(union - keys)[:50] for rel, keys in parsed.items() if union - keys
        }
    return {
        "name": "locale JSON parse",
        "files": [str(p.relative_to(ROOT)).replace("\\", "/") for p in entries],
        "key_counts": {rel: len(keys) for rel, keys in parsed.items()},
        "key_parity": parity,
        "exit_code": 0 if not errors else 1,
        "errors": errors,
    }


def _pending_paths() -> list[str]:
    _, pending = run(["git", "status", "--porcelain=v1"])
    _, untracked = run(["git", "ls-files", "--others", "--exclude-standard"])
    out: list[str] = []
    for line in pending.splitlines():
        if len(line) >= 4:
            tail = line[3:].strip()
            out.append(tail.split(" -> ", 1)[1] if " -> " in tail else tail)
    out.extend(x.strip() for x in untracked.splitlines() if x.strip())
    return out


def web_scope_section(files: list[str]) -> dict[str, Any]:
    _, diff_out = run(["git", "diff", "--name-only", CFG.base, "HEAD", "--", "web/src"])
    changed = sorted(
        {
            *(x.strip() for x in diff_out.splitlines() if x.strip()),
            *(x for x in files if x.startswith("web/src/")),
            *(x for x in _pending_paths() if x.startswith("web/src/")),
        }
    )
    cmd = f"git diff --name-only {CFG.base[:7]}..HEAD -- web/src"
    if not changed:
        return {
            "name": "web/src scope",
            "command": cmd,
            "output": diff_out,
            "changed_files": 0,
            "exit_code": 0,
            "result": (
                "web/src has zero diff in this phase; frontend gates and page E2E "
                "conditionally skipped (documented, not faked)"
            ),
        }
    web = ROOT / "web"
    results = [
        logged("web: tsc --noEmit", ["npx", "tsc", "--noEmit"], "web-tsc.log", cwd=web),
        logged(
            "web: eslint", ["npx", "eslint", "src", "--max-warnings=0"], "web-eslint.log", cwd=web
        ),
        logged("web: vitest", ["npx", "vitest", "run"], "web-vitest.log", cwd=web),
        logged("web: build", ["npm", "run", "build"], "web-build.log", cwd=web),
    ]
    return {
        "name": "web/src scope",
        "command": cmd,
        "changed_files": len(changed),
        "files": changed,
        "gates": results,
        "exit_code": 0 if all(r["exit_code"] == 0 for r in results) else 1,
    }


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
    parser = argparse.ArgumentParser(description="phase static checks")
    parser.add_argument("--base", default=PHASE32_BASE)
    parser.add_argument("--scratch", default=str(ROOT / ".codex-pytest-tmp" / "phase32"))
    parser.add_argument(
        "--docs-out", default=str(ROOT / "docs" / "notes" / "phase32-static-checks.json")
    )
    parser.add_argument("--tool", default="phase32-static-final")
    args = parser.parse_args()

    CFG.base = args.base
    CFG.scratch = Path(args.scratch)
    if not CFG.scratch.is_absolute():
        CFG.scratch = ROOT / CFG.scratch
    CFG.out = CFG.scratch / "static-checks.json"
    CFG.docs_out = Path(args.docs_out)
    if not CFG.docs_out.is_absolute():
        CFG.docs_out = ROOT / CFG.docs_out
    CFG.tool = args.tool
    code, _ = run(["git", "rev-parse", "--verify", CFG.base])
    if code != 0:
        print(json.dumps({"error": f"unknown base commit {CFG.base}"}))
        return 2

    CFG.scratch.mkdir(parents=True, exist_ok=True)
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
    checks.append(locale_check())
    checks.append(
        logged("git diff --check", ["git", "diff", "--check", CFG.base], "diff-check.log")
    )
    crlf_aware = logged(
        "git diff --check (CRLF-aware)",
        ["git", "-c", "core.whitespace=cr-at-eol", "diff", "--check", CFG.base],
        "diff-check-crlf.log",
    )
    if checks[-1]["exit_code"] != 0 and crlf_aware["exit_code"] == 0:
        crlf_aware["note"] = (
            "plain git diff --check flags CRLF-only whitespace; the CRLF-aware rerun passes, "
            "so the plain-check failure is a documented false positive"
        )
    checks.append(crlf_aware)
    checks.append(eol_check(files))
    checks.append(credential_scan())
    checks.append(web_scope_section(files))
    payload = {
        "tool": CFG.tool,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "base_commit": CFG.base,
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
    CFG.out.write_text(text, encoding="utf-8", newline="\n")
    CFG.docs_out.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps({"files": len(files), "failed": payload["failed"]}, indent=2))
    return 0 if not payload["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
