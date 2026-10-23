"""Explicit recovery from frozen dispatch ledgers.

An uncertain request reserves its money but never gets a provider usage. The old
ledger must stay frozen and byte-identical; recovery adds a *new* ledger whose
opening balance is the conservative upper bound of every source ledger. No code
path resets, splits, or re-prices a source ledger, and no call is retried
automatically.

Two manifest shapes are supported:

* schema 1: one frozen source ledger plus already-settled sibling ledgers;
* schema 2: several frozen and/or settled sources, each carrying its instance id.

The batch guard only needs the aggregate upper bound, but the attempt registry
also needs per-instance totals to enforce the per-task cap across retries.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codepilot.llm.dispatch_budget import DispatchBudget

__all__ = [
    "ATTEMPT_REGISTRY_SCHEMA_VERSION",
    "MULTI_SOURCE_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "RecoveryLedgerSource",
    "attempt_instance_totals",
    "build_multi_source_recovery_manifest",
    "build_recovery_manifest",
    "create_recovery_ledger",
    "load_attempt_registry",
    "load_recovery_manifest",
    "record_attempt_from_ledger",
    "validate_recovery_manifest",
    "write_attempt_registry",
    "write_recovery_manifest",
]

SCHEMA_VERSION = 1
MULTI_SOURCE_SCHEMA_VERSION = 2
ATTEMPT_REGISTRY_SCHEMA_VERSION = 1
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class RecoveryLedgerSource:
    """One ledger participating in a recovery opening.

    ``instance_id`` is recorded explicitly because a ledger itself does not carry
    it; deriving it from a directory name would silently change meaning if an
    artifact root is renamed.
    """

    path: Path
    instance_id: str


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _manifest_reference(path: Path, *, repo_root: Path) -> str:
    """Record a ledger as a repository-relative POSIX path.

    A ledger outside the repository cannot be referenced portably, so it is a
    hard error rather than an absolute path that only resolves on this host.
    """
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"dispatch ledger must live inside the repository root: {path}") from exc


def _resolve_manifest_reference(raw: object, *, repo_root: Path, allow_absolute: bool) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("recovery manifest ledger path is invalid")
    if "\\" in raw:
        raise ValueError(f"recovery manifest paths must use POSIX separators: {raw!r}")
    candidate = Path(raw)
    if candidate.is_absolute() or _DRIVE_PATH.match(raw):
        if not allow_absolute:
            raise ValueError(
                f"recovery manifest uses an absolute path; pass allow_absolute=True: {raw!r}"
            )
        return candidate.resolve()
    resolved = (repo_root / candidate).resolve()
    if not resolved.is_relative_to(repo_root.resolve()):
        raise ValueError(f"recovery manifest path escapes the repository root: {raw!r}")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _amount(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is invalid: {value!r}")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{label} is invalid: {value!r}")
    return amount


def _ledger_totals(path: Path) -> dict[str, Any]:
    """Read a dispatch ledger without modifying it."""
    target = Path(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"dispatch ledger must be a JSON object: {target}")
    if payload.get("currency") != "CNY":
        raise ValueError(f"dispatch ledger currency must be CNY: {target}")
    records = payload.get("requests")
    if not isinstance(records, list):
        raise ValueError(f"dispatch ledger has no requests list: {target}")
    settled = 0.0
    unknown = 0.0
    unknown_calls = 0
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"dispatch ledger has an invalid request record: {target}")
        if record.get("status") == "settled":
            settled += _amount(record.get("cost_cny"), "settled record cost_cny")
        else:
            unknown += _amount(record.get("reserved_cny"), "non-settled record reservation")
            unknown_calls += 1
    return {
        "settled_cny": settled,
        "unknown_reserved_cny": unknown,
        "total_upper_bound_cny": settled + unknown,
        "unknown_cost_calls": unknown_calls,
        "frozen": bool(payload.get("frozen")),
    }


def _source_entry(
    source: RecoveryLedgerSource,
    *,
    repo_root: Path,
    settled_only: bool,
) -> dict[str, Any]:
    if not isinstance(source.instance_id, str) or not source.instance_id.strip():
        raise ValueError("recovery source instance_id is invalid")
    path = Path(source.path)
    reference = _manifest_reference(path, repo_root=repo_root)
    before = _sha256_file(path)
    totals = _ledger_totals(path)
    after = _sha256_file(path)
    if before != after:
        raise ValueError(f"source dispatch ledger changed while reading it: {path}")
    calls = int(totals["unknown_cost_calls"])
    if settled_only and calls:
        raise ValueError(f"settled ledger contains non-settled records: {path}")
    if not settled_only and calls and not totals["frozen"]:
        raise ValueError(f"source ledger with unknown cost must be frozen: {path}")
    return {
        "path": reference,
        "sha256": before,
        "instance_id": source.instance_id.strip(),
        "frozen": bool(totals["frozen"]),
        "settled_cny": totals["settled_cny"],
        "unknown_reserved_cny": totals["unknown_reserved_cny"],
        "total_upper_bound_cny": totals["total_upper_bound_cny"],
        "unknown_cost_calls": calls,
    }


def _aggregate_manifest(
    source_entries: Sequence[dict[str, Any]],
    settled_entries: Sequence[dict[str, Any]],
    *,
    run_id: str,
    reason: str,
    created_at: str | None,
) -> dict[str, Any]:
    settled = math.fsum(
        [float(item["settled_cny"]) for item in source_entries]
        + [float(item["settled_cny"]) for item in settled_entries]
    )
    unknown = math.fsum(float(item["unknown_reserved_cny"]) for item in source_entries)
    unknown_calls = sum(int(item["unknown_cost_calls"]) for item in source_entries)
    total = settled + unknown
    return {
        "schema_version": MULTI_SOURCE_SCHEMA_VERSION,
        "currency": "CNY",
        "source_run_id": run_id,
        "sources": list(source_entries),
        "additional_settled_ledgers": list(settled_entries),
        "settled_cny": settled,
        "unknown_reserved_cny": unknown,
        "total_upper_bound_cny": total,
        "unknown_cost_calls": unknown_calls,
        "actual_usage_unknown_calls": unknown_calls,
        "unreconciled_cost_calls": 0,
        "reconciliation_complete": True,
        "reason": reason,
        "created_at": created_at or datetime.now(UTC).isoformat(timespec="seconds"),
    }


def build_multi_source_recovery_manifest(
    sources: Sequence[RecoveryLedgerSource],
    *,
    run_id: str,
    reason: str,
    settled_ledgers: Sequence[RecoveryLedgerSource] = (),
    created_at: str | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Aggregate one opening bound from several ledgers.

    Frozen sources may carry unknown reservations; settled siblings may not.
    Every ledger is read twice and must stay byte-identical. Listing the same
    ledger twice is rejected: an opening must never count one source twice.
    """
    root = Path(repo_root) if repo_root is not None else _repository_root()
    if not sources:
        raise ValueError("multi-source recovery needs at least one frozen source")
    source_entries = [
        _source_entry(source, repo_root=root, settled_only=False) for source in sources
    ]
    settled_entries = [
        _source_entry(source, repo_root=root, settled_only=True) for source in settled_ledgers
    ]
    references = [str(item["path"]) for item in [*source_entries, *settled_entries]]
    if len(references) != len(set(references)):
        raise ValueError("recovery manifest lists the same ledger more than once")
    return _aggregate_manifest(
        source_entries,
        settled_entries,
        run_id=run_id,
        reason=reason,
        created_at=created_at,
    )


def build_recovery_manifest(
    source_ledger: Path,
    *,
    run_id: str,
    reason: str,
    created_at: str | None = None,
    additional_settled_ledgers: Sequence[Path] = (),
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Read one frozen ledger and record its conservative upper bound.

    The source file is only read; its hash is captured before and after parsing
    so a concurrent rewrite is rejected instead of silently manifesting a
    different ledger. ``additional_settled_ledgers`` lets a batch-level manifest
    carry already-settled spend from other instances without rewriting them.
    """
    root = Path(repo_root) if repo_root is not None else _repository_root()
    path = Path(source_ledger)
    source_reference = _manifest_reference(path, repo_root=root)
    before = _sha256_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    after = _sha256_file(path)
    if before != after:
        raise ValueError("source dispatch ledger changed while building the recovery manifest")
    if payload.get("currency") != "CNY":
        raise ValueError("source dispatch ledger currency must be CNY")
    records = payload.get("requests")
    if not isinstance(records, list):
        raise ValueError("source dispatch ledger has no requests list")

    settled = 0.0
    unknown = 0.0
    unknown_calls = 0
    for record in records:
        status = record.get("status")
        if status == "settled":
            cost = record.get("cost_cny")
            if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
                raise ValueError("settled record has an invalid cost_cny")
            settled += float(cost)
        else:
            reserved = record.get("reserved_cny")
            if not isinstance(reserved, (int, float)) or not math.isfinite(reserved):
                raise ValueError("non-settled record has an invalid reservation")
            if reserved < 0:
                raise ValueError("non-settled record has a negative reservation")
            unknown += float(reserved)
            unknown_calls += 1

    additional: list[dict[str, Any]] = []
    for extra in additional_settled_ledgers:
        extra_path = Path(extra)
        extra_before = _sha256_file(extra_path)
        extra_payload = json.loads(extra_path.read_text(encoding="utf-8"))
        if extra_payload.get("currency") != "CNY":
            raise ValueError(f"additional ledger currency must be CNY: {extra_path}")
        extra_records = extra_payload.get("requests")
        if not isinstance(extra_records, list):
            raise ValueError(f"additional ledger has no requests list: {extra_path}")
        extra_settled = 0.0
        for record in extra_records:
            if record.get("status") != "settled":
                raise ValueError(f"additional ledger contains a non-settled record: {extra_path}")
            cost = record.get("cost_cny")
            if not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
                raise ValueError(f"additional ledger has an invalid cost: {extra_path}")
            extra_settled += float(cost)
        settled += extra_settled
        additional.append(
            {
                "path": _manifest_reference(extra_path, repo_root=root),
                "sha256": extra_before,
                "settled_cny": extra_settled,
            }
        )

    total = settled + unknown
    return {
        "schema_version": SCHEMA_VERSION,
        "currency": "CNY",
        "source_ledger": source_reference,
        "source_ledger_sha256": before,
        "source_run_id": run_id,
        "source_frozen": bool(payload.get("frozen")),
        "settled_cny": settled,
        "unknown_reserved_cny": unknown,
        "total_upper_bound_cny": total,
        "unknown_cost_calls": unknown_calls,
        "actual_usage_unknown_calls": unknown_calls,
        "unreconciled_cost_calls": 0,
        "reconciliation_complete": True,
        "additional_settled_ledgers": additional,
        "reason": reason,
        "created_at": created_at or datetime.now(UTC).isoformat(timespec="seconds"),
    }


def write_recovery_manifest(manifest: dict[str, Any], path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".tmp")
    staging.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    staging.replace(target)


def load_recovery_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("recovery manifest must be a JSON object")
    return payload


def _validate_multi_source_manifest(
    manifest: dict[str, Any],
    *,
    repo_root: Path,
) -> None:
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("multi-source recovery manifest has no sources")
    settled_ledgers = manifest.get("additional_settled_ledgers") or []
    if not isinstance(settled_ledgers, list):
        raise ValueError("recovery manifest additional_settled_ledgers is invalid")

    resolved_paths: list[Path] = []
    manifest_settled = 0.0
    manifest_unknown = 0.0
    manifest_unknown_calls = 0
    for item in sources:
        if not isinstance(item, dict):
            raise ValueError("recovery manifest source entry is invalid")
        path = _resolve_manifest_reference(
            item.get("path"), repo_root=repo_root, allow_absolute=False
        )
        resolved_paths.append(path)
        if not path.is_file():
            raise ValueError(f"source dispatch ledger is missing: {path}")
        instance_id = item.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError("recovery manifest source instance_id is invalid")
        before = _sha256_file(path)
        if before != item.get("sha256"):
            raise ValueError(f"source dispatch ledger hash does not match the manifest: {path}")
        totals = _ledger_totals(path)
        calls = int(totals["unknown_cost_calls"])
        if calls and not totals["frozen"]:
            raise ValueError(f"source ledger with unknown cost must be frozen: {path}")
        if calls != item.get("unknown_cost_calls"):
            raise ValueError(f"source dispatch ledger unknown-call count changed: {path}")
        settled = _amount(item.get("settled_cny"), "source settled_cny")
        unknown = _amount(item.get("unknown_reserved_cny"), "source unknown_reserved_cny")
        total = _amount(item.get("total_upper_bound_cny"), "source total_upper_bound_cny")
        if not math.isclose(settled, float(totals["settled_cny"]), rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"source dispatch ledger settled amount changed: {path}")
        if not math.isclose(
            unknown, float(totals["unknown_reserved_cny"]), rel_tol=0, abs_tol=1e-9
        ):
            raise ValueError(f"source dispatch ledger unknown reservation changed: {path}")
        if not math.isclose(total, settled + unknown, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"source upper bound does not equal settled + unknown: {path}")
        manifest_settled += settled
        manifest_unknown += unknown
        manifest_unknown_calls += calls

    for item in settled_ledgers:
        if not isinstance(item, dict):
            raise ValueError("recovery manifest additional settled ledger entry is invalid")
        path = _resolve_manifest_reference(
            item.get("path"), repo_root=repo_root, allow_absolute=False
        )
        resolved_paths.append(path)
        if not path.is_file():
            raise ValueError(f"additional dispatch ledger is missing: {path}")
        instance_id = item.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError("additional settled ledger instance_id is invalid")
        if _sha256_file(path) != item.get("sha256"):
            raise ValueError("additional dispatch ledger hash does not match the recovery manifest")
        totals = _ledger_totals(path)
        if int(totals["unknown_cost_calls"]):
            raise ValueError(f"additional ledger contains a non-settled record: {path}")
        settled = _amount(item.get("settled_cny"), "additional settled_cny")
        if not math.isclose(settled, float(totals["settled_cny"]), rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"additional dispatch ledger settled amount changed: {path}")
        manifest_settled += settled

    if len(resolved_paths) != len(set(resolved_paths)):
        raise ValueError("recovery manifest lists the same ledger more than once")

    settled = _amount(manifest.get("settled_cny"), "recovery manifest settled_cny")
    unknown = _amount(
        manifest.get("unknown_reserved_cny"), "recovery manifest unknown_reserved_cny"
    )
    total = _amount(
        manifest.get("total_upper_bound_cny"), "recovery manifest total_upper_bound_cny"
    )
    if not math.isclose(settled, manifest_settled, rel_tol=0, abs_tol=1e-9):
        raise ValueError("recovery manifest settled total does not match its sources")
    if not math.isclose(unknown, manifest_unknown, rel_tol=0, abs_tol=1e-9):
        raise ValueError("recovery manifest unknown total does not match its sources")
    if not math.isclose(total, settled + unknown, rel_tol=0, abs_tol=1e-9):
        raise ValueError("recovery manifest upper bound does not equal settled + unknown")
    if manifest.get("unknown_cost_calls") != manifest_unknown_calls:
        raise ValueError("recovery manifest unknown-call total does not match its sources")
    if manifest.get("actual_usage_unknown_calls") != manifest.get("unknown_cost_calls"):
        raise ValueError("recovery manifest unknown-call counts disagree")
    if manifest.get("unreconciled_cost_calls") != 0:
        raise ValueError("recovery manifest still has unreconciled calls")
    if manifest.get("reconciliation_complete") is not True:
        raise ValueError("recovery manifest is not reconciled")


def validate_recovery_manifest(
    manifest: dict[str, Any],
    *,
    source_ledger: Path | None = None,
    repo_root: Path | None = None,
    allow_absolute: bool = False,
) -> None:
    version = manifest.get("schema_version")
    if version == MULTI_SOURCE_SCHEMA_VERSION:
        if source_ledger is not None or allow_absolute:
            raise ValueError("multi-source manifests do not accept single-source overrides")
        _validate_multi_source_manifest(
            manifest, repo_root=Path(repo_root) if repo_root is not None else _repository_root()
        )
        return
    if version != SCHEMA_VERSION:
        raise ValueError(f"unsupported recovery manifest schema: {version!r}")
    if manifest.get("currency") != "CNY":
        raise ValueError("recovery manifest currency must be CNY")
    settled = manifest.get("settled_cny")
    unknown = manifest.get("unknown_reserved_cny")
    total = manifest.get("total_upper_bound_cny")
    for name, value in (
        ("settled_cny", settled),
        ("unknown_reserved_cny", unknown),
        ("total_upper_bound_cny", total),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"recovery manifest {name} is invalid: {value!r}")
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"recovery manifest {name} is invalid: {value!r}")
    assert isinstance(settled, (int, float))
    assert isinstance(unknown, (int, float))
    assert isinstance(total, (int, float))
    if not math.isclose(float(settled) + float(unknown), float(total), rel_tol=0, abs_tol=1e-9):
        raise ValueError("recovery manifest upper bound does not equal settled + unknown")
    if manifest.get("actual_usage_unknown_calls") != manifest.get("unknown_cost_calls"):
        raise ValueError("recovery manifest unknown-call counts disagree")
    if manifest.get("unreconciled_cost_calls") != 0:
        raise ValueError("recovery manifest still has unreconciled calls")
    if manifest.get("reconciliation_complete") is not True:
        raise ValueError("recovery manifest is not reconciled")

    root = Path(repo_root) if repo_root is not None else _repository_root()
    source = (
        Path(source_ledger)
        if source_ledger is not None
        else _resolve_manifest_reference(
            manifest.get("source_ledger"), repo_root=root, allow_absolute=allow_absolute
        )
    )
    if not source.is_file():
        raise ValueError(f"source dispatch ledger is missing: {source}")
    actual = _sha256_file(source)
    if actual != manifest.get("source_ledger_sha256"):
        raise ValueError("source dispatch ledger hash does not match the recovery manifest")

    additional = manifest.get("additional_settled_ledgers") or []
    if not isinstance(additional, list):
        raise ValueError("recovery manifest additional_settled_ledgers is invalid")
    for item in additional:
        if not isinstance(item, dict):
            raise ValueError("recovery manifest additional ledger entry is invalid")
        extra = _resolve_manifest_reference(
            item.get("path"), repo_root=root, allow_absolute=allow_absolute
        )
        if not extra.is_file():
            raise ValueError(f"additional dispatch ledger is missing: {extra}")
        if _sha256_file(extra) != item.get("sha256"):
            raise ValueError("additional dispatch ledger hash does not match the recovery manifest")


def create_recovery_ledger(
    manifest: dict[str, Any],
    *,
    path: Path,
    limit: float,
    manifest_path: Path | None = None,
    repo_root: Path | None = None,
    allow_absolute: bool = False,
) -> DispatchBudget:
    """Create a new, explicitly-opened ledger; the source ledgers are never touched."""
    validate_recovery_manifest(manifest, repo_root=repo_root, allow_absolute=allow_absolute)
    root = Path(repo_root) if repo_root is not None else _repository_root()
    manifest_reference = (
        _manifest_reference(Path(manifest_path), repo_root=root)
        if manifest_path is not None
        else None
    )
    manifest_sha = _sha256_file(Path(manifest_path)) if manifest_path is not None else None
    if manifest.get("schema_version") == MULTI_SOURCE_SCHEMA_VERSION:
        opening_source: dict[str, Any] = {
            "kind": "multi_source",
            "currency": manifest["currency"],
            "sources": [
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "instance_id": item["instance_id"],
                    "settled_cny": item["settled_cny"],
                    "unknown_reserved_cny": item["unknown_reserved_cny"],
                    "total_upper_bound_cny": item["total_upper_bound_cny"],
                }
                for item in manifest["sources"]
            ],
            "additional_settled_ledgers": [
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "instance_id": item["instance_id"],
                    "settled_cny": item["settled_cny"],
                }
                for item in manifest.get("additional_settled_ledgers") or []
            ],
            "settled_cny": manifest["settled_cny"],
            "unknown_reserved_cny": manifest["unknown_reserved_cny"],
            "total_upper_bound_cny": manifest["total_upper_bound_cny"],
            "reason": manifest.get("reason"),
            "created_at": manifest.get("created_at"),
            "recovery_manifest": manifest_reference,
            "recovery_manifest_sha256": manifest_sha,
        }
    else:
        opening_source = {
            key: manifest[key]
            for key in (
                "source_ledger",
                "source_ledger_sha256",
                "source_run_id",
                "settled_cny",
                "unknown_reserved_cny",
                "total_upper_bound_cny",
                "currency",
                "reason",
                "created_at",
            )
        }
        opening_source["recovery_manifest"] = manifest_reference
        opening_source["recovery_manifest_sha256"] = manifest_sha
    return DispatchBudget(
        limit,
        Path(path),
        opening_balance=float(manifest["total_upper_bound_cny"]),
        opening_source=opening_source,
    )


# ---------------------------------------------------------------------------
# Attempt registry
# ---------------------------------------------------------------------------


def _registry_path(raw: object, *, repo_root: Path, label: str) -> Path:
    try:
        return _resolve_manifest_reference(raw, repo_root=repo_root, allow_absolute=False)
    except ValueError as exc:
        raise ValueError(f"attempt registry {label} is invalid: {exc}") from exc


def _validate_attempt_entry(entry: dict[str, Any], *, repo_root: Path) -> dict[str, Any]:
    for key in ("attempt_label", "instance_id", "artifact_root", "ledger_path", "status"):
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"attempt registry field {key} is invalid")
    settled = _amount(entry.get("settled_cny"), "attempt settled_cny")
    unknown = _amount(entry.get("unknown_reserved_cny"), "attempt unknown_reserved_cny")
    total = _amount(entry.get("total_upper_bound_cny"), "attempt total_upper_bound_cny")
    if not math.isclose(total, settled + unknown, rel_tol=0, abs_tol=1e-9):
        raise ValueError("attempt upper bound does not equal settled + unknown")
    artifact_root = _registry_path(entry.get("artifact_root"), repo_root=repo_root, label="root")
    ledger_path = _registry_path(entry.get("ledger_path"), repo_root=repo_root, label="ledger")
    if not ledger_path.is_relative_to(artifact_root):
        raise ValueError("attempt ledger must live inside its artifact root")
    present = entry.get("ledger_present")
    if present:
        if not ledger_path.is_file():
            raise ValueError(f"attempt ledger is missing: {ledger_path}")
        expected = entry.get("ledger_sha256")
        if not isinstance(expected, str) or not expected:
            raise ValueError("attempt ledger hash is missing")
        if _sha256_file(ledger_path) != expected:
            raise ValueError(f"attempt ledger hash changed: {ledger_path}")
        totals = _ledger_totals(ledger_path)
        if not math.isclose(settled, float(totals["settled_cny"]), rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"attempt ledger settled amount changed: {ledger_path}")
        if not math.isclose(
            unknown, float(totals["unknown_reserved_cny"]), rel_tol=0, abs_tol=1e-9
        ):
            raise ValueError(f"attempt ledger unknown reservation changed: {ledger_path}")
    else:
        if settled or unknown:
            raise ValueError("attempt without a ledger cannot carry spend")
        if entry.get("ledger_sha256") not in (None, ""):
            raise ValueError("attempt without a ledger cannot carry a ledger hash")
    return {
        "attempt_label": entry["attempt_label"].strip(),
        "instance_id": entry["instance_id"].strip(),
        "artifact_root": entry["artifact_root"],
        "ledger_path": entry["ledger_path"],
        "ledger_sha256": entry.get("ledger_sha256"),
        "ledger_present": bool(present),
        "status": entry["status"].strip(),
        "settled_cny": settled,
        "unknown_reserved_cny": unknown,
        "total_upper_bound_cny": total,
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


def load_attempt_registry(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("attempt registry must be a JSON object")
    if payload.get("schema_version") != ATTEMPT_REGISTRY_SCHEMA_VERSION:
        raise ValueError(f"unsupported attempt registry schema: {payload.get('schema_version')!r}")
    if payload.get("currency") != "CNY":
        raise ValueError("attempt registry currency must be CNY")
    attempts = payload.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("attempt registry has no attempts list")
    return payload


def _validated_attempt_entries(
    registry: dict[str, Any], *, repo_root: Path
) -> list[dict[str, Any]]:
    attempts = registry.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("attempt registry has no attempts list")
    entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    ledgers: set[Path] = set()
    for raw in attempts:
        if not isinstance(raw, dict):
            raise ValueError("attempt registry entry is invalid")
        entry = _validate_attempt_entry(raw, repo_root=repo_root)
        identity = (entry["attempt_label"], entry["instance_id"])
        if identity in identities:
            raise ValueError("attempt registry contains a duplicate attempt label")
        identities.add(identity)
        if entry["ledger_present"]:
            ledger = Path(entry["ledger_path"])
            if ledger in ledgers:
                raise ValueError("attempt registry lists the same ledger more than once")
            ledgers.add(ledger)
        entries.append(entry)
    return entries


def attempt_instance_totals(
    registry: dict[str, Any], *, repo_root: Path | None = None
) -> dict[str, float]:
    """Sum the conservative upper bound per instance; hashes are re-verified."""
    if registry.get("schema_version") != ATTEMPT_REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported attempt registry schema")
    if registry.get("currency") != "CNY":
        raise ValueError("attempt registry currency must be CNY")
    root = Path(repo_root) if repo_root is not None else _repository_root()
    totals: dict[str, float] = {}
    for entry in _validated_attempt_entries(registry, repo_root=root):
        instance_id = str(entry["instance_id"])
        totals[instance_id] = totals.get(instance_id, 0.0) + float(entry["total_upper_bound_cny"])
    return totals


def write_attempt_registry(
    registry: dict[str, Any], path: Path, *, repo_root: Path | None = None
) -> None:
    if registry.get("schema_version") != ATTEMPT_REGISTRY_SCHEMA_VERSION:
        raise ValueError("unsupported attempt registry schema")
    if registry.get("currency") != "CNY":
        raise ValueError("attempt registry currency must be CNY")
    if repo_root is not None:
        _validated_attempt_entries(registry, repo_root=Path(repo_root))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".tmp")
    staging.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    staging.replace(target)


def record_attempt_from_ledger(
    registry_path: Path,
    *,
    attempt_label: str,
    instance_id: str,
    artifact_root: Path,
    ledger: Path,
    status: str,
    repo_root: Path | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Insert or replace one attempt in the registry after a run.

    A missing ledger is recorded as ``ledger_present=False`` with zero spend,
    which is how pre-dispatch failures stay visible without inventing cost.
    """
    if not attempt_label.strip() or not instance_id.strip() or not status.strip():
        raise ValueError("attempt label, instance and status are required")
    root = Path(repo_root) if repo_root is not None else _repository_root()
    target = Path(registry_path)
    if target.is_file():
        registry = load_attempt_registry(target)
    else:
        registry = {
            "schema_version": ATTEMPT_REGISTRY_SCHEMA_VERSION,
            "currency": "CNY",
            "attempts": [],
        }
    artifact_reference = _manifest_reference(Path(artifact_root), repo_root=root)
    ledger_reference = _manifest_reference(Path(ledger), repo_root=root)
    existing = next(
        (
            item
            for item in registry.get("attempts", [])
            if isinstance(item, dict)
            and item.get("attempt_label") == attempt_label
            and item.get("instance_id") == instance_id
        ),
        None,
    )
    if ledger.is_file():
        before = _sha256_file(ledger)
        totals = _ledger_totals(ledger)
        after = _sha256_file(ledger)
        if before != after:
            raise ValueError("attempt ledger changed while recording it")
        present = True
        ledger_sha: str | None = before
    else:
        # A missing ledger is only acceptable for a brand-new pre-dispatch
        # attempt. If this label previously carried spend, replacing it with an
        # all-zero row would silently erase money from the registry.
        previous_total = (existing or {}).get("total_upper_bound_cny")
        if (
            (existing or {}).get("ledger_present")
            and isinstance(previous_total, (int, float))
            and not isinstance(previous_total, bool)
            and float(previous_total) > 0
        ):
            raise ValueError(
                "attempt ledger disappeared; refusing to reset previously recorded spend to zero: "
                f"{ledger_reference}"
            )
        totals = {
            "settled_cny": 0.0,
            "unknown_reserved_cny": 0.0,
            "total_upper_bound_cny": 0.0,
        }
        present = False
        ledger_sha = None
    now = updated_at or datetime.now(UTC).isoformat(timespec="seconds")
    attempts = [
        item
        for item in registry.get("attempts", [])
        if not (
            isinstance(item, dict)
            and item.get("attempt_label") == attempt_label
            and item.get("instance_id") == instance_id
        )
    ]
    entry = {
        "attempt_label": attempt_label,
        "instance_id": instance_id,
        "artifact_root": artifact_reference,
        "ledger_path": ledger_reference,
        "ledger_sha256": ledger_sha,
        "ledger_present": present,
        "status": status,
        "settled_cny": float(totals["settled_cny"]),
        "unknown_reserved_cny": float(totals["unknown_reserved_cny"]),
        "total_upper_bound_cny": float(totals["total_upper_bound_cny"]),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }
    attempts.append(entry)
    attempts.sort(key=lambda item: (str(item.get("instance_id")), str(item.get("attempt_label"))))
    registry["attempts"] = attempts
    registry["updated_at"] = now
    _validated_attempt_entries(registry, repo_root=root)
    write_attempt_registry(registry, target, repo_root=root)
    return entry
