"""Pre-dispatch budget guard for a resumed SWE-bench batch.

The guard scans only ledgers carrying the *new* batch label, adds the explicit
opening upper bound from a validated recovery manifest exactly once, and fails
closed on frozen/unsettled ledgers or a missing/tampered reconciliation.

Per-instance accounting is separate from the batch opening: the opening already
contains every earlier attempt (so the batch total counts it once), while the
attempt registry is the only place that knows which earlier attempt belongs to
which instance. Adding the registry total to the batch total would double count
retries, so it is used for the per-task cap only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(ROOT / "src"))

from codepilot.llm.dispatch_recovery import (  # noqa: E402
    MULTI_SOURCE_SCHEMA_VERSION,
    attempt_instance_totals,
    instance_opening_cny,
    load_attempt_registry,
    load_recovery_manifest,
    validate_recovery_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--ledger", default="dispatch-ledger-l2cal10d.json")
    parser.add_argument("--batch-cap", type=float, default=30.0)
    parser.add_argument("--per-instance", type=float, default=3.0)
    parser.add_argument("--min", dest="minimum", type=float, default=0.05)
    parser.add_argument("--opening-reconciliation", type=Path, default=None)
    parser.add_argument("--opening-cny", type=float, default=None)
    parser.add_argument("--instance", default=None)
    parser.add_argument("--attempt-registry", type=Path, default=None)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="root used to resolve relative ledger paths in the recovery manifest",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _opening(
    args: argparse.Namespace,
) -> tuple[float, dict[str, Any] | None, str | None, set[Path]]:
    if args.opening_reconciliation is None:
        if args.opening_cny is not None:
            raise ValueError("--opening-cny requires a validated --opening-reconciliation")
        return 0.0, None, None, set()
    manifest_path = Path(args.opening_reconciliation)
    manifest = load_recovery_manifest(manifest_path)
    validate_recovery_manifest(manifest, repo_root=args.repo_root)
    amount = float(manifest["total_upper_bound_cny"])
    if args.opening_cny is not None and not math.isclose(
        args.opening_cny, amount, rel_tol=0, abs_tol=1e-9
    ):
        raise ValueError(
            f"--opening-cny {args.opening_cny} does not match manifest upper bound {amount}"
        )
    root = Path(args.repo_root) if args.repo_root is not None else ROOT
    return amount, manifest, _sha256(manifest_path), _manifest_ledgers(manifest, root)


def _registry(
    args: argparse.Namespace,
    opening_ledgers: set[Path],
) -> tuple[dict[str, Any] | None, dict[str, float], set[Path]]:
    if args.attempt_registry is None:
        return None, {}, set()
    if not Path(args.attempt_registry).is_file():
        raise ValueError(f"attempt registry is missing: {args.attempt_registry}")
    registry = load_attempt_registry(args.attempt_registry)
    # Validate every entry (including the current attempt) first; then only sum
    # the attempts whose ledgers are already inside the opening. Current-label
    # ledgers are counted by the scan below, never by the registry.
    attempt_instance_totals(registry, repo_root=args.repo_root)
    root = Path(args.repo_root) if args.repo_root is not None else ROOT
    totals: dict[str, float] = {}
    ledgers: set[Path] = set()
    for item in registry.get("attempts") or []:
        if not isinstance(item, dict) or not item.get("ledger_present", False):
            continue
        raw = item.get("ledger_path")
        if not isinstance(raw, str) or not raw:
            continue
        ledger = (root / raw).resolve()
        ledgers.add(ledger)
        if ledger not in opening_ledgers:
            continue
        instance = str(item.get("instance_id") or "")
        totals[instance] = totals.get(instance, 0.0) + float(
            item.get("total_upper_bound_cny") or 0.0
        )
    return registry, totals, ledgers


def _old_frozen_ledgers(base: Path, new_label: str) -> list[Path]:
    frozen: list[Path] = []
    for path in sorted(base.glob("*/dispatch-ledger-*.json")):
        if path.name == new_label:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            frozen.append(path)
            continue
        if payload.get("frozen") or any(
            record.get("status") != "settled" for record in payload.get("requests") or []
        ):
            frozen.append(path)
    return frozen


def _earlier_ledgers(base: Path, new_label: str) -> list[Path]:
    return [
        path for path in sorted(base.glob("*/dispatch-ledger-*.json")) if path.name != new_label
    ]


def _instance_ledgers(base: Path, instance: str, new_label: str) -> list[Path]:
    found: list[Path] = []
    for path in sorted(base.glob("*/dispatch-ledger-*.json")):
        if path.name == new_label:
            continue
        parent = path.parent.name
        if parent == instance or parent.startswith(f"{instance}-"):
            found.append(path)
    return found


def _belongs_to_instance(path: Path, instance: str) -> bool:
    parent = path.parent.name
    return parent == instance or parent.startswith(f"{instance}-")


def _ledger_net(
    path: Path,
    *,
    manifest: dict[str, Any] | None,
    manifest_sha: str | None,
) -> tuple[float, int, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return 0.0, 3, f"STOP ledger unreadable: {path}: {exc}"
    if payload.get("frozen"):
        return 0.0, 4, f"STOP ledger frozen: {path}"
    records = payload.get("requests") or []
    unsettled = [record for record in records if record.get("status") != "settled"]
    if unsettled:
        return 0.0, 5, f"STOP unsettled requests: {path}: {len(unsettled)}"
    amount = payload.get("accounted_or_reserved")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return 0.0, 6, f"STOP ledger amount invalid: {path}: {amount!r}"
    if not math.isfinite(amount) or amount < 0:
        return 0.0, 6, f"STOP ledger amount invalid: {path}: {amount!r}"
    ledger_opening = payload.get("opening_balance", 0.0)
    if (
        not isinstance(ledger_opening, (int, float))
        or isinstance(ledger_opening, bool)
        or not math.isfinite(ledger_opening)
        or ledger_opening < 0
    ):
        return 0.0, 6, f"STOP ledger opening invalid: {path}: {ledger_opening!r}"
    if ledger_opening > 0:
        source = payload.get("opening_source") or {}
        if not isinstance(source, dict) or manifest is None:
            return 0.0, 8, f"STOP ledger has an opening but no reconciliation: {path}"
        if manifest.get("schema_version") == MULTI_SOURCE_SCHEMA_VERSION:
            if source.get("recovery_manifest_sha256") != manifest_sha:
                return 0.0, 8, f"STOP ledger opening source does not match manifest: {path}"
        elif source.get("source_ledger_sha256") != manifest.get("source_ledger_sha256"):
            return 0.0, 8, f"STOP ledger opening source does not match manifest: {path}"
        if float(amount) < float(ledger_opening):
            return 0.0, 6, f"STOP ledger spend is negative: {path}"
    return float(amount) - float(ledger_opening), 0, None


def _manifest_ledgers(manifest: dict[str, Any], root: Path) -> set[Path]:
    raw: list[str] = []
    sources = manifest.get("sources")
    if isinstance(sources, list):
        raw.extend(item["path"] for item in sources if isinstance(item, dict) and "path" in item)
    additional = manifest.get("additional_settled_ledgers")
    if isinstance(additional, list):
        raw.extend(item["path"] for item in additional if isinstance(item, dict) and "path" in item)
    source = manifest.get("source_ledger")
    if isinstance(source, str):
        raw.append(source)
    return {(root / item).resolve() for item in raw if isinstance(item, str)}


def _opening_instance(
    manifest: dict[str, Any] | None,
    instance: str,
    *,
    repo_root: Path | None = None,
) -> float | None:
    """Instance-bound share of a validated schema-2 opening.

    A schema-2 opening already contains every earlier attempt (including the ones
    in the registry). Counting the registry on top would double count retries.
    Schema 1 cannot authorize a per-instance dispatch because it carries no
    instance attribution; it remains readable for historical batch accounting.
    """
    if manifest is None:
        return None
    if manifest.get("schema_version") != MULTI_SOURCE_SCHEMA_VERSION:
        raise ValueError(
            "schema 1 recovery manifests have no instance attribution; "
            "a new per-instance dispatch requires schema 2"
        )
    return instance_opening_cny(manifest, instance, repo_root=repo_root)


def evaluate(args: argparse.Namespace) -> tuple[int, str]:
    try:
        opening, manifest, manifest_sha, opening_ledgers = _opening(args)
        registry, registry_totals, registry_ledgers = _registry(args, opening_ledgers)
    except (OSError, ValueError) as exc:
        return 3, f"STOP invalid opening reconciliation: {exc}"

    if manifest is None and _old_frozen_ledgers(args.base, args.ledger):
        return 9, (
            "STOP frozen/unsettled historical ledgers exist but no --opening-reconciliation "
            "was supplied"
        )
    if manifest is not None:
        root = Path(args.repo_root) if args.repo_root is not None else ROOT
        covered = _manifest_ledgers(manifest, root)
        uncovered = [
            path
            for path in _earlier_ledgers(args.base, args.ledger)
            if path.resolve() not in covered
        ]
        if uncovered:
            return 9, (
                f"STOP historical ledger is absent from the opening reconciliation: {uncovered[0]}"
            )

    instance = args.instance
    root = Path(args.repo_root) if args.repo_root is not None else ROOT
    opening_instance = (
        _opening_instance(manifest, instance, repo_root=root) if instance else None
    )
    registry_prior = float(registry_totals.get(instance, 0.0)) if instance else 0.0
    if opening_instance is not None and registry_prior > opening_instance + 1e-9:
        return 9, (
            "STOP registry spend exceeds the instance share of the opening: "
            f"instance={instance} registry={registry_prior:.6f} opening={opening_instance:.6f}"
        )
    prior_instance = opening_instance if opening_instance is not None else registry_prior
    if instance:
        earlier = _instance_ledgers(args.base, instance, args.ledger)
        if earlier and registry is None:
            return 9, (
                "STOP earlier attempt ledgers exist but no --attempt-registry was supplied: "
                f"{len(earlier)}"
            )
        missing = [path for path in earlier if path.resolve() not in registry_ledgers]
        if missing:
            return 9, (f"STOP earlier attempt ledger is absent from the registry: {missing[0]}")

    spent = 0.0
    current_instance = 0.0
    ledger_count = 0
    for path in sorted(args.base.glob(f"*/{args.ledger}")):
        net, code, error = _ledger_net(path, manifest=manifest, manifest_sha=manifest_sha)
        if error is not None:
            return code, error
        spent += net
        ledger_count += 1
        if instance and _belongs_to_instance(path, instance):
            current_instance += net

    total = opening + spent
    remaining = args.batch_cap - total
    instance_total = prior_instance + current_instance
    instance_remaining = args.per_instance - instance_total if instance else args.per_instance
    if instance and instance_remaining < args.minimum:
        return 7, (
            f"STOP per-instance budget exhausted: instance={instance} "
            f"prior={prior_instance:.6f} current={current_instance:.6f} "
            f"total={instance_total:.6f} remaining={instance_remaining:.6f}"
        )
    allowed = min(instance_remaining, remaining)
    if allowed < args.minimum:
        return 7, (
            f"STOP batch budget exhausted: opening={opening:.6f} spent={spent:.6f} "
            f"total={total:.6f} remaining={remaining:.6f}"
        )
    message = (
        f"OK opening={opening:.6f} spent={spent:.6f} total={total:.6f} "
        f"remaining={remaining:.6f} allowed={allowed:.6f} ledgers={ledger_count}"
    )
    if instance:
        message += (
            f" instance={instance} instance_prior={prior_instance:.6f} "
            f"instance_opening={prior_instance:.6f} "
            f"instance_current={current_instance:.6f} "
            f"instance_total={instance_total:.6f} "
            f"instance_remaining={instance_remaining:.6f}"
        )
    return 0, message


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    code, message = evaluate(args)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
