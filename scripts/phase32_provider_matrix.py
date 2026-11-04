"""Phase32 T07 / Phase33 T01: provider matrix, zero cost by construction.

Why this shape: the Phase32 budget gate (PHASE32_REAL_BUDGET_CNY) is unset in this
session, so no real request may be issued. Instead of claiming "verified", this
script records exactly two things:

1. what the fake/loopback suites prove (streaming, usage accounting, delayed
   headers, slow bodies, total deadline, cancellation isolation, no hidden retry),
2. the state of the real-provider rows: blocked by default, with the missing
   environment items named per row, 0 requests / 0 cost.

Phase33 adds a real execution path behind an explicit flag:

    scripts/phase32_provider_matrix.py --real

--real issues AT MOST ONE request per provider (three total) and only when
PHASE32_REAL_BUDGET_CNY > 0 (capped at 20 CNY) AND that provider's credentials
and model id are present in the environment. Hidden retries are disabled on
purpose (RetryPolicy(max_attempts=1); boto3 retries max_attempts=1) so
real_requests always equals the number of dispatched calls. A real row is only
one of three statuses:

- real-verified: the provider returned a complete, acknowledged usage record;
- real-failed: a request was dispatched and failed (status code / exception kept);
- blocked: budget, credentials, region or model id missing — nothing dispatched.

Costs come from the existing usage ledger (codepilot.schemas.usage.Usage via
UsageTrackingProvider) and the price table in configs/llm.yaml. When no complete
CNY-convertible price is configured for the probed model, the row reports
cost_known=false instead of inventing a number.

Attribution rule: each protocol row lists the test modules that cover it. Rows
overlap on purpose (the deadline suite covers every HTTP protocol), so the row
counts must never be summed into a "total tests" claim; the junit totals are the
only authoritative number.

Usage: .venv/Scripts/python.exe scripts/phase32_provider_matrix.py [--real]
       [--out PATH] [--scratch DIR] [--llm-config PATH]
       [--real-model-openai ID] [--real-model-anthropic ID] [--real-model-bedrock ID]

Default output: .codex-pytest-tmp/phase32/provider-matrix.json and
docs/notes/phase32-provider-matrix.json. With --out the payload is written ONLY
to that path; with --scratch the junit/log/json artifacts move to that directory
and the docs copy is left untouched.

Environment for the real channel (each provider checked independently):
- openai-compatible: OPENAI_API_KEY + OPENAI_BASE_URL + PHASE32_REAL_MODEL_OPENAI
- anthropic: ANTHROPIC_API_KEY + PHASE32_REAL_MODEL_ANTHROPIC
  (optional ANTHROPIC_BASE_URL, default https://api.anthropic.com)
- bedrock: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY + importable boto3/botocore
  + AWS_REGION (or AWS_DEFAULT_REGION) + PHASE32_REAL_MODEL_BEDROCK
"""

from __future__ import annotations

import argparse
import contextlib
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
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

SCRATCH = ROOT / ".codex-pytest-tmp" / "phase32"
OUT = SCRATCH / "provider-matrix.json"
DOCS_JSON = ROOT / "docs" / "notes" / "phase32-provider-matrix.json"
JUNIT = SCRATCH / "provider-matrix.xml"
LOG = SCRATCH / "provider-matrix.log"
BUDGET_ENV = "PHASE32_REAL_BUDGET_CNY"
#: Phase33 T01: hard ceiling for the real channel. The effective budget is
#: min(PHASE32_REAL_BUDGET_CNY, BUDGET_CAP_CNY); no second budget variable exists.
BUDGET_CAP_CNY = 20.0
DEFAULT_LLM_CONFIG = ROOT / "configs" / "llm.yaml"
RG_CANDIDATES = (
    Path.home() / "AppData/Local/OpenAI/Codex/bin/rg.exe",
    Path.home() / "scoop/shims/rg.exe",
    Path("C:/ProgramData/chocolatey/bin/rg.exe"),
    Path("C:/Program Files/ripgrep/rg.exe"),
)

#: The probe reuses the smoke question: tiny output, unambiguous completion.
PROBE_TEXT = "只回复两个字：收到"
PROBE_MAX_OUTPUT_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 60.0
PROBE_PURPOSE = "phase33-provider-matrix-real"

REAL_PROVIDER_ORDER = ("openai-compatible", "anthropic", "bedrock")
REAL_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai-compatible": {
        "row": "openai-compatible real endpoint",
        "protocol": "openai_compatible",
        "required_env": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
        "model_env": "PHASE32_REAL_MODEL_OPENAI",
    },
    "anthropic": {
        "row": "anthropic real endpoint",
        "protocol": "anthropic_messages",
        "required_env": ("ANTHROPIC_API_KEY",),
        "model_env": "PHASE32_REAL_MODEL_ANTHROPIC",
    },
    "bedrock": {
        "row": "bedrock real endpoint (boto3 + credentials)",
        "protocol": "bedrock_converse",
        "required_env": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
        "required_modules": ("boto3", "botocore"),
        "region_env": ("AWS_REGION", "AWS_DEFAULT_REGION"),
        "model_env": "PHASE32_REAL_MODEL_BEDROCK",
    },
}

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
    env["PYTHONUTF8"] = "1"
    return env


def budget() -> dict[str, Any]:
    raw = os.environ.get(BUDGET_ENV)
    value: float | None = None
    if raw not in (None, ""):
        try:
            value = float(raw)
        except ValueError:
            value = None
    allowed = value is not None and value > 0
    effective = min(value, BUDGET_CAP_CNY) if allowed and value is not None else None
    return {
        "env": BUDGET_ENV,
        "raw_present": raw not in (None, ""),
        "budget_cny": value,
        "cap_cny": BUDGET_CAP_CNY,
        "effective_budget_cny": effective,
        "capped": bool(allowed and value is not None and value > BUDGET_CAP_CNY),
        "real_requests_allowed": allowed,
    }


def credentials_present() -> dict[str, bool]:
    names = [
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
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


def run_pytest(junit: Path, log: Path, basetemp: Path) -> dict[str, Any]:
    for path in (junit, log):
        path.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *MODULES,
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp=" + str(basetemp),
        "--junitxml=" + str(junit),
    ]
    done = subprocess.run(
        command,
        cwd=ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    log.write_text((done.stdout or "") + (done.stderr or ""), encoding="utf-8", newline="\n")
    tail = [line for line in (done.stdout or "").splitlines() if line.strip()][-1:]

    def _rel(path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    return {
        "command": " ".join(command),
        "exit_code": done.returncode,
        "log": _rel(log),
        "junit": _rel(junit) if junit.is_file() else None,
        "summary": tail,
    }


def junit_cases(junit: Path) -> list[dict[str, str]]:
    if not junit.is_file():
        return []
    tree = ET.parse(junit)
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


# ---------------------------------------------------------------------------
# Real channel (Phase33 T01)
# ---------------------------------------------------------------------------


def resolve_model_id(key: str, overrides: dict[str, str | None] | None) -> str | None:
    override = (overrides or {}).get(key)
    raw = override or os.environ.get(REAL_PROVIDERS[key]["model_env"]) or ""
    raw = raw.strip()
    return raw or None


def row_missing(
    key: str,
    plan: dict[str, Any],
    model_id: str | None,
    *,
    spent_cny: float | None = None,
    creds: dict[str, bool] | None = None,
) -> list[str]:
    """Per-row missing list: every provider row names its OWN gaps.

    Never share one list between rows — that was the D1 defect: a single
    missing_real list was copied onto all three rows. When ``creds`` is given
    (the ``credentials_present()`` map) the gate reads that map instead of
    re-probing the environment, so presence is computed exactly once.
    """
    meta = REAL_PROVIDERS[key]
    missing: list[str] = []
    effective = plan.get("effective_budget_cny")
    if not plan["real_requests_allowed"]:
        missing.append(BUDGET_ENV + " > 0")
    elif spent_cny is not None and effective is not None and spent_cny >= effective:
        missing.append(
            f"{BUDGET_ENV} exhausted: spent {spent_cny:.6f} CNY >= "
            f"effective cap {effective:.6f} CNY"
        )
    for name in meta["required_env"]:
        present = bool(creds.get(name)) if creds is not None else bool(os.environ.get(name))
        if not present:
            missing.append(name)
    for module in meta.get("required_modules", ()):
        if creds is not None:
            installed = bool(creds.get(module + "_installed"))
        else:
            try:
                __import__(module)
            except ImportError:
                installed = False
            else:
                installed = True
        if not installed:
            missing.append(module + " importable")
    region_env = meta.get("region_env")
    if region_env and not (
        any(creds.get(name) for name in region_env)
        if creds is not None
        else any(os.environ.get(name) for name in region_env)
    ):
        missing.append(" or ".join(region_env))
    if not model_id:
        missing.append(meta["model_env"])
    return missing


def _load_llm_config(path: Path) -> tuple[Any, str | None]:
    """Best-effort config load: prices come from the existing table, never invented."""
    try:
        from codepilot.llm.config import load_llm_config

        return load_llm_config(path), None
    except Exception as exc:  # a broken config must not crash the matrix
        return None, (
            f"llm config {path} unavailable ({type(exc).__name__}: {str(exc)[:200]}); "
            "prices treated as unknown"
        )


def usable_price(cfg: Any, model_id: str) -> tuple[Any, str | None]:
    """Reuse the configured price for this exact model id, or stay unknown.

    A price is only usable when it is complete AND convertible to the budget
    currency without guessing: cost.convert() silently falls back to rate 1.0
    for missing exchange rates, which would under-count a USD price as CNY.
    """
    if cfg is None:
        return None, None
    spec = next(
        (m for m in cfg.models.values() if m.model == model_id or m.alias == model_id),
        None,
    )
    if spec is None or spec.price is None:
        return None, f"no price configured for model {model_id!r}; cost stays unknown"
    price = spec.price
    if not price.is_complete:
        return None, f"price for model {model_id!r} is incomplete; cost stays unknown"
    if price.currency != cfg.budget_currency and price.currency not in cfg.exchange_rates:
        return None, (
            f"price for model {model_id!r} is in {price.currency} with no exchange "
            f"rate to {cfg.budget_currency}; cost stays unknown"
        )
    return price, None


class CountingBedrockClient:
    """Wraps a bedrock-runtime client and counts converse dispatches.

    BedrockConverseProvider has no attempts counter, and real_requests must
    equal the number of actually dispatched calls — so the count lives here.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.converse_calls = 0

    def converse(self, **kwargs: Any) -> Any:
        self.converse_calls += 1
        return self._inner.converse(**kwargs)

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _default_bedrock_client(pspec: Any) -> Any:
    from codepilot.llm.bedrock_converse import _build_client

    return _build_client(pspec)


def _dispatched(inner: Any, counter: CountingBedrockClient | None) -> int:
    if counter is not None:
        return counter.converse_calls
    return int(getattr(inner, "attempts", 0) or 0)


def probe_provider(
    key: str,
    model_id: str,
    *,
    cfg: Any,
    cfg_note: str | None,
    usage: Any,
    http_client_factory: Any = None,
    bedrock_client_factory: Any = None,
) -> dict[str, Any]:
    """Dispatch exactly ONE probe request for one provider and report honestly.

    No hidden retries: RetryPolicy(max_attempts=1) for the HTTP protocols and
    ProviderSpec(max_retries=0) so boto3 configures retries max_attempts=1.
    """
    import httpx

    from codepilot.llm.anthropic_messages import AnthropicMessagesProvider
    from codepilot.llm.bedrock_converse import BedrockConverseProvider
    from codepilot.llm.config import ModelSpec, ProviderSpec
    from codepilot.llm.openai_compatible import OpenAICompatibleProvider
    from codepilot.llm.retry import RetryPolicy
    from codepilot.llm.tracking import UsageTrackingProvider
    from codepilot.llm.types import LLMRequest, Message
    from codepilot.net import build_http_client
    from codepilot.observability.redaction import redact_text

    meta = REAL_PROVIDERS[key]
    price, price_note = usable_price(cfg, model_id) if cfg is not None else (None, cfg_note)
    row: dict[str, Any] = {
        "row": meta["row"],
        "provider": key,
        "model": model_id,
        "missing": [],
    }
    if price_note:
        row["cost_note"] = price_note

    common: dict[str, Any] = {
        "name": key,
        "protocol": meta["protocol"],
        "max_retries": 0,  # boto3 standard-mode retries collapse to 1 attempt
        "timeout_seconds": PROBE_TIMEOUT_SECONDS,
        "max_concurrency": 1,
    }
    if key == "bedrock":
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
        pspec = ProviderSpec(base_url="", region=region, **common)
    elif key == "anthropic":
        base_url = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        pspec = ProviderSpec(base_url=base_url, **common)
    else:
        pspec = ProviderSpec(base_url=os.environ["OPENAI_BASE_URL"].rstrip("/"), **common)
    mspec = ModelSpec(
        alias="phase33-real-" + key,
        provider=key,
        model=model_id,
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        price=price,
    )
    policy = RetryPolicy(max_attempts=1)
    rates = dict(cfg.exchange_rates) if cfg is not None else {}
    currency = cfg.budget_currency if cfg is not None else "CNY"
    counter: CountingBedrockClient | None = None
    try:
        if meta["protocol"] == "bedrock_converse":
            client = (
                bedrock_client_factory(pspec)
                if bedrock_client_factory is not None
                else _default_bedrock_client(pspec)
            )
            counter = CountingBedrockClient(client)
            inner: Any = BedrockConverseProvider(
                spec=mspec,
                provider=pspec,
                client=counter,
                exchange_rates=rates,
                budget_currency=currency,
            )
        else:
            api_key = os.environ["ANTHROPIC_API_KEY" if key == "anthropic" else "OPENAI_API_KEY"]
            if http_client_factory is not None:
                client = http_client_factory(pspec, api_key)
            else:
                client = build_http_client(
                    base_url=pspec.base_url,
                    timeout=httpx.Timeout(
                        pspec.timeout_seconds, connect=min(10.0, pspec.timeout_seconds)
                    ),
                    trust_env=pspec.trust_env,
                )
            provider_cls = (
                AnthropicMessagesProvider if key == "anthropic" else OpenAICompatibleProvider
            )
            inner = provider_cls(
                spec=mspec,
                provider=pspec,
                api_key=api_key,
                client=client,
                policy=policy,
                exchange_rates=rates,
                budget_currency=currency,
            )
    except Exception as exc:  # construction failure dispatches nothing
        row.update(
            status="blocked",
            missing=[
                f"provider construction failed: {type(exc).__name__}: "
                + redact_text(str(exc))[:300]
            ],
            real_requests=0,
        )
        return row

    tracked = UsageTrackingProvider(
        inner,
        usage=usage,
        node="phase33-matrix-" + key,
        role="phase33_provider_matrix",
        provider=key,
    )
    request = LLMRequest(
        messages=[Message.user(PROBE_TEXT)],
        max_output_tokens=PROBE_MAX_OUTPUT_TOKENS,
        purpose=PROBE_PURPOSE,
    )
    try:
        response = tracked.complete(request)
    except Exception as exc:  # every dispatched failure is reported, not hidden
        # KeyboardInterrupt/SystemExit are control flow, not provider results:
        # they must propagate instead of being turned into a "real-failed" row.
        row.update(
            status="real-failed",
            real_requests=_dispatched(inner, counter),
            error={
                "type": type(exc).__name__,
                "status": getattr(exc, "status", None),
                "uncertain": bool(getattr(exc, "uncertain", False)),
                "message": redact_text(str(exc))[:500],
            },
            cost_cny=0.0,
            cost_known=False,
        )
        return row
    finally:
        # close must never mask the result
        with contextlib.suppress(Exception):
            inner.close()

    unacknowledged = int(getattr(inner, "unacknowledged", 0) or 0)
    usage_complete = response.input_tokens > 0 and response.output_tokens > 0
    if usage_complete and unacknowledged == 0:
        status = "real-verified"
    else:
        status = "real-failed"
        row["failure_reason"] = (
            f"unacknowledged transport errors: {unacknowledged}"
            if unacknowledged
            else "response did not include complete usage (input_tokens and output_tokens must be > 0)"
        )
    row.update(
        status=status,
        real_requests=_dispatched(inner, counter),
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cached_input_tokens=response.cached_input_tokens,
        finish_reason=response.finish_reason,
        cost_cny=response.cost,
        cost_known=response.cost_known,
    )
    return row


def run_real_channels(
    plan: dict[str, Any],
    *,
    llm_config_path: Path = DEFAULT_LLM_CONFIG,
    model_overrides: dict[str, str | None] | None = None,
    http_client_factory: Any = None,
    bedrock_client_factory: Any = None,
) -> dict[str, Any]:
    """Run the real channel: at most one request per provider, three total."""
    from codepilot.schemas.usage import Usage

    creds = credentials_present()
    usage = Usage()
    cfg, cfg_note = (None, None)
    if plan["real_requests_allowed"]:
        cfg, cfg_note = _load_llm_config(llm_config_path)

    rows: list[dict[str, Any]] = []
    total_requests = 0
    for key in REAL_PROVIDER_ORDER:
        model_id = resolve_model_id(key, model_overrides)
        missing = row_missing(key, plan, model_id, spent_cny=usage.cost, creds=creds)
        if missing or not model_id:
            rows.append(
                {
                    "row": REAL_PROVIDERS[key]["row"],
                    "provider": key,
                    "model": model_id,
                    "status": "blocked",
                    "missing": missing,
                    "real_requests": 0,
                }
            )
            continue
        row = probe_provider(
            key,
            model_id,
            cfg=cfg,
            cfg_note=cfg_note,
            usage=usage,
            http_client_factory=http_client_factory,
            bedrock_client_factory=bedrock_client_factory,
        )
        rows.append(row)
        total_requests += int(row.get("real_requests") or 0)

    return {
        "budget": plan,
        "credentials_present": creds,
        "rows": rows,
        "real_requests": total_requests,
        "real_cost_cny": round(usage.cost, 8),
        "cost_known": usage.cost_unknown_calls == 0,
        "cost_unknown_calls": usage.cost_unknown_calls,
        "cost_is_lower_bound": usage.cost_is_lower_bound,
        "llm_calls": usage.llm_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "note": (
            "--real was requested: at most one request per provider was dispatched "
            "(no hidden retries). Costs come from the usage ledger; cost_known=false "
            "wherever no complete CNY-convertible price is configured."
        ),
    }


def blocked_real_provider(
    plan: dict[str, Any],
    *,
    model_overrides: dict[str, str | None] | None = None,
    real_requested: bool = False,
) -> dict[str, Any]:
    """Default (no --real) real section: every row blocked, 0 requests, per-row missing."""
    creds = credentials_present()
    rows: list[dict[str, Any]] = []
    for key in REAL_PROVIDER_ORDER:
        model_id = resolve_model_id(key, model_overrides)
        missing = row_missing(key, plan, model_id, creds=creds)
        if not real_requested:
            missing.insert(0, "--real flag (real channel disabled by default)")
        rows.append(
            {
                "row": REAL_PROVIDERS[key]["row"],
                "provider": key,
                "model": model_id,
                "status": "blocked",
                "missing": missing,
                "real_requests": 0,
            }
        )
    return {
        "budget": plan,
        "credentials_present": creds,
        "rows": rows,
        "real_requests": 0,
        "real_cost_cny": 0.0,
        "cost_known": True,
        "cost_unknown_calls": 0,
        "cost_is_lower_bound": False,
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "note": (
            "No real request was issued in this run. Zero-cost fake coverage is "
            "not a substitute for a real-provider row and is not reported as one."
            + ("" if real_requested else " Pass --real to enable the real channel.")
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase32 provider matrix: fake coverage plus the optional real channel."
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help=(
            "enable the real channel: at most one request per provider, gated by "
            + BUDGET_ENV
            + " > 0 (capped at 20 CNY) and per-provider credentials"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the JSON payload ONLY to this path (skips scratch + docs copies)",
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        default=None,
        help="scratch directory for junit/log/json artifacts (default .codex-pytest-tmp/phase32)",
    )
    parser.add_argument(
        "--llm-config",
        type=Path,
        default=DEFAULT_LLM_CONFIG,
        help="price table used by the real channel (default configs/llm.yaml)",
    )
    parser.add_argument(
        "--real-model-openai", default=None, help="model id for the openai-compatible probe"
    )
    parser.add_argument(
        "--real-model-anthropic", default=None, help="model id for the anthropic probe"
    )
    parser.add_argument("--real-model-bedrock", default=None, help="model id for the bedrock probe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scratch = args.scratch if args.scratch is not None else SCRATCH
    if not scratch.is_absolute():
        scratch = ROOT / scratch
    scratch.mkdir(parents=True, exist_ok=True)
    junit = scratch / "provider-matrix.xml"
    log = scratch / "provider-matrix.log"
    plan = budget()
    creds = credentials_present()
    result = run_pytest(junit, log, scratch / "pytest-basetemp")
    cases = junit_cases(junit)
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

    model_overrides = {
        "openai-compatible": args.real_model_openai,
        "anthropic": args.real_model_anthropic,
        "bedrock": args.real_model_bedrock,
    }
    if args.real:
        real_section = run_real_channels(
            plan,
            llm_config_path=args.llm_config,
            model_overrides=model_overrides,
        )
    else:
        real_section = blocked_real_provider(plan, model_overrides=model_overrides)

    totals = {
        "tests": len(cases),
        "failures": sum(1 for case in cases if case["outcome"] == "failure"),
        "errors": sum(1 for case in cases if case["outcome"] == "error"),
        "skipped": sum(1 for case in cases if case["outcome"] == "skipped"),
    }
    payload = {
        "tool": "phase32-provider-matrix",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "real_mode": bool(args.real),
        "pytest": result,
        "totals_from_junit": totals,
        "fake_rows": rows,
        "real_provider": real_section,
        "overlap_note": "fake_rows overlap; never sum them into a test total",
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.out is not None:
        out = args.out if args.out.is_absolute() else ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
    else:
        (scratch / "provider-matrix.json").write_text(text, encoding="utf-8", newline="\n")
        if args.scratch is None:
            # Only a fully-default invocation refreshes the docs copy; any
            # redirect must never overwrite docs/notes/phase32-provider-matrix.json.
            DOCS_JSON.write_text(text, encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "exit_code": result["exit_code"],
                "totals": totals,
                "real_mode": bool(args.real),
                "real_requests": real_section["real_requests"],
                "real_cost_cny": real_section["real_cost_cny"],
                "budget_allowed": plan["real_requests_allowed"],
            },
            indent=2,
        )
    )
    return 0 if result["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
