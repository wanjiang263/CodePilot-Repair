"""Phase 19: per-instance openings and monotonic registry spend (not executed)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codepilot.llm.dispatch_budget import DispatchBudget
from codepilot.llm.dispatch_recovery import (
    RecoveryLedgerSource,
    build_multi_source_recovery_manifest,
    build_recovery_manifest,
    instance_opening_cny,
    record_attempt_from_ledger,
    write_recovery_manifest,
)


def _ledger(path: Path, amount: float, *, status: str = "settled") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "currency": "CNY",
                "limit": 10.0,
                "accounted_or_reserved": amount,
                "frozen": False,
                "opening_balance": 0.0,
                "opening_source": None,
                "requests": [
                    {
                        "seq": 1,
                        "model": "test",
                        "reserved_cny": amount,
                        "status": status,
                        "cost_cny": amount if status == "settled" else None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(tmp_path: Path, source: Path, instance: str) -> tuple[dict, Path]:
    manifest = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(path=source, instance_id=instance)],
        run_id="next-run",
        reason="test",
        repo_root=tmp_path,
    )
    path = tmp_path / "manifest.json"
    write_recovery_manifest(manifest, path)
    return manifest, path


def test_instance_opening_is_the_manifest_share_not_the_batch_total(tmp_path: Path) -> None:
    pytest_ledger = _ledger(tmp_path / "pytest" / "dispatch.json", 1.25)
    other_ledger = _ledger(tmp_path / "other" / "dispatch.json", 2.50)
    manifest = build_multi_source_recovery_manifest(
        [
            RecoveryLedgerSource(path=pytest_ledger, instance_id="pytest-dev__pytest-11148"),
            RecoveryLedgerSource(path=other_ledger, instance_id="other__instance-1"),
        ],
        run_id="next-run",
        reason="test",
        repo_root=tmp_path,
    )
    assert manifest["total_upper_bound_cny"] == pytest.approx(3.75)
    assert instance_opening_cny(
        manifest, "pytest-dev__pytest-11148", repo_root=tmp_path
    ) == pytest.approx(1.25)
    with pytest.raises(ValueError, match="schema 1"):
        instance_opening_cny({"schema_version": 1}, "pytest-dev__pytest-11148")


def test_per_instance_dispatch_rejects_a_wrong_opening_before_any_request(
    tmp_path: Path,
) -> None:
    source = _ledger(tmp_path / "pytest" / "dispatch.json", 1.25)
    manifest, manifest_path = _manifest(tmp_path, source, "pytest-dev__pytest-11148")
    opening = instance_opening_cny(manifest, "pytest-dev__pytest-11148", repo_root=tmp_path)
    base_source = {
        "kind": "per_instance",
        "instance_id": "pytest-dev__pytest-11148",
        "instance_opening_cny": opening,
        "repo_root": str(tmp_path),
        "recovery_manifest": str(manifest_path),
        "recovery_manifest_sha256": _sha(manifest_path),
    }
    ledger = tmp_path / "new" / "dispatch.json"
    DispatchBudget(5.0, ledger, opening_balance=opening, opening_source=base_source)

    with pytest.raises(ValueError, match="instance share"):
        DispatchBudget(
            5.0,
            tmp_path / "new2" / "dispatch.json",
            opening_balance=opening + 0.01,
            opening_source=base_source,
        )
    wrong_instance = {**base_source, "instance_id": "other__instance-1"}
    with pytest.raises(ValueError, match="instance share"):
        DispatchBudget(
            5.0,
            tmp_path / "new3" / "dispatch.json",
            opening_balance=opening,
            opening_source=wrong_instance,
        )


def test_registry_refuses_to_lower_settled_spend(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "pytest" / "dispatch.json", 1.0)
    registry = tmp_path / "attempts.json"
    record_attempt_from_ledger(
        registry,
        attempt_label="a1",
        instance_id="pytest-dev__pytest-11148",
        artifact_root=ledger.parent,
        ledger=ledger,
        status="completed",
        repo_root=tmp_path,
    )
    _ledger(ledger, 0.5)
    with pytest.raises(ValueError, match="lower already-settled"):
        record_attempt_from_ledger(
            registry,
            attempt_label="a1",
            instance_id="pytest-dev__pytest-11148",
            artifact_root=ledger.parent,
            ledger=ledger,
            status="completed",
            repo_root=tmp_path,
        )


def test_schema_one_manifest_can_be_read_but_not_used_for_instance_opening(
    tmp_path: Path,
) -> None:
    source = _ledger(tmp_path / "pytest" / "dispatch.json", 1.0)
    manifest = build_recovery_manifest(source, run_id="legacy", reason="legacy", repo_root=tmp_path)
    assert manifest["schema_version"] == 1
    with pytest.raises(ValueError, match="schema 1"):
        instance_opening_cny(manifest, "pytest-dev__pytest-11148", repo_root=tmp_path)
