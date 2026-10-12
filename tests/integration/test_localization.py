"""C-12: fixture source → rule/LLM analysis → actual Phase 2 → localization."""

from pathlib import Path

import pytest

from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.schemas import LocalizationResult
from codepilot.llm.errors import StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.repo.graph import RepoGraph
from codepilot.retrieval.config import RetrievalConfig
from codepilot.retrieval.service import retrieve_symbols
from codepilot.schemas.retrieval import FusionMethod
from support.localization import indexed_fixture

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "mini_calc"
SEMANTICS = {
    "issue_type": "bug",
    "summary": "divide 对零除数抛出异常",
    "expected_behavior": "report validation error",
    "observed_behavior": "crashes",
    "keywords": ["divide", "validation"],
    "mentioned_files": ["invented.py"],
    "traceback_frames": [
        {"file": "invented.py", "line": 999, "function": "guessed", "in_repo": True}
    ],
    "error_messages": ["InventedError: root cause proven"],
}


@pytest.fixture
def repo(stores, tmp_path):
    files = {
        p.relative_to(FIXTURE).as_posix(): p.read_text(encoding="utf-8")
        for p in FIXTURE.rglob("*.py")
    }
    return indexed_fixture(stores, tmp_path, files)


def chain(repo, stores, issue, llm):
    analyzer = IssueAnalyzer(llm)
    evidence = analyzer.extract(
        issue, repo_files=(f.path for f in repo.index.files()), repo_root=repo.root
    )
    analysis = analyzer.analyze(issue, evidence=evidence)
    graph = RepoGraph.from_stores(stores, repo.index.repository_id)
    seeds = analyzer.traceback_seeds(evidence.query, repo.index.symbol_objects())
    retrieval = retrieve_symbols(
        repo.root,
        repo.index.symbol_objects(),
        issue,
        seeds=seeds,
        graph=graph,
        config=RetrievalConfig(bm25=True, test=True),
        fusion_method=FusionMethod.WEIGHTED,
        limit=1000,
    )
    return analysis, retrieval


def issue_for(repo):
    divide = next(s for s in repo.definitions if s.name == "divide")
    return (
        "divide in `calc/core.py` crashes with zero divisor.\nSteps to Reproduce\n"
        "1. call divide(1, 0)\n2. observe crash\n"
        "Traceback (most recent call last):\n"
        f'  File "{repo.root / divide.path}", line {divide.start_line + 1}, in divide\n'
        '  File "/usr/lib/external.py", line 3, in outside\n'
        "ValueError: 除数不能为零\n"
    )


def test_full_chain_preserves_facts_evidence_snapshot_and_source(repo, stores, tmp_path):
    issue = issue_for(repo)
    divide = next(s for s in repo.definitions if s.name == "divide")
    original_source = {f.path: (repo.root / f.path).read_bytes() for f in repo.index.files()}
    llm = FakeLLM([SEMANTICS, repo.file_reply(divide.path), repo.symbol_reply([divide])])
    analysis, retrieval = chain(repo, stores, issue, llm)
    original_retrieval = retrieval.model_dump_json()
    localizer = repo.localizer(llm, tmp_path)
    result = localizer.localize(analysis, retrieval)
    assert "invented.py" not in analysis.mentioned_files
    assert analysis.traceback_frames[0].file == divide.path
    assert analysis.traceback_frames[0].line == divide.start_line + 1
    assert not analysis.traceback_frames[-1].in_repo
    assert "InventedError: root cause proven" not in analysis.error_messages
    assert any("除数不能为零" in error for error in analysis.error_messages)
    assert retrieval.enabled_channels == ["bm25", "test"]
    assert retrieval.fused_symbols[0].symbol_id == divide.id
    assert retrieval.fused_symbols[0].pinned and retrieval.fused_symbols[0].score == 0
    assert result.symbols[0] == retrieval.fused_symbols[0]
    assert result.files[0] == retrieval.files[0]
    assert result.symbol_decisions[0].reason != result.symbols[0].reason
    assert result.symbols[0].start_line == divide.start_line
    assert result.symbols[0].end_line == divide.end_line
    assert localizer.snapshots.load(result.retrieval_snapshot_id) == retrieval
    assert retrieval.model_dump_json() == original_retrieval
    assert llm.purposes() == ["issue_analysis", "localizer_files", "localizer_symbols"]
    assert original_source == {p: (repo.root / p).read_bytes() for p in original_source}
    assert repo.git.git("status", "--porcelain") == ""
    assert LocalizationResult.model_validate_json(result.model_dump_json()) == result
    for decision in result.symbol_decisions:
        assert 1 <= decision.start_line <= decision.end_line <= decision.file_line_count


def test_two_complete_runs_are_identical(repo, stores, tmp_path):
    divide = next(s for s in repo.definitions if s.name == "divide")
    outputs = []
    for _ in range(2):
        llm = FakeLLM([SEMANTICS, repo.file_reply(divide.path), repo.symbol_reply([divide])])
        analysis, retrieval = chain(repo, stores, issue_for(repo), llm)
        localizer = repo.localizer(llm, tmp_path)
        result = localizer.localize(analysis, retrieval)
        outputs.append(
            (
                analysis.model_dump_json(),
                retrieval.model_dump_json(),
                result.model_dump_json(),
                localizer.snapshots.path_for(result.retrieval_snapshot_id).read_bytes(),
                llm.prompt_texts(),
            )
        )
    assert outputs[0] == outputs[1]


def test_multifile_chain(repo, stores, tmp_path):
    divide = next(s for s in repo.definitions if s.name == "divide")
    issue = issue_for(repo) + "\nAlso inspect helpers in calc/utils.py"
    analyzer = FakeLLM([SEMANTICS])
    analysis, retrieval = chain(repo, stores, issue, analyzer)
    other = next(
        f
        for f in retrieval.files
        if f.path != divide.path and any(s.path == f.path for s in repo.definitions)
    )
    second = next(
        s
        for s in repo.definitions
        if s.path == other.path and any(r.symbol_id == s.id for r in retrieval.fused_symbols)
    )
    llm = FakeLLM([repo.file_reply(divide.path, other.path), repo.symbol_reply([divide, second])])
    result = repo.localizer(llm, tmp_path).localize(analysis, retrieval)
    assert len(result.files) == 2 and len(result.symbols) == 2


def test_actual_empty_retrieval_has_real_empty_snapshot(repo, stores, tmp_path):
    llm = FakeLLM([{"summary": "unknown query"}])
    analysis, retrieval = chain(repo, stores, "zzzzunknownqwerty", llm)
    assert retrieval.files == []
    localizer = repo.localizer(llm, tmp_path)
    result = localizer.localize(analysis, retrieval)
    assert result.files == result.symbols == []
    assert llm.purposes() == ["issue_analysis"]
    assert localizer.snapshots.load(result.retrieval_snapshot_id) == retrieval


@pytest.mark.parametrize("stage", ["files", "symbols"])
def test_illegal_model_output_propagates_without_false_artifact(repo, stores, tmp_path, stage):
    divide = next(s for s in repo.definitions if s.name == "divide")
    replies = [SEMANTICS] + ([repo.file_reply(divide.path)] if stage == "symbols" else [])
    llm = FakeLLM(replies, default="not JSON")
    analysis, retrieval = chain(repo, stores, issue_for(repo), llm)
    localizer = repo.localizer(llm, tmp_path)
    with pytest.raises(StructuredOutputError):
        localizer.localize(analysis, retrieval)
    assert list(localizer.snapshots.artifacts.run_dir.glob("retrieval_snapshots/*.json")) == []
