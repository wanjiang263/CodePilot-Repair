import importlib.util
from pathlib import Path

import pytest


def load_collector():
    path = Path(__file__).resolve().parents[3] / "scripts/collect_failures.py"
    spec = importlib.util.spec_from_file_location("collector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collection_is_repeatable_sanitized_and_never_overwrites_golden(tmp_path):
    collector = load_collector()
    source = tmp_path / "input"
    source.mkdir()
    (source / "junit.xml").write_text(
        '<testsuite><testcase classname="test_a" name="test_x"><failure type="ValueError" message="api_key=abcdef123456">a.py:1: ValueError</failure></testcase></testsuite>'
    )
    (source / "broken.xml").write_text("<broken")
    output = tmp_path / "output"
    output.mkdir()
    (output / "expected.json").write_text('{"manual": true}')
    first = collector.collect(source, output, project_root=tmp_path)
    second = collector.collect(source, output, project_root=tmp_path)
    assert first["collected"] == 1 and first["errors"] == 1
    assert second["collected"] == 0 and second["duplicates"] == 1
    assert "abcdef123456" not in next(output.glob("*.xml")).read_text()
    assert (output / "expected.json").read_text() == '{"manual": true}'
    with pytest.raises(ValueError):
        collector.collect(source, tmp_path.parent / "escape", project_root=tmp_path)
    with pytest.raises(ValueError):
        collector.collect(source, source / "nested", project_root=tmp_path)
