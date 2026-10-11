"""P2-1: a repository is recognized by Git, not by a ``.git`` child directory."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codepilot.cli import app
from codepilot.config import get_settings
from codepilot.retrieval.input import RetrievalInput
from codepilot.retrieval.runner import indexed_repository
from support.gitfixture import init_repo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/repos/mini_auth"
SOURCE_FILES = {
    path.relative_to(FIXTURE).as_posix(): path.read_text(encoding="utf-8")
    for path in FIXTURE.rglob("*")
    if path.is_file()
}
TOKENS = "src/miniauth/tokens.py"
BARE_LAYOUT = {"objects", "refs", "hooks"}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEPILOT_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _task(repo: Path, base_commit: str = "HEAD") -> RetrievalInput:
    return RetrievalInput(
        task_id="p2-1",
        repo=str(repo),
        base_commit=base_commit,
        issue="Freshly issued tokens stop working at the exact expiry second.",
    )


def _bare_clone(repo, target: Path) -> str:
    """``git clone --bare`` a fixture repository; return its HEAD SHA."""
    repo.git("clone", "--bare", "--quiet", str(repo.path), str(target))
    return repo.git("rev-parse", "HEAD").strip()


def _indexed_paths(indexed) -> set[str]:
    return {symbol.path for symbol in indexed.symbols}


def test_normal_repository_is_a_git_source(isolated):
    repo = init_repo(isolated / "normal", SOURCE_FILES)
    with indexed_repository(_task(repo.path), isolated / "cache") as indexed:
        assert indexed.source_kind == "git"
        assert indexed.base_commit == repo.head
        assert TOKENS in _indexed_paths(indexed)


def test_bare_repository_is_a_git_source_and_indexes_the_checked_out_tree(isolated):
    repo = init_repo(isolated / "normal", SOURCE_FILES)
    bare = isolated / "bare.git"
    assert _bare_clone(repo, bare) == repo.head

    with indexed_repository(_task(bare), isolated / "cache") as indexed:
        assert indexed.source_kind == "git"
        assert indexed.base_commit == repo.head
        assert TOKENS in _indexed_paths(indexed)
        assert not any(BARE_LAYOUT & set(Path(path).parts) for path in _indexed_paths(indexed))


def test_bare_repository_keeps_base_commit_isolation(isolated):
    repo = init_repo(isolated / "normal", SOURCE_FILES)
    base = repo.head
    repo.write("src/miniauth/late.py", "def only_after_base() -> None:\n    return None\n")
    after = repo.commit("after base")
    assert base != after
    bare = isolated / "late.git"
    _bare_clone(repo, bare)

    for commit, expected in ((base, False), (after, True)):
        with indexed_repository(_task(bare, commit), isolated / "cache") as indexed:
            assert indexed.source_kind == "git" and indexed.base_commit == commit
            assert ("src/miniauth/late.py" in _indexed_paths(indexed)) is expected


def test_linked_worktree_is_a_git_source(isolated):
    repo = init_repo(isolated / "normal", SOURCE_FILES)
    worktree = isolated / "worktree"
    repo.git("worktree", "add", "--quiet", "--detach", str(worktree), repo.head)
    assert (worktree / ".git").is_file()

    with indexed_repository(_task(worktree), isolated / "cache") as indexed:
        assert indexed.source_kind == "git"
        assert indexed.base_commit == repo.head
        assert TOKENS in _indexed_paths(indexed)


def test_plain_directory_keeps_the_snapshot_behaviour(isolated):
    plain = isolated / "plain"
    for relpath, content in SOURCE_FILES.items():
        target = plain / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    with indexed_repository(_task(plain), isolated / "cache") as indexed:
        assert indexed.source_kind == "directory_snapshot"
        assert len(indexed.base_commit) == 40 and TOKENS in _indexed_paths(indexed)

    with (
        pytest.raises(ValueError, match="Directory snapshot has no Git history"),
        indexed_repository(_task(plain, "0" * 40), isolated / "cache"),
    ):
        pytest.fail("A snapshot accepted an explicit base commit")


def test_git_source_still_rejects_the_snapshot_sentinel(isolated):
    repo = init_repo(isolated / "normal", SOURCE_FILES)
    bare = isolated / "rejected.git"
    _bare_clone(repo, bare)
    for path in (repo.path, bare):
        with (
            pytest.raises(ValueError, match="requires an explicit full base SHA"),
            indexed_repository(_task(path, "snapshot"), isolated / "cache"),
        ):
            pytest.fail("A Git source accepted base_commit=snapshot")


def test_cli_retrieve_reports_a_git_source_for_a_bare_repository(isolated):
    repo = init_repo(isolated / "normal", SOURCE_FILES)
    bare = isolated / "cli.git"
    _bare_clone(repo, bare)

    result = CliRunner().invoke(
        app,
        [
            "retrieve",
            "--repo",
            str(bare),
            "--issue",
            "token expiry boundary",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["execution"]["source_kind"] == "git"
    assert data["execution"]["base_commit"] == repo.head
    files = {item["path"] for item in data["files"]}
    assert files and TOKENS in files
