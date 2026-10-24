"""Deterministic target-test selection for the internal M2 closure.

目标测试不是"Planner 写了什么"这么简单：真实运行里 Planner 经常返回空
``tests_to_run``，而基线、影响分析与仓库图已经足以指出可执行的既有测试。
这里把所有合法来源按固定优先级收敛成一组 NodeID，并保留每个被拒候选的理由，
让"没有目标测试"与"目标测试被拒绝"都能在事件和报告里区分开。

安全边界与 Planner 相同：每个候选都要经过 ``WorkspacePathPolicy`` /
``validate_test_target``；越界路径、受保护目录、命令字符串、``planned:`` 前缀与
不存在的测试一律不能成为目标。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from codepilot.graph.routing import reproduction_target_tests
from codepilot.patch.files import PatchValidationError, patch_files
from codepilot.schemas.enums import TestOutcome
from codepilot.schemas.execution import TestBaseline
from codepilot.schemas.state import RepairState
from codepilot.tools.base import PathAccessError, WorkspacePathPolicy, is_test_path
from codepilot.tools.write_tools import validate_test_target

__all__ = ["TargetTestSelection", "changed_files", "select_target_tests"]

_INTENT_MARKER = re.compile(r"^(planned|todo)\s*:", re.IGNORECASE)
_MODEL_DEF = re.compile(r"\b(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True, slots=True)
class TargetTestSelection:
    tests: tuple[str, ...]
    source: str
    rejected: tuple[dict[str, str], ...] = ()
    detail: str | None = None
    baseline_failures: tuple[str, ...] = ()
    """A file target's baseline-failing nodes that were not turned into L1 targets.

    They are still part of the authorized regression scope, but hiding them from
    the selection record made a file target look like "everything in this file
    passes". ``target_tests`` is only the L1 boundary; these are disclosed
    separately rather than silently dropped.
    """


def select_target_tests(
    state: RepairState,
    *,
    policy: WorkspacePathPolicy,
    index: Any,
    repo_graph: Any,
    baseline: TestBaseline | None = None,
) -> TargetTestSelection:
    """按固定优先级选择目标测试；返回空元组时 ``source == "none"``。"""
    baseline = baseline if baseline is not None else state.baseline
    changed = _changed_files(state.current_patch)
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()

    groups: list[tuple[str, list[str], bool]] = [
        # An existing planner target is a real regression boundary; a synthetic
        # reproduction test narrows the coder onto the symptom and can leave
        # existing tests broken. Reproduction still runs and is recorded.
        (
            "planner",
            list(state.plan.tests_to_run) if state.plan is not None else [],
            True,
        ),
        ("reproduction", list(reproduction_target_tests(state)), False),
        ("impact", _impact_candidates(state), True),
        ("baseline", _same_directory_tests(changed, index, baseline), True),
        ("related", _graph_related_tests(changed, index, repo_graph, baseline), True),
    ]

    baseline_failures: list[str] = []
    for source, candidates, require_baseline in groups:
        if source == "impact" and baseline is None:
            continue
        selected: list[str] = []
        for raw in candidates:
            expanded, existing_failures = _expand_file_target(raw, baseline)
            baseline_failures.extend(existing_failures)
            for candidate in expanded:
                normalized_or_error = _validate_candidate(
                    candidate,
                    source=source,
                    policy=policy,
                    baseline=baseline,
                    require_baseline=require_baseline,
                )
                if isinstance(normalized_or_error, dict):
                    rejected.append(normalized_or_error)
                    continue
                if normalized_or_error in seen:
                    continue
                seen.add(normalized_or_error)
                selected.append(normalized_or_error)
        if selected:
            return TargetTestSelection(
                tests=tuple(selected),
                source=source,
                rejected=tuple(rejected),
                baseline_failures=tuple(dict.fromkeys(baseline_failures)),
            )

    return TargetTestSelection(
        tests=(),
        source="none",
        rejected=tuple(rejected),
        detail="no_authorized_target_test",
        baseline_failures=tuple(dict.fromkeys(baseline_failures)),
    )


def _baseline_order(baseline: TestBaseline) -> list[str]:
    """Baseline NodeIDs in pytest collection order.

    ``TestBaseline.outcomes`` is sorted for stable storage. Reusing that order
    for a file target reorders session-scoped fixtures: pytest's own suite moves
    ``sampledir`` in ``test_move_dir``, and doing that before ``test_visit_*``
    turns baseline-passing tests into false failures.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for mapping in baseline.run_outcomes:
        for node_id in mapping:
            if node_id not in seen:
                seen.add(node_id)
                ordered.append(node_id)
    for node_id in baseline.outcomes:
        if node_id not in seen:
            seen.add(node_id)
            ordered.append(node_id)
    return ordered


def _expand_file_target(
    raw: str, baseline: TestBaseline | None
) -> tuple[list[str], list[str]]:
    """Turn a file target into baseline-passing NodeIDs in collection order.

    A whole test file can contain tests that already fail on the base commit;
    requiring an absolute PASS for the file would make the target unreachable
    even when the patch fixes the issue. Only the passing baseline nodes are a
    fair target boundary. The failures are returned separately so the report can
    disclose them instead of silently claiming the whole file was targeted.
    """
    target = raw.strip() if isinstance(raw, str) else ""
    if not target or "::" in target or not target.endswith(".py") or baseline is None:
        return [raw], []
    passing: list[str] = []
    failing: list[str] = []
    for node_id in _baseline_order(baseline):
        if not node_id.startswith(target + "::"):
            continue
        outcome = baseline.outcomes.get(node_id)
        if outcome is TestOutcome.PASSED and node_id not in baseline.flaky:
            passing.append(node_id)
        elif outcome in {TestOutcome.FAILED, TestOutcome.ERROR}:
            failing.append(node_id)
    return (passing or [raw]), failing


def _validate_candidate(
    raw: str,
    *,
    source: str,
    policy: WorkspacePathPolicy,
    baseline: TestBaseline | None,
    require_baseline: bool,
) -> str | dict[str, str]:
    if not isinstance(raw, str) or not raw.strip():
        return _reject(source, raw, "empty_target", "目标测试不能为空")
    target = raw.strip()
    if _INTENT_MARKER.match(target):
        return _reject(
            source,
            target,
            "intent_marker",
            "tests_to_run 只接受已存在、已授权执行的测试，不接受计划中的测试",
        )
    if "::" in target:
        path_text, _, _ = target.partition("::")
        try:
            policy.resolve(path_text, must_exist=True)
        except (PathAccessError, OSError) as exc:
            return _reject(source, target, "path_rejected", str(exc))
    try:
        normalized = validate_test_target(policy, target)
    except (PathAccessError, ValueError) as exc:
        return _reject(source, target, "invalid_target", str(exc))
    if (
        baseline is not None
        and not _baseline_has(baseline, normalized)
        and (require_baseline or not _node_exists(policy, normalized))
    ):
        return _reject(
            source,
            target,
            "not_in_baseline",
            "目标测试不在 base commit 的基线结果里，不能作为可执行目标",
        )
    return normalized


def _reject(source: str, target: str, code: str, reason: str) -> dict[str, str]:
    return {"source": source, "target": str(target), "code": code, "reason": reason}


def _changed_files(patch: str) -> tuple[str, ...]:
    try:
        return patch_files(patch)
    except (PatchValidationError, ValueError):
        return ()


def changed_files(patch: str) -> tuple[str, ...]:
    """补丁修改的仓库相对路径；非法/空补丁返回空元组。"""
    return _changed_files(patch)


def _impact_candidates(state: RepairState) -> list[str]:
    impact = state.impact
    if impact is None:
        return []
    values = [*impact.related_tests]
    values.extend(item.path for item in impact.related_test_evidence)
    return values


def _same_directory_tests(
    changed: tuple[str, ...], index: Any, baseline: TestBaseline | None
) -> list[str]:
    if not changed or baseline is None:
        return []
    directories = {PurePosixPath(path).parent for path in changed}
    candidates: list[str] = []
    for item in index.files():
        path = getattr(item, "path", "")
        if (
            isinstance(path, str)
            and is_test_path(path)
            and PurePosixPath(path).parent in directories
            and _baseline_has(baseline, path)
        ):
            candidates.append(path)
    return candidates


def _graph_related_tests(
    changed: tuple[str, ...],
    index: Any,
    repo_graph: Any,
    baseline: TestBaseline | None,
) -> list[str]:
    if not changed or baseline is None:
        return []
    changed_set = set(changed)
    candidates: list[str] = []
    for symbol in index.symbol_objects():
        if symbol.path not in changed_set:
            continue
        for neighbor in repo_graph.related_tests(symbol.qualified_name):
            path = neighbor.symbol.path
            if is_test_path(path) and _baseline_has(baseline, path):
                candidates.append(path)
    return candidates


def _baseline_has(baseline: TestBaseline, target: str) -> bool:
    return any(_matches(node_id, target) for node_id in baseline.outcomes)


def _matches(node_id: str, target: str) -> bool:
    candidate = target.replace("\\", "/")
    node = node_id.replace("\\", "/")
    return (
        node == candidate or node.startswith(candidate + "::") or node.startswith(candidate + "[")
    )


def _node_exists(policy: WorkspacePathPolicy, target: str) -> bool:
    if "::" not in target:
        return True
    path_text, _, suffix = target.partition("::")
    try:
        source = (policy.root / path_text).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    names = {name for _, name in _MODEL_DEF.findall(source)}
    for segment in suffix.split("::"):
        name = segment.split("[", 1)[0].strip()
        if name and name not in names:
            return False
    return True
