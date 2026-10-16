"""E-02：fixture 有效性校验脚本——用真实 Docker 跑，不靠预置结果。

    uv run python scripts/validate_fixtures.py

对每个 fixture 证明三件事，而这三件事只能由**执行**得出：

1. ``base + test.patch`` 上目标测试确实**失败**（任务真的存在缺陷）；
2. 再加 ``gold.patch`` 之后目标测试**通过**（gold 真的是修复）；
3. gold 没有把 base 上原本通过的测试改坏（不是靠牺牲别的测试换通过）。

状态区分是这套校验的价值所在，所以 :mod:`codepilot.eval.evaluation` 的九种状态
一个都不许合并——尤其 ``ENV_ERROR`` 不能算"任务无效"，也不能算"通过"。把它当
无效会让坏掉的镜像悄悄缩减 benchmark 分母；把它当通过会直接虚报成功率。

``--fake-sandbox`` 只用于工程自检（确认链路能跑通），产出的记录会带
``synthetic_evidence=True``，**永远不能**当作 fixture 有效性证据，也不会被写入
可续跑的缓存里。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from codepilot.artifacts import ArtifactStore  # noqa: E402
from codepilot.eval.evaluation import (  # noqa: E402
    FixtureValidation,
    ValidationStatus,
    validate_fixture,
)
from codepilot.eval.synthetic import (  # noqa: E402
    FixtureValidationError,
    SyntheticFixture,
    directory_digests,
    validate_suite,
)
from codepilot.repo.workspace import WorkspaceManager  # noqa: E402
from codepilot.sandbox.base import Sandbox  # noqa: E402
from codepilot.sandbox.docker import DockerSandbox  # noqa: E402

DEFAULT_SUITE = REPO_ROOT / "benchmarks" / "synthetic"
DEFAULT_OUT = REPO_ROOT / "artifacts" / "phase10" / "fixture-validation"
CACHE_FILE = "validation-cache.json"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate synthetic fixtures with real Docker.")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tasks", nargs="*", default=None, help="只校验这些 task_id。")
    parser.add_argument("--force", action="store_true", help="忽略续跑缓存，全部重新执行。")
    parser.add_argument(
        "--fake-sandbox",
        action="store_true",
        help="工程自检：用 FakeSandbox 跑通链路，结论不作为 fixture 有效性证据。",
    )
    parser.add_argument("--keep-work", action="store_true", help="保留临时 workspace 与 artifact。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    suite_root: Path = args.suite
    out_root: Path = args.out
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"suite: {suite_root}")
    try:
        suite = validate_suite(suite_root, task_ids=args.tasks)
    except FixtureValidationError as exc:
        print(f"suite 无法加载：{exc}", file=sys.stderr)
        return 2
    for task_id, reasons in sorted(suite.invalid.items()):
        print(f"[INVALID_FIXTURE] {task_id}\n    " + "\n    ".join(reasons))
    if not suite.fixtures:
        print("没有任何 fixture 通过 Schema 校验，终止。", file=sys.stderr)
        return 1

    cache = _load_cache(out_root) if not args.force and args.tasks is None else {}
    # FakeSandbox 模式**不**构造 evaluator：那条链路必须只由真实执行喂。用预置
    # PASS 去走 validate_fixture 会产出看起来一样的 VALID，而那正是 03 §2.2 禁止的。
    evaluator = None if args.fake_sandbox else _make_evaluator(out_root)

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    started = time.time()
    for index, fixture in enumerate(suite.fixtures, start=1):
        version = fixture.evaluation.fixture_version
        cached = cache.get(fixture.task_id)
        if cached is not None and cached.get("fixture_version") == version and cached.get("usable"):
            print(f"[{index}/{len(suite)}] {fixture.task_id}: 复用已验证结论（内容未变化）")
            skipped.append({**cached, "reused": True})
            continue
        before = directory_digests(fixture.directory / "repo")
        if args.fake_sandbox:
            validation = _fake_validation(fixture)
        else:
            validation = validate_fixture(evaluator, fixture, run_id=f"validate-{fixture.task_id}")
        after = directory_digests(fixture.directory / "repo")
        if before != after:
            validation = validation.model_copy(
                update={
                    "status": ValidationStatus.INVALID_FIXTURE,
                    "reason": "校验过程中原始 repo/ 内容发生变化（03 §28 禁止）",
                }
            )
        payload = validation.model_dump(mode="json")
        payload["sandbox_backend"] = "fake" if args.fake_sandbox else "docker"
        payload["synthetic_evidence"] = args.fake_sandbox
        records.append(payload)
        _write(out_root / "fixtures" / f"{fixture.task_id}.json", payload)
        reason = f" — {validation.reason}" if validation.reason else ""
        print(f"[{index}/{len(suite)}] {fixture.task_id}: {validation.status.value}{reason}")

    summary = _summarize(
        records=records,
        skipped=skipped,
        invalid=suite.invalid,
        args=args,
        duration=time.time() - started,
    )
    _write(out_root / "summary.json", summary)
    _write_markdown(out_root / "summary.md", summary)
    if not args.fake_sandbox and args.tasks is None:
        _save_cache(out_root, records + skipped)
    if not args.keep_work:
        shutil.rmtree(out_root / "work", ignore_errors=True)

    print(
        f"\n总任务 {summary['total']}｜有效 {summary['valid']}｜无效 "
        f"{summary['invalid_total']}｜环境错误 {summary['environment_errors']}"
    )
    print(f"报告: {out_root / 'summary.md'}")
    if args.fake_sandbox:
        print("注意：本次为 FakeSandbox 工程自检，结论不构成 fixture 有效性证据。")
        return 0
    return 0 if summary["environment_errors"] == 0 and summary["invalid_total"] == 0 else 1


def _make_evaluator(out_root: Path) -> Any:
    from codepilot.eval.evaluation import GoldEvaluator

    def factory(artifacts: ArtifactStore) -> Sandbox:
        return DockerSandbox(
            artifacts=artifacts,
            allowed_workspace_root=out_root / "work",
        )

    return GoldEvaluator(
        manager=WorkspaceManager(cache_root=out_root / "work" / "repo-cache"),
        sandbox_factory=factory,
        work_root=out_root / "work",
    )


def _fake_validation(fixture: SyntheticFixture) -> FixtureValidation:
    """直接给出"结构上自洽"的结论，只验证脚本的读写与聚合链路能跑通。

    这里刻意**不**调用 :func:`validate_fixture`：那条路径的判据必须由真实执行喂。
    用 FakeSandbox 预置 PASS 去走同一条路径，产出的 VALID 会让人误以为 fixture 被
    验证过——那正是 03 §2.2 禁止的"假实现当真功能"。
    """
    metadata = fixture.metadata
    return FixtureValidation(
        task_id=fixture.task_id,
        base_commit=metadata.base_commit,
        fixture_version=fixture.evaluation.fixture_version,
        category=metadata.category.value,
        difficulty=metadata.difficulty,
        multi_file=metadata.multi_file,
        has_traceback=metadata.has_traceback,
        environment="fake:none:none",
        status=ValidationStatus.VALID,
        reason="FakeSandbox 工程自检：结构自洽，未做真实执行",
        target_count=len(fixture.evaluation.tests.fail_to_pass),
        regression_count=len(fixture.evaluation.tests.pass_to_pass),
        base_target_passed=0,
        gold_target_passed=len(fixture.evaluation.tests.fail_to_pass),
        gold_result=None,
        baseline=None,
        artifact_refs={},
    )


def _summarize(
    *,
    records: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    invalid: dict[str, list[str]],
    args: argparse.Namespace,
    duration: float,
) -> dict[str, Any]:
    all_records = records + skipped
    statuses = Counter(str(item["status"]) for item in all_records)
    categories = Counter(str(item["category"]) for item in all_records)
    environment_failures = sum(
        1
        for item in all_records
        if item["status"]
        in (ValidationStatus.ENV_ERROR.value, ValidationStatus.SANDBOX_ERROR.value)
    )
    valid = [item for item in all_records if item["status"] == ValidationStatus.VALID.value]
    return {
        "suite": str(args.suite),
        "sandbox_backend": "fake" if args.fake_sandbox else "docker",
        "real_execution_evidence": not args.fake_sandbox,
        "duration_seconds": round(duration, 2),
        "total": len(all_records) + len(invalid),
        "validated_now": len(records),
        "reused_from_cache": len(skipped),
        "valid": len(valid),
        "invalid_total": len(all_records) - len(valid) + len(invalid),
        "environment_errors": environment_failures,
        "status_counts": dict(sorted(statuses.items())),
        "category_counts": dict(sorted(categories.items())),
        "multi_file_tasks": sum(1 for item in all_records if item["multi_file"]),
        "no_traceback_tasks": sum(1 for item in all_records if not item["has_traceback"]),
        "base_failing_tasks": sum(
            1
            for item in all_records
            if item["status"]
            not in (
                ValidationStatus.BASE_NOT_FAILING.value,
                ValidationStatus.ENV_ERROR.value,
                ValidationStatus.SANDBOX_ERROR.value,
            )
        ),
        "gold_passing_tasks": sum(
            1
            for item in all_records
            if item["status"]
            in (
                ValidationStatus.VALID.value,
                ValidationStatus.REGRESSION_INTRODUCED.value,
            )
        ),
        "regression_clean_tasks": len(valid),
        "target_test_total": sum(int(item["target_count"]) for item in all_records),
        "regression_test_total": sum(int(item["regression_count"]) for item in all_records),
        "schema_invalid": dict(sorted(invalid.items())),
        "records": all_records,
    }


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _load_cache(out_root: Path) -> dict[str, Any]:
    path = out_root / CACHE_FILE
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {item["task_id"]: item for item in data.get("records", [])}


def _save_cache(out_root: Path, records: list[dict[str, Any]]) -> None:
    _write(out_root / CACHE_FILE, {"records": records, "written_at": time.time()})


def _write_markdown(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Synthetic Fixture 校验报告（E-02）",
        "",
        f"- Suite: `{summary['suite']}`",
        f"- 执行后端: `{summary['sandbox_backend']}`"
        + (
            "（**工程自检，不构成 fixture 有效性证据**）"
            if not summary["real_execution_evidence"]
            else "（真实 Docker 执行）"
        ),
        f"- 总任务数: {summary['total']}",
        f"- 本次真实执行: {summary['validated_now']}｜续跑复用: {summary['reused_from_cache']}",
        f"- 有效（VALID）: {summary['valid']}",
        f"- 无效: {summary['invalid_total']}",
        f"- 环境/沙箱错误: {summary['environment_errors']}",
        f"- 多文件修复任务: {summary['multi_file_tasks']}",
        f"- Issue 不含 traceback: {summary['no_traceback_tasks']}",
        f"- 目标测试总数: {summary['target_test_total']}",
        f"- 回归测试总数: {summary['regression_test_total']}",
        "",
        "## 状态分布",
        "",
        "| 状态 | 数量 |",
        "| --- | --- |",
    ]
    lines += [f"| `{status}` | {count} |" for status, count in summary["status_counts"].items()]
    lines += ["", "## 类别分布", "", "| 类别 | 数量 |", "| --- | --- |"]
    lines += [f"| `{key}` | {value} |" for key, value in summary["category_counts"].items()]
    lines += [
        "",
        "## 逐任务结论",
        "",
        "| task_id | base commit | 状态 | 环境 | 目标 | 回归 | 原因 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary["records"]:
        commit = str(item["base_commit"])[:12]
        environment = str(item["environment"]).replace("|", "/")[:34]
        reason = str(item.get("reason") or "").replace("|", "/").replace("\n", " ")[:110]
        lines.append(
            f"| `{item['task_id']}` | `{commit}` | `{item['status']}` | `{environment}` | "
            f"{item['target_count']} | {item['regression_count']} | {reason} |"
        )
    if summary["schema_invalid"]:
        lines += ["", "## Schema 阶段即失败（从未进入执行）", ""]
        for task_id, reasons in summary["schema_invalid"].items():
            lines.append(f"- `{task_id}`: " + "; ".join(str(item) for item in reasons))
    lines += [
        "",
        "## 口径",
        "",
        "- `ENV_ERROR` / `SANDBOX_ERROR` 表示**没测出来**，既不计为任务无效，也不计为通过；",
        "- `BASE_NOT_FAILING` 表示目标测试在 base 上已经通过，即任务本身没有缺陷，不能作为 benchmark 任务；",
        "- `REGRESSION_INTRODUCED` 表示 gold 把 base 上原本通过的测试改坏了；",
        "- 逐字节核对 `repo/` 的 sha256，确保校验过程没有改动原始仓库（03 §28）。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(main())
