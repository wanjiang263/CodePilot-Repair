from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.repo.imports import ImportResolver
from support.analysis import fixture_context, make_context


def test_fixture_relative_import_and_package_reexport(fixtures_dir: Path) -> None:
    context = fixture_context(fixtures_dir, "mini_auth")
    resolver = ImportResolver(context)
    session = context.module_symbols("miniauth.session")[0]
    bindings = resolver.for_scope(session, "AuthBackend")
    assert len(bindings) == 1
    assert (
        context.by_id[bindings[0].target_ids[0]].qualified_name
        == "miniauth.backends.base.AuthBackend"
    )
    assert not bindings[0].external


@pytest.mark.parametrize(
    ("path", "statement"),
    [
        ("src/pkg/deep/mod.py", "from ..core import f as renamed"),
        ("src/pkg/deep/__init__.py", "from ..core import f as renamed"),
        ("src/pkg/mod.py", "from .core import f as renamed"),
    ],
)
def test_relative_import_levels_and_init_package(path: str, statement: str) -> None:
    context = make_context({"src/pkg/core.py": "def f(): pass", path: statement})
    binding = next(b for b in ImportResolver(context).bindings if b.bound_name == "renamed")
    assert binding.alias == "renamed"
    assert binding.target_module == "pkg.core"
    assert context.by_id[binding.target_ids[0]].qualified_name == "pkg.core.f"


def test_import_dotted_module_aliases_and_multiple_names() -> None:
    context = make_context(
        {
            "pkg/core.py": "def f(): pass",
            "mod.py": "import pkg.core, pkg.core as c\nfrom pkg.core import f as g, missing\n",
        }
    )
    bindings = ImportResolver(context).bindings
    assert [b.bound_name for b in bindings] == ["pkg", "c", "g", "missing"]
    assert bindings[0].target_module == "pkg.core"
    assert bindings[1].alias == "c"
    assert context.by_id[bindings[2].target_ids[0]].name == "f"
    assert bindings[3].target_ids == ()
    assert not bindings[3].external


def test_from_package_import_submodule_and_external_dependency() -> None:
    context = make_context(
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "",
            "mod.py": "from pkg import core as c\nfrom typing import Callable\nimport os.path\n",
        }
    )
    bindings = ImportResolver(context).bindings
    assert bindings[0].target_module == "pkg.core"
    assert len(bindings[0].target_ids) == 1
    assert not bindings[0].external
    assert bindings[1].external and bindings[1].target_ids == ()
    assert bindings[2].external and bindings[2].target_ids == ()


def test_relative_import_beyond_package_is_unresolved() -> None:
    context = make_context({"pkg/mod.py": "from ...core import f", "core.py": "def f(): pass"})
    binding = ImportResolver(context).bindings[0]
    assert binding.target_module is None
    assert binding.target_ids == ()
    assert not binding.external


def test_local_import_keeps_scope_and_repeated_bindings() -> None:
    context = make_context(
        {
            "core.py": "def f(): pass",
            "mod.py": "def outer():\n    from core import f\n    from core import f\n",
        }
    )
    resolver = ImportResolver(context)
    outer = next(s for s in context.symbols if s.name == "outer")
    assert len(resolver.for_scope(outer, "f")) == 2
    assert all(b.source_id == outer.id for b in resolver.bindings)
    assert resolver.for_scope(context.module_symbols("mod")[0], "f") == ()


def test_wildcard_and_circular_exports_do_not_invent_targets() -> None:
    context = make_context(
        {"a.py": "from b import f", "b.py": "from a import f", "mod.py": "from a import *"}
    )
    resolver = ImportResolver(context)
    assert resolver.bindings[-1].wildcard
    assert resolver.bindings[0].target_ids == ()
    assert resolver.bindings[1].target_ids == ()
    assert all(edge.confidence == "low" for edge in resolver.edges())


def test_duplicate_definitions_are_not_overwritten() -> None:
    context = make_context(
        {"core.py": "def f(): pass\ndef f(): pass", "mod.py": "from core import f"}
    )
    resolver = ImportResolver(context)
    assert len(resolver.bindings[0].target_ids) == 2
    assert [context.by_id[id_].qualified_name for id_ in resolver.bindings[0].target_ids] == [
        "core.f",
        "core.f#2",
    ]
    assert all(edge.confidence == "low" for edge in resolver.edges())


def test_duplicate_module_paths_fail_explicitly() -> None:
    with pytest.raises(ValueError, match="符号 ID 冲突"):
        make_context({"src/pkg.py": "", "pkg.py": ""})


def test_module_and_imported_function_can_share_name() -> None:
    context = make_context({"pkg/f.py": "def f(): pass", "mod.py": "from pkg.f import f"})
    binding = ImportResolver(context).bindings[0]
    assert context.by_id[binding.target_ids[0]].qualified_name == "pkg.f.f"
