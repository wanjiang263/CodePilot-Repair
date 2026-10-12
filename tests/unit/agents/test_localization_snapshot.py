import pytest
from pydantic import ValidationError

from codepilot.agents.schemas import LocalizationResult
from codepilot.artifacts import ArtifactStore, RetrievalSnapshotStore, SnapshotError
from codepilot.schemas.retrieval import RankedFile, RetrievalQuery, RetrievalResult


def test_snapshot_round_trip_and_deterministic_id(tmp_path):
    store = RetrievalSnapshotStore(ArtifactStore(tmp_path, "run"))
    retrieval = RetrievalResult(
        query=RetrievalQuery(raw_issue="中文 issue"), snapshot_id="upstream"
    )
    sid = store.save(retrieval)
    assert store.load(sid) == retrieval
    assert store.save(retrieval.model_copy(deep=True)) == sid
    result = LocalizationResult(retrieval_snapshot_id=sid)
    assert LocalizationResult.model_validate_json(result.model_dump_json()) == result
    assert store.load(result.retrieval_snapshot_id).snapshot_id == "upstream"


def test_missing_corrupt_or_invalid_snapshot(tmp_path):
    artifacts = ArtifactStore(tmp_path, "run")
    store = RetrievalSnapshotStore(artifacts)
    sid = store.save(RetrievalResult(query=RetrievalQuery(raw_issue="x")))
    path = store.path_for(sid)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotError):
        store.load(sid)
    path.unlink()
    with pytest.raises(SnapshotError):
        store.load(sid)
    with pytest.raises(SnapshotError):
        store.load("../fake")


def test_result_constraints():
    with pytest.raises(ValidationError):
        LocalizationResult(files=[RankedFile(path=str(i), score=0) for i in range(4)])


def test_atomic_write_failure_never_returns_a_snapshot(tmp_path, monkeypatch):
    from pathlib import Path

    artifacts = ArtifactStore(tmp_path, "run")
    store = RetrievalSnapshotStore(artifacts)

    def fail_replace(self, target):
        raise OSError("atomic commit failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(SnapshotError, match="Cannot commit"):
        store.save(RetrievalResult(query=RetrievalQuery(raw_issue="x")))
    assert list(artifacts.run_dir.rglob("*.json")) == []
    assert list(artifacts.run_dir.rglob("*.tmp")) == []


def test_hash_protects_scores_configuration_and_candidates(tmp_path):
    import json

    store = RetrievalSnapshotStore(ArtifactStore(tmp_path, "run"))
    retrieval = RetrievalResult(
        query=RetrievalQuery(raw_issue="x"),
        files=[RankedFile(path="a.py", score=0.5, pinned=True)],
        channel_weights={"bm25": 0.4},
    )
    sid = store.save(retrieval)
    payload = json.loads(store.path_for(sid).read_text(encoding="utf-8"))
    payload["retrieval"]["files"][0]["score"] = 999
    store.path_for(sid).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotError):
        store.load(sid)
    with pytest.raises(SnapshotError):
        store.save(retrieval)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_file",
        "duplicate_symbol",
        "missing_file_decision",
        "duplicate_file_id",
        "missing_symbol_decision",
        "no_snapshot",
        "blank_reason",
        "wrong_file",
        "range_missing",
        "range_outside",
        "suggestion_outside",
        "too_many_symbols",
    ],
)
def test_result_rejects_untraceable_or_invalid_localization(fault):
    from codepilot.schemas.localization import FileDecision, SymbolDecision
    from codepilot.schemas.retrieval import RankedSymbol

    files = [RankedFile(path="a.py", score=0.4)]
    symbols = [
        RankedSymbol(
            symbol_id="s", qualified_name="a.run", file="a.py", score=0.4, start_line=1, end_line=3
        )
    ]
    fd = [FileDecision(candidate_id="f", path="a.py", reason="file inference")]
    sd = [
        SymbolDecision(
            symbol_id="s",
            file="a.py",
            start_line=2,
            end_line=2,
            file_line_count=3,
            reason="symbol inference",
        )
    ]
    kwargs = {
        "files": files,
        "symbols": symbols,
        "file_decisions": fd,
        "symbol_decisions": sd,
        "retrieval_snapshot_id": "snapshot",
    }
    if fault == "duplicate_file":
        files.append(files[0])
    elif fault == "duplicate_symbol":
        symbols.append(symbols[0])
    elif fault == "missing_file_decision":
        fd.clear()
    elif fault == "duplicate_file_id":
        files.append(RankedFile(path="b.py", score=0.3))
        fd.append(FileDecision(candidate_id="f", path="b.py", reason="inference"))
    elif fault == "missing_symbol_decision":
        sd.clear()
    elif fault == "no_snapshot":
        kwargs["retrieval_snapshot_id"] = ""
    elif fault == "blank_reason":
        fd[0].reason = " "
    elif fault == "wrong_file":
        sd[0].file = "b.py"
    elif fault == "range_missing":
        symbols[0].start_line = None
    elif fault == "range_outside":
        symbols[0].end_line = 4
    elif fault == "suggestion_outside":
        sd[0].start_line = sd[0].end_line = 4
    elif fault == "too_many_symbols":
        symbols.extend(symbols * 5)
    with pytest.raises(ValidationError):
        LocalizationResult(**kwargs)
