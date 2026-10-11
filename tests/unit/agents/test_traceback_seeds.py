"""C-04：仓库内 traceback 帧 → V-18 置顶种子（复用 Phase 2 表示，不改冻结算法）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.repo.models import Symbol
from codepilot.retrieval.result import build_result
from codepilot.schemas.retrieval import FusionMethod, RankedSymbol, RetrievalQuery

MAIN = "calc/main.py"
REPO_FILES = [MAIN, "calc/util.py"]


def symbol(
    sid: str,
    name: str,
    *,
    path: str = MAIN,
    start: int = 10,
    end: int = 15,
    kind: str = "function",
    qualified_name: str | None = None,
) -> Symbol:
    return Symbol(
        name=name,
        qualified_name=qualified_name or f"calc.main.{name}",
        kind=kind,  # type: ignore[arg-type]
        id=sid,
        path=path,
        start_line=start,
        end_line=end,
    )


SYMBOLS = [
    symbol("s-divide", "divide", start=10, end=14),
    symbol("s-save", "save", start=30, end=45),
    symbol(
        "s-module", "main", path=MAIN, start=1, end=60, kind="module", qualified_name="calc.main"
    ),
    symbol("s-util", "helper", path="calc/util.py", start=3, end=8),
]

ANALYZER = IssueAnalyzer()


def frame_issue(path: str = MAIN, *, line: int = 12, function: str = "divide") -> str:
    return (
        "Boom.\n"
        "Traceback (most recent call last):\n"
        f'  File "{path}", line {line}, in {function}\n'
        "    return a / b\n"
        "ZeroDivisionError: division by zero\n"
    )


def seeds_for(issue: str, repo_files: list[str] | None = None) -> list[RankedSymbol]:
    query = ANALYZER.retrieval_query(issue, repo_files=repo_files if repo_files else REPO_FILES)
    return ANALYZER.traceback_seeds(query, SYMBOLS)


def test_internal_frame_becomes_a_pinned_seed_with_its_evidence() -> None:
    pinned = seeds_for(frame_issue())

    assert [seed.symbol_id for seed in pinned] == ["s-divide"]
    seed = pinned[0]
    assert seed.pinned is True
    assert seed.file == MAIN
    assert (seed.start_line, seed.end_line) == (10, 14)
    assert seed.qualified_name == "calc.main.divide"
    assert f"{MAIN}:12 in divide" in (seed.reason or "")


def test_third_party_and_unindexed_paths_never_become_seeds() -> None:
    external = seeds_for(
        frame_issue("/usr/lib/python3.12/json/__init__.py"),
        repo_files=["json/__init__.py"],
    )
    assert external == []

    unindexed = ANALYZER.retrieval_query(frame_issue(MAIN), repo_files=["other.py"])
    assert ANALYZER.traceback_seeds(unindexed, SYMBOLS) == []


def test_path_traversal_is_rejected_before_pinning() -> None:
    escaping = frame_issue("../calc/main.py")
    query = ANALYZER.retrieval_query(escaping, repo_files=REPO_FILES)

    assert query.parsed_frames[0].in_repo is False
    assert ANALYZER.traceback_seeds(query, SYMBOLS) == []


@pytest.mark.parametrize(
    ("line", "function", "path"),
    [
        (9, "divide", MAIN),
        (15, "divide", MAIN),
        (12, "unrelated_name", MAIN),
        (12, "divide", "calc/util.py"),
        (12, "divide", "missing/file.py"),
    ],
    ids=[
        "before range",
        "after range",
        "name without identity match",
        "wrong file",
        "file not in repo",
    ],
)
def test_name_alone_is_never_enough(line: int, function: str, path: str) -> None:
    assert seeds_for(frame_issue(path, line=line, function=function)) == []


def test_module_level_frame_matches_the_module_symbol() -> None:
    pinned = seeds_for(frame_issue(MAIN, line=5, function="<module>"))

    assert [seed.symbol_id for seed in pinned] == ["s-module"]


def test_repeated_frames_collapse_into_one_seed_but_keep_every_reason() -> None:
    issue = frame_issue() + frame_issue(MAIN, line=13, function="divide")
    pinned = seeds_for(issue)

    assert [seed.symbol_id for seed in pinned] == ["s-divide"]
    assert (pinned[0].reason or "").count(":12") == 1
    assert ":13" in (pinned[0].reason or "")


def test_no_traceback_degrades_to_no_seeds_without_breaking_analysis() -> None:
    issue = "The export button does nothing when clicked."
    query = ANALYZER.retrieval_query(issue, repo_files=REPO_FILES)

    assert query.parsed_frames == []
    assert ANALYZER.traceback_seeds(query, SYMBOLS) == []
    assert ANALYZER.analyze(issue, repo_files=REPO_FILES).traceback_frames == []


def test_multiple_internal_frames_yield_sorted_distinct_seeds() -> None:
    issue = (
        "Crash on save.\n"
        "Traceback (most recent call last):\n"
        f'  File "{MAIN}", line 40, in save\n'
        "    helper()\n"
        '  File "calc/util.py", line 5, in helper\n'
        "    x = 1\n"
        "TypeError: bad\n"
    )
    pinned = seeds_for(issue)

    assert [seed.symbol_id for seed in pinned] == ["s-save", "s-util"]
    assert [seed.file for seed in pinned] == [MAIN, "calc/util.py"]


def test_windows_style_traceback_resolves_to_the_indexed_spelling(tmp_path: Path) -> None:
    native = str(tmp_path / MAIN)
    issue = frame_issue(native)
    query = ANALYZER.retrieval_query(issue, repo_files=REPO_FILES, repo_root=tmp_path)

    assert query.parsed_frames[0].file == MAIN
    assert [seed.symbol_id for seed in ANALYZER.traceback_seeds(query, SYMBOLS)] == ["s-divide"]


def test_seeds_pin_through_the_real_v18_path_without_touching_scores() -> None:
    """种子进入 V-18 后仍然零分、置顶、不参与归一化与融合。"""
    query = ANALYZER.retrieval_query(frame_issue(), repo_files=REPO_FILES)
    candidates = {
        "bm25": [
            RankedSymbol(
                symbol_id="s-divide",
                qualified_name="calc.main.divide",
                file=MAIN,
                score=9.0,
                start_line=10,
                end_line=14,
            ),
            RankedSymbol(
                symbol_id="s-save",
                qualified_name="calc.main.save",
                file=MAIN,
                score=2.0,
                start_line=30,
                end_line=45,
            ),
        ]
    }
    result = build_result(
        query,
        candidates,
        repository_symbols=SYMBOLS,
        fusion_method=FusionMethod.WEIGHTED,
        limit=5,
    )

    assert [item.symbol_id for item in result.fused_symbols] == ["s-divide", "s-save"]
    top = result.fused_symbols[0]
    assert top.pinned is True and top.score == 0.0
    assert ":12 in divide" in (top.reason or "")
    assert result.files[0].path == MAIN and result.files[0].pinned is True
    assert result.per_channel["bm25"][0].symbol_id == "s-divide"


def test_seeding_is_deterministic_across_calls() -> None:
    issue = frame_issue()
    first = seeds_for(issue)
    second = seeds_for(issue)

    assert [seed.model_dump_json() for seed in first] == [seed.model_dump_json() for seed in second]
    assert isinstance(ANALYZER.retrieval_query(issue, repo_files=REPO_FILES), RetrievalQuery)
