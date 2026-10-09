from __future__ import annotations

import pytest

from codepilot.repo.calls import extract_calls
from support.analysis import make_context


@pytest.mark.parametrize("count", [0, 1, 5, 6, 10])
def test_unknown_method_expansion_threshold(count: int) -> None:
    classes = "\n".join(f"class C{i}:\n    def run(self): pass" for i in range(count))
    context = make_context({"mod.py": classes + "\ndef caller(obj):\n    obj.run()\n"})
    edges = extract_calls(context)
    assert all(e.target_name == "obj.run" and e.confidence == "low" for e in edges)
    if 1 <= count <= 5:
        assert len(edges) == count
        assert {context.by_id[e.target_id].qualified_name for e in edges} == {
            f"mod.C{i}.run" for i in range(count)
        }
    else:
        assert len(edges) == 1
        assert edges[0].target_id is None


def test_known_self_and_class_calls_are_not_suppressed() -> None:
    classes = "\n".join(
        f"class C{i}:\n    def run(self): pass\n    def __init__(self): pass" for i in range(6)
    )
    context = make_context(
        {
            "mod.py": classes
            + "\nclass Caller(C0):\n    def call(self):\n        self.run()\n        C0()\n"
        }
    )
    edges = extract_calls(context)
    assert [(context.by_id[e.target_id].qualified_name, e.confidence) for e in edges] == [
        ("mod.C0.run", "medium"),
        ("mod.C0.__init__", "medium"),
    ]


def test_duplicate_methods_count_as_distinct_candidates() -> None:
    context = make_context(
        {
            "mod.py": "class C:\n"
            + "    def run(self): pass\n" * 6
            + "def caller(obj):\n    obj.run()\n"
        }
    )
    assert len([s for s in context.symbols if s.kind == "method"]) == 6
    edge = extract_calls(context)[0]
    assert edge.target_id is None and edge.target_name == "obj.run"


def test_explicit_external_receiver_does_not_expand_internal_names() -> None:
    context = make_context(
        {"mod.py": "import external as obj\nclass C:\n    def run(self): pass\nobj.run()\n"}
    )
    edge = extract_calls(context)[0]
    assert edge.target_id is None and edge.confidence == "low"
