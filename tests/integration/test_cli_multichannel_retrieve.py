"""V-23: real indexing and configured retrieval through the public CLI."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codepilot.cli import app
from codepilot.config import get_settings
from codepilot.retrieval.result import validate_result
from codepilot.schemas.retrieval import RetrievalResult
from support.gitfixture import init_repo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/repos/mini_auth"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEPILOT_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_documented_issue_file_command_and_snapshot_copy(isolated):
    issue = FIXTURE.parents[1] / "issues/expired_token.md"
    result = CliRunner().invoke(app, ["retrieve", "--repo", str(FIXTURE), "--issue", str(issue)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    validate_result(RetrievalResult.model_validate(data))
    assert (
        data["files"]
        and data["symbols"]
        and data["execution"]["source_kind"] == "directory_snapshot"
    )
    assert not (FIXTURE / ".git").exists()
    assert data["query"]["raw_issue"] == issue.read_text(encoding="utf-8")


@pytest.mark.parametrize("method", ["weighted", "rrf"])
def test_all_six_channels_real_repo_with_explicit_offline_embedding(isolated, method):
    repo = init_repo(
        isolated / "repo",
        {
            p.relative_to(FIXTURE).as_posix(): p.read_text(encoding="utf-8")
            for p in FIXTURE.rglob("*")
            if p.is_file()
        },
    )
    config = isolated / "retrieval.json"
    config.write_text(
        json.dumps(
            {
                "channels": dict.fromkeys(
                    ["bm25", "ripgrep", "semantic", "structural", "test", "history"], True
                ),
                "method": method,
                "embedding": "offline-hash",
                "weights": {"bm25": 0.4, "semantic": 0.6} if method == "weighted" else None,
            }
        )
    )
    result = CliRunner().invoke(
        app,
        [
            "retrieve",
            "--repo",
            str(repo.path),
            "--issue",
            "test_verify_rejects_garbage verify_token",
            "--config",
            str(config),
            "--limit",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    restored = RetrievalResult.model_validate(data)
    validate_result(restored)
    assert len(data["per_channel"]) == 6 and data["fusion_method"] == method
    assert "not a learned semantic model" in data["execution"]["embedding_description"]
    assert data["execution"]["base_commit"] == repo.head
    assert repo.git("status", "--porcelain") == ""
    assert not list((isolated / "cache/cli-workspaces").iterdir())


def test_cli_structural_only_has_explicit_seed_provenance(isolated):
    result = CliRunner().invoke(
        app,
        ["retrieve", "--repo", str(FIXTURE), "--issue", "Session.login", "--channel", "structural"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert list(data["per_channel"]) == ["structural"]
    assert data["execution"]["structural_seed"] == "bm25 Top-20 (seed source only)"


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--issue", "x", "query"],
        ["--issue", ""],
        ["--issue", "x", "--channel", "invalid"],
        ["--issue", "x", "--channel", "semantic"],
        ["--issue", "x", "--config", "missing.json"],
        ["--issue", "missing.md"],
    ],
)
def test_bad_inputs_fail_explicitly_before_retrieval(isolated, extra):
    result = CliRunner().invoke(app, ["retrieve", "--repo", str(FIXTURE), *extra])
    assert result.exit_code == 2, result.output


def test_offline_vectors_are_order_independent_and_never_use_python_hash():
    import numpy as np

    from codepilot.retrieval.offline import OfflineHashEmbedding

    provider = OfflineHashEmbedding()
    texts = ["verify_token expired", "login password", ""]
    vectors = provider.embed(texts)
    assert vectors.shape == (3, 256) and np.isfinite(vectors).all()
    assert np.array_equal(vectors, provider.embed(texts))
    assert np.array_equal(vectors, provider.embed(list(reversed(texts)))[::-1])
    assert np.count_nonzero(vectors[-1]) == 0
    assert provider.identity.provider == "offline-hash"


def test_inference_runner_cannot_import_evaluation_modules():
    import ast

    source = Path(__file__).resolve().parents[2] / "src/codepilot/retrieval/runner.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assert not any(
        isinstance(n, ast.ImportFrom)
        and n.module in {"codepilot.retrieval.dataset", "codepilot.retrieval.eval"}
        for n in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "data",
    [
        {"method": "rrf", "weights": {"bm25": 0.4}},
        {"weights": {"bm25": float("inf")}},
        {"weights": {"unknown": 1}},
        {"weights": {"lexical": 0.2, "bm25": 0.2}},
        {"rrf_k": 3},
        {"method": "none"},
        {"channels": {"semantic": True}},
    ],
)
def test_invalid_runtime_config_preflight(data):
    from codepilot.retrieval.runner import RetrievalOptions

    with pytest.raises(ValueError):
        RetrievalOptions.model_validate(data)


@pytest.mark.parametrize(
    "text",
    [
        "Please fix handling of README.md",
        "Attach the revoked session log to incident-notes.txt",
        "Sessions end too early.\nUpdate expiry handling in notes.md",
    ],
)
def test_issue_prose_ending_like_a_file_name_stays_text(isolated, text):
    result = CliRunner().invoke(app, ["retrieve", "--repo", str(FIXTURE), "--issue", text])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["query"]["raw_issue"] == text


def test_issue_reads_a_real_file_whose_name_contains_spaces(isolated):
    issue = isolated / "my issue.md"
    issue.write_text("Tokens stop working at the expiry second.", encoding="utf-8")
    result = CliRunner().invoke(app, ["retrieve", "--repo", str(FIXTURE), "--issue", str(issue)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["query"]["raw_issue"] == issue.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["missing.md", "docs/missing.md", "no/such/issue.txt"])
def test_an_absent_issue_path_still_fails_before_retrieval(isolated, path):
    result = CliRunner().invoke(app, ["retrieve", "--repo", str(FIXTURE), "--issue", path])
    assert result.exit_code == 2, result.stdout + result.stderr
    assert "Issue 文件不存在" in result.stdout + result.stderr
