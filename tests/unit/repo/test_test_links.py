from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.repo.test_links import extract_test_links
from support.analysis import fixture_context, make_context


def test_fixture_direct_calls_have_high_confidence(fixtures_dir: Path) -> None:
    context = fixture_context(fixtures_dir, "mini_calc")
    links = extract_test_links(context)
    assert len(links) == 2
    assert {context.by_id[e.source_id].name for e in links} == {
        "test_divide_returns_float",
        "test_divide_by_zero_raises_value_error",
    }
    assert all(e.target_name == "calc.core.divide" and e.confidence == "high" for e in links)


@pytest.mark.parametrize("statement", ["import core as c", "from core import process as p"])
def test_import_and_test_name_without_calls_are_medium(statement: str) -> None:
    context = make_context(
        {
            "core.py": "def process(): pass",
            "tests/test_behavior.py": statement + "\ndef test_process_result(): pass",
        }
    )
    links = extract_test_links(context)
    assert len(links) == 1
    assert links[0].target_name == "core.process" and links[0].confidence == "medium"


@pytest.mark.parametrize("path", ["tests/test_core.py", "tests/core_test.py"])
def test_filename_convention_without_imports_is_low(path: str) -> None:
    context = make_context(
        {"src/core.py": "def process(): pass", path: "def test_behavior(): pass"}
    )
    links = extract_test_links(context)
    assert {e.target_name for e in links} == {"core", "core.process"}
    assert all(e.confidence == "low" for e in links)


def test_same_pair_keeps_only_highest_confidence_and_first_direct_call_line() -> None:
    context = make_context(
        {
            "core.py": "def process(): pass",
            "tests/test_core.py": "from core import process\ndef test_process():\n    process()\n    process()\n",
        }
    )
    links = extract_test_links(context)
    function = [e for e in links if e.target_name == "core.process"]
    assert len(function) == 1
    assert function[0].confidence == "high" and function[0].line == 3
    assert len({(e.source_id, e.target_id) for e in links}) == len(links)


def test_fixture_duplicate_definitions_and_duplicate_tests_keep_distinct_pairs(
    fixtures_dir: Path,
) -> None:
    fixture = fixture_context(fixtures_dir, "mini_pkg")
    sources = {file.path: file.parsed.root.text for file in fixture.files}
    sources["tests/test_behavior.py"] = (
        "from pkg.duplicates import platform_path\n"
        "def test_platform_path(): pass\ndef test_platform_path(): pass"
    )
    context = make_context(sources)
    links = extract_test_links(context)
    assert len(links) == 6
    assert len({e.source_id for e in links}) == 2
    assert {e.target_name for e in links} == {
        "pkg.duplicates.platform_path",
        "pkg.duplicates.platform_path#2",
        "pkg.duplicates.platform_path#3",
    }
    assert all(e.confidence == "medium" for e in links)


def test_unknown_receiver_candidates_are_not_promoted_to_high() -> None:
    context = make_context(
        {
            "core.py": "class C:\n    def execute(self): pass",
            "tests/test_behavior.py": "def test_behavior(obj):\n    obj.execute()",
        }
    )
    link = extract_test_links(context)[0]
    assert link.target_name == "core.C.execute" and link.confidence == "low"


def test_nested_helper_and_default_calls_are_not_direct_test_body_calls() -> None:
    context = make_context(
        {
            "core.py": "def process(): pass",
            "tests/test_behavior.py": "from core import process\ndef test_behavior(value=process()):\n"
            "    def helper():\n        process()\n    helper()",
        }
    )
    assert extract_test_links(context) == ()


def test_fixture_test_methods_link_explicit_constructors(fixtures_dir: Path) -> None:
    context = fixture_context(fixtures_dir, "mini_auth")
    links = extract_test_links(context)
    constructors = [e for e in links if e.target_name == "miniauth.session.Session.__init__"]
    assert len(constructors) >= 2
    assert all(
        context.by_id[e.source_id].kind == "test_method" and e.confidence == "high"
        for e in constructors
    )


def test_external_imports_and_other_test_local_imports_do_not_imply_links() -> None:
    context = make_context(
        {
            "core.py": "def process(): pass",
            "tests/test_behavior.py": "import external\ndef test_process():\n    external.process()\n"
            "def helper():\n    from core import process",
        }
    )
    assert extract_test_links(context) == ()


def test_medium_beats_filename_low_without_direct_call() -> None:
    context = make_context(
        {
            "core.py": "def process(): pass",
            "tests/test_core.py": "import core\ndef test_process(): pass",
        }
    )
    links = extract_test_links(context)
    link = next(e for e in links if e.target_name == "core.process")
    assert link.confidence == "medium"
