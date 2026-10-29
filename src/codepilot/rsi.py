"""Offline, append-only improvement archives. No model, sandbox or network imports.

Only explicit selection changes future task advice. Historical results and costs
are never inputs to a write operation. Python code still uses worker/checkpoints.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARCHIVE = ROOT / "configs" / "harness"
SOURCE_FILES = (
    "agents/coder.py",
    "agents/planner.py",
    "agents/reviewer.py",
    "agents/exploration.py",
    "tools/query_cache.py",
    "tools/read_tools.py",
    "observability/resources.py",
    "observability/dispatch.py",
    "graph/validation_time.py",
    "tools/base.py",
    "llm/retry.py",
    "llm/dispatch_budget.py",
    "llm/openai_compatible.py",
    "llm/anthropic_messages.py",
    "storage/models.py",
    "storage/repository.py",
    "observability/runtime.py",
    "graph/deps.py",
    "graph/nodes.py",
    "graph/builder.py",
    "graph/checkpoint.py",
    "repo/index.py",
    "eval/assembly.py",
    "llm/tracking.py",
    "observability/lifecycle.py",
    "graph/evidence.py",
    "graph/harness.py",
    "tools/registry.py",
    "rsi.py",
)


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def version_dir(root: Path, version: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", version) or ".." in version:
        raise ValueError("invalid version identifier")
    path = root.resolve() / "candidates" / version
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("archive path escapes root")
    return path


def snapshot_sources() -> dict[str, str]:
    base = Path(__file__).resolve().parent
    return {name: (base / name).read_text(encoding="utf-8") for name in SOURCE_FILES}


def extract_prompts(sources: dict[str, str]) -> dict[str, str]:
    prompts = {}
    for role in ("coder", "planner", "reviewer"):
        for node in ast.walk(ast.parse(sources[f"agents/{role}.py"])):
            if not isinstance(node, ast.Assign):
                continue
            if any(
                (isinstance(t, ast.Name) and t.id == "_SYSTEM")
                or (isinstance(t, ast.Attribute) and t.attr == "system_prompt")
                for t in node.targets
            ):
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    continue
                if isinstance(value, str):
                    prompts[role] = value
                    break
    return prompts


def validate_snapshot(snapshot: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    if snapshot.get("schema_version") != 1:
        raise ValueError("unsupported policy snapshot")
    if not isinstance(snapshot.get("instruction", ""), str):
        raise ValueError("instruction must be text")
    prompts = snapshot.get("prompts", {})
    if not isinstance(prompts, dict) or any(
        k not in {"coder", "planner", "reviewer"} or not isinstance(v, str)
        for k, v in prompts.items()
    ):
        raise ValueError("prompts must be versioned role text, not executable objects")
    entries = snapshot.get("experiences", [])
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("experiences must be a bounded list")
    required = {
        "id",
        "level",
        "source",
        "source_run",
        "applies_when",
        "invalid_when",
        "action",
        "version",
        "status",
        "evaluation_exposure",
    }
    for entry in entries:
        if not isinstance(entry, dict) or not required <= entry.keys():
            raise ValueError("experience provenance is incomplete")
        if entry["level"] not in {"L1", "L2", "L3"}:
            raise ValueError("L0 is task state; archived entries must be L1/L2/L3")
        if entry["evaluation_exposure"] != "public_only":
            raise ValueError("hidden-test/reference-patch experience is not injectable")
        if entry["level"] == "L3" and not entry.get("comparison_refs"):
            raise ValueError("L3 requires version comparison evidence")
        if entry["level"] == "L3":
            if root is None:
                raise ValueError("L3 comparison archive root required")
            for ref in entry["comparison_refs"]:
                if not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{64}", ref):
                    raise ValueError("L3 comparison reference must be a content hash")
                comparison = read_json(root / "comparisons" / f"{ref}.json")
                if _json_hash(comparison) != ref:
                    raise ValueError("comparison identity mismatch")
                validate_comparison(root, comparison)
        if entry["status"] not in {"pending", "validated", "retired"}:
            raise ValueError("invalid experience status")
    return snapshot


def archive_root() -> Path:
    return Path(os.environ.get("CODEPILOT_HARNESS_ARCHIVE", str(DEFAULT_ARCHIVE))).resolve()


def load_selection(root: Path | None = None) -> dict[str, Any]:
    root = root if root is not None else archive_root()
    pointer = root / "active.json"
    if not pointer.exists():
        return {
            "schema_version": 1,
            "version": "builtin-phase26-v1",
            "parent": None,
            "status": "pending",
            "instruction": "",
            "experiences": [],
        }
    version = read_json(pointer)["version"]
    return validate_snapshot(read_json(version_dir(root, str(version)) / "snapshot.json"), root)


def selection_view(current: dict[str, Any]) -> dict[str, Any]:
    """Read-only configuration comparison; never applies the active pointer."""
    try:
        desired = load_selection()
        next_task = {"version": desired.get("version"), "status": desired.get("status", "pending")}
        return {
            "expected": next_task,
            "current_task": current,
            "next_task": next_task,
            "application": "new_tasks_only",
        }
    except (OSError, ValueError, KeyError) as exc:
        return {
            "current_task": current,
            "expected": None,
            "next_task": None,
            "application": "current_task_unchanged",
            "error": str(exc),
        }


def _condition(condition: Any, context: dict[str, Any]) -> bool | None:
    """Typed conjunction only. Unknown keys/values are not executable conditions."""
    if condition in (None, "", [], {}):
        return True
    if not isinstance(condition, dict):
        return None
    supported = {
        "repository",
        "stage",
        "failure_category",
        "runner",
        "environment",
        "version",
        "tags",
    }
    if not condition.keys() <= supported:
        return None
    for key, expected in condition.items():
        actual = context.get(key)
        if actual is None:
            return None
        if key == "tags":
            if not isinstance(expected, list) or not all(isinstance(v, str) for v in expected):
                return None
            if not set(expected) <= set(actual):
                return False
        elif isinstance(expected, str):
            if expected != "*" and actual != expected:
                return False
        elif isinstance(expected, list) and all(isinstance(v, str) for v in expected):
            if actual not in expected:
                return False
        else:
            return None
    return True


def retrieve(
    snapshot: dict[str, Any],
    repository: str,
    *,
    stage: str | None = None,
    failure_category: str | None = None,
    environment: dict[str, Any] | None = None,
    tags: tuple[str, ...] = (),
    audit: list[dict[str, Any]] | None = None,
    limit: int = 4,
    max_chars: int = 6000,
) -> list[dict[str, Any]]:
    """Rank advisory atomic entries; legacy prose is explicitly unevaluated."""
    context = {
        "repository": repository,
        "stage": stage,
        "failure_category": failure_category,
        "tags": tags,
        **(environment or {}),
    }
    decisions = audit if audit is not None else []
    eligible = []
    for entry in snapshot.get("experiences", []):
        reason = ""
        applicable = entry.get("applies_when")
        invalid = entry.get("invalid_when")
        invalid_match = _condition(invalid, context) if invalid else False
        legacy = isinstance(applicable, (str, list)) or isinstance(invalid, (str, list))
        if entry.get("status") == "retired":
            reason = "retired"
        elif entry.get("repository", "*") not in ("*", repository):
            reason = "repository_mismatch"
        elif entry.get("level") == "L1" and entry.get("repository", "*") == "*":
            reason = "L1_repository_required"
        elif invalid_match is True:
            reason = "invalid_when_matched"
        elif isinstance(invalid, dict) and invalid_match is None:
            reason = "invalid_when_unknown"
        elif isinstance(applicable, dict) and _condition(applicable, context) is not True:
            reason = "applies_when_unmet_or_unknown"
        if reason:
            decisions.append({"id": entry.get("id"), "decision": "rejected", "reason": reason})
            continue
        specificity = len(applicable) if isinstance(applicable, dict) else 0
        rank = (
            -specificity,
            entry.get("repository") != repository,
            entry.get("status") != "validated",
            not bool(entry.get("source_run")),
            str(entry.get("id", "")),
            str(entry.get("version", "")),
        )
        eligible.append(
            (
                rank,
                {
                    **entry,
                    "advisory": entry.get("status") != "validated" or legacy,
                    "conditions_evaluated": not legacy,
                    "condition_note": "legacy prose retained as explanation only"
                    if legacy
                    else "typed conditions",
                },
            )
        )
    chosen: list[dict[str, Any]] = []
    used = 0
    for _, entry in sorted(eligible, key=lambda pair: pair[0]):
        size = len(json.dumps(entry, ensure_ascii=False))
        reason = (
            "count_limit"
            if len(chosen) >= max(0, limit)
            else "context_limit"
            if used + size > max_chars
            else "matched"
        )
        decisions.append(
            {
                "id": entry["id"],
                "decision": "selected" if reason == "matched" else "truncated",
                "reason": reason,
            }
        )
        if reason == "matched":
            chosen.append(entry)
            used += size
    return chosen


def archive_candidate(root: Path, version: str, proposal: dict[str, Any]) -> Path:
    if not all(
        proposal.get(k)
        for k in ("hypothesis", "input_evidence", "candidate_changes", "code_revision")
    ):
        raise ValueError(
            "hypothesis, public evidence, candidate changes and code revision are required"
        )
    parent = proposal.get("parent")
    if parent:
        read_json(version_dir(root, str(parent)) / "snapshot.json")
    sources = snapshot_sources()
    snapshot = validate_snapshot(
        {
            "schema_version": 1,
            "version": version,
            "parent": parent,
            "status": "pending",
            "instruction": proposal.get("instruction", ""),
            "prompts": proposal.get("prompts", extract_prompts(sources)),
            "experiences": [{**e, "status": "pending"} for e in proposal.get("experiences", [])],
        },
        root,
    )
    folder = version_dir(root, version)
    folder.mkdir(parents=True, exist_ok=False)
    payload = {
        **proposal,
        "schema_version": 1,
        "version": version,
        "parent": parent,
        "status": "pending",
        "snapshot": snapshot,
        "source_hashes": {p: hashlib.sha256(t.encode()).hexdigest() for p, t in sources.items()},
        "source_snapshot": sources,
        "static_results": proposal.get("static_results", ["not checked"]),
        "future_metrics": proposal.get(
            "future_metrics", ["quality", "regressions", "time", "cost"]
        ),
        "effect_validation": "NOT_RUN",
        "auto_promote": False,
    }
    for name, content in (("snapshot.json", snapshot), ("candidate.json", payload)):
        (folder / name).write_text(
            json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return folder


def select(root: Path, version: str, *, allow_pending: bool = False) -> None:
    snapshot = validate_snapshot(read_json(version_dir(root, version) / "snapshot.json"), root)
    if snapshot.get("status") != "validated" and not allow_pending:
        raise ValueError(
            "pending candidate: explicit --allow-pending required; effects remain unverified"
        )
    root.mkdir(parents=True, exist_ok=True)
    target = root / "active.json"
    temporary = root / "active.next.json"
    temporary.write_text(
        json.dumps({"version": version, "new_tasks_only": True}) + "\n", encoding="utf-8"
    )
    temporary.replace(target)


def replay(path: Path, *, decisions: bool = False) -> list[dict[str, Any]]:
    from codepilot.agents.exploration import exploration_feedback

    records = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
            except (ValueError, TypeError) as exc:
                records.append({"line": line_no, "error": str(exc)})
                continue
            if decisions:
                extra = event.get("extra")
                if not isinstance(extra, dict) or extra.get("hook") != "exploration":
                    continue
                facts = extra.get("facts")
                if not isinstance(facts, dict):
                    records.append({"line": line_no, "error": "exploration facts unavailable"})
                    continue
                records.append(
                    {
                        "line": line_no,
                        "mode": "deterministic_decision",
                        "old_feedback": facts.get("feedback"),
                        "feedback": exploration_feedback(facts),
                        "effect_validation": "NOT_RUN",
                    }
                )
            else:
                records.append({"line": line_no, "mode": "trace", "event": event})
    return records


def _json_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def validate_comparison(root: Path, comparison: dict[str, Any]) -> None:
    """Validate imported evidence identity and comparability, never invent effects."""
    import math

    if (
        comparison.get("schema_version") != 1
        or comparison.get("evaluation_exposure") != "public_only"
    ):
        raise ValueError("comparison schema/public provenance required")
    if comparison.get("decision") not in {"accept", "reject", "observe"}:
        raise ValueError("explicit comparison decision required")
    if not comparison.get("reason") or not comparison.get("evidence_refs"):
        raise ValueError("comparison reason and original evidence references required")
    references = comparison["evidence_refs"]
    if not isinstance(references, list):
        raise ValueError("evidence_refs must contain hashed local evidence")
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise ValueError("each evidence reference requires path and sha256")
        evidence = (root / str(reference["path"])).resolve(strict=True)
        if not evidence.is_relative_to(root.resolve()) or not evidence.is_file():
            raise ValueError("comparison evidence escapes archive")
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != reference["sha256"]:
            raise ValueError("comparison original evidence changed")
    sides = [comparison.get("baseline"), comparison.get("candidate")]
    required_conditions = {
        "model",
        "provider",
        "budget_hash",
        "environment_hash",
        "success_definition_hash",
        "task_set_hash",
    }
    for side in sides:
        if not isinstance(side, dict):
            raise ValueError("baseline and candidate records required")
        snapshot = read_json(version_dir(root, side["version"]) / "snapshot.json")
        if side.get("snapshot_hash") != _json_hash(snapshot):
            raise ValueError("comparison snapshot identity mismatch")
        conditions = side.get("conditions", {})
        if set(conditions) != required_conditions or not all(
            isinstance(v, str) and v for v in conditions.values()
        ):
            raise ValueError("frozen comparison conditions required")
        if any(
            not re.fullmatch(r"[a-f0-9]{64}", conditions[key])
            for key in required_conditions
            if key.endswith("_hash")
        ):
            raise ValueError("comparison condition hashes must be SHA256")
        runs = side.get("runs")
        if (
            not isinstance(runs, list)
            or not runs
            or not all(isinstance(run, str) and run for run in runs)
            or len(set(runs)) != len(runs)
        ):
            raise ValueError("unique original run IDs required")
        metrics = side.get("metrics", {})
        if set(metrics) != {"quality", "regressions", "seconds", "cost", "tool_calls"}:
            raise ValueError("independent quality/time/cost metrics required")
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
            for v in metrics.values()
        ):
            raise ValueError("finite nonnegative observed metrics required")
        if side.get("cost_known") is not True:
            raise ValueError("known cost and source runs required")
    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    if (
        baseline["version"] == candidate["version"]
        or baseline["conditions"] != candidate["conditions"]
    ):
        raise ValueError("distinct versions under identical frozen conditions required")


def import_comparison(root: Path, comparison: dict[str, Any]) -> str:
    validate_comparison(root, comparison)
    identity = _json_hash(comparison)
    folder = root / "comparisons"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{identity}.json"
    if not path.exists():
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n")
    return identity


def promotion_readiness(root: Path, version: str, reference: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", reference):
        raise ValueError("comparison hash required")
    comparison = read_json(root / "comparisons" / f"{reference}.json")
    if _json_hash(comparison) != reference:
        raise ValueError("comparison hash mismatch")
    validate_comparison(root, comparison)
    if comparison["candidate"]["version"] != version:
        raise ValueError("comparison belongs to another candidate")
    quality_floor = (
        comparison["candidate"]["metrics"]["quality"]
        >= comparison["baseline"]["metrics"]["quality"]
        and comparison["candidate"]["metrics"]["regressions"]
        <= comparison["baseline"]["metrics"]["regressions"]
    )
    return {
        "version": version,
        "comparison": reference,
        "eligible_for_manual_review": comparison["decision"] == "accept" and quality_floor,
        "quality_floor_met": quality_floor,
        "status": "pending",
        "auto_promote": False,
        "next_action": "Review original evidence and independent coverage before explicitly selecting; no source checkout is changed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ARCHIVE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    comparison_parser = sub.add_parser("import-comparison")
    comparison_parser.add_argument("path", type=Path)
    promotion = sub.add_parser("promotion-readiness")
    promotion.add_argument("version")
    promotion.add_argument("reference")
    for command in ("show", "select", "rollback"):
        item = sub.add_parser(command)
        item.add_argument("version")
        if command != "show":
            item.add_argument("--allow-pending", action="store_true")
    create = sub.add_parser("archive")
    create.add_argument("version")
    create.add_argument("--proposal", type=Path, required=True)
    trace = sub.add_parser("replay")
    trace.add_argument("path", type=Path)
    trace.add_argument("--mode", choices=("trace", "decision"), default="trace")
    args = parser.parse_args()
    if args.command == "list":
        result: Any = [
            p.parent.name for p in sorted((args.root / "candidates").glob("*/snapshot.json"))
        ]
    elif args.command == "import-comparison":
        result = import_comparison(args.root, read_json(args.path))
    elif args.command == "promotion-readiness":
        result = promotion_readiness(args.root, args.version, args.reference)
    elif args.command == "replay":
        result = replay(args.path, decisions=args.mode == "decision")
    elif args.command == "archive":
        result = str(archive_candidate(args.root, args.version, read_json(args.proposal)))
    elif args.command == "show":
        result = read_json(version_dir(args.root, args.version) / "candidate.json")
    else:
        version = args.version
        if args.command == "rollback":
            version = read_json(version_dir(args.root, version) / "snapshot.json").get("parent")
            if not version:
                raise ValueError("candidate has no archived parent")
        select(args.root, version, allow_pending=args.allow_pending)
        result = {"selected_for_new_tasks": version, "historical_results_unchanged": True}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
