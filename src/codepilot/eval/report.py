"""E-09：评测产物 —— ``results.jsonl`` / ``metrics.json`` / ``summary.md`` / ``manifest.json``。

四份文件回答四个不同的问题，缺一份都会让另一份变得不可信：

* ``results.jsonl``：**每一次** (task, seed) 观测的原始记录。指标必须由它复算得出，
  不能反过来从 Markdown 的自然语言里读。
* ``metrics.json``：指标连同分母。每个比例都带 ``numerator``/``denominator``。
* ``summary.md``：给人看的报告。
* ``manifest.json``：这次运行的身份 —— 代码 commit、配置 hash、数据摘要。没有它，
  报告里任何数字都不属于一个可复现的实验。

标题级别由数据决定，而不是由写报告的人决定：``FakeLLM 工程 smoke`` 与
``真实模型 Benchmark``、``Full`` 与 ``Partial`` 必须在最显眼的地方分开，否则一次
脚本化的跑通会被读成一次有效果结论的评测 —— 这是本轮最容易造成、也最难事后纠正
的一类误读。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codepilot.eval.metrics import (
    SuiteMetrics,
    aggregate_metric_values,
    per_seed_metrics,
    summarize,
)
from codepilot.eval.runner import BenchmarkRun, ExecutionState, TaskRecord

__all__ = [
    "MANIFEST_JSON",
    "METRICS_JSON",
    "RESULTS_JSONL",
    "SUMMARY_MD",
    "ReportPaths",
    "write_benchmark_report",
]

RESULTS_JSONL = "results.jsonl"
METRICS_JSON = "metrics.json"
SUMMARY_MD = "summary.md"
MANIFEST_JSON = "manifest.json"

_Evidence = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportPaths:
    run_dir: Path
    results: Path
    metrics: Path
    summary: Path
    manifest: Path


def evidence_classification(run: BenchmarkRun) -> dict[str, Any]:
    """这批证据是什么、不是什么。

    刻意把"不是"写在前面：FakeLLM 的脚本恰好解出全部合成任务，也只证明图与评测
    链路能跑通，不证明真实模型有同等能力。这一条不写清楚，报告越详细越有害。
    """
    records = run.records
    simulated = {record.simulated_llm for record in records}
    docker = {record.real_docker_evaluation for record in records}
    completed = [item for item in records if item.counted]
    return {
        "engine": "FakeLLM 工程 smoke"
        if simulated == {True}
        else (
            "真实模型 Benchmark"
            if simulated == {False}
            else "混合（同批里既有 FakeLLM 又有真实模型）"
        ),
        "engine_is_simulated": True in simulated,
        "real_docker_evaluation": all(docker) if docker else False,
        "evaluation_coverage": (
            "full"
            if len(completed) == run.declared_observations and run.declared_observations
            else "partial"
        ),
        "verification_modes": sorted({item.verification_mode for item in records}),
        "unknown_cost_tasks": run.ledger["unknown_cost_tasks"],
        "claims_model_capability": False,
        "claim_note": (
            "本报告不构成模型修复能力结论。FakeLLM 按脚本应答，其成功率衡量的是评测"
            "基础设施；真实模型效果验收（E-10）本轮 NOT RUN。"
        ),
    }


def _suite_hash(run: BenchmarkRun) -> str:
    """suite 的数据摘要：跑过哪些 fixture、各是什么版本。"""
    payload = sorted(f"{item.task_id}@{item.fixture_version}" for item in run.records)
    return json.dumps({"fixtures": sorted(set(payload))}, sort_keys=True)


def build_metrics(run: BenchmarkRun) -> dict[str, Any]:
    """指标全集 + 每 seed 指标 + 跨 seed 统计 + 完整性声明。"""
    outcomes = run.metric_inputs()
    overall: SuiteMetrics = summarize(outcomes)
    by_seed = per_seed_metrics(outcomes)
    aggregate = aggregate_metric_values(list(by_seed.values()))
    counts = run.counts()
    categories: dict[str, dict[str, int]] = {}
    for record in run.records:
        bucket = categories.setdefault(
            record.category, {"observations": 0, "resolved": 0, "env_errors": 0}
        )
        bucket["observations"] += 1
        if record.resolved is True:
            bucket["resolved"] += 1
        if record.execution_state == ExecutionState.EVALUATION_ERROR.value:
            bucket["env_errors"] += 1
    return {
        "integrity": run.integrity,
        "declared_observations": run.declared_observations,
        "recorded_observations": len(run.records),
        "execution_state_counts": counts,
        "budget": run.ledger,
        "overall": overall.as_dict(),
        "per_seed": {str(seed): value.as_dict() for seed, value in by_seed.items()},
        "seed_aggregate": aggregate,
        "by_category": {
            key: dict(sorted(value.items())) for key, value in sorted(categories.items())
        },
        "self_verified_precision_note": overall.repair.self_verified_precision.describe(),
        "bootstrap_basis": {
            "note": "配对 bootstrap（Phase 12）用的任务级二元结果；这里只给原始数据，不算区间。",
            "per_seed_task_outcomes": {
                str(seed): [
                    {"task_id": item.task_id, "resolved": item.resolved}
                    for item in outcomes
                    if item.seed == seed
                ]
                for seed in by_seed
            },
        },
        "data_integrity": {
            "every_number_computed_from_results_jsonl": True,
            "denominators_visible": True,
            "undefined_ratios_rendered_as_none": True,
            "environment_failures_in_denominator": True,
        },
    }


def build_manifest(run: BenchmarkRun) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run": run.identity,
        "suite": {
            "name": run.config.suite,
            "hash": _suite_hash(run),
            "declared_tasks": run.declared_observations,
        },
        "config": {
            "hash": run.config.config_hash,
            "seeds": list(run.config.seeds),
            "max_total_cost": run.config.max_total_cost,
            "task_reservation": run.config.task_reservation,
            "max_in_flight": run.config.max_in_flight,
            "budget_currency": run.config.budget_currency,
            "per_task_budget": run.config.per_task_budget.model_dump(mode="json"),
        },
        "data_source": {
            "suite": run.config.suite,
            "fixture_versions": sorted({item.fixture_version for item in run.records}),
        },
        "evidence": evidence_classification(run),
        "artifact_files": [RESULTS_JSONL, METRICS_JSON, SUMMARY_MD, MANIFEST_JSON],
    }


def render_summary(run: BenchmarkRun, metrics: dict[str, Any]) -> str:
    evidence = evidence_classification(run)
    repair = metrics["overall"]["repair"]
    cost = metrics["overall"]["cost"]
    iterations = metrics["overall"]["iterations"]
    lines: list[str] = [
        f"# Phase 10 Synthetic Benchmark —— {evidence['engine']}",
        "",
        f"> **{evidence['claim_note']}**",
        "",
        "## 运行身份",
        "",
        f"- run_id: `{run.identity['run_id']}`",
        f"- 代码版本: `{run.identity['code']['version']}` @ `{str(run.identity['code']['commit'])[:12]}`"
        + (" **(dirty)**" if run.identity["code"]["dirty"] else ""),
        f"- 模型: `{run.identity['model_identity']}`",
        f"- 配置 hash: `{run.identity['config_hash']}`",
        f"- LLM 配置 hash: `{run.identity['provider_config_hash']}`",
        f"- 开始 / 结束: {run.started_at} / {run.finished_at}",
        f"- 数据完整性: **{metrics['integrity']}**"
        f"（记录 {metrics['recorded_observations']}/{metrics['declared_observations']}）",
        "",
        "## 证据边界",
        "",
        f"- 推理引擎: {evidence['engine']}（simulated={evidence['engine_is_simulated']}）",
        f"- 评测是否真实 Docker: {evidence['real_docker_evaluation']}",
        f"- 评测覆盖: {evidence['evaluation_coverage']}",
        f"- 验证模式分布: {', '.join(f'`{item}`' for item in evidence['verification_modes'])}",
        f"- 费用未知的任务数: {evidence['unknown_cost_tasks']}",
        "",
        "## 修复指标（分母含环境失败任务）",
        "",
        "| 指标 | 值 | 分子/分母 |",
        "| --- | --- | --- |",
        f"| Resolved Rate | {_pct(repair['resolved_rate'])} | {_frac(repair['resolved_rate'])} |",
        f"| First Attempt Resolved | {_pct(repair['first_attempt_resolved'])} | "
        f"{_frac(repair['first_attempt_resolved'])} |",
        f"| Repair Gain | {_num(repair['repair_gain'])} | Resolved − First Attempt |",
        f"| Repair Success Rate（条件） | {_pct(repair['repair_success_rate'])} | "
        f"{_frac(repair['repair_success_rate'])} |",
        f"| Self-verified Precision（条件） | "
        f"{_pct_or_num(repair['self_verified_precision']['value'])} | "
        f"{repair['self_verified_precision']['numerator']}/"
        f"{repair['self_verified_precision']['denominator']} |",
        f"| Empty Patch Rate | {_pct(repair['empty_patch_rate'])} | {_frac(repair['empty_patch_rate'])} |",
        f"| Invalid Patch Rate | {_pct(repair['invalid_patch_rate'])} | {_frac(repair['invalid_patch_rate'])} |",
        "",
        f"- 评测未能判定的任务: **{repair['undetermined_tasks']}**"
        "（仍计入总体分母，不计入条件分母）",
        f"- {metrics['self_verified_precision_note']}",
        "",
        "## 迭代与成本",
        "",
        f"- 迭代次数 mean/median/P90 = {_num(iterations['mean'])} / "
        f"{_num(iterations['median'])} / {_num(iterations['p90'])}（记录到迭代的任务数 "
        f"{iterations['count']}）",
        f"- 总 token: {cost['total_tokens'] if cost['total_tokens'] is not None else 'n/a'}"
        f"；每任务平均: {_num(cost['mean_tokens_per_task'])}"
        f"（埋点覆盖 {cost['tokens_tasks_recorded']}/{cost['total_tasks']}）",
        f"- 总费用: {cost['total_cost'] if cost['total_cost'] is not None else 'n/a'} "
        f"{cost['cost_currency']}"
        + (" **(下界：有任务费用未知)**" if cost["total_cost_is_lower_bound"] else ""),
        f"- 每任务平均费用: {_num(cost['mean_cost_per_task'])}；"
        f"每解决一个任务平均费用: {_num(cost['mean_cost_per_resolved_task'])}"
        f"（分母 {cost['resolved_tasks_with_cost']}）",
        f"- 模型调用次数: {cost['total_llm_calls'] if cost['total_llm_calls'] is not None else 'n/a'}",
        f"- 每任务平均墙钟: {_num(cost['mean_runtime_seconds'])}s"
        f"（{cost['runtime_tasks_recorded']}/{cost['total_tasks']}）",
        "",
        "## 跨 seed 统计",
        "",
        "| 指标 | mean | stddev | seeds |",
        "| --- | --- | --- | --- |",
    ]
    for name, values in metrics["seed_aggregate"].items():
        lines.append(
            f"| `{name}` | {_num(values['mean'])} | {_num(values['stddev'])} | {values['seeds']} |"
        )
    lines += [
        "",
        "## 分类别表现",
        "",
        "| 类别 | 观测数 | 解决 | 评测失败 |",
        "| --- | --- | --- | --- |",
    ]
    for category, values in metrics["by_category"].items():
        lines.append(
            f"| `{category}` | {values['observations']} | {values['resolved']} | {values['env_errors']} |"
        )
    lines += [
        "",
        "## 可靠性",
        "",
        "```json",
        json.dumps(metrics["overall"]["reliability"], ensure_ascii=False, indent=2),
        "```",
    ]
    lines += ["", "## 已知局限", ""]
    lines += [f"- {item}" for item in _limitations(run, evidence)]
    return "\n".join(lines) + "\n"


def _limitations(run: BenchmarkRun, evidence: dict[str, Any]) -> list[str]:
    items = [
        evidence["claim_note"],
        "Synthetic fixture 用于开发调试与回归，不作为主要实验结论来源（02 §17.3）。",
        "Issue 语义与答案泄漏的人工复核未执行时，记 HUMAN_REVIEW_PENDING，不声称正式人工验收完成。",
        "分母含环境失败与预算截停；样本量下小于约 10 个百分点的差异通常无法得出显著结论（02 §17.5）。",
    ]
    if not evidence["real_docker_evaluation"]:
        items.append("本批评测未使用真实 Docker 执行，因此不构成交付意义上的评测结论。")
    if evidence["evaluation_coverage"] != "full":
        items.append(
            f"评测只覆盖了部分派发（{run.integrity}）；被截停或未执行的任务已计入总体分母但无结论。"
        )
    return items


def _pct(ratio: dict[str, Any]) -> str:
    value = ratio.get("value")
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _pct_or_num(value: Any) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _frac(ratio: dict[str, Any]) -> str:
    return f"{ratio['numerator']}/{ratio['denominator']}"


def _num(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


def write_benchmark_report(run: BenchmarkRun, out_dir: Path) -> ReportPaths:
    """写出四份产物。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    metrics = build_metrics(run)
    paths = ReportPaths(
        run_dir=directory,
        results=directory / RESULTS_JSONL,
        metrics=directory / METRICS_JSON,
        summary=directory / SUMMARY_MD,
        manifest=directory / MANIFEST_JSON,
    )
    with paths.results.open("w", encoding="utf-8", newline="\n") as handle:
        for record in run.records:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False, default=str) + "\n")
    paths.metrics.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    paths.summary.write_text(render_summary(run, metrics), encoding="utf-8", newline="\n")
    paths.manifest.write_text(
        json.dumps(build_manifest(run), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return paths


def read_results(path: Path) -> list[TaskRecord]:
    """从 ``results.jsonl`` 读回记录。

    缺行/多行在这里都会显式暴露：部分写入的文件不能冒充完整结果，否则中断一次就
    少一批任务，而分母是按读到的条数算的。
    """
    records: list[TaskRecord] = []
    text = Path(path).read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"results.jsonl 第 {number} 行是空行；部分写入的文件不可复用")
        try:
            records.append(TaskRecord.from_dict(json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"results.jsonl 第 {number} 行不是合法 JSON: {exc}") from exc
    return records
