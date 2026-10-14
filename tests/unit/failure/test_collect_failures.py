import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from codepilot.observability.redaction import REDACTED
from codepilot.schemas.execution import ExecutionResult


def load_collector():
    path = Path(__file__).resolve().parents[3] / "scripts/collect_failures.py"
    spec = importlib.util.spec_from_file_location("collector", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_structured_secrets_are_removed_from_sample_and_metadata(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    result = ExecutionResult(
        status="ENV_ERROR",
        diagnostics={
            "api_key": "dummy-secret",
            "nested": [{"password": "dummy-password"}],
            "input_tokens": 42,
        },
    )
    (source / "execution.json").write_text(result.model_dump_json())
    output = tmp_path / "output"
    collector = load_collector()
    assert collector.collect(source, output, project_root=tmp_path)["collected"] == 1
    for path in output.glob("*.json"):
        text = path.read_text()
        assert "dummy-secret" not in text and "dummy-password" not in text
        data = json.loads(text)
        diagnostics = (data.get("execution_failure") or data)["diagnostics"]
        assert diagnostics["api_key"] == REDACTED
        assert diagnostics["nested"][0]["password"] == REDACTED
        assert diagnostics["input_tokens"] == 42
    assert collector.collect(source, output, project_root=tmp_path)["duplicates"] == 1


@pytest.mark.parametrize("leaf", [".json", ".metadata.json"])
def test_output_symlink_is_rejected_before_any_write(tmp_path, leaf):
    root = tmp_path / "project"
    source, output = root / "input", root / "output"
    source.mkdir(parents=True)
    output.mkdir()
    value = ExecutionResult(status="ENV_ERROR").model_dump_json()
    (source / "execution.json").write_text(value)
    digest = hashlib.sha256(value.encode()).hexdigest()
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel")
    link = output / f"sample-{digest}{leaf}"
    link.symlink_to(outside)
    report = load_collector().collect(source, output, project_root=root)
    assert report["errors"] == 1 and report["collected"] == 0
    assert outside.read_text() == "sentinel"
    assert list(output.iterdir()) == [link]


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
