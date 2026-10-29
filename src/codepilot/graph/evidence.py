"""Bounded evidence projections. These do not change acceptance or test accounting."""

from __future__ import annotations

from typing import Any

from codepilot.graph.stop_rules import patch_hash
from codepilot.sandbox.runners import requested_filter
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.state import RepairState


def task_experience(state: RepairState) -> dict[str, Any]:
    """L0 references the authoritative task state rather than copying a memory DB."""
    return {
        "id": f"L0:{state.run_id}",
        "level": "L0",
        "source_run": state.run_id,
        "source": ["RepairState.plan", "RepairState.failure_summary", "RepairState.tool_calls"],
        "applies_when": {"run_id": state.run_id, "candidate_hash": state.frozen_patch_hash},
        "invalid_when": "task, candidate or public requirement changes; rederive from RepairState",
        "version": state.policy_snapshot.get("version", "legacy"),
        "status": "pending",
        "action": "Use public contract and actual failure evidence; revise the hypothesis before repeating a failed patch.",
        "evaluation_exposure": "public_only",
    }


def failure_summary(state: RepairState) -> dict[str, Any]:
    attempt = state.current_attempt
    if attempt is None or attempt.patch_hash != patch_hash(state.current_patch):
        return {}
    regression = attempt.regression
    execution = attempt.execution
    stage = "regression" if regression is not None else "target_tests"
    if regression and regression.executions:
        executions = list(regression.executions.values())
        execution = next((e for e in executions if e.status.value != "PASS"), executions[-1])
    if execution is None:
        if attempt.validation.passed:
            return {}
        return {
            "stage": "patch_validation",
            "category": "product",
            "expected": "A legal nonempty candidate satisfying patch rules",
            "actual": attempt.validation.errors,
            "candidate_hash": attempt.patch_hash,
            "evidence_refs": [f"attempts/{attempt.iteration}/validation.json"],
            "baseline_relation": "unknown",
            "scope": {"observed": 0, "missing": None},
            "new_since_last_attempt": {
                "validation_changed": state.failure_summary.get("actual")
                != attempt.validation.errors
            },
        }
    executions = list(regression.executions.values()) if regression else [execution]
    observed = sorted({t.node_id for e in executions for t in e.tests})
    missing = (
        list(regression.unexecuted_tests)
        if regression
        else list(
            execution.diagnostics.get(
                "requested_missing",
                requested_filter(execution.tests, tuple(attempt.target_tests))[1],
            )
        )
    )
    requested = (
        sorted(set(regression.executed_tests) | set(missing))
        if regression
        else list(attempt.target_tests)
    )
    failures = sorted({t.node_id for e in executions for t in e.failed_tests})
    previous = state.failure_summary
    baseline = state.baseline
    new_failures = (
        list(regression.new_failures)
        if regression
        else [
            t
            for t in failures
            if baseline and str(baseline.outcomes.get(t)) == "passed" and t not in baseline.flaky
        ]
    )
    categories = {"TIMEOUT": "timeout", "ENV_ERROR": "environment", "SANDBOX_ERROR": "tool"}
    discriminator = state.plan.contract.discriminator if state.plan else None
    return {
        "stage": stage,
        "category": categories.get(
            execution.status.value, "product" if failures else "measurement"
        ),
        "expected": discriminator.text
        if discriminator and discriminator.confidence != "unknown"
        else "Public behavior expectation is unknown; selected validation scope must still complete.",
        "actual": execution.error_reason or execution.status.value,
        "candidate_hash": attempt.patch_hash if attempt else patch_hash(state.current_patch),
        "evidence_refs": [e.artifact_dir for e in regression.executions.values()]
        if regression
        else [execution.artifact_dir],
        "baseline_relation": "new_failures" if new_failures else "unchanged_or_unknown",
        "scope": {
            "requested": len(requested) if requested else None,
            "observed": len(observed),
            "missing": len(missing),
            "missing_refs": missing[:20],
            "selector_unit": "selectors; observed counts are test cases",
        },
        "failure_ids": failures[:20],
        "failure_messages": [
            {"node_id": t.node_id, "message": (t.message or "")[:1000]}
            for e in executions
            for t in e.failed_tests
        ][:3],
        "new_since_last_attempt": {
            "new_missing_ids": sorted(
                set(missing) - set(previous.get("scope", {}).get("missing_refs", []))
            )[:20],
            "new_failure_ids": sorted(set(failures) - set(previous.get("failure_ids", [])))[:20],
            "category_changed": previous.get("actual")
            != (execution.error_reason or execution.status.value),
            "candidate_changed": previous.get("candidate_hash")
            != (attempt.patch_hash if attempt else patch_hash(state.current_patch)),
        },
    }


def verification_view(state: RepairState) -> dict[str, Any]:
    """Keep execution, behavior, regression, review and coverage independent."""
    attempt = state.current_attempt
    candidate_bound = bool(attempt and attempt.patch_hash == patch_hash(state.current_patch))
    regression = attempt.regression if attempt and candidate_bound else None
    execution = attempt.execution if attempt and candidate_bound else None
    review = state.review
    bound = bool(review and review.reviewed_patch_hash == patch_hash(state.current_patch))
    target_missing = (
        requested_filter(execution.tests, tuple(attempt.target_tests))[1]
        if execution and attempt
        else []
    )
    layers = {
        "requirement_behavior": {
            "conclusion": "unknown",
            "reason": "No independently bound public-behavior observation",
            "sources": state.plan.contract.discriminator.source if state.plan else [],
        },
        "affected_regression": {
            "candidate_bound": candidate_bound,
            "requested": len(attempt.target_tests) if attempt else None,
            "observed": len(execution.tests) if execution else 0,
            "missing": target_missing if execution else None,
        },
        "expanded_validation": {
            "mode": regression.mode.value if regression else None,
            "complete": regression.complete if regression else None,
            "missing": list(regression.unexecuted_tests) if regression else None,
        },
    }
    return {
        "layers": layers,
        "execution": execution.status.value if execution else "NOT_EXECUTED",
        "behavior": "unknown",  # Passing self-tests alone cannot prove the public contract.
        "regression": "not_measured"
        if regression is None
        else (
            "incomplete"
            if not regression.complete
            else "new_failures"
            if regression.new_failures
            else "clean"
        ),
        "review": "not_executed"
        if review is None
        else "stale"
        if not bound
        else "agree"
        if review.approved
        else "disagree",
        "coverage": {
            "observed": len(regression.executed_tests) if regression else 0,
            "missing": len(regression.unexecuted_tests) if regression else None,
            "mode": regression.mode.value if regression else None,
        },
        "official": "NOT_EVALUATED",  # Official verdicts remain external, bound records.
    }


def stage_handoff(state: RepairState, plan: RepairPlan) -> dict[str, Any]:
    """Deterministic boundary summary, never an extra model call or mid-edit reset."""
    failure = failure_summary(state)
    return {
        "kind": "deterministic_projection",
        "model_summary": None,
        "run_id": state.run_id,
        "revision": state.runtime.get("revision", 0),
        "policy_hash": state.policy_snapshot.get("sha256"),
        "candidate_hash": patch_hash(state.current_patch) if state.current_patch else "",
        "requirements": state.issue,
        "contract": plan.contract.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
        "confirmed_facts": {"failure": failure, "sources": failure.get("evidence_refs", [])},
        "excluded_hypotheses": [],
        "excluded_hypotheses_note": "No explicit exclusion evidence; do not infer from repeated failure.",
        "missing_facts": [
            name
            for name, item in plan.contract.model_dump().items()
            if isinstance(item, dict) and item.get("confidence") == "unknown"
        ],
        "next_minimal_action": "Inspect the missing contract/source evidence, then modify the smallest supported production scope.",
        "full_evidence_refs": [
            "events.jsonl",
            f"exploration/{state.iteration}.json",
            *[
                call.get("result_summary", {}).get("artifact_ref")
                for call in state.tool_calls
                if call.get("result_summary", {}).get("artifact_ref")
            ],
        ],
    }
