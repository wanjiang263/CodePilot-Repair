from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.repo.calls import extract_calls
from support.analysis import fixture_context, make_context


def test_fixture_same_module_recursion_and_imported_function(fixtures_dir: Path) -> None:
    context = fixture_context(fixtures_dir, "mini_auth")
    calls = extract_calls(context)
    recursive = next(e for e in calls if e.target_name == "count_down")
    assert recursive.confidence == "high"
    assert recursive.source_id == recursive.target_id
    issue = next(e for e in calls if e.target_name == "issue_token")
    assert issue.confidence == "high"
    assert context.by_id[issue.target_id].qualified_name == "miniauth.tokens.issue_token"


@pytest.mark.parametrize(
    ("statement", "call"),
    [
        ("from core import helper as h", "h"),
        ("import core as c", "c.helper"),
        ("import pkg.core", "pkg.core.helper"),
        ("from pkg import core as c", "c.helper"),
    ],
)
def test_alias_and_module_calls(statement: str, call: str) -> None:
    context = make_context(
        {
            "core.py": "def helper(): pass",
            "pkg/core.py": "def helper(): pass",
            "mod.py": f"{statement}\ndef run():\n    {call}()",
        }
    )
    edge = next(e for e in extract_calls(context) if e.target_name == call)
    assert edge.confidence == "high"
    assert context.by_id[edge.target_id].name == "helper"


def test_self_method_and_inherited_method_are_medium() -> None:
    context = make_context(
        {
            "mod.py": "class Base:\n    def helper(self): pass\nclass Child(Base):\n    def run(self):\n        self.helper()\n        self.run()\n"
        }
    )
    edges = extract_calls(context)
    assert [(context.by_id[e.target_id].qualified_name, e.confidence) for e in edges] == [
        ("mod.Base.helper", "medium"),
        ("mod.Child.run", "medium"),
    ]


def test_class_construction_resolves_init() -> None:
    context = make_context(
        {"mod.py": "class C:\n    def __init__(self): pass\ndef run():\n    C()\n"}
    )
    edge = extract_calls(context)[0]
    assert context.by_id[edge.target_id].qualified_name == "mod.C.__init__"
    assert edge.confidence == "medium"


def test_unknown_object_method_uses_low_candidates() -> None:
    context = make_context(
        {
            "mod.py": "class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\ndef f(obj):\n    obj.run()\n"
        }
    )
    edges = extract_calls(context)
    assert {context.by_id[e.target_id].qualified_name for e in edges} == {"mod.A.run", "mod.B.run"}
    assert all(e.confidence == "low" for e in edges)


def test_unresolved_dynamic_and_external_calls_keep_names() -> None:
    context = make_context(
        {
            "mod.py": "import thirdparty as p\ndef f():\n    p.run()\n    factory()()\n    missing()\n"
        }
    )
    edges = extract_calls(context)
    assert {e.target_name for e in edges} == {"p.run", "factory()", "factory", "missing"}
    assert all(e.target_id is None and e.confidence == "low" for e in edges)


@pytest.mark.parametrize("binding", ["helper", "helper: int", "*helper", "**helper"])
def test_parameter_shadowing_never_binds_module_function(binding: str) -> None:
    context = make_context({"mod.py": f"def helper(): pass\ndef f({binding}):\n    helper()\n"})
    edge = extract_calls(context)[0]
    assert edge.target_id is None and edge.confidence == "low"


def test_assignment_and_wildcard_shadowing_are_conservative() -> None:
    context = make_context(
        {
            "mod.py": "def helper(): pass\ndef f():\n    helper = unknown\n    helper()\n",
            "other.py": "from unknown import *\ndef g():\n    missing()\n",
        }
    )
    assert all(e.target_id is None for e in extract_calls(context))


def test_nested_function_and_local_import_have_lexical_scope() -> None:
    context = make_context(
        {
            "core.py": "def helper(): pass",
            "mod.py": "def outer():\n    from core import helper\n    def inner():\n        helper()\n    inner()\n",
        }
    )
    edges = extract_calls(context)
    assert [
        (
            context.by_id[e.source_id].qualified_name,
            context.by_id[e.target_id].qualified_name,
            e.confidence,
        )
        for e in edges
    ] == [
        ("mod.outer.<locals>.inner", "core.helper", "high"),
        ("mod.outer", "mod.outer.<locals>.inner", "high"),
    ]


def test_duplicate_function_calls_retain_all_candidates_at_low_confidence() -> None:
    context = make_context(
        {"mod.py": "def helper(): pass\ndef helper(): pass\ndef f():\n    helper()\n"}
    )
    edges = extract_calls(context)
    assert {context.by_id[e.target_id].qualified_name for e in edges} == {
        "mod.helper",
        "mod.helper#2",
    }
    assert all(e.confidence == "low" for e in edges)


def test_default_and_decorator_calls_belong_to_outer_scope() -> None:
    context = make_context(
        {"mod.py": "def helper(): pass\n@helper()\ndef test_f(value=helper()):\n    helper()\n"}
    )
    edges = extract_calls(context)
    assert [context.by_id[e.source_id].name for e in edges] == ["mod", "mod", "test_f"]


def test_multilevel_diamond_uses_c3_method_order() -> None:
    context = make_context(
        {
            "mod.py": "class Root:\n    def work(self): pass\nclass A(Root): pass\nclass B(Root):\n    def work(self): pass\nclass C(A, B):\n    def run(self):\n        self.work()\n"
        }
    )
    edge = extract_calls(context)[0]
    assert context.by_id[edge.target_id].qualified_name == "mod.B.work"
    assert edge.confidence == "medium"


def test_outer_parameter_shadows_dotted_import() -> None:
    context = make_context(
        {
            "pkg/core.py": "def helper(): pass",
            "mod.py": "import pkg.core\ndef outer(pkg):\n    def inner():\n        pkg.core.helper()\n",
        }
    )
    edge = extract_calls(context)[0]
    assert edge.target_id is None and edge.confidence == "low"


def test_dotted_import_does_not_bind_submodule_members_on_root() -> None:
    context = make_context(
        {
            "pkg/core.py": "def helper(): pass",
            "mod.py": "import pkg.core\npkg.helper()\npkg.core.helper()",
        }
    )
    wrong, right = extract_calls(context)
    assert wrong.target_id is None and wrong.confidence == "low"
    assert context.by_id[right.target_id].qualified_name == "pkg.core.helper"
    assert right.confidence == "high"


@pytest.mark.parametrize("bases", ["External, Base", "Base, External", "Base"])
def test_unknown_or_duplicate_bases_never_imply_certain_method_order(bases: str) -> None:
    context = make_context(
        {
            "mod.py": "class Base:\n    def work(self): pass\nclass Base:\n    def work(self): pass\n"
            + f"class Child({bases}):\n    def run(self):\n        self.work()\n"
        }
    )
    edges = extract_calls(context)
    assert {context.by_id[e.target_id].qualified_name for e in edges} == {
        "mod.Base.work",
        "mod.Base#2.work",
    }
    assert all(e.confidence == "low" for e in edges)


@pytest.mark.parametrize(
    "body", ["return lambda helper: helper()", "return [helper() for helper in items]"]
)
def test_anonymous_and_comprehension_bindings_do_not_fake_high_calls(body: str) -> None:
    context = make_context({"mod.py": "def helper(): pass\ndef f(items):\n    " + body})
    edge = extract_calls(context)[0]
    assert edge.target_id is None and edge.confidence == "low"


def test_cyclic_base_lookup_does_not_loop_or_choose_medium_target() -> None:
    context = make_context(
        {
            "mod.py": "class A(B):\n    def work(self): pass\nclass B(A):\n    def run(self):\n        self.work()"
        }
    )
    edge = extract_calls(context)[0]
    assert context.by_id[edge.target_id].qualified_name == "mod.A.work"
    assert edge.confidence == "low"
