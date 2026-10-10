import pytest

from codepilot.repo.models import Symbol
from codepilot.retrieval.documents import RetrievalError, build_documents


@pytest.mark.parametrize("count,truncated", [(60, False), (61, True)])
def test_document_boundary_keeps_first_40_and_last_10(tmp_path, count, truncated):
    lines = [f"line_{i}" for i in range(1, count + 1)]
    (tmp_path / "a.py").write_text("\n".join(lines), encoding="utf-8")
    symbol = Symbol(
        "f",
        "a.f",
        "function",
        id="f",
        path="a.py",
        start_line=1,
        end_line=count,
        signature="def f()",
        docstring="description",
    )
    (doc,) = build_documents(tmp_path, [symbol])
    assert doc.truncated is truncated
    assert doc.text.startswith(
        "path: a.py\nsymbol: a.f\nsignature: def f()\ndocstring: description\ncode:"
    )
    if truncated:
        assert doc.code.splitlines() == [*lines[:40], "# ... truncated ...", *lines[-10:]]
    else:
        assert doc.code.splitlines() == lines


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "C:\\outside.py"])
def test_document_rejects_escape(tmp_path, path):
    with pytest.raises(RetrievalError, match="Unsafe"):
        build_documents(
            tmp_path, [Symbol("f", "f", "function", id="f", path=path, start_line=1, end_line=1)]
        )


def test_missing_duplicate_and_invalid_range(tmp_path):
    symbol = Symbol("f", "a.f", "function", id="f", path="a.py", start_line=1, end_line=1)
    with pytest.raises(RetrievalError, match="Cannot read"):
        build_documents(tmp_path, [symbol])
    (tmp_path / "a.py").write_text("pass\n")
    with pytest.raises(RetrievalError, match="duplicate"):
        build_documents(tmp_path, [symbol, symbol])
    with pytest.raises(RetrievalError, match="Invalid symbol range"):
        build_documents(
            tmp_path,
            [Symbol("f", "a.f", "function", id="f", path="a.py", start_line=2, end_line=4)],
        )


def test_document_rejects_links_before_read(tmp_path, monkeypatch):
    monkeypatch.setattr("codepilot.retrieval.documents.is_link", lambda path: True)
    with pytest.raises(RetrievalError, match="Linked"):
        build_documents(
            tmp_path,
            [Symbol("f", "a.f", "function", id="f", path="a.py", start_line=1, end_line=1)],
        )
