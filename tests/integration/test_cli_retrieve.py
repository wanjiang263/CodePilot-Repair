"""Real CLI retrieval against a committed mini_auth fixture."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codepilot.cli import app
from codepilot.config import get_settings
from support.gitfixture import init_repo


@pytest.mark.parametrize("channel", ["bm25", "ripgrep"])
def test_retrieve_mini_auth(tmp_path, monkeypatch, channel):
    fixture = Path(__file__).resolve().parents[1] / "fixtures/repos/mini_auth"
    files = {
        p.relative_to(fixture).as_posix(): p.read_text(encoding="utf-8")
        for p in fixture.rglob("*")
        if p.is_file() and p.suffix in {".py", ".toml", ".md"}
    }
    repo = init_repo(tmp_path / "repo", files)
    monkeypatch.setenv("CODEPILOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEPILOT_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(
            app,
            [
                "retrieve",
                "Session.login password",
                "--repo",
                str(repo.path),
                "--base-commit",
                repo.head,
                "--channel",
                channel,
                "--limit",
                "3",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["enabled_channels"] == [channel]
        assert 0 < len(data["symbols"]) <= 3
        assert any(
            s["qualified_name"] == "miniauth.session.Session.login"
            for s in data["per_channel"][channel]
        )
        assert data["fusion_method"] == "none"
        assert not list((tmp_path / "cache/cli-workspaces").iterdir())
        assert repo.git("status", "--porcelain") == ""
    finally:
        get_settings.cache_clear()


def test_retrieve_rejects_unknown_channel():
    result = CliRunner().invoke(app, ["retrieve", "issue", "--repo", ".", "--channel", "semantic"])
    assert result.exit_code == 2
