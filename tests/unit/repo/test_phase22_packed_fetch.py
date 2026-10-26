from pathlib import Path

from codepilot.repo.gitcmd import run_git
from codepilot.repo.workspace import _local_fetch_base
from support.gitfixture import init_repo


def test_packed_tree_and_binary_blob_keep_their_exact_git_identity(tmp_path: Path) -> None:
    source = init_repo(
        tmp_path / "source", {"nested/a.py": "value = 1\n", "nested/raw.bin": "a\x00b\n"}
    )
    source.git("repack", "-ad")
    tree = source.git("rev-parse", "HEAD^{tree}").strip()
    assert not (source.path / ".git/objects" / tree[:2] / tree[2:]).exists()
    target = tmp_path / "target"
    target.mkdir()
    run_git(["init", "-q"], cwd=target)
    run_git(["config", "core.autocrlf", "false"], cwd=target)
    _local_fetch_base(source.path / ".git", source.head, target, timeout=30)
    run_git(["checkout", "--detach", source.head], cwd=target)
    assert (target / "nested/raw.bin").read_bytes() == b"a\x00b\n"
    assert run_git(["rev-parse", "HEAD^{tree}"], cwd=target).stdout.strip() == tree
    assert run_git(["rev-list", "--all", "--count"], cwd=target).stdout.strip() == "1"
    assert not run_git(["status", "--porcelain"], cwd=target).stdout.strip()
    assert run_git(["fsck", "--full"], cwd=target).returncode == 0
