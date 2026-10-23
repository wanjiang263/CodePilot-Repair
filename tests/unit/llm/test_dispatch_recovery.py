"""P3: explicit recovery from an unknown-cost dispatch ledger."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

from codepilot.llm.config import ModelPrice, ModelSpec
from codepilot.llm.dispatch_budget import DispatchBudget, DispatchBudgetStoppedError
from codepilot.llm.dispatch_recovery import (
    MULTI_SOURCE_SCHEMA_VERSION,
    RecoveryLedgerSource,
    build_multi_source_recovery_manifest,
    build_recovery_manifest,
    create_recovery_ledger,
    load_recovery_manifest,
    record_attempt_from_ledger,
    validate_recovery_manifest,
    write_recovery_manifest,
)

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import batch_budget  # noqa: E402


def _model() -> ModelSpec:
    return ModelSpec(
        alias="test",
        provider="test",
        model="test",
        context_window=1_000,
        price=ModelPrice(input_per_mtok=0.8, output_per_mtok=2.8),
    )


def _response() -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test"),
        json={
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
        },
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frozen_ledger(tmp_path: Path, name: str = "dispatch-ledger-l2cal10c.json") -> Path:
    return _frozen_ledger_at(tmp_path / "xarray", name)


def _frozen_ledger_at(directory: Path, name: str = "dispatch-ledger-l2cal10c.json") -> Path:
    path = directory / name
    guard = DispatchBudget(3.0, path)
    guard.send(_model(), {"max_tokens": 4096}, _response)

    def timeout() -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(DispatchBudgetStoppedError):
        guard.send(_model(), {"max_tokens": 4096}, timeout)
    with pytest.raises(DispatchBudgetStoppedError):
        guard.send(_model(), {"max_tokens": 4096}, _response)
    return path


def _settled_ledger(path: Path, amount: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "currency": "CNY",
        "limit": 3.0,
        "accounted_or_reserved": amount,
        "frozen": False,
        "opening_balance": 0.0,
        "opening_source": None,
        "requests": [
            {
                "seq": 1,
                "model": "test",
                "reserved_cny": amount,
                "status": "settled",
                "cost_cny": amount,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _budget_args(tmp_path: Path, base: Path, manifest_path: Path, **extra: str) -> list[str]:
    args = [
        "--base",
        str(base),
        "--ledger",
        "dispatch-ledger-l2cal10e.json",
        "--batch-cap",
        "30.0",
        "--per-instance",
        "3.0",
        "--repo-root",
        str(tmp_path),
        "--opening-reconciliation",
        str(manifest_path),
    ]
    for key, value in extra.items():
        args.extend([f"--{key.replace('_', '-')}", value])
    return args


def test_record_refuses_to_zero_spend_when_an_existing_ledger_disappears(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "pytest" / "dispatch-ledger-l2cal10e.json"
    ledger.parent.mkdir(parents=True)
    guard = DispatchBudget(3.0, ledger)
    guard.send(_model(), {"max_tokens": 4096}, _response)
    registry = tmp_path / "attempts.json"
    entry = record_attempt_from_ledger(
        registry,
        attempt_label="l2cal10e-a1",
        instance_id="pytest-dev__pytest-11148",
        artifact_root=ledger.parent,
        ledger=ledger,
        status="completed",
        repo_root=tmp_path,
    )
    assert entry["total_upper_bound_cny"] > 0

    ledger.unlink()
    with pytest.raises(ValueError, match="refusing to reset"):
        record_attempt_from_ledger(
            registry,
            attempt_label="l2cal10e-a1",
            instance_id="pytest-dev__pytest-11148",
            artifact_root=ledger.parent,
            ledger=ledger,
            status="completed",
            repo_root=tmp_path,
        )


def test_unknown_cost_is_frozen_and_recovery_keeps_source_hash(tmp_path: Path) -> None:
    source = _frozen_ledger(tmp_path)
    before = _sha(source)
    payload = json.loads(source.read_text(encoding="utf-8"))
    assert payload["frozen"] is True
    assert payload["requests"][-1]["status"] == "unknown_frozen"
    assert payload["requests"][-1]["cost_cny"] is None

    manifest = build_recovery_manifest(
        source,
        run_id="l2cal10c-xarray",
        reason="ReadTimeout left usage unknown",
        repo_root=tmp_path,
    )
    manifest_path = tmp_path / "recovery-manifest.json"
    write_recovery_manifest(manifest, manifest_path)

    assert manifest["source_ledger"] == "xarray/dispatch-ledger-l2cal10c.json"
    assert "\\" not in manifest["source_ledger"]
    assert not Path(manifest["source_ledger"]).is_absolute()
    assert manifest["settled_cny"] > 0
    assert manifest["unknown_reserved_cny"] == pytest.approx(
        payload["requests"][-1]["reserved_cny"]
    )
    assert manifest["total_upper_bound_cny"] == pytest.approx(
        manifest["settled_cny"] + manifest["unknown_reserved_cny"]
    )
    assert manifest["actual_usage_unknown_calls"] == 1
    assert manifest["unreconciled_cost_calls"] == 0
    assert manifest["reconciliation_complete"] is True

    recovery_path = tmp_path / "pylint" / "dispatch-ledger-l2cal10d.json"
    recovery = create_recovery_ledger(
        load_recovery_manifest(manifest_path),
        path=recovery_path,
        limit=30.0,
        manifest_path=manifest_path,
        repo_root=tmp_path,
    )
    assert recovery.total == pytest.approx(manifest["total_upper_bound_cny"])
    ledger = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert ledger["opening_balance"] == pytest.approx(manifest["total_upper_bound_cny"])
    assert ledger["opening_source"]["source_ledger_sha256"] == before
    assert ledger["opening_source"]["recovery_manifest_sha256"] == _sha(manifest_path)
    assert ledger["frozen"] is False

    recovery.send(_model(), {"max_tokens": 4096}, _response)
    assert _sha(source) == before, "source ledger must remain byte-identical"
    assert len(recovery.records) == 1


def test_batch_manifest_can_carry_other_settled_ledgers_without_counting_twice(
    tmp_path: Path,
) -> None:
    source = _frozen_ledger(tmp_path)
    other_path = tmp_path / "django" / "dispatch-ledger-l2cal10c.json"
    other = DispatchBudget(3.0, other_path)
    other.send(_model(), {"max_tokens": 4096}, _response)
    other_cost = float(other.records[0]["cost_cny"])

    manifest = build_recovery_manifest(
        source,
        run_id="batch",
        reason="timeout plus settled siblings",
        additional_settled_ledgers=[other_path],
        repo_root=tmp_path,
    )

    assert manifest["settled_cny"] == pytest.approx(
        float(json.loads(source.read_text(encoding="utf-8"))["requests"][0]["cost_cny"])
        + other_cost
    )
    assert manifest["unknown_reserved_cny"] > 0
    assert manifest["total_upper_bound_cny"] == pytest.approx(
        manifest["settled_cny"] + manifest["unknown_reserved_cny"]
    )
    assert len(manifest["additional_settled_ledgers"]) == 1
    assert manifest["additional_settled_ledgers"][0]["path"] == (
        "django/dispatch-ledger-l2cal10c.json"
    )
    validate_recovery_manifest(manifest, repo_root=tmp_path)


def test_batch_guard_adds_opening_once_and_requires_reconciliation(tmp_path: Path) -> None:
    source = _frozen_ledger(tmp_path)
    manifest = build_recovery_manifest(
        source, run_id="l2cal10c-xarray", reason="timeout", repo_root=tmp_path
    )
    manifest_path = tmp_path / "recovery-manifest.json"
    write_recovery_manifest(manifest, manifest_path)
    new_path = tmp_path / "pylint" / "dispatch-ledger-l2cal10d.json"
    recovery = create_recovery_ledger(
        manifest, path=new_path, limit=30.0, manifest_path=manifest_path, repo_root=tmp_path
    )
    recovery.send(_model(), {"max_tokens": 4096}, _response)
    added = float(recovery.records[0]["cost_cny"])

    base_args = [
        "--base",
        str(tmp_path),
        "--ledger",
        "dispatch-ledger-l2cal10d.json",
        "--batch-cap",
        "30.0",
        "--repo-root",
        str(tmp_path),
    ]
    code, message = batch_budget.evaluate(
        batch_budget._parser().parse_args(
            [*base_args, "--opening-reconciliation", str(manifest_path)]
        )
    )
    assert code == 0, message
    expected_total = float(manifest["total_upper_bound_cny"]) + added
    assert f"total={expected_total:.6f}" in message
    assert f"opening={float(manifest['total_upper_bound_cny']):.6f}" in message

    code, message = batch_budget.evaluate(batch_budget._parser().parse_args(base_args))
    assert code == 9, message

    code, message = batch_budget.evaluate(
        batch_budget._parser().parse_args([*base_args, "--opening-cny", "1.868388"])
    )
    assert code == 3, message

    tampered = dict(manifest)
    tampered["total_upper_bound_cny"] = 0.1
    tampered_path = tmp_path / "tampered-manifest.json"
    write_recovery_manifest(tampered, tampered_path)
    code, message = batch_budget.evaluate(
        batch_budget._parser().parse_args(
            [*base_args, "--opening-reconciliation", str(tampered_path)]
        )
    )
    assert code == 3, message


def test_manifest_paths_fail_closed_on_backslash_absolute_or_escaping_paths(
    tmp_path: Path,
) -> None:
    source = _frozen_ledger(tmp_path)
    manifest = build_recovery_manifest(
        source, run_id="portable", reason="timeout", repo_root=tmp_path
    )
    validate_recovery_manifest(manifest, repo_root=tmp_path)

    backslash = dict(manifest)
    backslash["source_ledger"] = manifest["source_ledger"].replace("/", "\\")
    with pytest.raises(ValueError, match="POSIX"):
        validate_recovery_manifest(backslash, repo_root=tmp_path)

    escaping = dict(manifest)
    escaping["source_ledger"] = "../outside/dispatch-ledger.json"
    with pytest.raises(ValueError, match="escapes"):
        validate_recovery_manifest(escaping, repo_root=tmp_path)

    absolute = dict(manifest)
    absolute["source_ledger"] = source.resolve().as_posix()
    with pytest.raises(ValueError, match="absolute"):
        validate_recovery_manifest(absolute, repo_root=tmp_path)
    validate_recovery_manifest(absolute, repo_root=tmp_path, allow_absolute=True)

    tampered = dict(manifest)
    tampered["source_ledger_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        validate_recovery_manifest(tampered, repo_root=tmp_path)


def test_multi_source_manifest_sums_two_frozen_unknowns(tmp_path: Path) -> None:
    first = _frozen_ledger_at(tmp_path / "first")
    second = _frozen_ledger_at(tmp_path / "second")
    before = {path: path.read_bytes() for path in (first, second)}
    first_payload = json.loads(first.read_text(encoding="utf-8"))
    second_payload = json.loads(second.read_text(encoding="utf-8"))
    first_cost = float(first_payload["requests"][0]["cost_cny"])
    second_cost = float(second_payload["requests"][0]["cost_cny"])
    first_unknown = float(first_payload["requests"][-1]["reserved_cny"])
    second_unknown = float(second_payload["requests"][-1]["reserved_cny"])

    manifest = build_multi_source_recovery_manifest(
        [
            RecoveryLedgerSource(first, "first-inst"),
            RecoveryLedgerSource(second, "second-inst"),
        ],
        run_id="l2cal10e",
        reason="two frozen timeouts",
        repo_root=tmp_path,
    )

    assert manifest["schema_version"] == MULTI_SOURCE_SCHEMA_VERSION
    assert manifest["unknown_cost_calls"] == 2
    assert manifest["settled_cny"] == pytest.approx(first_cost + second_cost)
    assert manifest["unknown_reserved_cny"] == pytest.approx(first_unknown + second_unknown)
    assert manifest["total_upper_bound_cny"] == pytest.approx(
        first_cost + second_cost + first_unknown + second_unknown
    )
    assert [item["instance_id"] for item in manifest["sources"]] == [
        "first-inst",
        "second-inst",
    ]
    assert {item["path"] for item in manifest["sources"]} == {
        "first/dispatch-ledger-l2cal10c.json",
        "second/dispatch-ledger-l2cal10c.json",
    }
    validate_recovery_manifest(manifest, repo_root=tmp_path)
    assert all(path.read_bytes() == content for path, content in before.items())


def test_multi_source_manifest_rejects_changed_source(tmp_path: Path) -> None:
    first = _frozen_ledger_at(tmp_path / "first")
    second = _frozen_ledger_at(tmp_path / "second")
    manifest = build_multi_source_recovery_manifest(
        [
            RecoveryLedgerSource(first, "first-inst"),
            RecoveryLedgerSource(second, "second-inst"),
        ],
        run_id="l2cal10e",
        reason="one frozen timeout",
        repo_root=tmp_path,
    )
    validate_recovery_manifest(manifest, repo_root=tmp_path)

    second.write_bytes(second.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="hash"):
        validate_recovery_manifest(manifest, repo_root=tmp_path)


def test_attempt_registry_enforces_per_instance_cap(tmp_path: Path) -> None:
    tiny = _frozen_ledger_at(tmp_path / "tiny")
    base = tmp_path / "batch"
    registry = tmp_path / "attempts.json"
    attempt1_root = base / "inst-a1"
    attempt1 = _settled_ledger(attempt1_root / "dispatch-ledger-l2cal10e-a1.json", 2.9)
    record_attempt_from_ledger(
        registry,
        attempt_label="l2cal10e-a1",
        instance_id="inst",
        artifact_root=attempt1_root,
        ledger=attempt1,
        status="done",
        repo_root=tmp_path,
    )
    before = attempt1.read_bytes()
    manifest = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(tiny, "other-inst")],
        run_id="l2cal10e-a2",
        reason="retry after a settled attempt",
        settled_ledgers=[RecoveryLedgerSource(attempt1, "inst")],
        repo_root=tmp_path,
    )
    manifest_path = tmp_path / "manifest-a2.json"
    write_recovery_manifest(manifest, manifest_path)

    args = batch_budget._parser().parse_args(
        _budget_args(
            tmp_path,
            base,
            manifest_path,
            instance="inst",
            attempt_registry=str(registry),
        )
    )
    code, message = batch_budget.evaluate(args)
    assert code == 0, message
    assert "allowed=0.100000" in message
    assert "instance_total=2.900000" in message
    assert attempt1.read_bytes() == before

    attempt2_root = base / "inst-b2"
    attempt2 = _settled_ledger(attempt2_root / "dispatch-ledger-l2cal10e-a2.json", 0.08)
    record_attempt_from_ledger(
        registry,
        attempt_label="l2cal10e-a2",
        instance_id="inst",
        artifact_root=attempt2_root,
        ledger=attempt2,
        status="done",
        repo_root=tmp_path,
    )
    manifest2 = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(tiny, "other-inst")],
        run_id="l2cal10e-a3",
        reason="second retry",
        settled_ledgers=[
            RecoveryLedgerSource(attempt1, "inst"),
            RecoveryLedgerSource(attempt2, "inst"),
        ],
        repo_root=tmp_path,
    )
    manifest2_path = tmp_path / "manifest-a3.json"
    write_recovery_manifest(manifest2, manifest2_path)
    args2 = batch_budget._parser().parse_args(
        _budget_args(
            tmp_path,
            base,
            manifest2_path,
            instance="inst",
            attempt_registry=str(registry),
        )
    )
    code, message = batch_budget.evaluate(args2)
    assert code == 7, message
    assert "per-instance budget exhausted" in message
    assert "total=2.980000" in message


def test_batch_guard_counts_ledger_opening_once(tmp_path: Path) -> None:
    source = _frozen_ledger_at(tmp_path / "source")
    before = source.read_bytes()
    manifest = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(source, "source-inst")],
        run_id="l2cal10e",
        reason="opening is counted at the batch level",
        repo_root=tmp_path,
    )
    manifest_path = tmp_path / "manifest.json"
    write_recovery_manifest(manifest, manifest_path)
    base = tmp_path / "batch"
    ledger_path = base / "inst" / "dispatch-ledger-l2cal10e.json"
    ledger = create_recovery_ledger(
        manifest,
        path=ledger_path,
        limit=30.0,
        manifest_path=manifest_path,
        repo_root=tmp_path,
    )
    ledger.send(_model(), {"max_tokens": 4096}, _response)
    cost = float(ledger.records[0]["cost_cny"])

    args = batch_budget._parser().parse_args(
        _budget_args(tmp_path, base, manifest_path, instance="inst")
    )
    code, message = batch_budget.evaluate(args)

    expected_total = float(manifest["total_upper_bound_cny"]) + cost
    assert code == 0, message
    assert f"opening={float(manifest['total_upper_bound_cny']):.6f}" in message
    assert f"total={expected_total:.6f}" in message
    assert source.read_bytes() == before


def test_current_attempt_ledger_is_not_compared_with_the_opening(tmp_path: Path) -> None:
    tiny = _frozen_ledger_at(tmp_path / "tiny")
    base = tmp_path / "batch"
    registry = tmp_path / "attempts.json"
    prior_root = base / "inst-a1"
    prior = _settled_ledger(prior_root / "dispatch-ledger-l2cal10e-a1.json", 1.0)
    record_attempt_from_ledger(
        registry,
        attempt_label="l2cal10e-a1",
        instance_id="inst",
        artifact_root=prior_root,
        ledger=prior,
        status="completed",
        repo_root=tmp_path,
    )
    manifest = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(tiny, "other-inst")],
        run_id="l2cal10e",
        reason="prior attempt is already in the opening",
        settled_ledgers=[RecoveryLedgerSource(prior, "inst")],
        repo_root=tmp_path,
    )
    manifest_path = tmp_path / "manifest.json"
    write_recovery_manifest(manifest, manifest_path)

    current_root = base / "inst"
    current = _settled_ledger(current_root / "dispatch-ledger-l2cal10e.json", 0.05)
    record_attempt_from_ledger(
        registry,
        attempt_label="l2cal10e",
        instance_id="inst",
        artifact_root=current_root,
        ledger=current,
        status="completed",
        repo_root=tmp_path,
    )
    args = batch_budget._parser().parse_args(
        _budget_args(
            tmp_path,
            base,
            manifest_path,
            instance="inst",
            attempt_registry=str(registry),
        )
    )
    code, message = batch_budget.evaluate(args)
    assert code == 0, message
    assert "instance_prior=1.000000" in message
    assert "instance_current=0.050000" in message
    assert "instance_total=1.050000" in message
