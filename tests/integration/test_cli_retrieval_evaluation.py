"""V-24: actual offline ablation, labels never accepted by the inference runner."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codepilot.cli import app
from codepilot.config import get_settings

DATASET = Path(__file__).resolve().parents[1] / "fixtures/retrieval_cases.jsonl"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEPILOT_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_documented_eval_command_outputs_all_rows_and_reproducible_raw_evidence(isolated):
    output = isolated / "report.json"
    run = CliRunner().invoke(
        app, ["eval-retrieval", "--dataset", str(DATASET), "--output", str(output)]
    )
    assert run.exit_code == 0, run.output
    assert "File@1" in run.stdout and "Function@10" in run.stdout
    assert "not a learned semantic model" in run.stdout
    report = json.loads(output.read_text(encoding="utf-8"))
    assert [row["name"] for row in report["rows"]] == [
        "bm25",
        "ripgrep",
        "semantic",
        "structural",
        "test",
        "history",
        "fused",
    ]
    assert report["total_tasks"] == 12
    assert report["not_run"] == ["trained semantic embedding model"]
    for row in report["rows"]:
        assert row["metrics"]["total_tasks"] == 12
        assert row["failures"] == {}
        assert len(row["results"]) == 12 and len(row["execution"]) == 12
        for key, counts in (("file_recall", "file_hits"), ("function_recall", "function_hits")):
            for k in ("1", "5", "10"):
                assert row["metrics"][key][k] == row["metrics"][counts][k] / 12
        assert all(
            set(result["per_channel"])
            == {name for name in row["options"]["channels"] if row["options"]["channels"][name]}
            for result in row["results"].values()
        )
    assert not list((isolated / "cache/cli-workspaces").iterdir())


def test_no_embedding_mode_reports_semantic_not_run_instead_of_fabricating(isolated):
    from codepilot.retrieval.dataset import load_dataset
    from codepilot.retrieval.evaluation import run_evaluation

    report = run_evaluation(
        load_dataset(DATASET)[:1], DATASET.parent, isolated / "cache", embedding="none"
    )
    semantic = next(row for row in report.rows if row.name == "semantic")
    assert semantic.status == "NOT RUN" and semantic.metrics is None and semantic.results == {}
    assert "trained semantic embedding model" in report.not_run
    assert "semantic channel (no embedding configured)" in report.not_run
    from codepilot.retrieval.evaluation import render_table

    assert "semantic | NOT RUN" in render_table(report)


def test_gold_change_does_not_change_inference_outputs(isolated, monkeypatch):
    from codepilot.retrieval.dataset import load_dataset
    from codepilot.retrieval.evaluation import run_evaluation
    from codepilot.retrieval.runner import IndexedRepository

    original = load_dataset(DATASET)[:1]
    changed = [original[0].model_copy(deep=True)]
    changed[0].gold_locations[0].qualified_name = "injected.answer"
    captured = []
    retrieve = IndexedRepository.retrieve

    def capture(self, task, options, **kwargs):
        result, info = retrieve(self, task, options, **kwargs)
        captured.append(result.model_dump_json())
        return result, info

    monkeypatch.setattr(IndexedRepository, "retrieve", capture)
    run_evaluation(original, DATASET.parent, isolated / "cache")
    # Invalid gold fails validation after predictions, rather than steering them.
    with pytest.raises(ValueError, match="Gold location"):
        run_evaluation(changed, DATASET.parent, isolated / "cache")
    assert len(captured) == 14 and captured[:7] == captured[7:]


def test_retrieval_failure_remains_in_all_task_denominators(isolated, monkeypatch):
    from codepilot.retrieval.dataset import load_dataset
    from codepilot.retrieval.documents import RetrievalError
    from codepilot.retrieval.evaluation import run_evaluation
    from codepilot.retrieval.runner import IndexedRepository

    def fail(self, task, options, **kwargs):
        raise RetrievalError("intentional dependency failure")

    monkeypatch.setattr(IndexedRepository, "retrieve", fail)
    report = run_evaluation(load_dataset(DATASET)[:1], DATASET.parent, isolated / "cache")
    for row in report.rows:
        assert len(row.failures) == 1 and row.results == {}
        assert row.metrics.total_tasks == 1 and row.metrics.file_recall[10] == 0
        assert len(row.metrics.missing_predictions) == 1


@pytest.mark.parametrize(
    "extra",
    [
        ["--dataset", "missing.jsonl"],
        ["--dataset", str(DATASET), "--embedding", "fake"],
        ["--dataset", str(DATASET), "--ranking", "none"],
    ],
)
def test_bad_evaluation_inputs_are_explicit(isolated, extra):
    result = CliRunner().invoke(app, ["eval-retrieval", *extra])
    assert result.exit_code == 2, result.output


def test_rrf_and_seed_configuration_is_not_silently_ignored(isolated):
    from codepilot.retrieval.dataset import load_dataset
    from codepilot.retrieval.evaluation import RetrievalEvaluation, run_evaluation
    from codepilot.schemas.retrieval import FusionMethod

    report = run_evaluation(
        load_dataset(DATASET)[:1],
        DATASET.parent,
        isolated / "cache",
        method=FusionMethod.RRF,
        rrf_k=3,
        seed_channel="ripgrep",
    )
    assert RetrievalEvaluation.model_validate_json(report.model_dump_json()) == report
    for row in report.rows:
        assert row.options.rrf_k == 3 and row.options.seed_channel == "ripgrep"
        assert all(result.rrf_k == 3 for result in row.results.values())


def test_environment_failure_counts_as_a_miss(isolated, monkeypatch):
    from contextlib import contextmanager

    import codepilot.retrieval.evaluation as module
    from codepilot.repo.workspace import WorkspaceError
    from codepilot.retrieval.dataset import load_dataset

    @contextmanager
    def unavailable(*args, **kwargs):
        raise WorkspaceError("intentional environment failure")
        yield  # pragma: no cover

    monkeypatch.setattr(module, "indexed_repository", unavailable)
    report = module.run_evaluation(load_dataset(DATASET)[:1], DATASET.parent, isolated / "cache")
    assert all(
        row.metrics.total_tasks == 1 and row.metrics.file_recall[10] == 0 and len(row.failures) == 1
        for row in report.rows
    )
