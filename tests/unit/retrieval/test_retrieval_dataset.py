"""V-22: reproducible synthetic labels with a separate inference projection."""

import ast
import json
from pathlib import Path

import pytest

from codepilot.repo.symbols import extract_symbols
from codepilot.retrieval.dataset import load_dataset, validate_gold

DATASET = Path(__file__).resolve().parents[2] / "fixtures/retrieval_cases.jsonl"


def symbols(root):
    # Real parser and Symbol extraction, not manually supplied gold IDs.
    from codepilot.repo.parser import default_parser

    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        parsed = default_parser().parse(path.read_bytes())
        found.extend(extract_symbols(parsed, path=rel, repository_id="dataset-validation"))
    return found


def test_at_least_ten_valid_traceable_samples_and_gold_symbol_mapping():
    cases = load_dataset(DATASET)
    assert len(cases) >= 10 and len({c.task_id for c in cases}) == len(cases)
    root = DATASET.parent / "repos/mini_auth"
    indexed = symbols(root)
    for case in cases:
        validate_gold(case, root, indexed, DATASET.parent)
        inference = case.inference(DATASET.parent)
        assert set(type(inference).model_fields) == {"task_id", "repo", "base_commit", "issue"}
        assert (
            "gold" not in inference.model_dump_json() and "patch" not in inference.model_dump_json()
        )
        assert inference.issue == case.issue


def test_dataset_gold_is_external_to_repository():
    cases = load_dataset(DATASET)
    for case in cases:
        assert not (DATASET.parent / case.gold_patch).is_relative_to(DATASET.parent / case.repo)
        assert case.source == "synthetic-development"


@pytest.mark.parametrize(
    "mutation",
    [
        {"gold_files": ["../escape.py"]},
        {"gold_files": ["C:/escape.py"]},
        {"gold_files": ["src\\escape.py"]},
        {"issue": ""},
        {"surprise": True},
        {"gold_locations": []},
        {"source_hashes": {}},
    ],
)
def test_invalid_format_rejected(tmp_path, mutation):
    original = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    original.update(mutation)
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(path)


def test_duplicate_ids_and_empty_file_rejected(tmp_path):
    line = DATASET.read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "bad.jsonl"
    for content in ("", line + "\n" + line):
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            load_dataset(path)


@pytest.mark.parametrize("change", ["qname", "line", "hash", "patch"])
def test_untraceable_gold_rejected(change):
    case = load_dataset(DATASET)[0].model_copy(deep=True)
    if change == "qname":
        case.gold_locations[0].qualified_name = "does.not.exist"
    elif change == "line":
        case.gold_locations[0].line = 1
    elif change == "hash":
        case.source_hashes[case.gold_files[0]] = "0" * 64
    else:
        case.patch_sha256 = "0" * 64
    root = DATASET.parent / "repos/mini_auth"
    with pytest.raises(ValueError):
        validate_gold(case, root, symbols(root), DATASET.parent)


def test_inference_modules_do_not_import_gold_evaluator():
    source = Path(__file__).resolve().parents[3] / "src/codepilot/retrieval"
    for name in ("service.py", "result.py", "fusion.py", "query.py"):
        tree = ast.parse((source / name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(n, ast.ImportFrom)
            and n.module in {"codepilot.retrieval.dataset", "codepilot.retrieval.eval"}
            for n in ast.walk(tree)
        )


def test_gold_patch_under_repository_is_rejected_before_input_projection():
    from codepilot.retrieval.dataset import RetrievalCase

    row = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    row["gold_patch"] = "repos/mini_auth/reference.patch"
    with pytest.raises(ValueError, match="outside"):
        RetrievalCase.model_validate(row).inference(DATASET.parent)


@pytest.mark.parametrize(
    "patch",
    [
        "",
        "garbage",
        "--- a/a.py\n+++ b/b.py\n",
        "--- a/a.py\n+++ b/a.py\n@@ -1 +99 @@\n-old\n+new\n",
    ],
)
def test_malformed_patch_is_not_a_traceable_gold(tmp_path, patch):
    from codepilot.retrieval.dataset import patch_lines

    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    with pytest.raises(ValueError):
        patch_lines(patch, tmp_path)
