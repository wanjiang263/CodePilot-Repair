"""Artifact → API view normalization.

This module is deliberately read-only. Patch parsing never applies a diff and
report loading never rewrites a run directory; historical experiment packages
must remain byte-for-byte untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from codepilot.api.schemas import (
    BudgetView,
    InternalVerificationView,
    OfficialVerdictView,
    PatchFileView,
    PatchView,
    ReportView,
    UsageView,
)
from codepilot.api.status import ui_status_for
from codepilot.graph.stop_rules import patch_hash

__all__ = [
    "budget_view",
    "build_patch_view",
    "build_report_view",
    "load_json",
    "parse_patch",
    "usage_view",
]


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _as_dict(payload)


def _as_dict(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def usage_view(payload: dict[str, Any] | None) -> UsageView:
    data = payload or {}
    calls = int(data.get("llm_calls") or 0)
    unknown = int(data.get("cost_unknown_calls") or 0)
    return UsageView(
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        cached_input_tokens=int(data.get("cached_input_tokens") or 0),
        cost=float(data.get("cost") or 0.0),
        currency=str(data.get("currency") or "CNY"),
        llm_calls=calls,
        cost_known=unknown == 0,
    )


def budget_view(payload: dict[str, Any] | None) -> BudgetView:
    data = payload or {}
    return BudgetView(
        max_iterations=_optional_int(data.get("max_iterations")),
        max_test_runs=_optional_int(data.get("max_test_runs")),
        max_files_changed=_optional_int(data.get("max_files_changed")),
        max_lines_changed=_optional_int(data.get("max_lines_changed")),
        max_tool_calls_per_iteration=_optional_int(data.get("max_tool_calls_per_iteration")),
        max_review_calls=_optional_int(data.get("max_review_calls")),
        max_runtime_seconds=_optional_int(data.get("max_runtime_seconds")),
        max_total_tokens=_optional_int(data.get("max_total_tokens")),
        max_cost=_optional_float(data.get("max_cost")),
        budget_currency=str(data.get("budget_currency") or "CNY"),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _junit_status(value: Any) -> str | None:
    if isinstance(value, dict):
        status = value.get("status")
        return str(status) if status is not None else None
    return None


def parse_patch(diff: str) -> list[PatchFileView]:
    """Split a unified git diff into safe per-file entries.

    Only metadata is parsed. A malformed path is dropped rather than surfaced as
    a filesystem path; the raw diff remains available separately.
    """
    files: list[PatchFileView] = []
    if not diff.strip():
        return files
    chunks = diff.split("\ndiff --git ")
    for chunk in chunks:
        text = chunk if chunk.startswith("diff --git ") else "diff --git " + chunk
        if not text.strip():
            continue
        first = text.splitlines()[0]
        parts = first.split()
        if len(parts) < 4:
            continue
        old_path = parts[2].removeprefix("a/")
        new_path = parts[3].removeprefix("b/")
        path = new_path or old_path
        if not _safe_repo_path(path):
            continue
        additions = 0
        deletions = 0
        for line in text.splitlines()[1:]:
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        status = "modified"
        if "new file mode" in text or "--- /dev/null" in text:
            status = "added"
        elif "deleted file mode" in text or "+++ /dev/null" in text:
            status = "deleted"
        files.append(
            PatchFileView(
                path=path,
                additions=additions,
                deletions=deletions,
                status=status,
                diff=text.rstrip("\n") + "\n",
            )
        )
    return files


def _safe_repo_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if not normalized or "\x00" in normalized:
        return False
    windows = PureWindowsPath(path)
    posix = PurePosixPath(normalized)
    return not (
        windows.is_absolute()
        or windows.anchor
        or posix.is_absolute()
        or ".." in posix.parts
        or normalized.startswith("/")
    )


def build_patch_view(
    *,
    run_id: str,
    task_id: str,
    diff: str,
    report: dict[str, Any],
    simulated: bool,
    identity_verified: bool = True,
    identity_note: str | None = None,
) -> PatchView:
    files = parse_patch(diff)
    closure = _as_dict(report.get("closure"))
    review = _as_dict(report.get("review"))
    validation = _as_dict(report.get("patch_validation"))
    return PatchView(
        run_id=run_id,
        task_id=task_id,
        sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        identity_sha256=patch_hash(diff) if diff else None,
        size_bytes=len(diff.encode("utf-8")),
        files=files,
        files_count=len(files),
        additions=sum(item.additions for item in files),
        deletions=sum(item.deletions for item in files),
        diff=diff,
        diff_truncated=False,
        validation_passed=validation.get("passed") if validation else None,
        reviewer_approved=review.get("approved") if review else None,
        reviewed_patch_hash=review.get("reviewed_patch_hash") or None,
        frozen_by_coding_stage=bool(closure.get("frozen_by_coding_stage")) if closure else None,
        identity_verified=identity_verified,
        identity_note=identity_note,
        simulated=simulated,
    )


def build_report_view(
    *,
    run_id: str,
    task_id: str,
    run_dir: Path,
    report: dict[str, Any],
    state: dict[str, Any],
    status: str,
    stop_reason: str | None,
    official: OfficialVerdictView | None = None,
    events_count: int = 0,
    readonly: bool = False,
    extra_summary: dict[str, Any] | None = None,
) -> ReportView:
    closure = _as_dict(report.get("closure"))
    resolved_status = status or str(state.get("status") or report.get("status") or "error")
    reason = stop_reason or _as_str(report.get("stop_reason")) or _as_str(state.get("stop_reason"))
    ui_status = ui_status_for(resolved_status, reason)
    usage_payload = _as_dict(report.get("usage"))
    if not usage_payload:
        usage_payload = _as_dict(state.get("usage"))
    budget_payload = _as_dict(state.get("budget"))
    internal = _internal_view(report, state, closure)
    markdown_path = run_dir / "report.md"
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    summary = _summary(report, state, closure, events_count)
    if extra_summary:
        summary.update(extra_summary)
    raw = _trim_raw(report)
    return ReportView(
        run_id=run_id or str(report.get("run_id") or state.get("run_id") or ""),
        task_id=task_id or str(report.get("task_id") or state.get("task_id") or ""),
        status=resolved_status,
        raw_status=resolved_status,
        ui_status=ui_status,
        stop_reason=reason,
        verification_level=_as_str(report.get("verification_level")),
        official=official or OfficialVerdictView(),
        internal=internal,
        issue=_as_str(report.get("issue_summary") or state.get("issue")),
        repository=_as_str(report.get("repository")),
        base_commit=_as_str(report.get("base_commit") or state.get("base_commit")),
        summary=summary,
        tests=_tests_view(report, state, closure),
        review=_review_view(report, closure),
        regression=_as_dict(report.get("regression")),
        usage=usage_view(usage_payload),
        budget=budget_view(budget_payload),
        attempts=_attempts(report, state),
        localization=_as_dict(report.get("localization")),
        plan=_as_dict(report.get("repair_plan")),
        reproduction=_as_dict(report.get("reproduction")),
        failures=[
            str(item)
            for item in _as_list(report.get("failure_reasons") or state.get("failure_reasons"))
        ],
        unexecuted_stages=[str(item) for item in _as_list(closure.get("unexecuted_stages"))],
        artifacts=_artifact_map(run_dir),
        markdown=markdown,
        raw=raw,
        readonly=readonly,
    )


def _with_admission_remaining(closure: dict[str, Any]) -> dict[str, Any]:
    """Expose the derived ``remaining`` value without changing the persisted schema.

    ``ClosureAdmission.remaining`` is a property on the core model, so it is not
    present in ``model_dump``. The dashboard and static report must show the same
    arithmetic as the state machine instead of ``None``.
    """
    admission = closure.get("closure_admission")
    if not isinstance(admission, dict) or "remaining" in admission:
        return closure
    allowance = admission.get("allowance")
    spent = admission.get("runs_spent")
    if not isinstance(allowance, int) or not isinstance(spent, int):
        return closure
    return {
        **closure,
        "closure_admission": {
            **admission,
            "remaining": max(allowance - spent, 0),
        },
    }


def _as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _internal_view(
    report: dict[str, Any], state: dict[str, Any], closure: dict[str, Any]
) -> InternalVerificationView:
    closure = _with_admission_remaining(closure)
    status = _as_str(report.get("status") or state.get("status"))
    return InternalVerificationView(
        status=status or None,
        raw_status=status or None,
        stop_reason=_as_str(report.get("stop_reason") or state.get("stop_reason")) or None,
        verification_level=_as_str(report.get("verification_level")) or None,
        review_executed=bool(closure.get("review_executed")) if closure else None,
        review_bound_to_candidate=(
            bool(closure.get("review_bound_to_candidate")) if closure else None
        ),
        regression_complete=bool(closure.get("regression_complete")) if closure else None,
        regression_new_failures=_optional_int(closure.get("regression_new_failures")),
        target_tests_status=_as_str(closure.get("target_tests_status")) or None,
        unexecuted_stages=[str(item) for item in _as_list(closure.get("unexecuted_stages"))],
        closure=closure,
    )


def _summary(
    report: dict[str, Any],
    state: dict[str, Any],
    closure: dict[str, Any],
    events_count: int,
) -> dict[str, Any]:
    attempts = _as_list(report.get("attempts"))
    return {
        "attempts": len(attempts),
        "runtime_seconds": report.get("runtime_seconds"),
        "test_runs": report.get("test_runs") or state.get("test_runs"),
        "simulated_llm": bool(report.get("simulated_llm")),
        "simulated_sandbox": bool(report.get("simulated_sandbox")),
        "modified_files": report.get("modified_files") or [],
        "patch_bytes": len(str(report.get("git_diff") or "").encode("utf-8")),
        "events_count": events_count,
        "review_executed": closure.get("review_executed"),
        "regression_complete": closure.get("regression_complete"),
        "coding_stop_reason": closure.get("coding_stop_reason"),
        "closure_admission": closure.get("closure_admission"),
        "budget_usage": closure.get("budget_usage") or {},
        "plan_corrections": report.get("plan_corrections"),
        "error_class": report.get("error_class"),
        "error_origin": report.get("error_origin"),
    }


def _tests_view(
    report: dict[str, Any], state: dict[str, Any], closure: dict[str, Any]
) -> dict[str, Any]:
    baseline = _as_dict(report.get("baseline"))
    regression = _as_dict(report.get("regression"))
    execution = report.get("test_execution")
    return {
        "target_tests_executed": closure.get("target_tests_executed"),
        "target_tests_status": closure.get("target_tests_status"),
        "target_test_sha256": closure.get("target_test_sha256"),
        "target_execution": execution if isinstance(execution, dict) else None,
        "test_runs": report.get("test_runs") or state.get("test_runs"),
        "baseline": {
            "complete": baseline.get("complete"),
            "base_commit": baseline.get("base_commit"),
            "test_count": len(baseline.get("outcomes") or {}),
            "flaky": sorted(baseline.get("flaky") or []),
            "status": "recorded" if baseline else "NOT RECORDED",
        }
        if baseline
        else {"status": "NOT RECORDED"},
        "regression": {
            "complete": regression.get("complete"),
            "mode": report.get("regression_mode") or regression.get("mode"),
            "levels": closure.get("regression_levels") or regression.get("levels_run") or [],
            "new_failures": closure.get("regression_new_failures"),
            "status": regression.get("status"),
            "failure_reason": regression.get("failure_reason"),
            "executed_tests": len(regression.get("executed_tests") or []),
        }
        if regression
        else {"status": "NOT EXECUTED"},
        "closure": closure,
    }


def _review_view(report: dict[str, Any], closure: dict[str, Any]) -> dict[str, Any]:
    review = _as_dict(report.get("review"))
    if not review:
        return {
            "executed": False,
            "status": "NOT EXECUTED",
            "skip_reason": _as_list(closure.get("unexecuted_stages")),
        }
    return {
        "executed": True,
        "approved": review.get("approved"),
        "summary": review.get("summary"),
        "risk": review.get("risk"),
        "issues": review.get("issues") or [],
        "reviewed_patch_hash": review.get("reviewed_patch_hash"),
        "bound_to_candidate": closure.get("review_bound_to_candidate"),
        "high_issues": closure.get("review_high_issues"),
        "review_calls": closure.get("review_calls"),
        "post_review_repair": closure.get("post_review_repair"),
    }


def _attempts(report: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    source = _as_list(report.get("attempts"))
    if not source:
        source = _as_list(state.get("attempts"))
    output: list[dict[str, Any]] = []
    for raw_item in source:
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, Any]", raw_item)
        execution = _as_dict(item.get("execution"))
        validation = _as_dict(item.get("validation"))
        review = _as_dict(item.get("review"))
        output.append(
            {
                "iteration": item.get("iteration"),
                "patch_hash": item.get("patch_hash"),
                "patch_bytes": len(str(item.get("patch_diff") or "").encode("utf-8")),
                "validation_passed": validation.get("passed"),
                "target_status": _junit_status(execution),
                "tests_failed": execution.get("tests_failed"),
                "failure_signatures": item.get("failure_signatures") or [],
                "review_approved": review.get("approved") if review else None,
                "diagnosis": bool(item.get("diagnosis")),
            }
        )
    return output


def _artifact_map(run_dir: Path) -> dict[str, Any]:
    names = (
        "report.json",
        "report.md",
        "state_final.json",
        "events.jsonl",
        "patch_final.diff",
        "regression.json",
        "review.json",
        "checkpoints.sqlite",
    )
    output: dict[str, Any] = {"run_dir": "<run>"}
    for name in names:
        path = run_dir / name
        if path.is_file():
            output[name] = {"size": path.stat().st_size}
        else:
            output[name] = None
    attempts = run_dir / "attempts"
    if attempts.is_dir():
        output["attempt_dirs"] = sorted(item.name for item in attempts.iterdir() if item.is_dir())
    else:
        output["attempt_dirs"] = []
    return output


def _trim_raw(report: dict[str, Any]) -> dict[str, Any]:
    raw = dict(report)
    # The canonical diff is served by /patch; keeping a duplicate here would make
    # it easy to accidentally rewrite code while scrubbing host paths.
    raw.pop("git_diff", None)
    baseline = raw.get("baseline")
    if isinstance(baseline, dict):
        outcomes = baseline.get("outcomes")
        if isinstance(outcomes, dict) and len(outcomes) > 50:
            raw["baseline"] = {**baseline, "outcomes": {"<truncated>": len(outcomes)}}
    evidence = raw.get("evidence")
    if isinstance(evidence, list) and len(evidence) > 200:
        raw["evidence"] = evidence[:200]
    return raw
