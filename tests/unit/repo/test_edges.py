from __future__ import annotations

from pathlib import Path

from codepilot.repo.edges import extract_edges
from support.analysis import fixture_context, make_context


def test_fixture_internal_inheritance_and_external_abc(fixtures_dir: Path) -> None:
    context = fixture_context(fixtures_dir, "mini_auth")
    edges = extract_edges(context)
    inheritance = [e for e in edges if e.kind == "INHERIT"]
    pairs = {
        (context.by_id[e.source_id].qualified_name, context.by_id[e.target_id].qualified_name)
        for e in inheritance
    }
    assert ("miniauth.backends.memory.MemoryBackend", "miniauth.backends.base.AuthBackend") in pairs
    assert ("miniauth.session.AdminSession", "miniauth.session.Session") in pairs
    assert all(context.by_id[e.source_id].name != "AuthBackend" for e in inheritance)
    assert all(e.confidence == "high" for e in inheritance)


def test_multiple_and_indirect_inheritance_with_relative_alias() -> None:
    context = make_context(
        {
            "src/pkg/base.py": "class A: pass\nclass B: pass",
            "src/pkg/child.py": "from .base import A as First, B\nclass C(First, B): pass\nclass D(C): pass",
        }
    )
    edges = extract_edges(context)
    inheritance = [e for e in edges if e.kind == "INHERIT"]
    assert [
        (context.by_id[e.source_id].name, context.by_id[e.target_id].name, e.target_name)
        for e in inheritance
    ] == [("C", "A", "First"), ("C", "B", "B"), ("D", "C", "C")]
    references = [e for e in edges if e.kind == "REFERENCE"]
    assert {(e.source_id, e.target_id) for e in references} == {
        (e.source_id, e.target_id) for e in inheritance
    }


def test_external_base_generic_base_and_metaclass_are_distinguished() -> None:
    context = make_context(
        {
            "mod.py": "from external import ABC\nclass Base: pass\nclass Meta: pass\n"
            "class Child(ABC, Base[int], metaclass=Meta): pass"
        }
    )
    edges = extract_edges(context)
    inherit = [e for e in edges if e.kind == "INHERIT"]
    assert len(inherit) == 1
    assert context.by_id[inherit[0].target_id].name == "Base"
    assert inherit[0].target_name == "Base[int]"
    assert any(context.by_id[e.target_id].name == "Meta" for e in edges if e.kind == "REFERENCE")


def test_duplicate_bases_keep_both_low_confidence_edges() -> None:
    context = make_context({"mod.py": "class Base: pass\nclass Base: pass\nclass C(Base): pass"})
    edges = [e for e in extract_edges(context) if e.kind == "INHERIT"]
    assert {context.by_id[e.target_id].qualified_name for e in edges} == {"mod.Base", "mod.Base#2"}
    assert all(e.confidence == "low" for e in edges)


def test_cyclic_inheritance_is_finite_without_transitive_edges() -> None:
    context = make_context({"mod.py": "class A(B): pass\nclass B(A): pass"})
    assert len(extract_edges(context)) == 4


def test_callback_annotation_and_module_references_exclude_declarations_and_calls() -> None:
    context = make_context(
        {
            "core.py": "class Item: pass\ndef helper(): pass",
            "mod.py": "import core as c\nfrom core import Item\n"
            "def consume(value: Item):\n    callback = c.helper\n    c.helper()\n    return Item\n",
        }
    )
    edges = [e for e in extract_edges(context) if context.by_id[e.source_id].path == "mod.py"]
    assert [(e.target_name, e.line) for e in edges] == [("Item", 3), ("c.helper", 4), ("Item", 6)]
    assert all(e.kind == "REFERENCE" and e.confidence == "high" for e in edges)


def test_shadowed_and_unknown_attribute_references_are_not_guessed() -> None:
    context = make_context(
        {
            "mod.py": "class Item: pass\ndef helper(): pass\n"
            "def f(Item, obj):\n    alias = Item\n    callback = obj.helper\n    helper = obj\n"
        }
    )
    assert extract_edges(context) == ()


def test_self_method_reference_is_medium_and_nested_reference_owns_inner_symbol() -> None:
    context = make_context(
        {
            "mod.py": "class C:\n    def work(self): pass\n    def run(self):\n"
            "        def inner():\n            return self.work\n        return inner\n"
        }
    )
    edges = extract_edges(context)
    assert [
        (context.by_id[e.source_id].qualified_name, e.target_name, e.confidence) for e in edges
    ] == [("mod.C.run.<locals>.inner", "self.work", "medium"), ("mod.C.run", "inner", "high")]
