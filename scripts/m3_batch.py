from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import l2cal10e_acceptance as acceptance  # noqa: E402
import l2cal10e_official as official  # noqa: E402

from codepilot.llm.config import load_llm_config  # noqa: E402
from codepilot.llm.cost import compute_cost  # noqa: E402
from codepilot.llm.dispatch_recovery import (  # noqa: E402
    instance_opening_cny,
    load_recovery_manifest,
    record_attempt_from_ledger,
    validate_recovery_manifest,
)
from codepilot.sandbox.runners import RunnerOverrides  # noqa: E402

TERMINAL = {
    "RESOLVED",
    "UNRESOLVED",
    "OFFICIAL_ERROR",
    "REJECTED",
    "NO_PATCH",
    "ENV_ERROR",
    "NOT_DISPATCHED",
}


class BatchStopError(RuntimeError):
    pass


@dataclass(frozen=True)
class InternalRequest:
    instance: dict[str, Any]
    root: Path
    ledger: Path
    resume: bool
    budget_limit: float
    instance_opening: float
    manifest: Path
    manifest_sha256: str


@dataclass(frozen=True)
class OfficialRequest:
    instance: dict[str, Any]
    root: Path
    run_id: str


def canonical_repo_source(repo: str) -> Any:
    """The same source identity the SWE-bench loader gives the internal run."""
    from codepilot.repo.workspace import normalize_source

    return normalize_source(repo if "://" in repo else f"https://github.com/{repo}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    staging.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    staging.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise BatchStopError(f"expected JSON object: {path}")
    return payload


def _ledger_totals(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    records = payload.get("requests")
    if not isinstance(records, list):
        raise BatchStopError(f"ledger requests are invalid: {path}")
    settled = 0.0
    unknown = 0.0
    unknown_calls = 0
    for record in records:
        if not isinstance(record, dict):
            raise BatchStopError(f"ledger record is invalid: {path}")
        if record.get("status") == "settled":
            cost = record.get("cost_cny")
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
                raise BatchStopError(f"settled cost is invalid: {path}")
            settled += float(cost)
        else:
            reserved = record.get("reserved_cny")
            if (
                isinstance(reserved, bool)
                or not isinstance(reserved, (int, float))
                or reserved <= 0
            ):
                raise BatchStopError(f"unknown reservation is invalid: {path}")
            unknown += float(reserved)
            unknown_calls += 1
    return {
        "settled": settled,
        "unknown": unknown,
        "unknown_calls": unknown_calls,
        "frozen": bool(payload.get("frozen")) or unknown_calls > 0,
        "requests": len(records),
        "path": path,
    }


class M3Batch:
    def __init__(
        self,
        *,
        repo_root: Path,
        catalog_path: Path,
        config_path: Path,
        artifact_root: Path,
        live: bool,
        internal_runner: Callable[[InternalRequest], int] | None = None,
        official_runner: Callable[[OfficialRequest], int] | None = None,
        validate_source: bool = True,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.catalog_path = self._absolute(catalog_path)
        self.config_path = self._absolute(config_path)
        self.artifact_root = self._absolute(artifact_root)
        self.live = live
        self.internal_runner = internal_runner or self._run_internal
        self.official_runner = official_runner or self._run_official
        self.validate_source = validate_source
        self.catalog = _read_json(self.catalog_path)
        self.config = _read_json(self.config_path)
        self.batch_id = str(self.config.get("batch_id") or "")
        if not self.batch_id or self.catalog.get("batch_id") != self.batch_id:
            raise BatchStopError("batch_id must match the frozen catalog")
        self.instances = list(self.catalog.get("instances") or [])
        self.catalog_image_ids = dict(self.catalog.get("image_ids") or {})
        self.catalog_runner_kinds = dict(self.catalog.get("runner_kinds") or {})
        if len(self.instances) != 6:
            raise BatchStopError("the frozen M3 catalog must contain exactly six instances")
        ids = [str(item.get("instance_id") or "") for item in self.instances]
        repos = [str(item.get("repo") or "") for item in self.instances]
        if len(set(ids)) != 6 or len(set(repos)) != 6:
            raise BatchStopError("catalog instances and repositories must be unique")
        self.registry_path = self.artifact_root / "attempts.json"
        self.manifest_path = self._absolute(Path(str(self.config["opening_manifest"])))
        self.opening_manifest = load_recovery_manifest(self.manifest_path)
        validate_recovery_manifest(self.opening_manifest, repo_root=self.repo_root)
        self.manifest_sha256 = _sha256(self.manifest_path)
        self.historical_opening = float(self.opening_manifest["total_upper_bound_cny"])
        expected_opening = float(self.config["historical_opening_cny"])
        if abs(expected_opening - self.historical_opening) > 1e-9:
            raise BatchStopError("historical opening does not match the validated manifest")
        self.prior_authorized_upper = 0.0
        prior_sources = self.config.get("prior_authorization_sources", [])
        covered = {
            item["path"]
            for item in [
                *self.opening_manifest.get("sources", []),
                *self.opening_manifest.get("additional_settled_ledgers", []),
            ]
        }
        seen: set[str] = set()
        for source in prior_sources:
            relative = source["path"]
            if relative in seen or relative not in covered:
                raise BatchStopError("prior authorization source duplicated or absent from opening")
            seen.add(relative)
            path = self._absolute(Path(relative))
            if _sha256(path) != source["sha256"]:
                raise BatchStopError("prior authorization ledger hash mismatch")
            costs = _ledger_totals(path)
            self.prior_authorized_upper += costs["settled"] + costs["unknown"]
        if (
            prior_sources
            and abs(
                float(self.config["batch_new_limit_cny"])
                + self.prior_authorized_upper
                - float(self.config["original_authorization_cny"])
            )
            > 1e-9
        ):
            raise BatchStopError("remaining original authorization does not match ledger costs")
        self.model = str(self.config["model"])
        self.llm_config_path = self._absolute(Path(str(self.config["llm_config"])))
        self.model_spec = load_llm_config(self.llm_config_path).models[self.model]
        self.limits = {
            "max_iterations": int(self.config.get("max_iterations", 5)),
            "max_test_runs": int(self.config.get("max_test_runs", 12)),
            "max_tool_calls_per_iteration": int(
                self.config.get("max_tool_calls_per_iteration", 30)
            ),
            "max_review_calls": int(self.config.get("max_review_calls", 2)),
            "timeout_seconds": int(self.config.get("timeout_seconds", 1800)),
        }
        if not 3 <= self.limits["max_test_runs"] <= 60:
            raise BatchStopError("max_test_runs must be in 3..60")
        runner_path = self.config.get("runner_overrides")
        self.runner_overrides_path = self._absolute(Path(str(runner_path))) if runner_path else None
        self.runner_overrides_sha256: str | None = None
        self.runner_overrides: RunnerOverrides | None = None
        if self.runner_overrides_path is not None:
            if not self.runner_overrides_path.is_file():
                raise BatchStopError("runner overrides file is missing")
            overrides = RunnerOverrides.load(self.runner_overrides_path)
            if overrides is None:
                raise BatchStopError("runner overrides could not be loaded")
            self.runner_overrides_sha256 = _sha256(self.runner_overrides_path)
            self.runner_overrides = overrides
            expected_ids = {str(item["instance_id"]) for item in self.instances}
            configured = set(overrides.instances)
            if configured != expected_ids:
                raise BatchStopError(
                    "runner overrides must cover exactly the frozen catalog: "
                    f"missing={sorted(expected_ids - configured)} "
                    f"extra={sorted(configured - expected_ids)}"
                )
            if self.catalog_runner_kinds and (overrides.kinds() != self.catalog_runner_kinds):
                raise BatchStopError("catalog runner kinds do not match the frozen overrides")
        seeds_value = self.config.get("repo_cache_seeds")
        self.repo_cache_seeds_path = self._absolute(Path(str(seeds_value))) if seeds_value else None
        self.repo_cache_seeds: dict[str, str] = {}
        if self.repo_cache_seeds_path is not None:
            if not self.repo_cache_seeds_path.is_file():
                raise BatchStopError("repo cache seeds file is missing")
            self.repo_cache_seeds = dict(_read_json(self.repo_cache_seeds_path).get("seeds") or {})
        self.reserve = compute_cost(
            price=self.model_spec.price,
            input_tokens=self.model_spec.context_window,
            output_tokens=int(self.config["max_output_tokens"]),
        ).amount
        self.code_sha = self._git("rev-parse", "HEAD").stdout.strip()
        self.catalog_sha256 = _sha256(self.catalog_path)
        self.config_sha256 = _sha256(self.config_path)
        self.state_path = self.artifact_root / "batch-state.json"
        self.summary_path = self.artifact_root / "batch-summary.json"
        self.freeze_path = self.artifact_root / "batch-freeze.json"
        self.state = self._load_or_initialize()
        # Continue only undispatched tasks after an explicit repaired segment.
        for instance_id, source in self.config.get("completed_segment_sources", {}).items():
            path = self._absolute(Path(source["path"]))
            costs = _ledger_totals(path)
            if _sha256(path) != source["sha256"] or costs["frozen"] or not costs["requests"]:
                raise BatchStopError("completed segment source is not a settled paid attempt")
            if source["path"] not in covered:
                raise BatchStopError("completed segment ledger is absent from opening")
            self._entry(instance_id).update(
                status="DISPATCHED_STOPPED",
                internal="SYSTEM_INTERRUPTED",
                terminal=True,
                previous_segment=source,
            )
            if source.get("result_path"):
                result_path = self._absolute(Path(source["result_path"]))
                result = _read_json(result_path)
                if (
                    _sha256(result_path) != source["result_sha256"]
                    or result.get("instance_id") != instance_id
                    or result.get("ledger_sha256") != source["sha256"]
                    or result.get("terminal") is not True
                ):
                    raise BatchStopError("completed segment result identity mismatch")
                self._entry(instance_id).update(result)
            self._entry(instance_id).update(settled_cny=costs["settled"], unknown_reserved_cny=0)

    def _absolute(self, path: Path) -> Path:
        return path if path.is_absolute() else self.repo_root / path

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=self.repo_root, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise BatchStopError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def _load_or_initialize(self) -> dict[str, Any]:
        freeze = {
            "schema_version": 1,
            "batch_id": self.batch_id,
            "created_at": _now(),
            "code_sha": self.code_sha,
            "catalog": str(self.catalog_path),
            "catalog_sha256": self.catalog_sha256,
            "config": str(self.config_path),
            "config_sha256": self.config_sha256,
            "dataset_revision": self.config["dataset_revision"],
            "selection_seed": self.config["selection_seed"],
            "opening_manifest": str(self.manifest_path),
            "opening_manifest_sha256": self.manifest_sha256,
            "model": self.model,
            "temperature": self.config["temperature"],
            "seed_support": "not_dispatched_by_provider",
            "http_retries": 0,
            "llm_config_sha256": _sha256(self.llm_config_path),
            "limits": self.limits,
            "limit_source": "typed_batch_config",
            "runner_overrides": (
                str(self.runner_overrides_path) if self.runner_overrides_path else None
            ),
            "runner_overrides_sha256": self.runner_overrides_sha256,
            "runner_kinds": self.runner_overrides.kinds() if self.runner_overrides else {},
            "repo_cache_seeds": (
                str(self.repo_cache_seeds_path) if self.repo_cache_seeds_path else None
            ),
            "repo_cache_seeds_sha256": (
                _sha256(self.repo_cache_seeds_path) if self.repo_cache_seeds_path else None
            ),
            "artifact_root": str(self.artifact_root),
        }
        if self.artifact_root.exists():
            if not self.freeze_path.is_file() or not self.state_path.is_file():
                raise BatchStopError("artifact root exists without a complete batch identity")
            previous = _read_json(self.freeze_path)
            for key in (
                "batch_id",
                "code_sha",
                "catalog_sha256",
                "config_sha256",
                "dataset_revision",
                "opening_manifest_sha256",
                "model",
                "temperature",
                "limits",
                "llm_config_sha256",
                "runner_overrides_sha256",
                "repo_cache_seeds_sha256",
            ):
                if previous.get(key) != freeze[key]:
                    raise BatchStopError(f"batch identity mismatch: {key}")
            self.freeze = previous
            return _read_json(self.state_path)
        if self.live:
            self._validate_live_source()
        self.artifact_root.mkdir(parents=True)
        self.freeze = freeze
        _write_json(self.freeze_path, freeze)
        state = {
            "schema_version": 1,
            "batch_id": self.batch_id,
            "status": "planned",
            "stop_reason": None,
            "instances": [
                {
                    "instance_id": item["instance_id"],
                    "repo": item["repo"],
                    "status": "PENDING",
                    "run_id": None,
                    "internal": "NOT_RUN",
                    "official": "NOT_EVALUATED",
                    "settled_cny": 0.0,
                    "unknown_reserved_cny": 0.0,
                    "terminal": False,
                }
                for item in self.instances
            ],
        }
        _write_json(self.state_path, state)
        return state

    def _validate_live_source(self) -> None:
        tracked = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if tracked.returncode != 0 or tracked.stdout.strip():
            raise BatchStopError("live batch requires committed and clean tracked sources")

    def _entry(self, instance_id: str) -> dict[str, Any]:
        return next(item for item in self.state["instances"] if item["instance_id"] == instance_id)

    def _save(self) -> None:
        _write_json(self.state_path, self.state)
        _write_json(self.summary_path, self._summary())

    def _all_ledgers(self) -> list[Path]:
        return sorted(self.artifact_root.glob("instances/*/dispatch-ledger-*.json"))

    def _instance_ledgers(self, instance_id: str) -> list[Path]:
        return sorted(
            (self.artifact_root / "instances" / instance_id).glob("dispatch-ledger-*.json")
        )

    def _costs(self, instance_id: str | None = None) -> dict[str, Any]:
        paths = self._all_ledgers() if instance_id is None else self._instance_ledgers(instance_id)
        totals = [_ledger_totals(path) for path in paths]
        return {
            "settled": sum(item["settled"] for item in totals),
            "unknown": sum(item["unknown"] for item in totals),
            "unknown_calls": sum(item["unknown_calls"] for item in totals),
            "requests": sum(item["requests"] for item in totals),
            "frozen": any(item["frozen"] for item in totals),
        }

    def _budget(self, instance_id: str) -> dict[str, float]:
        batch = self._costs()
        instance = self._costs(instance_id)
        if batch["frozen"]:
            raise BatchStopError("unknown/frozen ledger stops every later dispatch")
        new_upper = batch["settled"] + batch["unknown"]
        instance_upper = instance["settled"] + instance["unknown"]
        batch_remaining = float(self.config["batch_new_limit_cny"]) - new_upper
        main_remaining = float(self.config["main_batch_limit_cny"]) - new_upper
        instance_remaining = float(self.config["per_instance_new_limit_cny"]) - instance_upper
        if min(batch_remaining, main_remaining, instance_remaining) < self.reserve:
            raise BatchStopError(
                f"insufficient pre-dispatch reserve {self.reserve:.8f}: "
                f"batch={batch_remaining:.8f} main={main_remaining:.8f} "
                f"instance={instance_remaining:.8f}"
            )
        return {
            "batch_remaining": batch_remaining,
            "main_remaining": main_remaining,
            "instance_remaining": instance_remaining,
            "allowed": min(batch_remaining, main_remaining, instance_remaining),
        }

    def _summary(self) -> dict[str, Any]:
        batch = self._costs()
        planned = len(self.instances)
        verdicts = [item["official"] for item in self.state["instances"]]
        resolved = verdicts.count("RESOLVED")
        unresolved = verdicts.count("UNRESOLVED")
        accepted = sum(item["internal"] == "ACCEPTED" for item in self.state["instances"])
        invalid = sum(item["status"] == "ENV_ERROR" for item in self.state["instances"])
        return {
            "schema_version": 1,
            "batch_id": self.batch_id,
            "status": self.state["status"],
            "stop_reason": self.state["stop_reason"],
            "planned_instances": planned,
            "instances": self.state["instances"],
            "metrics": {
                "valid_official_resolution_rate": (
                    resolved / (resolved + unresolved) if resolved + unresolved else None
                ),
                "official_coverage": (resolved + unresolved) / planned,
                "task_resolution_rate": resolved / planned,
                "internal_acceptance_rate": accepted / planned,
                "environment_invalid_rate": invalid / planned,
            },
            "cost": {
                "historical_opening_cny": self.historical_opening,
                "historical_settled_cny": float(self.opening_manifest["settled_cny"]),
                "historical_unknown_reserved_cny": float(
                    self.opening_manifest["unknown_reserved_cny"]
                ),
                "new_settled_cny": batch["settled"],
                "new_unknown_reserved_cny": batch["unknown"],
                "new_upper_bound_cny": batch["settled"] + batch["unknown"],
                "prior_authorization_upper_bound_cny": self.prior_authorized_upper,
                "original_authorization_used_upper_bound_cny": self.prior_authorized_upper
                + batch["settled"]
                + batch["unknown"],
                "cumulative_upper_bound_cny": self.historical_opening
                + batch["settled"]
                + batch["unknown"],
                "new_authorization_cny": self.config["batch_new_limit_cny"],
                "new_authorization_remaining_cny": self.config["batch_new_limit_cny"]
                - batch["settled"]
                - batch["unknown"],
                "main_batch_limit_cny": self.config["main_batch_limit_cny"],
                "requests": batch["requests"],
                "unknown_calls": batch["unknown_calls"],
            },
        }

    def _stop(self, reason: str) -> None:
        self.state["status"] = "stopped"
        self.state["stop_reason"] = reason
        frozen = self._costs()["frozen"]
        for entry in self.state["instances"]:
            if entry["terminal"] or entry["status"] == "OFFICIAL_PENDING":
                continue
            costs = self._costs(entry["instance_id"])
            entry.update(
                status="DISPATCHED_STOPPED" if costs["requests"] else "NOT_DISPATCHED",
                internal=("PROVIDER_USAGE_UNCERTAIN" if costs["frozen"] else entry["internal"]),
                stop_reason=(
                    "BLOCKED_BY_FROZEN_LEDGER" if frozen and not costs["requests"] else reason
                ),
                settled_cny=costs["settled"],
                unknown_reserved_cny=costs["unknown"],
                terminal=True,
            )
        self._save()

    def run(self) -> dict[str, Any]:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        lock = self.artifact_root / "batch.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BatchStopError("batch lock exists; inspect recorded PID before recovery") from exc
        try:
            os.write(descriptor, json.dumps({"pid": os.getpid(), "started_at": _now()}).encode())
            return self._run_locked()
        finally:
            os.close(descriptor)
            lock.unlink()

    def _run_locked(self) -> dict[str, Any]:
        if not self.live:
            self.state["status"] = "dry_run"
            for item in self.state["instances"]:
                item["status"] = "DRY_RUN"
            self._save()
            return self._summary()
        self._validate_live_source()
        if self.code_sha != self.freeze["code_sha"]:
            raise BatchStopError("source changed after batch freeze")
        if self.state["status"] == "stopped":
            # A stop is durable. Only explicit official ingestion may reset it;
            # rerunning an all-terminal batch must not erase a frozen stop.
            return self._summary()
        if self.config.get("require_preflight"):
            try:
                self._preflight = self._load_preflight()
            except BatchStopError as exc:
                self._stop(str(exc))
                return self._summary()
            if not any(item["ok"] for item in self._preflight.values()):
                for entry in self.state["instances"]:
                    entry["status"] = "ENV_ERROR"
                    entry["terminal"] = True
                self._stop("no frozen instance has a usable environment")
                return self._summary()
        else:
            self._preflight = {}
        self.state["status"] = "running"
        self._save()
        try:
            for instance in self.instances:
                instance_id = instance["instance_id"]
                entry = self._entry(instance_id)
                if entry["terminal"]:
                    continue
                try:
                    self._run_instance(instance, entry)
                except (BatchStopError, OSError, ValueError) as exc:
                    if entry["status"] == "PENDING":
                        entry["status"] = "STOPPED"
                    self._stop(f"{instance_id}: {type(exc).__name__}: {exc}")
                    break
                if self.state["status"] == "stopped":
                    break
            else:
                self.state["status"] = "completed"
                self._save()
        except KeyboardInterrupt:
            self._stop("cancelled; no later instance was dispatched")
        return self._summary()

    def ingest_official(self) -> dict[str, Any]:
        """Ingest externally produced official verdicts without paid dispatch.

        The Windows Python process cannot spawn ``wsl.exe`` in the current
        environment, so an accepted instance is handed off as an explicit
        request file.  This method only reads the official record written by the
        WSL harness; it never re-runs the internal model or a sandbox test.
        """
        ingested = 0
        for entry in self.state["instances"]:
            if entry.get("status") != "OFFICIAL_PENDING":
                continue
            instance_id = entry["instance_id"]
            root = self.artifact_root / "instances" / instance_id
            official_path = root / f"official-evaluation-{self.batch_id}.json"
            request_path = root / f"official-request-{self.batch_id}.json"
            if not official_path.is_file():
                raise BatchStopError(f"{instance_id}: official record is missing: {official_path}")
            official_result = _read_json(official_path)
            request = _read_json(request_path) if request_path.is_file() else {}
            if request:
                if str(official_result.get("instance_id") or instance_id) != instance_id:
                    raise BatchStopError(f"{instance_id}: official instance identity mismatch")
                if str(official_result.get("run_id") or entry.get("run_id")) != entry.get("run_id"):
                    raise BatchStopError(f"{instance_id}: official run_id mismatch")
            verdict = str(official_result.get("official_verdict") or "NOT_EVALUATED")
            if verdict not in {"RESOLVED", "UNRESOLVED"}:
                entry.update(
                    official=str(official_result.get("status") or "ERROR"),
                    status="OFFICIAL_ERROR",
                    terminal=True,
                )
                self._stop(f"{instance_id}: official record has no valid verdict: {verdict}")
                return {"ingested": ingested, "summary": self._summary()}
            entry["official"] = verdict
            entry["status"] = verdict
            entry["terminal"] = True
            ingested += 1
        if ingested:
            for entry in self.state["instances"]:
                if entry["status"] == "NOT_DISPATCHED":
                    entry.update(status="PENDING", terminal=False)
            self.state["status"] = "running"
            self._save()
        return {"ingested": ingested, "summary": self._summary()}

    def _load_preflight(self) -> dict[str, dict[str, Any]]:
        path = self.artifact_root / "preflight/preflight.json"
        if not path.is_file():
            raise BatchStopError("required preflight report is missing")
        report = _read_json(path)
        if not report.get("credential_reference_present"):
            raise BatchStopError("model credential reference is not resolvable")
        if self.runner_overrides_sha256 is not None and (
            report.get("runner_overrides_sha256") != self.runner_overrides_sha256
        ):
            raise BatchStopError("preflight runner identity does not match the frozen batch")
        instances = report.get("instances") or []
        mapped = {str(item.get("instance_id")): item for item in instances}
        expected = {item["instance_id"] for item in self.instances}
        if set(mapped) != expected:
            raise BatchStopError("preflight instance identities do not match the frozen catalog")
        return mapped

    def _verify_run_budget(self, run: dict[str, Any], instance_id: str) -> None:
        """The report's effective budget must be the frozen typed config.

        This is the post-run half of the Phase 20 ``18 -> 12`` guard: the
        internal process also checks its own assembly before any model call, and
        the batch refuses to accept a report whose budget came from anywhere
        else (a default, an old checkpoint or an environment override).
        """
        budget = run.get("budget")
        if not isinstance(budget, dict):
            raise BatchStopError(f"{instance_id}: run report has no budget")
        expected = {
            "max_iterations": self.limits["max_iterations"],
            "max_test_runs": self.limits["max_test_runs"],
            "max_tool_calls_per_iteration": self.limits["max_tool_calls_per_iteration"],
            "max_review_calls": self.limits["max_review_calls"],
            "max_runtime_seconds": self.limits["timeout_seconds"],
        }
        actual = {name: budget.get(name) for name in expected}
        if actual != expected:
            raise BatchStopError(
                f"{instance_id}: effective report budget {actual} does not match "
                f"frozen config {expected}"
            )
        source = budget.get("source") or {}
        if source.get("max_test_runs") != "cli":
            raise BatchStopError(
                f"{instance_id}: frozen batch limits must come from the CLI, "
                f"not a default or environment override: {source}"
            )

    def _verify_preflight_image(self, instance_id: str) -> None:
        expected = self._preflight[instance_id]["image"]["id"]
        frozen = self.catalog_image_ids.get(instance_id)
        if frozen is not None and frozen != expected:
            raise BatchStopError(
                f"preflight image {expected} does not match frozen catalog {frozen}: {instance_id}"
            )
        completed = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                self._preflight[instance_id]["image"]["ref"],
            ],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or completed.stdout.strip() != expected:
            raise BatchStopError(f"image identity changed after preflight: {instance_id}")

    def _seed_repo_cache(self, instance: dict[str, Any]) -> None:
        """Seed the batch's base-only bare cache from a frozen Phase 20 cache.

        Local git fetch/clone from a Python child can fail before opening the
        repository (MSYS ``sh`` startup).  The bare object database is
        deterministic; copying it into the batch cache and letting the
        WorkspaceManager use copy mode avoids the broken local transport while
        keeping the workspace isolated from the seed.
        """
        if not self.repo_cache_seeds:
            return
        seed_value = self.repo_cache_seeds.get(instance["instance_id"])
        if not seed_value:
            raise BatchStopError(f"no repo cache seed for {instance['instance_id']}")
        from codepilot.repo.workspace import WorkspaceManager

        # The internal runner loads the task from the frozen dataset and uses
        # its normalized source (https://github.com/owner/repo). Hashing the
        # catalog's bare "owner/repo" string instead resolves to a different
        # cache key; the seed is then invisible and the internal run clones the
        # full remote history (the r4 Django attempt hit 53k commits and was
        # correctly stopped by the base-only isolation assertion).
        source = canonical_repo_source(str(instance["repo"]))
        manager = WorkspaceManager(
            cache_root=self.artifact_root / "instances" / instance["instance_id"] / "repo-cache"
        )
        target = manager.cache_path(source)
        if not target.exists():
            seed_root = (self.repo_root / seed_value).resolve()
            children = [path for path in seed_root.iterdir() if path.is_dir()]
            if len(children) != 1:
                raise BatchStopError(f"seed cache must contain one bare repo: {seed_root}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(children[0], target, symlinks=True)
        count = subprocess.run(
            ["git", "-C", str(target), "rev-list", "--all", "--count"],
            capture_output=True,
            text=True,
            check=False,
        )
        if count.returncode != 0 or count.stdout.strip() != "1":
            raise BatchStopError(
                f"seeded repo cache is not base-only for {instance['instance_id']}: "
                f"{count.stdout.strip() or count.stderr.strip()}"
            )

    def _run_instance(self, instance: dict[str, Any], entry: dict[str, Any]) -> None:
        instance_id = instance["instance_id"]
        root = self.artifact_root / "instances" / instance_id
        root.mkdir(parents=True, exist_ok=True)
        if (root / "eval").exists():
            raise BatchStopError(
                "existing eval directory requires explicit recovery; refusing overwrite"
            )
        self._seed_repo_cache(instance)
        if self.config.get("require_preflight"):
            preflight = self._preflight[instance_id]
            if not preflight["ok"]:
                entry["status"] = "ENV_ERROR"
                entry["official"] = "NOT_EVALUATED"
                entry["terminal"] = True
                self._save()
                return
            self._verify_preflight_image(instance_id)
        budget = self._budget(instance_id)
        ledgers = self._instance_ledgers(instance_id)
        totals = [_ledger_totals(path) for path in ledgers]
        if any(item["frozen"] for item in totals):
            raise BatchStopError("instance ledger is frozen")
        if any(item["requests"] > 0 for item in totals):
            raise BatchStopError(
                "interrupted instance already dispatched; refusing a second paid attempt"
            )
        resume = bool(totals)
        sequence = len(totals) + 1
        ledger = root / f"dispatch-ledger-{self.batch_id}-r{sequence}.json"
        if ledger.exists():
            raise BatchStopError(f"ledger already exists: {ledger}")
        opening = instance_opening_cny(self.opening_manifest, instance_id, repo_root=self.repo_root)
        run_code = self.internal_runner(
            InternalRequest(
                instance=instance,
                root=root,
                ledger=ledger,
                resume=resume,
                budget_limit=budget["allowed"],
                instance_opening=opening,
                manifest=self.manifest_path,
                manifest_sha256=self.manifest_sha256,
            )
        )
        current = _ledger_totals(ledger) if ledger.is_file() else None
        attempt_status = (
            "stopped"
            if current and current["frozen"]
            else "not_dispatched"
            if current is None or not current["requests"]
            else "completed"
            if run_code == 0
            else "invalid"
        )
        record_attempt_from_ledger(
            self.registry_path,
            attempt_label=self.batch_id,
            instance_id=instance_id,
            artifact_root=root,
            ledger=ledger,
            status=attempt_status,
            repo_root=self.repo_root,
        )
        if current is not None:
            entry["settled_cny"] = current["settled"]
            entry["unknown_reserved_cny"] = current["unknown"]
        if current is not None and current["frozen"]:
            raise BatchStopError("unknown usage/cost; ledger frozen after registry recording")
        self._budget(instance_id)
        found = acceptance._run_report(root, instance=instance_id, label=self.batch_id, run_id=None)
        if found is None:
            dispatched = bool(current and current["requests"])
            entry["status"] = (
                "DISPATCHED_STOPPED"
                if dispatched
                else "ENV_ERROR"
                if run_code
                else "NOT_DISPATCHED"
            )
            if dispatched:
                entry["internal"] = "SYSTEM_INTERRUPTED"
            entry["official"] = "NOT_EVALUATED"
            entry["terminal"] = True
            self._save()
            raise BatchStopError("internal execution ended without a unique run report")
        _report_path, run = found
        run_id = str(run["run_id"])
        entry["run_id"] = run_id
        if run.get("status") == "sandbox_error" or run.get("error_origin") in {
            "harness",
            "sandbox",
            "environment",
        }:
            entry.update(
                status="ENV_ERROR", internal="ENV_ERROR", official="NOT_EVALUATED", terminal=True
            )
            record_attempt_from_ledger(
                self.registry_path,
                attempt_label=self.batch_id,
                instance_id=instance_id,
                artifact_root=root,
                ledger=ledger,
                status="stopped",
                repo_root=self.repo_root,
            )
            self._save()
            raise BatchStopError(f"infrastructure failure in inner run: {run.get('status')}")
        budget_report = run
        if not isinstance(run.get("budget"), dict):
            budget_report = acceptance._root_report(root, run_id)
            if (
                budget_report.get("run_id") != run_id
                or budget_report.get("run_label") != self.batch_id
            ):
                raise BatchStopError("budget report identity mismatch")
        self._verify_run_budget(budget_report, instance_id)
        result = acceptance.evaluate(root, instance=instance_id, label=self.batch_id, run_id=run_id)
        _write_json(root / f"acceptance-{self.batch_id}.json", result)
        entry["internal"] = result["verdict"]
        if not result["accepted"]:
            root_report = acceptance._root_report(root, run_id)
            closure = run.get("closure") or {}
            root_status = str(root_report.get("status") or "")
            if root_status in {"ENV_ERROR", "SANDBOX_ERROR"}:
                entry["status"] = "ENV_ERROR"
                entry["terminal"] = True
                self._save()
                raise BatchStopError(f"infrastructure failure: {root_status}")
            entry["status"] = "NO_PATCH" if not closure.get("patch_present") else "REJECTED"
            entry["official"] = "NOT_EVALUATED"
            entry["terminal"] = True
            self._save()
            return
        root_report = acceptance._root_report(root, run_id)
        prediction = root_report.get("prediction") or {}
        manifest = prediction.get("manifest") or {}
        model = manifest.get("model_name_or_path") or (root_report.get("runtime") or {}).get(
            "model_identity"
        )
        evidence = result["evidence"]
        official._verify_prediction(
            root,
            run_id=run_id,
            instance=instance_id,
            prediction=prediction,
            expected_patch_sha256=str(evidence["delivered_patch_sha256"]),
            expected_model=str(model),
        )
        if self.config.get("official_mode") == "external":
            _write_json(
                root / f"official-request-{self.batch_id}.json",
                {
                    "schema_version": 1,
                    "batch_id": self.batch_id,
                    "instance_id": instance_id,
                    "run_id": run_id,
                    "prediction_sha256": evidence["prediction_sha256"],
                    "delivered_patch_sha256": evidence["delivered_patch_sha256"],
                    "model": model,
                    "status": "PENDING_EXTERNAL_WSL",
                    "created_at": _now(),
                },
            )
            entry["status"] = "OFFICIAL_PENDING"
            entry["official"] = "NOT_EVALUATED"
            entry["terminal"] = False
            self._save()
            raise BatchStopError(
                "external official harness required; batch paused before any later instance"
            )
        official_code = self.official_runner(OfficialRequest(instance, root, run_id))
        official_path = root / f"official-evaluation-{self.batch_id}.json"
        if not official_path.is_file():
            entry.update(status="OFFICIAL_ERROR", official="ERROR", terminal=True)
            self._save()
            raise BatchStopError("official runner returned without an evaluation record")
        official_result = _read_json(official_path)
        verdict = str(official_result.get("official_verdict") or "NOT_EVALUATED")
        entry["official"] = verdict
        entry["status"] = verdict if verdict in {"RESOLVED", "UNRESOLVED"} else "OFFICIAL_ERROR"
        entry["terminal"] = True
        self._save()
        if official_code != 0 or verdict not in {"RESOLVED", "UNRESOLVED"}:
            raise BatchStopError(f"official evaluation did not produce a verdict: {verdict}")

    def _internal_command(self, request: InternalRequest) -> tuple[list[str], dict[str, str]]:
        instance = request.instance
        instance_id = instance["instance_id"]
        opening_source = json.dumps(
            {
                "kind": "per_instance",
                "instance_id": instance_id,
                "instance_opening_cny": request.instance_opening,
                "repo_root": str(self.repo_root),
                "recovery_manifest": str(request.manifest),
                "recovery_manifest_sha256": request.manifest_sha256,
            },
            separators=(",", ":"),
        )
        env = {
            **os.environ,
            "CODEPILOT_LLM_CONFIG": str(self.llm_config_path),
            "CODEPILOT_DISABLE_HTTP_RETRIES": "1",
            "CODEPILOT_REQUIRE_TEMPERATURE": "1",
            "CODEPILOT_BIND_WORKSPACE": "1",
            "CODEPILOT_WORKSPACE_MODE": "copy",
            "CODEPILOT_DISPATCH_BUDGET_CNY": str(request.instance_opening + request.budget_limit),
            "CODEPILOT_DISPATCH_OPENING_CNY": str(request.instance_opening),
            "CODEPILOT_DISPATCH_OPENING_SOURCE": opening_source,
            "CODEPILOT_DISPATCH_LEDGER": str(request.ledger),
            "CODEPILOT_TESTDEPS_DIR": str(self.repo_root / "artifacts/phase18/l2-cal10e/testdeps"),
        }
        snapshots = self.config.get("index_snapshots", {})
        if instance_id in snapshots:
            seed = snapshots[instance_id]
            env["CODEPILOT_INDEX_SNAPSHOT"] = json.dumps(
                {
                    "path": str(self._absolute(Path(seed["path"]))),
                    "sha256": seed["sha256"],
                }
            )
        docker_host = os.environ.get("DOCKER_HOST", "")
        if docker_host.startswith("tcp://"):
            from urllib.parse import urlsplit

            host = urlsplit(docker_host).hostname
            if host:
                bypass = [item for item in env.get("NO_PROXY", "").split(",") if item]
                if host not in bypass:
                    env["NO_PROXY"] = ",".join([*bypass, host])
                env.setdefault("no_proxy", env["NO_PROXY"])
        command = [
            sys.executable,
            str(self.repo_root / "scripts/l2cal10e_internal.py"),
            "--instance-id",
            instance_id,
            "--split",
            "test",
            "--revision",
            str(self.config["dataset_revision"]),
            "--provider",
            "bigmodel",
            "--model",
            self.model,
            "--budget",
            str(request.instance_opening + request.budget_limit),
            "--live-llm",
            "--record-llm",
            "--artifact-root",
            str(request.root),
            "--snapshot-root",
            str(self.repo_root / "cache/swebench"),
            "--spec-snapshot",
            str(self.repo_root / "configs/swebench/env-specs-4.1.0.json"),
            "--harness-python",
            str(self.config["harness_python"]),
            "--run-label",
            self.batch_id,
            "--timeout",
            str(self.config["timeout_seconds"]),
            "--seed",
            str(self.config["run_seed"]),
            "--temperature",
            str(self.config["temperature"]),
            "--max-iterations",
            str(self.limits["max_iterations"]),
            "--max-test-runs",
            str(self.limits["max_test_runs"]),
            "--max-tool-calls-per-iteration",
            str(self.limits["max_tool_calls_per_iteration"]),
            "--max-review-calls",
            str(self.limits["max_review_calls"]),
        ]
        if self.runner_overrides_path is not None:
            command.extend(["--runner-overrides", str(self.runner_overrides_path)])
        overrides = self.repo_root / "configs/swebench/instance-image-overrides.json"
        if overrides.is_file():
            command.extend(["--image-overrides", str(overrides)])
        if request.resume:
            command.extend(["--resume", "task"])
        return command, env

    def _run_internal(self, request: InternalRequest) -> int:
        command, env = self._internal_command(request)
        log = request.root / f"smoke-{self.batch_id}.log"
        identity = request.root / "process.json"
        with log.open("ab") as handle:
            process = subprocess.Popen(
                command,
                cwd=self.repo_root,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            _write_json(
                identity,
                {
                    "pid": process.pid,
                    "supervisor_pid": os.getpid(),
                    "started_at": _now(),
                    "status": "running",
                    "exit_code": None,
                    "log": str(log),
                    "code_sha": self.code_sha,
                },
            )
            code = process.wait()
            _write_json(
                identity,
                {
                    "pid": process.pid,
                    "supervisor_pid": os.getpid(),
                    "finished_at": _now(),
                    "status": "exited",
                    "exit_code": code,
                    "log": str(log),
                    "code_sha": self.code_sha,
                },
            )
        return code

    def _run_official(self, request: OfficialRequest) -> int:
        instance_id = request.instance["instance_id"]
        output = request.root / f"official-evaluation-{self.batch_id}.json"
        if self.config.get("official_mode") == "wsl":
            linux_root = str(self.config["linux_repo_root"]).rstrip("/")
            relative_attempt = request.root.relative_to(self.repo_root).as_posix()
            linux_attempt = f"{linux_root}/{relative_attempt}"
            command = [
                "wsl",
                "-d",
                str(self.config["wsl_distro"]),
                "--",
                "env",
                "DOCKER_HOST=unix:///mnt/wsl/docker-desktop/shared-sockets/host-services/docker.proxy.sock",
                str(self.config["linux_python"]),
                f"{linux_root}/scripts/l2cal10e_official.py",
                "--root",
                linux_attempt,
                "--instance",
                instance_id,
                "--label",
                self.batch_id,
                "--run-id",
                request.run_id,
                "--snapshot-root",
                f"{linux_root}/cache/swebench",
                "--harness-python",
                str(self.config["harness_python"]),
                "--output",
                f"{linux_attempt}/official-evaluation-{self.batch_id}.json",
            ]
        else:
            command = [
                sys.executable,
                str(self.repo_root / "scripts/l2cal10e_official.py"),
                "--root",
                str(request.root),
                "--instance",
                instance_id,
                "--label",
                self.batch_id,
                "--run-id",
                request.run_id,
                "--snapshot-root",
                str(self.repo_root / "cache/swebench"),
                "--harness-python",
                str(self.config["harness_python"]),
                "--output",
                str(output),
            ]
        log = request.root / f"official-{self.batch_id}.log"
        with log.open("wb") as handle:
            completed = subprocess.run(
                command,
                cwd=self.repo_root,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--ingest-official", action="store_true")
    args = parser.parse_args()
    try:
        batch = M3Batch(
            repo_root=ROOT,
            catalog_path=args.catalog,
            config_path=args.config,
            artifact_root=args.artifact_root,
            live=args.live,
        )
        summary = batch.ingest_official() if args.ingest_official else batch.run()
    except (BatchStopError, OSError, ValueError) as exc:
        print(f"STOP {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] in {"dry_run", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
