"""P1: real index diagnostics distinguish execution failures from valid empty ranks."""

import gzip
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

from codepilot.cli import app
from codepilot.config import get_settings
from codepilot.observability.logging import configure_logging, get_logger
from codepilot.repo.index import Indexer
from codepilot.repo.scanner import RepositoryScanner, ScanResult
from codepilot.retrieval.dataset import load_dataset
from codepilot.retrieval.evaluation import RetrievalEvaluation, render_table, run_evaluation
from codepilot.retrieval.runner import IndexedRepository, indexed_repository
from support.gitfixture import init_repo

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DATASET = FIXTURES / "retrieval_cases.jsonl"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEPILOT_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    root_logger = logging.getLogger()
    previous_level, previous_handlers = root_logger.level, root_logger.handlers[:]
    previous_config = structlog.get_config().copy()
    # CliRunner shares a process with direct indexing tests. Replace the cached
    # index logger so ERROR filtering matches a fresh production CLI process.
    configure_logging("ERROR")
    monkeypatch.setattr("codepilot.repo.index.log", get_logger("codepilot.repo.index"))
    try:
        yield tmp_path
    finally:
        # CLI callbacks replace global logging configuration, too. Restore both
        # filtering and pytest capture handlers, so later cache warnings survive.
        structlog.configure(**previous_config)
        root_logger.handlers = previous_handlers
        root_logger.setLevel(previous_level)
        get_settings.cache_clear()


@pytest.fixture
def unreadable(monkeypatch):
    read = Path.read_bytes
    calls = []

    def deny(path):
        if path.as_posix().endswith("/workspace/src/miniauth/plugins.py"):
            calls.append(path)
            raise PermissionError("injected non-Gold file read denied")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", deny)
    return calls


@pytest.mark.parametrize("embedding", ["none", "offline-hash"])
def test_unreadable_non_gold_file_is_an_index_failure_with_deterministic_evidence(
    isolated, unreadable, embedding
):
    cases = load_dataset(DATASET)[:1]
    first = run_evaluation(cases, FIXTURES, isolated / "cache", embedding=embedding)
    second = run_evaluation(cases, FIXTURES, isolated / "cache", embedding=embedding)
    assert len(unreadable) == 2
    assert first.model_dump_json() == second.model_dump_json()
    assert render_table(first) == render_table(second)
    assert RetrievalEvaluation.model_validate_json(first.model_dump_json()) == first
    task = cases[0].task_id
    for row in first.rows:
        if row.status == "NOT RUN":
            assert row.failures == {} and row.metrics is None
            continue
        assert len(row.failures) == 1 and row.results == {} and row.execution == {}
        detail = row.failure_details[task]
        assert detail.stage == "index" and detail.category == "environment"
        assert detail.code == "unreadable" and detail.count == 1
        assert detail.paths == ["src/miniauth/plugins.py"]
        assert detail.scan_counts["unreadable"] == 1 and detail.scan_counts["files"] == 13
        assert row.metrics.total_tasks == 1 and row.metrics.missing_predictions == [task]
        assert (
            row.metrics.file_hits
            == row.metrics.function_hits
            == row.metrics.exact_hits
            == {1: 0, 5: 0, 10: 0}
        )
    assert "environment/index/unreadable" in render_table(first)
    assert not list((isolated / "cache/cli-workspaces").iterdir())


def test_indexstats_unreadable_count_alone_cannot_be_silently_ignored(isolated, monkeypatch):
    index = Indexer.index

    def incomplete(self, stores, workspace):
        stats = index(self, stores, workspace)
        stats.scan["unreadable"] = 2
        return stats

    monkeypatch.setattr(Indexer, "index", incomplete)
    task = load_dataset(DATASET)[0].inference(FIXTURES)
    with (
        pytest.raises(OSError, match="incomplete"),
        indexed_repository(task, isolated / "cache"),
    ):
        pytest.fail("Incomplete index was yielded")


def test_unreadable_root_keeps_its_path_when_no_files_were_scanned(isolated, monkeypatch):
    monkeypatch.setattr(
        RepositoryScanner, "scan", lambda self, root: ScanResult(files=(), unreadable=(".",))
    )
    report = run_evaluation(load_dataset(DATASET)[:1], FIXTURES, isolated / "cache")
    for row in report.rows:
        detail = next(iter(row.failure_details.values()))
        assert detail.paths == ["."]
        assert detail.scan_counts["files"] == 0 and detail.scan_counts["unreadable"] == 1
        assert detail.count == 1 and row.results == {}


@pytest.mark.parametrize(
    "args",
    [
        ["verify_token"],
        ["--issue", "verify_token"],
        ["--issue", "verify_token", "--channel", "bm25,ripgrep"],
    ],
)
def test_retrieve_cli_rejects_incomplete_index_in_legacy_single_and_multi_modes(
    isolated, unreadable, args
):
    command = [
        "--log-level",
        "ERROR",
        "retrieve",
        "--repo",
        str(FIXTURES / "repos/mini_auth"),
        *args,
    ]
    first = CliRunner().invoke(app, command)
    second = CliRunner().invoke(app, command)
    assert first.exit_code == second.exit_code == 1
    assert first.stdout == second.stdout == ""
    assert first.stderr == second.stderr
    assert "incomplete" in first.stderr and "src/miniauth/plugins.py" in first.stderr


def test_eval_cli_saves_failures_prints_reasons_and_exits_nonzero(isolated, unreadable):
    output = isolated / "failed.json"
    command = [
        "--log-level",
        "ERROR",
        "eval-retrieval",
        "--dataset",
        str(DATASET),
        "--output",
        str(output),
    ]
    first = CliRunner().invoke(app, command)
    first_json = output.read_bytes()
    second = CliRunner().invoke(app, command)
    assert first.exit_code == second.exit_code == 1
    assert first.stdout == second.stdout and first.stderr == second.stderr == ""
    assert output.read_bytes() == first_json
    assert "environment/index/unreadable" in first.stdout
    report = RetrievalEvaluation.model_validate_json(first_json)
    assert report.total_tasks == 12
    for row in report.rows:
        assert len(row.failures) == len(row.failure_details) == 12
        assert row.results == {} and row.metrics.total_tasks == 12
        assert row.metrics.file_recall[10] == row.metrics.function_recall[10] == 0


def test_normal_empty_candidates_are_successful_zero_recall(isolated, monkeypatch):
    retrieve = IndexedRepository.retrieve

    def empty(self, task, options, **kwargs):
        return retrieve(replace(self, symbols=()), task, options, **kwargs)

    monkeypatch.setattr(IndexedRepository, "retrieve", empty)
    report = run_evaluation(load_dataset(DATASET)[:1], FIXTURES, isolated / "cache")
    for row in report.rows:
        assert row.failures == {} and getattr(row, "failure_details", {}) == {}
        assert len(row.results) == 1 and row.metrics.missing_predictions == []
        assert all(
            result.symbols == result.files == result.fused_symbols == []
            for result in row.results.values()
        )
        assert row.metrics.file_recall[10] == row.metrics.function_recall[10] == 0
    assert "FAILURE" not in render_table(report)


def test_complete_index_success_retains_prior_raw_candidates_and_execution_json(isolated):
    report = run_evaluation(load_dataset(DATASET)[:1], FIXTURES, isolated / "cache")
    previous = json.loads(
        gzip.decompress(
            (
                FIXTURES.parents[1]
                / "docs/evidence/phase2-retrieval-eval-20261011/retrieval-evaluation.json.gz"
            ).read_bytes()
        )
    )
    for row, old in zip(report.rows, previous["rows"], strict=True):
        assert row.failures == {}
        assert "failure_details" not in row.model_dump()
        task = next(iter(row.results))
        assert row.results[task].model_dump(mode="json") == old["results"][task]
        assert row.execution[task].model_dump(mode="json") == old["execution"][task]


@pytest.mark.parametrize("diagnostic", ["ignored", "too_large", "binary"])
def test_policy_skips_are_nonfatal_and_observable(isolated, monkeypatch, diagnostic):
    index = Indexer.index

    def skipped(self, stores, workspace):
        stats = index(self, stores, workspace)
        stats.scan[diagnostic] = 1
        return stats

    monkeypatch.setattr(Indexer, "index", skipped)
    report = run_evaluation(load_dataset(DATASET)[:1], FIXTURES, isolated / "cache")
    assert all(row.failures == {} for row in report.rows)
    assert all(
        next(iter(row.execution.values())).index_diagnostics.scan_counts[diagnostic] == 1
        for row in report.rows
    )


def test_real_parser_errors_keep_existing_tolerant_indexing_semantics(isolated):
    files = {
        p.relative_to(FIXTURES / "repos/mini_auth").as_posix(): p.read_text(encoding="utf-8")
        for p in (FIXTURES / "repos/mini_auth").rglob("*")
        if p.is_file()
    }
    files["damaged.py"] = "def broken(:\n    pass\n"
    repo = init_repo(isolated / "repo", files)
    case = load_dataset(DATASET)[0]
    (isolated / "reference.patch").write_bytes((FIXTURES / case.gold_patch).read_bytes())
    case = case.model_copy(
        update={"repo": "repo", "base_commit": repo.head, "gold_patch": "reference.patch"}
    )
    report = run_evaluation([case], isolated, isolated / "cache")
    assert all(row.failures == {} for row in report.rows)
    assert all(
        "damaged.py" in next(iter(row.execution.values())).index_diagnostics.parse_errors[0]
        for row in report.rows
    )
    assert repo.git("status", "--porcelain") == ""


@pytest.mark.parametrize(
    "error", [PermissionError("index read denied"), RuntimeError("index backend crashed")]
)
def test_index_backend_exceptions_are_reported_as_failed_tasks(isolated, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(Indexer, "index", fail)
    report = run_evaluation(load_dataset(DATASET)[:1], FIXTURES, isolated / "cache")
    for row in report.rows:
        assert len(row.failures) == 1 and row.results == {}
        detail = next(iter(row.failure_details.values()))
        assert detail.stage == "index" and detail.exception_type == type(error).__name__
        assert detail.message == str(error)
        assert row.metrics.total_tasks == 1 and row.metrics.file_hits[10] == 0


def test_retrieval_backend_exception_is_distinct_from_empty_candidates(isolated, monkeypatch):
    retrieve = IndexedRepository.retrieve

    def fail(self, task, options, **kwargs):
        if options.channels.enabled_channels == ("ripgrep",):
            raise RuntimeError("retrieval backend crashed")
        return retrieve(self, task, options, **kwargs)

    monkeypatch.setattr(IndexedRepository, "retrieve", fail)
    report = run_evaluation(load_dataset(DATASET)[:1], FIXTURES, isolated / "cache")
    row = next(row for row in report.rows if row.name == "ripgrep")
    assert len(row.failures) == 1 and row.results == {}
    detail = next(iter(row.failure_details.values()))
    assert detail.stage == "retrieval" and detail.category == "execution"
    assert detail.exception_type == "RuntimeError" and detail.message == "retrieval backend crashed"
    assert all(not r.failures for r in report.rows if r is not row)


def test_partial_environment_failure_keeps_full_denominator_and_not_run_state(
    isolated, monkeypatch
):
    read = Path.read_bytes
    denied = []

    def deny_once(path):
        if path.as_posix().endswith("/workspace/src/miniauth/plugins.py") and not denied:
            denied.append(path)
            raise PermissionError("one task only")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", deny_once)
    cases = load_dataset(DATASET)[:2]
    report = run_evaluation(cases, FIXTURES, isolated / "cache", embedding="none")
    for row in report.rows:
        if row.status == "NOT RUN":
            assert row.failures == {} and row.metrics is None
            continue
        assert len(row.failures) == 1 and len(row.results) == 1
        assert row.metrics.total_tasks == 2 and row.metrics.missing_predictions == [
            cases[0].task_id
        ]
        assert all(value <= 1 for value in row.metrics.file_hits.values())
