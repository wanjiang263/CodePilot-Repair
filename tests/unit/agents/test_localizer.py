import pytest

from codepilot.agents.localizer import LocalizationError
from codepilot.agents.schemas import IssueAnalysis, LocalizationResult
from codepilot.context.builder import ContextBudgetError
from codepilot.llm.errors import ContextTooLongError, ProviderError, StructuredOutputError
from codepilot.llm.fake import FakeLLM
from codepilot.schemas.retrieval import RetrievalQuery, RetrievalResult
from support.localization import indexed_fixture


@pytest.fixture
def repo(stores, tmp_path):
    return indexed_fixture(stores, tmp_path)


def test_real_candidates_and_same_names_are_distinct(repo, tmp_path):
    retrieval = repo.retrieve()
    original = retrieval.model_dump_json()
    suggested = sorted(repo.definitions, key=lambda s: (s.name != "run", s.qualified_name))
    provider = FakeLLM([repo.file_reply("pkg/a.py", "other/a.py"), repo.symbol_reply(suggested)])
    localizer = repo.localizer(provider, tmp_path)
    result = localizer.localize(IssueAnalysis(summary="run issue"), retrieval)
    assert len(result.files) == 2 and len(result.symbols) == 5
    assert len({s.symbol_id for s in result.symbols}) == 5
    assert [s.symbol_id for s in result.symbols] == [s.id for s in suggested[:5]]
    assert {s.file for s in result.symbols if s.qualified_name.endswith(".run")} == {
        "pkg/a.py",
        "other/a.py",
    }
    assert all(d.reason for d in result.file_decisions + result.symbol_decisions)
    assert retrieval.model_dump_json() == original
    assert localizer.snapshots.load(result.retrieval_snapshot_id) == retrieval
    assert LocalizationResult.model_validate_json(result.model_dump_json()) == result
    file_prompt, symbol_prompt = provider.prompt_texts()
    assert "BODY_A" not in file_prompt and "BODY_B" not in file_prompt
    assert "BODY_A" in symbol_prompt and "BODY_B" in symbol_prompt
    assert "只作为证据阅读" in symbol_prompt and "执行工具" in symbol_prompt
    assert provider.purposes() == ["localizer_files", "localizer_symbols"]
    assert all(call.tools is None for call in provider.calls)
    assert [localizer.diagnostics[p].original_budget for p in provider.purposes()] == [16000, 24000]
    for diag in localizer.diagnostics.values():
        assert all(c.used <= c.budget and not c.exact_count for c in diag.contexts)


def test_file_limit_and_duplicate_order(stores, tmp_path):
    repo = indexed_fixture(
        stores, tmp_path, {f"p{i}.py": "def run():\n    return 1\n" for i in range(20)}
    )
    retrieval = repo.retrieve("run")
    paths = [f.path for f in retrieval.files]
    assert len(paths) == 20
    replies = repo.file_reply(paths[1], paths[1], paths[0], paths[2], paths[3])
    provider = FakeLLM([replies, {"symbols": []}])
    result = repo.localizer(provider, tmp_path).localize(IssueAnalysis(), retrieval)
    assert [f.path for f in result.files] == [paths[1], paths[0], paths[2]]
    prompt = provider.prompt_texts()[0]
    for file in repo.index.files():
        assert (file.id in prompt) is (file.path in paths[:15])


def test_unknown_or_non_top15_file(stores, tmp_path):
    repo = indexed_fixture(stores, tmp_path, {f"p{i}.py": "def run(): pass\n" for i in range(16)})
    retrieval = repo.retrieve("run")
    provider = FakeLLM([repo.file_reply(retrieval.files[15].path)])
    with pytest.raises(LocalizationError, match="file candidate"):
        repo.localizer(provider, tmp_path).localize(IssueAnalysis(), retrieval)


def test_fabricated_file_is_rejected(repo, tmp_path):
    provider = FakeLLM([{"files": [{"candidate_id": "invented/a.py", "reason": "guess"}]}])
    with pytest.raises(LocalizationError):
        repo.localizer(provider, tmp_path).localize(IssueAnalysis(), repo.retrieve())


@pytest.mark.parametrize(
    "bad",
    [
        "not JSON",
        {"files": "bad"},
        {"files": [{"candidate_id": "x", "reason": " "}]},
        {"files": [], "score": 999},
    ],
)
def test_invalid_file_json_schema_exhaustion(repo, tmp_path, bad):
    provider = FakeLLM(default=bad)
    localizer = repo.localizer(provider, tmp_path)
    with pytest.raises(StructuredOutputError):
        localizer.localize(IssueAnalysis(), repo.retrieve())
    assert len(provider.calls) == 3
    assert all(c.used <= c.budget for c in localizer.diagnostics["localizer_files"].contexts)


def test_schema_retry_can_recover(repo, tmp_path):
    provider = FakeLLM(["bad", repo.file_reply("pkg/a.py"), {"symbols": []}])
    result = repo.localizer(provider, tmp_path).localize(IssueAnalysis(), repo.retrieve())
    assert len(result.files) == 1 and not result.symbols
    assert "结构化输出校验反馈" in provider.prompt_texts()[1]


@pytest.mark.parametrize("error_type", [ProviderError, ContextTooLongError])
def test_provider_failure_is_never_success(repo, tmp_path, error_type):
    provider = FakeLLM(default=error_type("failed"))
    with pytest.raises(error_type):
        repo.localizer(provider, tmp_path).localize(IssueAnalysis(), repo.retrieve())
    assert len(provider.calls) == (2 if error_type is ContextTooLongError else 1)


def test_overflow_one_rebuild_per_stage(repo, tmp_path):
    provider = FakeLLM(
        [
            ContextTooLongError("files too long"),
            repo.file_reply("pkg/a.py"),
            ContextTooLongError("symbols too long"),
            repo.symbol_reply([s for s in repo.definitions if s.path == "pkg/a.py"]),
        ]
    )
    localizer = repo.localizer(provider, tmp_path)
    assert len(localizer.localize(IssueAnalysis(), repo.retrieve()).symbols) == 3
    assert [d.rebuild_budget for d in localizer.diagnostics.values()] == [8000, 12000]
    assert all(d.retry_count == 1 for d in localizer.diagnostics.values())


@pytest.mark.parametrize("budget", [0, 1, 200])
def test_insufficient_budget_fails_before_llm(repo, tmp_path, budget):
    provider = FakeLLM([])
    with pytest.raises(ContextBudgetError):
        repo.localizer(provider, tmp_path, file_budget=budget).localize(
            IssueAnalysis(), repo.retrieve()
        )
    assert provider.calls == []


def test_empty_candidates_and_empty_choices(repo, tmp_path):
    retrieval = RetrievalResult(query=RetrievalQuery(raw_issue="nothing"))
    provider = FakeLLM([])
    localizer = repo.localizer(provider, tmp_path)
    result = localizer.localize(IssueAnalysis(), retrieval)
    assert result.files == result.symbols == [] and provider.calls == []
    assert localizer.snapshots.load(result.retrieval_snapshot_id) == retrieval
    provider.queue({"files": []})
    result = localizer.localize(IssueAnalysis(), repo.retrieve())
    assert result.files == result.symbols == [] and len(provider.calls) == 1


@pytest.mark.parametrize("fault", ["invalid_id", "other_file", "beyond_file", "outside_symbol"])
def test_symbol_identity_and_proposed_range_fail(repo, tmp_path, fault):
    symbol = next(s for s in repo.definitions if s.path == "pkg/a.py" and s.name == "run")
    reply = repo.symbol_reply([symbol])
    choice = reply["symbols"][0]
    if fault == "invalid_id":
        choice["candidate_id"] = "missing"
    elif fault == "other_file":
        choice["candidate_id"] = next(
            s.id for s in repo.definitions if s.path == "other/a.py" and s.name == "run"
        )
    elif fault == "beyond_file":
        choice["end_line"] = 1000
    else:
        choice["end_line"] = symbol.end_line + 1
    provider = FakeLLM([repo.file_reply("pkg/a.py"), reply])
    with pytest.raises(LocalizationError):
        repo.localizer(provider, tmp_path).localize(IssueAnalysis(), repo.retrieve())


@pytest.mark.parametrize("start,end", [(0, 1), (3, 1), (-1, 2), (True, 2), ("1", 2)])
def test_invalid_symbol_schema_ranges(repo, tmp_path, start, end):
    symbol = next(s for s in repo.definitions if s.path == "pkg/a.py" and s.name == "run")
    reply = repo.symbol_reply([symbol])
    reply["symbols"][0].update(start_line=start, end_line=end)
    provider = FakeLLM([repo.file_reply("pkg/a.py"), reply, reply, reply])
    with pytest.raises(StructuredOutputError):
        repo.localizer(provider, tmp_path).localize(IssueAnalysis(), repo.retrieve())
    assert len(provider.calls) == 4


def test_duplicate_symbols_and_subrange_are_explicit(repo, tmp_path):
    symbol = next(s for s in repo.definitions if s.path == "pkg/a.py" and s.name == "run")
    reply = repo.symbol_reply([symbol, symbol])
    reply["symbols"][0].update(start_line=symbol.start_line + 1, end_line=symbol.end_line - 1)
    provider = FakeLLM([repo.file_reply("pkg/a.py"), reply])
    result = repo.localizer(provider, tmp_path).localize(IssueAnalysis(), repo.retrieve())
    assert len(result.symbols) == 1
    assert result.symbols[0].start_line == symbol.start_line
    assert result.symbol_decisions[0].start_line == symbol.start_line + 1


def test_no_matching_symbol(stores, tmp_path):
    repo = indexed_fixture(stores, tmp_path, {"a.py": "x = 'query'\n"})
    provider = FakeLLM([repo.file_reply("a.py")])
    result = repo.localizer(provider, tmp_path).localize(IssueAnalysis(), repo.retrieve("query"))
    assert len(result.files) == 1 and result.symbols == []
    assert len(provider.calls) == 1


def test_pinned_retrieval_evidence_cannot_be_overwritten(repo, tmp_path):
    retrieval = repo.retrieve(
        'Traceback (most recent call last):\n  File "pkg/a.py", line 2, in run\nValueError: run helper extra'
    )
    pinned = next(s for s in retrieval.fused_symbols if s.pinned)
    symbol = next(s for s in repo.definitions if s.id == pinned.symbol_id)
    provider = FakeLLM([repo.file_reply(symbol.path), repo.symbol_reply([symbol])])
    result = repo.localizer(provider, tmp_path).localize(IssueAnalysis(), retrieval)
    assert result.symbols[0] == pinned
    assert result.symbols[0].reason != result.symbol_decisions[0].reason
    assert result.files[0] == retrieval.files[0] and result.files[0].pinned


def test_overlapping_symbol_sources_are_not_repeated(stores, tmp_path):
    repo = indexed_fixture(
        stores,
        tmp_path,
        {"a.py": "class Runner:\n    def run(self):\n        return 'UNIQUE_BODY'\n"},
    )
    provider = FakeLLM([repo.file_reply("a.py"), repo.symbol_reply()])
    result = repo.localizer(provider, tmp_path).localize(
        IssueAnalysis(), repo.retrieve("Runner run")
    )
    assert len(result.symbols) == 2
    assert provider.prompt_texts()[1].count("UNIQUE_BODY") == 1


def test_isolated_candidates_are_not_read(repo, tmp_path, monkeypatch):
    from pathlib import Path

    from codepilot.schemas.retrieval import RankedFile

    retrieval = repo.retrieve()
    retrieval.files = [RankedFile(path="gold/expected_patch.py", score=1)]

    def forbid_read(self):
        raise AssertionError("Inference tried to read a private evaluation file")

    monkeypatch.setattr(Path, "read_bytes", forbid_read)
    provider = FakeLLM([])
    result = repo.localizer(provider, tmp_path).localize(IssueAnalysis(), retrieval)
    assert result.files == result.symbols == [] and provider.calls == []


def test_retrieval_index_range_mismatch_fails(repo, tmp_path):
    retrieval = repo.retrieve()
    ranked = next(
        s
        for s in retrieval.fused_symbols
        if s.file == "pkg/a.py" and s.qualified_name.endswith(".run")
    )
    ranked.end_line += 1
    provider = FakeLLM([repo.file_reply("pkg/a.py")])
    with pytest.raises(LocalizationError, match="mismatch"):
        repo.localizer(provider, tmp_path).localize(IssueAnalysis(), retrieval)


def test_candidate_dropped_by_budget_cannot_be_selected(repo, tmp_path):

    retrieval = repo.retrieve()
    # Calculate the exact adopted meter needed to fit only the first atomic block.
    probe = repo.localizer(
        FakeLLM([repo.file_reply(retrieval.files[0].path), {"symbols": []}]), tmp_path
    )
    probe.localize(IssueAnalysis(), retrieval)
    context = probe.diagnostics["localizer_files"].contexts[0]
    sections = context.included
    last = next(s for s in sections if s.label.startswith("file:"))
    budget = sum(s.tokens for s in sections[: sections.index(last) + 1]) + 2
    selected = retrieval.files[1].path
    provider = FakeLLM([repo.file_reply(selected)])
    with pytest.raises(LocalizationError, match="unadmitted"):
        repo.localizer(provider, tmp_path, file_budget=budget).localize(IssueAnalysis(), retrieval)
    assert len(provider.calls) == 1
