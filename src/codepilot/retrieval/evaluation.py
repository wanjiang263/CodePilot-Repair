"""Evaluation-side orchestration; gold is used only after inference completes."""

import hashlib
import platform
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from codepilot.repo.gitcmd import GitError
from codepilot.repo.workspace import WorkspaceError
from codepilot.retrieval.config import CHANNELS, RetrievalConfig, SeedChannel
from codepilot.retrieval.dataset import RetrievalCase, validate_gold
from codepilot.retrieval.documents import RetrievalError
from codepilot.retrieval.eval import EvaluationReport, Prediction, evaluate
from codepilot.retrieval.offline import OFFLINE_DESCRIPTION
from codepilot.retrieval.runner import ExecutionInfo, RetrievalOptions, indexed_repository
from codepilot.schemas.retrieval import FusionMethod, RetrievalResult


class AblationRow(BaseModel):
    name: str
    status: Literal["RUN", "NOT RUN"] = "RUN"
    options: RetrievalOptions | None
    metrics: EvaluationReport | None = None
    results: dict[str, RetrievalResult] = {}
    execution: dict[str, ExecutionInfo] = {}
    failures: dict[str, str] = {}


class RetrievalEvaluation(BaseModel):
    schema_version: int = 1
    dataset_kind: str = "synthetic-development; not a performance benchmark"
    total_tasks: int
    dataset_sha256: str
    digest_definition: str = "SHA-256 of sorted canonical case JSON lines with final LF"
    inference_sha256: str
    versions: dict[str, str]
    embedding_description: str
    function_ranking: str = (
        "function/method/test/module symbols in fused order; class symbols excluded"
    )
    rows: list[AblationRow]
    not_run: list[str]


def run_evaluation(
    cases: list[RetrievalCase],
    dataset_root: Path,
    cache_dir: Path,
    *,
    embedding: Literal["none", "offline-hash"] = "offline-hash",
    method: FusionMethod = FusionMethod.WEIGHTED,
    weights: dict[str, float] | None = None,
    rrf_k: int = 60,
    seed_channel: SeedChannel = "bm25",
) -> RetrievalEvaluation:
    """Each task keeps its place in the denominator, even when indexing fails.

    Rows share an index of exactly the same base tree. Single-channel rows use
    their own raw rankings through the existing fusion/file aggregation service;
    structural-only depends on BM25 seeds, explicitly recorded in ExecutionInfo.
    No case, patch, labels or evaluation object is passed to IndexedRepository.
    """
    if not cases or len({c.task_id for c in cases}) != len(cases):
        raise ValueError("Evaluation requires nonempty unique tasks")
    if embedding not in {"none", "offline-hash"} or method not in {
        FusionMethod.WEIGHTED,
        FusionMethod.RRF,
    }:
        raise ValueError("Unsupported embedding or fusion method")
    ordered = sorted(cases, key=lambda c: c.task_id)
    rows = []
    for name in CHANNELS:
        if name == "semantic" and embedding == "none":
            rows.append(AblationRow(name=name, status="NOT RUN", options=None))
        else:
            config = RetrievalConfig.model_validate({n: n == name for n in CHANNELS})
            rows.append(
                AblationRow(
                    name=name,
                    options=RetrievalOptions(
                        channels=config,
                        method=method,
                        embedding=embedding,
                        rrf_k=rrf_k,
                        seed_channel=seed_channel,
                    ),
                )
            )
    combined = RetrievalConfig.model_validate(
        {name: name != "semantic" or embedding != "none" for name in CHANNELS}
    )
    rows.append(
        AblationRow(
            name="fused",
            options=RetrievalOptions(
                channels=combined,
                method=method,
                embedding=embedding,
                weights=weights,
                rrf_k=rrf_k,
                seed_channel=seed_channel,
            ),
        )
    )
    inputs = [case.inference(dataset_root) for case in ordered]
    for case, task in zip(ordered, inputs, strict=True):
        try:
            with indexed_repository(task, cache_dir) as indexed:
                for row in rows:
                    if row.options is None:
                        continue
                    try:
                        result, info = indexed.retrieve(task, row.options, limit=10)
                        row.results[task.task_id] = result
                        row.execution[task.task_id] = info
                    except (RetrievalError, ValueError, OSError, GitError) as exc:
                        row.failures[task.task_id] = f"{type(exc).__name__}: {exc}"
                # Gold parsing / mapping occurs on the evaluation side AFTER all inference.
                try:
                    validate_gold(case, indexed.root, indexed.symbols, dataset_root)
                except OSError as exc:
                    raise ValueError(f"Gold provenance unavailable: {exc}") from exc
        except (WorkspaceError, GitError, OSError) as exc:
            for row in rows:
                if row.options is not None:
                    row.failures[task.task_id] = f"{type(exc).__name__}: {exc}"
    gold = {case.task_id: case.labels() for case in ordered}
    for row in rows:
        if row.options is None:
            continue
        predictions = {
            task_id: Prediction(
                tuple(f.path for f in result.files),
                tuple(
                    (s.file, s.qualified_name) for s in result.fused_symbols if s.kind != "class"
                ),
            )
            for task_id, result in row.results.items()
            if task_id not in row.failures
        }
        row.metrics = evaluate(gold, predictions)
    not_run = ["trained semantic embedding model"]
    if embedding == "none":
        not_run.append("semantic channel (no embedding configured)")
    return RetrievalEvaluation(
        total_tasks=len(cases),
        dataset_sha256=hashlib.sha256(
            ("\n".join(c.model_dump_json() for c in ordered) + "\n").encode()
        ).hexdigest(),
        inference_sha256=hashlib.sha256(
            ("\n".join(t.model_dump_json() for t in inputs) + "\n").encode()
        ).hexdigest(),
        versions={
            "python": platform.python_version(),
            **{
                name: version(name)
                for name in (
                    "codepilot-repair",
                    "numpy",
                    "rank-bm25",
                    "tree-sitter",
                    "tree-sitter-python",
                    "networkx",
                )
            },
        },
        embedding_description=OFFLINE_DESCRIPTION if embedding == "offline-hash" else "not enabled",
        rows=rows,
        not_run=not_run,
    )


def render_table(report: RetrievalEvaluation) -> str:
    lines = [
        f"Dataset: {report.total_tasks} synthetic development tasks (denominator includes failures)",
        report.embedding_description,
        "channel | File@1 | File@5 | File@10 | Function@1 | Function@5 | Function@10 | failures",
    ]
    for row in report.rows:
        metrics = row.metrics
        values = (
            (
                [f"{metrics.file_recall[k]:.4f}" for k in (1, 5, 10)]
                + [f"{metrics.function_recall[k]:.4f}" for k in (1, 5, 10)]
            )
            if metrics
            else ["NOT RUN"] * 6
        )
        lines.append(" | ".join([row.name, *values, str(len(row.failures))]))
    lines.append("NOT RUN: " + "; ".join(report.not_run))
    return "\n".join(lines)
