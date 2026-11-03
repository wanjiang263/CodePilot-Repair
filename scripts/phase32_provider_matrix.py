"""Phase32 T07: provider matrix, zero cost by construction.

Why this shape: the Phase32 budget gate (PHASE32_REAL_BUDGET_CNY) is unset in this
session, so no real request may be issued. Instead of claiming "verified", this
script records exactly two things:

1. what the fake/loopback suites prove (streaming, usage accounting, delayed
   headers, slow bodies, total deadline, cancellation isolation, no hidden retry),
2. that the real-provider rows are blocked, with the missing environment items
   named (budget variable, credentials), and 0 requests / 0 cost.

Attribution rule: each protocol row lists the test modules that cover it. Rows
overlap on purpose (the deadline suite covers every HTTP protocol), so the row
counts must never be summed into a "total tests" claim; the junit totals are the
only authoritative number.

Usage: .venv/Scripts/python.exe scripts/phase32_provider_matrix.py
Output: .codex-pytest-tmp/phase32/provider-matrix.json and
docs/notes/phase32-provider-matrix.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".codex-pytest-tmp" / "phase32"
OUT = SCRATCH / "provider-matrix.json"
DOCS_JSON = ROOT / "docs" / "notes" / "phase32-provider-matrix.json"
JUNIT = SCRATCH / "provider-matrix.xml"
LOG = SCRATCH / "provider-matrix.log"
BUDGET_ENV = "PHASE32_REAL_BUDGET_CNY"
RG_CANDIDATES = (
    Path.home() / "AppData/Local/OpenAI/Codex/bin/rg.exe",
    Path.home() / "scoop/shims/rg.exe",
    Path("C:/ProgramData/chocolatey/bin/rg.exe"),
    Path("C:/Program Files/ripgrep/rg.exe"),
)
MODULES = [
    "tests/unit/llm/test_openai_compatible.py",
    "tests/unit/llm/test_phase14_protocols.py",
    "tests/unit/llm/test_phase28_http_matrix.py",
    "tests/unit/llm/test_phase29_http_matrix.py",
    "tests/unit/llm/test_http_total_deadline.py",
    "tests/unit/llm/test_tracking_chain_accounting.py",
    "tests/unit/llm/test_dispatch_ledger_reconciliation.py",
    "tests/unit/llm/test_cost.py",
    "tests/unit/llm/test_retry.py",
]
ROWS = [
    {
        "protocol": "openai_compatible",
        "mode": "fake-http-loopback",
        "modules": [
            "tests/unit/llm/test_openai_compatible.py",
            "tests/unit/llm/test_phase28_http_matrix.py",
            "tests/unit/llm/test_phase29_http_matrix.py",
            "tests/unit/llm/test_http_total_deadline.py",
        ],
        "covers": [
            "streaming chunks",
            "usage accounting on the wire",
            "delayed response headers",
            "slow response body inside the total deadline",
            "no hidden retry (one wire request)",
        ],
    },
    {
        "protocol": "anthropic",
        "mode": "fake-http-loopback",
        "modules": [
            "tests/unit/llm/test_phase14_protocols.py",
            "tests/unit/llm/test_http_total_deadline.py",
        ],
        "covers": ["message protocol mapping", "cancellation", "deadline behaviour"],
    },
    {
        "protocol": "bedrock",
        "mode": "fake-client",
        "modules": ["tests/unit/llm/test_phase28_http_matrix.py"],
        "covers": ["bedrock converse request shaping over a stubbed boto3 client"],
    },
    {
        "protocol": "cancellation",
        "mode": "fake-http-loopback",
        "modules": ["tests/unit/llm/test_http_total_deadline.py"],
        "covers": [
            "one cancel does not disturb another request",
            "no background request after cancel",
            "shared client survives an in-flight cancel",
        ],
    },
    {
        "protocol": "usage-and-cost",
        "mode": "fake-http-loopback",
        "modules": [
            "tests/unit/llm/test_tracking_chain_accounting.py",
            "tests/unit/llm/test_dispatch_ledger_reconciliation.py",
            "tests/unit/llm/test_cost.py",
            "tests/unit/llm/test_retry.py",
        ],
        "covers": [
            "usage reconciliation",
            "unknown vs zero cost attribution",
            "authorisation/frozen ledger rows are not overwritten",
        ],
    },
]


def locate_rg() -> str:
    found = shutil.which("rg")
    if found:
        return found
    for candidate in RG_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    raise SystemExit("ripgrep not found; the retrieval suite needs it on PATH")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = str(Path(locate_rg()).parent) + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def budget() -> dict[str, Any]:
    raw = os.environ.get(BUDGET_ENV)
    value: float | None = None
    if raw not in (None, ""):
        try:
            value = float(raw)
        except ValueError:
            value = None
    return {
        "env": BUDGET_ENV,
        "raw_present": raw not in (None, ""),
        "budget_cny": value,
        "real_requests_allowed": value is not None and value > 0,
    }


def credentials_present() -> dict[str, bool]:
    names = [
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ]
    presence = {name: bool(os.environ.get(name)) for name in names}
    for module in ("boto3", "botocore"):
        try:
            __import__(module)
        except ImportError:
            presence[module + "_installed"] = False
        else:
            presence[module + "_installed"] = True
    return presence


def run_pytest() -> dict[str, Any]:
    for path in (JUNIT, LOG):
        path.unlink(missing_ok=True)
    command = [sys.executable, "-m", "pytest", *MODULES, "-q", "--junitxml=" + str(JUNIT)]
    done = subprocess.run(
        command,
        cwd=ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    LOG.write_text((done.stdout or "") + (done.stderr or ""), encoding="utf-8", newline="\n")
    tail = [line for line in (done.stdout or "").splitlines() if line.strip()][-1:]
    return {
        "command": " ".join(command),
        "exit_code": done.returncode,
        "log": str(LOG.relative_to(ROOT)),
        "junit": str(JUNIT.relative_to(ROOT)) if JUNIT.is_file() else None,
        "summary": tail,
    }


def junit_cases() -> list[dict[str, str]]:
    if not JUNIT.is_file():
        return []
    tree = ET.parse(JUNIT)
    cases: list[dict[str, str]] = []
    for case in tree.iter("testcase"):
        outcome = "passed"
        for child in case:
            if child.tag in {"failure", "error", "skipped"}:
                outcome = child.tag
                break
        cases.append(
            {
                "classname": case.get("classname", ""),
                "name": case.get("name", ""),
                "outcome": outcome,
                "time": case.get("time", ""),
            }
        )
    return cases


def attribute(cases: list[dict[str, str]], modules: list[str]) -> dict[str, Any]:
    prefixes = tuple(module.removesuffix(".py").replace("/", ".") for module in modules)
    chosen = [
        case for case in cases if any(case["classname"].startswith(prefix) for prefix in prefixes)
    ]
    counts: dict[str, int] = {"passed": 0, "failure": 0, "error": 0, "skipped": 0}
    for case in chosen:
        counts[case["outcome"]] += 1
    return {"module_tests": counts, "node_count": len(chosen)}


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    plan = budget()
    creds = credentials_present()
    result = run_pytest()
    cases = junit_cases()
    rows = []
    for row in ROWS:
        entry: dict[str, Any] = {
            "protocol": row["protocol"],
            "mode": row["mode"],
            "modules": row["modules"],
            "covers": row["covers"],
            "result": attribute(cases, list(row["modules"])),
            "status": "fake-verified",
        }
        if row["protocol"] == "bedrock" and not creds.get("botocore_installed"):
            entry["status"] = "fake-verified (botocore missing: client is stubbed)"
        rows.append(entry)
    real_rows = [
        "openai-compatible real endpoint",
        "anthropic real endpoint",
        "bedrock real endpoint (boto3 + credentials)",
    ]
    missing_real = []
    if not plan["real_requests_allowed"]:
        missing_real.append(BUDGET_ENV + " > 0")
    if not any(
        creds[name] for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID")
    ):
        missing_real.append("provider credentials in the environment")
    blocked = {
        "budget": plan,
        "credentials_present": creds,
        "rows": [{"row": name, "status": "blocked", "missing": missing_real} for name in real_rows],
        "real_requests": 0,
        "real_cost_cny": 0.0,
        "note": (
            "No real request was issued in this phase. Zero-cost fake coverage is "
            "not a substitute for a real-provider row and is not reported as one."
        ),
    }
    totals = {
        "tests": len(cases),
        "failures": sum(1 for case in cases if case["outcome"] == "failure"),
        "errors": sum(1 for case in cases if case["outcome"] == "error"),
        "skipped": sum(1 for case in cases if case["outcome"] == "skipped"),
    }
    payload = {
        "tool": "phase32-provider-matrix",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pytest": result,
        "totals_from_junit": totals,
        "fake_rows": rows,
        "real_provider": blocked,
        "overlap_note": "fake_rows overlap; never sum them into a test total",
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    OUT.write_text(text, encoding="utf-8", newline="\n")
    DOCS_JSON.write_text(text, encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "exit_code": result["exit_code"],
                "totals": totals,
                "real_requests": blocked["real_requests"],
                "budget_allowed": plan["real_requests_allowed"],
            },
            indent=2,
        )
    )
    return 0 if result["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
