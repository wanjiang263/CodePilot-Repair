"""Windows MSYS local-transport fallback for workspace materialization."""

from __future__ import annotations

from pathlib import Path

from codepilot.repo.gitcmd import run_git
from codepilot.repo.workspace import _copy_local_bare, _local_fetch_base


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    run_git(["add", name], cwd=repo)
    run_git(
        ["-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", message],
        cwd=repo,
    )
    return run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()


def test_local_copy_and_fetch_copy_only_base_ancestry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_git(["init", "--quiet"], cwd=source)
    base = _commit(source, "a.txt", "base\n", "base")
    later = _commit(source, "a.txt", "later\n", "later")

    cache = tmp_path / "cache" / "repo"
    _copy_local_bare(source, cache, timeout=60)
    assert run_git(["rev-parse", "HEAD"], cwd=cache).stdout.strip() == later

    target = tmp_path / "workspace"
    run_git(["init", "--quiet", "--template=", str(target)])
    _local_fetch_base(cache, base, target, timeout=60)
    run_git(["checkout", "--detach", "--quiet", base], cwd=target)

    assert (target / "a.txt").read_text(encoding="utf-8") == "base\n"
    present = run_git(["cat-file", "-e", later], cwd=target, check=False)
    assert present.returncode != 0, (
        "a later commit must not leak into the workspace object database"
    )
