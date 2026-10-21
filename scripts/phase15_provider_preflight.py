"""No-cost Phase 15 provider preflight.

Reads the configured settings and evaluates template candidates locally. It
never calls a provider, never writes to the real settings document, and never
prints credential values. Candidate templates are materialised in a temporary
SQLite database so the existing SettingsService readiness rules are reused.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codepilot.config import get_settings
from codepilot.settings.catalog import catalog_templates
from codepilot.settings.credentials import CredentialVault
from codepilot.settings.models import CredentialRef, ProviderConfig
from codepilot.settings.service import SettingsError, SettingsService
from codepilot.settings.store import SettingsStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _model_summary(model: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    price = model["price"]
    estimate = None
    if price.get("input_per_mtok") is not None and price.get("output_per_mtok") is not None:
        # One tiny smoke request: 1,000 input tokens and 16 output tokens.
        amount = (1000 / 1_000_000) * price["input_per_mtok"] + (16 / 1_000_000) * price[
            "output_per_mtok"
        ]
        rate = 1.0
        if price["currency"] != document["currency"]:
            rate = document["exchange_rates"].get(price["currency"], 0.0)
        estimate = amount * rate if rate else None
    verification = model["verification"]
    return {
        "model_id": model["model_id"],
        "context_window": model["context_window"],
        "max_output_tokens": model["max_output_tokens"],
        "supports_tools": model["supports_tools"],
        "price": price,
        "source": model["source"],
        "source_note": model["source_note"],
        "config_complete": verification["config_complete"],
        "routing_eligible": verification["routing"]["eligible"],
        "routing_blockers": verification["routing"]["blockers"],
        "inference_state": verification["inference"]["state"],
        "tool_calling": verification["tool_calling"],
        "smoke_estimate_budget_currency": estimate,
    }


def _service_summary(svc: SettingsService) -> dict[str, Any]:
    document = svc.document()
    execution = document.execution
    result = {
        "version": document.version,
        "provider_count": len(document.providers),
        "providers": [],
        "currency": execution.budget_currency,
        "exchange_rates": dict(execution.exchange_rates),
    }
    for provider in document.providers.values():
        view = svc.provider_view(provider)
        result["providers"].append(
            {
                "provider_id": view["provider_id"],
                "name": view["name"],
                "protocol": view["protocol"],
                "base_url": view["base_url"],
                "enabled": view["enabled"],
                "credential_kind": view["credential"]["kind"],
                "credential_available": view["credential"]["available"],
                "credential_reason": view["credential"]["reason"],
                "protocol_available": view["protocol_supported"],
                "routing_eligible": view["verification"]["routing_eligible"],
                "inference_verified": view["verification"]["inference_verified"],
                "models": [_model_summary(model, result) for model in view["models"]],
            }
        )
    return result


def _template_provider(item: Any) -> ProviderConfig:
    return ProviderConfig(
        provider_id=f"preflight-{item.catalog_id}",
        name=item.name,
        protocol=item.protocol,
        base_url=item.base_url,
        catalog_id=item.catalog_id,
        custom=item.catalog_id == "custom",
        region="us-east-1" if item.protocol == "bedrock_converse" else None,
        models=item.models,
        credential=CredentialRef(kind="env", name=item.api_key_env)
        if item.api_key_env
        else CredentialRef(),
    )


def _candidate_summary() -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="codepilot-phase15-preflight-") as tmp:
        root = Path(tmp)
        svc = SettingsService(
            SettingsStore(root / "settings.sqlite3"),
            CredentialVault(root / "settings"),
        )
        summaries: list[dict[str, Any]] = []
        version = 0
        for item in catalog_templates():
            if item.catalog_id == "custom":
                continue
            provider = _template_provider(item)
            try:
                version = svc.create_provider(provider, expected_version=version).version
            except SettingsError as exc:
                summaries.append(
                    {
                        "catalog_id": item.catalog_id,
                        "available": False,
                        "reason": f"{exc.code}: {exc}",
                    }
                )
                continue
            view = svc.provider_view(svc.document().provider(provider.provider_id))
            doc = svc.document()
            summaries.append(
                {
                    "catalog_id": item.catalog_id,
                    "template_available": item.available,
                    "protocol_available": view["protocol_supported"],
                    "credential_kind": view["credential"]["kind"],
                    "credential_available": view["credential"]["available"],
                    "credential_reason": view["credential"]["reason"],
                    "routing_eligible": view["verification"]["routing_eligible"],
                    "models": [
                        _model_summary(
                            model,
                            {
                                "currency": doc.execution.budget_currency,
                                "exchange_rates": doc.execution.exchange_rates,
                            },
                        )
                        for model in view["models"]
                    ],
                }
            )
        return summaries


def _docker_status() -> dict[str, Any]:
    try:
        import docker

        client = docker.from_env(timeout=2)
        try:
            client.ping()
            version = client.version()
        finally:
            client.close()
        return {"available": True, "server_version": version.get("Version"), "via": "sdk"}
    except Exception as exc:  # pragma: no cover - environment dependent
        sdk_error = f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        import subprocess

        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return {
                "available": True,
                "server_version": probe.stdout.strip(),
                "via": "cli",
                "sdk_error": sdk_error,
            }
        cli_error = (probe.stderr or "docker version failed").strip()[:160]
    except Exception as exc:  # pragma: no cover - environment dependent
        cli_error = f"{type(exc).__name__}: {str(exc)[:160]}"
    return {
        "available": False,
        "error": sdk_error,
        "cli_error": cli_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "phase15" / "provider-preflight.json",
    )
    args = parser.parse_args()

    settings = get_settings()
    service = SettingsService(
        SettingsStore(settings.resolved_settings_path),
        CredentialVault(settings.resolved_settings_credentials_dir),
    )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "offline_preflight_no_network_no_paid_calls",
        "configured": _service_summary(service),
        "candidates": _candidate_summary(),
        "runtime": {
            "live_llm_enabled": settings.api_allow_live_llm,
            "execution_backend": settings.api_execution_backend,
            "simulated_live_sandbox_allowed": settings.api_live_allow_simulated_sandbox,
            "max_live_cost_per_task": settings.api_max_live_cost,
            "docker": _docker_status(),
        },
        "limits": {
            "configured_providers_only": True,
            "no_model_discovery": True,
            "no_inference_request": True,
            "credential_values_never_read_or_printed": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"preflight written to {args.output}")
    print(
        json.dumps(
            {
                "configured_providers": payload["configured"]["provider_count"],
                "live_llm_enabled": payload["runtime"]["live_llm_enabled"],
                "docker_available": payload["runtime"]["docker"]["available"],
                "candidates_with_credentials": [
                    item["catalog_id"]
                    for item in payload["candidates"]
                    if item.get("credential_available")
                    and any(model["routing_eligible"] for model in item.get("models", []))
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
