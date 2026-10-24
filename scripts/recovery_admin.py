"""Build a multi-source recovery manifest and maintain the attempt registry.

This is the small operational surface used by ``scripts/run_l2cal10e.sh``:
the library in :mod:`codepilot.llm.dispatch_recovery` owns validation; this CLI
only parses paths and prints a JSON receipt.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(ROOT / "src"))

from codepilot.llm.dispatch_recovery import (  # noqa: E402
    RecoveryLedgerSource,
    attempt_instance_totals,
    build_multi_source_recovery_manifest,
    instance_opening_cny,
    load_attempt_registry,
    load_recovery_manifest,
    record_attempt_from_ledger,
    write_recovery_manifest,
)


def _repo_root(value: Path | None) -> Path:
    return Path(value) if value is not None else ROOT


def _ledger_spec(raw: str) -> RecoveryLedgerSource:
    path_text, separator, instance = raw.rpartition("=")
    if not separator or not path_text or not instance:
        raise ValueError(f"ledger spec must be PATH=INSTANCE: {raw!r}")
    return RecoveryLedgerSource(path=Path(path_text), instance_id=instance)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("manifest")
    manifest.add_argument("--output", type=Path, required=True)
    manifest.add_argument("--run-id", required=True)
    manifest.add_argument("--reason", required=True)
    manifest.add_argument("--source", action="append", default=[])
    manifest.add_argument("--settled", action="append", default=[])
    manifest.add_argument("--include-registry", action="store_true")
    manifest.add_argument("--registry", type=Path, default=None)
    manifest.add_argument("--repo-root", type=Path, default=None)

    record = sub.add_parser("record")
    record.add_argument("--registry", type=Path, required=True)
    record.add_argument("--attempt-label", required=True)
    record.add_argument("--instance", required=True)
    record.add_argument("--artifact-root", type=Path, required=True)
    record.add_argument("--ledger", type=Path, required=True)
    record.add_argument("--status", required=True)
    record.add_argument("--repo-root", type=Path, default=None)

    totals = sub.add_parser("totals")
    totals.add_argument("--registry", type=Path, required=True)
    totals.add_argument("--instance", default=None)
    totals.add_argument("--repo-root", type=Path, default=None)

    opening = sub.add_parser("opening")
    opening.add_argument("--manifest", type=Path, required=True)
    opening.add_argument("--instance", required=True)
    opening.add_argument("--repo-root", type=Path, default=None)
    return parser


def _manifest(args: argparse.Namespace) -> dict[str, object]:
    root = _repo_root(args.repo_root)
    sources = [_ledger_spec(raw) for raw in args.source]
    settled = [_ledger_spec(raw) for raw in args.settled]
    listed = {str(Path(item.path).resolve()) for item in [*sources, *settled]}
    if args.include_registry:
        if args.registry is None or not Path(args.registry).is_file():
            raise ValueError("--include-registry requires an existing --registry")
        registry = load_attempt_registry(args.registry)
        for entry in registry.get("attempts") or []:
            if not isinstance(entry, dict) or not entry.get("ledger_present"):
                continue
            ledger = (root / str(entry["ledger_path"])).resolve()
            if str(ledger) in listed:
                continue
            spec = RecoveryLedgerSource(path=ledger, instance_id=str(entry["instance_id"]))
            if float(entry.get("unknown_reserved_cny") or 0.0) > 0:
                sources.append(spec)
            else:
                settled.append(spec)
            listed.add(str(ledger))
    manifest = build_multi_source_recovery_manifest(
        sources,
        run_id=args.run_id,
        reason=args.reason,
        settled_ledgers=settled,
        repo_root=root,
    )
    write_recovery_manifest(manifest, args.output)
    return {
        "output": str(args.output),
        "opening_cny": manifest["total_upper_bound_cny"],
        "settled_cny": manifest["settled_cny"],
        "unknown_reserved_cny": manifest["unknown_reserved_cny"],
        "unknown_cost_calls": manifest["unknown_cost_calls"],
        "source_count": len(manifest["sources"]),
        "settled_count": len(manifest["additional_settled_ledgers"]),
    }


def _record(args: argparse.Namespace) -> dict[str, object]:
    return record_attempt_from_ledger(
        args.registry,
        attempt_label=args.attempt_label,
        instance_id=args.instance,
        artifact_root=args.artifact_root,
        ledger=args.ledger,
        status=args.status,
        repo_root=_repo_root(args.repo_root),
    )


def _totals(args: argparse.Namespace) -> dict[str, object]:
    registry = load_attempt_registry(args.registry)
    totals = attempt_instance_totals(registry, repo_root=_repo_root(args.repo_root))
    if args.instance is not None:
        return {"instance": args.instance, "total_upper_bound_cny": totals.get(args.instance, 0.0)}
    return {"instances": totals}


def _opening(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_recovery_manifest(args.manifest)
    amount = instance_opening_cny(
        manifest, args.instance, repo_root=_repo_root(args.repo_root)
    )
    return {
        "manifest": str(args.manifest),
        "instance": args.instance,
        "instance_opening_cny": amount,
        "batch_opening_cny": manifest.get("total_upper_bound_cny"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    handlers = {
        "manifest": _manifest,
        "record": _record,
        "totals": _totals,
        "opening": _opening,
    }
    try:
        payload = handlers[args.command](args)
    except (OSError, ValueError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
