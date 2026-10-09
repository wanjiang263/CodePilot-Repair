"""R-01 / R-02 / R-03 的证明：workspace 隔离与 bare clone 缓存。

这些测试跑**真实的 git**，不 mock。理由见 ``tests/support/gitfixture.py``：
这一层的全部风险都在 git 的真实行为里（换行符转换、worktree 的 HEAD 语义、
bare 仓库的引用布局），mock 掉 git 之后测试只能证明"代码调用了自己打算调用的
命令"，证明不了那条命令做对了。

最关键的一组是 ``test_original_repo_is_never_modified`` 系列：04 FR-01 要求
"运行前后原目录无变化"。它是本系统唯一一条**会伤害用户**的失败模式——
把用户的仓库改坏，比跑不出结果严重得多。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from codepilot.fsutil import remove_tree
from codepilot.repo.workspace import (
    Workspace,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceMode,
    dirty_paths,
    normalize_source,
    repo_hash,
    repository_id,
)
from support.gitfixture import FixtureRepo, init_repo

# --------------------------------------------------------------- 规范化与哈希


def test_url_with_and_without_dot_git_normalize_the_same() -> None:
    a = normalize_source("https://github.com/psf/requests")
    b = normalize_source("https://github.com/psf/requests.git")
    c = normalize_source("https://github.com/psf/requests/")
    assert a.normalized == b.normalized == c.normalized


def test_url_case_is_preserved() -> None:
    """不做小写化：自建 Git 与本地路径区分大小写，统一小写会让两个仓库撞车。"""
    a = normalize_source("https://git.example.com/Team/Repo")
    b = normalize_source("https://git.example.com/team/repo")
    assert a.normalized != b.normalized


def test_scp_like_url_is_recognized_as_remote() -> None:
    src = normalize_source("git@github.com:psf/requests.git")
    assert src.is_local is False
    assert src.normalized == "ssh://git@github.com/psf/requests"


def test_local_path_is_recognized(tmp_path: Path) -> None:
    src = normalize_source(str(tmp_path))
    assert src.is_local is True
    assert src.local_path == tmp_path.resolve()


def test_windows_path_spellings_share_one_cache(tmp_path: Path) -> None:
    """``C:\\a\\b`` 与 ``C:/a/b`` 是同一个目录，缓存键必须一致。"""
    a = normalize_source(str(tmp_path))
    b = normalize_source(tmp_path.as_posix())
    assert repo_hash(a) == repo_hash(b)


def test_file_url_is_local_but_keeps_its_scheme(tmp_path: Path) -> None:
    """``file://`` 按本地路径处理，但 normalized 保留原样——03 §29 的白名单
    校验需要能把它识别出来并拒绝。"""
    src = normalize_source(f"file://{tmp_path.as_posix()}")
    assert src.is_local is True
    assert src.normalized.startswith("file://")


def test_empty_source_is_rejected() -> None:
    with pytest.raises(WorkspaceError, match="为空"):
        normalize_source("   ")


def test_repo_hash_ignores_commit() -> None:
    """缓存按仓库共享：换 commit 不该重新克隆（02 §6）。"""
    assert repo_hash("https://example.com/a/b") == repo_hash("https://example.com/a/b")


def test_repository_id_differs_by_commit() -> None:
    """同一个仓库的两个 commit 必须是两个 repository_id。

    否则两个 commit 的 symbol 会挂到同一个 id 上，检索结果里混进已改名的
    函数——症状极难定位。
    """
    a = repository_id("https://example.com/a/b", "a" * 40)
    b = repository_id("https://example.com/a/b", "b" * 40)
    assert a != b


def test_repository_id_is_stable() -> None:
    assert repository_id("https://example.com/a/b", "a" * 40) == repository_id(
        "https://example.com/a/b", "a" * 40
    )


# --------------------------------------------------------------- 缓存


@pytest.fixture
def origin(tmp_path: Path) -> FixtureRepo:
    """一个有两处提交的本地仓库，模拟"用户提供的本地路径"。"""
    repo = init_repo(
        tmp_path / "origin",
        {"src/app.py": "def f():\n    return 1\n", ".gitignore": ".venv/\n*.pyc\n"},
    )
    repo.write("src/util.py", "def g():\n    return 2\n")
    sha = repo.commit("add util")
    return FixtureRepo(path=repo.path, commits=[*repo.commits, sha])


@pytest.fixture
def manager(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(cache_root=tmp_path / "cache" / "repos")


def test_cache_is_a_bare_clone(manager: WorkspaceManager, origin: FixtureRepo) -> None:
    cache, src = manager.ensure_cache(str(origin.path))
    assert src.is_local is True
    assert (cache / "HEAD").exists()
    # bare 仓库没有工作副本。
    assert not (cache / ".git").exists()
    assert manager.rev_parse(cache, origin.head) == origin.head


def test_repeated_calls_reuse_the_cache(manager: WorkspaceManager, origin: FixtureRepo) -> None:
    """重复调用不重克隆。用"放一个哨兵文件"来证明，而不是看耗时——
    耗时断言在不同机器上会飘。"""
    cache, _ = manager.ensure_cache(str(origin.path))
    sentinel = cache / "SENTINEL"
    sentinel.write_text("keep me", encoding="utf-8")

    again, _ = manager.ensure_cache(str(origin.path))
    assert again == cache
    assert sentinel.exists(), "第二次调用重新克隆了缓存"


def test_half_finished_cache_is_rebuilt(manager: WorkspaceManager, origin: FixtureRepo) -> None:
    """中断的克隆会留下 HEAD 却没有对象库。只检查 HEAD 存在就当成可用，
    会让后续 rev-parse 报"commit 不存在"，把排查引向完全错误的方向。"""
    cache, _ = manager.ensure_cache(str(origin.path))
    # 必须用 remove_tree：git 的对象文件是只读的，shutil.rmtree 在 Windows 上
    # 会抛 PermissionError——这正是 remove_tree 存在的理由。
    assert remove_tree(cache / "objects") is True
    (cache / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    rebuilt, _ = manager.ensure_cache(str(origin.path))
    assert (rebuilt / "objects").is_dir()
    assert manager.rev_parse(rebuilt, origin.head) == origin.head


def test_missing_local_path_gives_an_actionable_error(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(WorkspaceError) as info:
        manager.ensure_cache(str(missing))
    assert "本地路径不存在" in str(info.value)


def test_non_git_directory_gives_an_actionable_error(
    manager: WorkspaceManager, tmp_path: Path
) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(WorkspaceError) as info:
        manager.ensure_cache(str(plain))
    assert ".git" in str(info.value)


def test_local_paths_are_refused_when_not_allowed(tmp_path: Path, origin: FixtureRepo) -> None:
    """API 层用 allow_local=False 构造（03 §29：拒绝 file:// 与本地路径）。"""
    manager = WorkspaceManager(cache_root=tmp_path / "cache", allow_local=False)
    with pytest.raises(WorkspaceError) as info:
        manager.ensure_cache(str(origin.path))
    assert "03 §29" in str(info.value)


# --------------------------------------------------------------- rev-parse


def test_short_sha_resolves_to_full_sha(manager: WorkspaceManager, origin: FixtureRepo) -> None:
    cache, _ = manager.ensure_cache(str(origin.path))
    assert manager.rev_parse(cache, origin.head[:8]) == origin.head


def test_branch_name_resolves_to_sha(manager: WorkspaceManager, origin: FixtureRepo) -> None:
    cache, _ = manager.ensure_cache(str(origin.path))
    assert manager.rev_parse(cache, "main") == origin.head


def test_tag_resolves_to_the_commit_not_the_tag_object(
    manager: WorkspaceManager, origin: FixtureRepo
) -> None:
    """``^{commit}`` 后缀的作用：传 annotated tag 时解析到 commit 本身。"""
    origin.git("tag", "-a", "v1", "-m", "release")
    cache, _ = manager.ensure_cache(str(origin.path))
    assert manager.rev_parse(cache, "v1") == origin.head


def test_unknown_revision_returns_none(manager: WorkspaceManager, origin: FixtureRepo) -> None:
    cache, _ = manager.ensure_cache(str(origin.path))
    assert manager.rev_parse(cache, "deadbeef") is None
    assert manager.has_commit(cache, "deadbeef") is False


# --------------------------------------------------------------- workspace


def test_workspace_is_clean_and_detached(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")

    assert ws.path.is_dir()
    assert dirty_paths(ws.path) == []
    assert ws.base_commit == origin.head
    # 游离 HEAD：否则 checkout/commit 会移动缓存里的分支指针。
    assert ws.path.joinpath(".git").is_dir(), "默认（fetch）模式的 .git 应当是一个目录"
    head_ref = origin.git  # 保持 origin 存活到断言之后（见下）
    assert head_ref is not None
    assert _git(ws.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert _git(ws.path, "rev-parse", "HEAD").strip() == origin.head


def test_workspace_contains_the_committed_files(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    assert (ws.path / "src" / "app.py").read_text(encoding="utf-8") == "def f():\n    return 1\n"
    assert (ws.path / "src" / "util.py").exists()


def test_checkout_semantics_are_pinned(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """换行符与长路径必须**被写进仓库配置**（02 §「Windows 环境差异」）。

    读的是仓库自己的配置文件，不是"git 认为生效的值"。两者的区别是这条测试
    的全部意义：开发机上 autocrlf 是 true 而 CI 上是 false，不钉死会让同一份
    patch 在两台机器上字节不同。

    而且它必须**能失败**——第一版用 ``git config <key>`` 读，而辅助函数自带
    ``-c core.autocrlf=false``，于是把生产代码里写配置的那行删掉测试依然全绿。
    验证方式是删掉 ``_apply_repo_config`` 里的 ``core.autocrlf`` 那一项，
    这条用例必须变红。
    """
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    assert _repo_config(ws.path, "core.autocrlf") == "false"
    assert _repo_config(ws.path, "core.longpaths") == "true"


def test_lf_line_endings_survive_checkout(manager: WorkspaceManager, tmp_path: Path) -> None:
    """仓库里存 LF，workspace 里也必须是 LF。

    带 CRLF 的源码会让 tree-sitter 的行号偏移、patch 全是噪声改动，
    而且 diff 的每一行都"变了"却看不出为什么。
    """
    repo = init_repo(tmp_path / "lf", {"a.py": "x = 1\ny = 2\nz = 3\n"})
    ws = manager.create(source=str(repo.path), base_commit=repo.head, target=tmp_path / "ws")
    raw = (ws.path / "a.py").read_bytes()
    assert b"\r\n" not in raw, f"checkout 之后出现了 CRLF: {raw!r}"


def test_older_commit_can_be_checked_out(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """base_commit 是历史提交时，workspace 里不该有之后的文件。"""
    first = origin.commits[0]
    ws = manager.create(source=str(origin.path), base_commit=first, target=tmp_path / "ws")
    assert (ws.path / "src" / "app.py").exists()
    assert not (ws.path / "src" / "util.py").exists()


def test_missing_base_commit_names_the_commit(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    with pytest.raises(WorkspaceError) as info:
        manager.create(source=str(origin.path), base_commit="0" * 40, target=tmp_path / "ws")
    assert "0" * 40 in str(info.value)
    assert "不在缓存中" in str(info.value)


def test_non_empty_target_is_refused(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    (target / "leftover.py").write_text("stale\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="非空"):
        manager.create(source=str(origin.path), base_commit=origin.head, target=target)


def test_two_commits_share_one_cache_but_get_distinct_ids(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    a = manager.create(
        source=str(origin.path), base_commit=origin.commits[0], target=tmp_path / "ws_a"
    )
    b = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws_b")

    assert repo_hash(a.source) == repo_hash(b.source)
    assert a.repository_id != b.repository_id


def _object_inodes(repo: Path) -> set[tuple[int, int]]:
    """仓库对象目录里全部文件的 ``(设备号, inode)``。

    比较整个集合而不是某个具体对象：对象可能被 gc 打包，那时就没有 loose
    object 可比了，但**打包文件本身**同样会被硬链接——所以"有没有共享 inode"
    这件事要在目录级别问。

    两种布局都要认：非 bare 在 ``.git/objects``，bare（缓存就是）在
    ``objects``。只认前者的后果很隐蔽——bare 那边永远返回空集，交集恒为空，
    断言无论如何都通过，于是测试看起来在验证"两边不共享 inode"，实际什么
    都没验证。这一版是靠变异测试发现并修掉的。
    """
    candidates = (repo / ".git" / "objects", repo / "objects")
    objects = next((p for p in candidates if p.is_dir()), None)
    if objects is None:  # worktree 模式下 .git 是文件且没有本地对象库
        return set()
    found: set[tuple[int, int]] = set()
    for path in objects.rglob("*"):
        if path.is_file():
            stat = path.stat()
            found.add((stat.st_dev, stat.st_ino))
    return found


def test_copy_mode_creates_an_independent_clone(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """复制模式的 workspace 与缓存**不共享对象文件**。

    这条测试的名字一直是 "independent"，但它原来只断言了 ``.git`` 是一个目录
    ——那只说明"走的是复制模式"，与"独立"无关：把 ``--no-hardlinks`` 删掉，
    它照样全绿。而硬链接恰恰是这里唯一的风险：共享 inode 意味着在 workspace
    里误删对象会连带毁掉缓存，共享的 chmod 也会互相影响。

    所以改成断言那个真正要紧的性质：两边的对象不是同一批 inode。

    **一处诚实的局限**：把 ``_clone_copy`` 的 ``--no-hardlinks`` 删掉，这条
    仍然会通过——因为紧接着的 ``_truncate_history_to`` 会跑
    ``git gc --prune=now``（Q12 要删掉 base 之后的对象），它重新打包了整个
    对象库，硬链接进来的那份 pack 被替换掉了。"两边不共享 inode"这个可观察
    性质于是无论有没有那个参数都成立，变异检测不到。

    那个参数仍然保留：它是 gc 那一步没跑成时的兜底（``gc`` 的 ``check=False``
    意味着失败不会中断创建），此时硬链接就会原样留在磁盘上。
    """
    ws = manager.create(
        source=str(origin.path),
        base_commit=origin.head,
        target=tmp_path / "ws",
        mode="copy",
    )
    assert ws.path.joinpath(".git").is_dir(), "复制模式的 .git 应当是一个目录"
    assert dirty_paths(ws.path) == []
    assert (ws.path / "src" / "util.py").exists()

    cache = manager.cache_path(str(origin.path))
    assert not (_object_inodes(ws.path) & _object_inodes(cache)), (
        "workspace 与缓存共享了对象文件的 inode——在 workspace 里误删或 chmod 对象会连带影响缓存"
    )


def test_original_is_never_modified(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """04 FR-01：运行前后原目录无变化。这是最不能出错的一条。"""
    before_status = origin.git("status", "--porcelain")
    before_head = origin.git("rev-parse", "HEAD").strip()
    before_branches = origin.git("branch", "--format=%(refname)")
    before_mtimes = {
        p.relative_to(origin.path).as_posix(): p.stat().st_mtime_ns
        for p in origin.path.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }

    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    # 在 workspace 里做各种破坏性操作——原目录必须毫发无伤。
    (ws.path / "src" / "app.py").write_text("broken\n", encoding="utf-8")
    (ws.path / "new_file.py").write_text("new\n", encoding="utf-8")
    (ws.path / "src" / "util.py").unlink()
    manager.reset(ws)

    assert origin.git("status", "--porcelain") == before_status
    assert origin.git("rev-parse", "HEAD").strip() == before_head
    assert origin.git("branch", "--format=%(refname)") == before_branches
    after_mtimes = {
        p.relative_to(origin.path).as_posix(): p.stat().st_mtime_ns
        for p in origin.path.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    assert after_mtimes == before_mtimes, "原目录里的文件被改动过"


def test_uncommitted_changes_do_not_leak_into_the_workspace(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """原目录里未提交的改动只存在于工作树，不该出现在 workspace。

    这条很重要：用户本地有一份"正在进行中"的修改是很常见的，
    而评测必须从 base_commit 的确定状态开始。
    """
    origin.write("src/app.py", "def f():\n    return 999  # 未提交的实验\n")
    assert origin.git("status", "--porcelain").strip() != ""

    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    assert "999" not in (ws.path / "src" / "app.py").read_text(encoding="utf-8")
    # 原目录的改动仍在。
    assert "999" in (origin.path / "src" / "app.py").read_text(encoding="utf-8")


def _junction(link: Path, target: Path) -> None:
    """在 ``link`` 处建一个指向 ``target`` 的 junction。

    junction 是 Windows 上唯一**不需要管理员权限、也不需要开发者模式**就能
    创建的目录级重解析点，所以它是默认配置的机器上真实存在的逃逸形态。
    非 Windows 平台跳过调用方的用例。
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"无法创建 junction: {completed.stderr.strip() or completed.stdout.strip()}")


needs_junction = pytest.mark.skipif(
    sys.platform != "win32", reason="junction 是 Windows 特有的重解析点"
)


@needs_junction
def test_reset_does_not_write_through_a_junction_into_the_original_repo(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """**reset 不能穿过 workspace 里的 junction 去改用户的原仓库。**

    实测（修复前，Windows 11 + git 2.51.1）：把 workspace 的 ``src`` 换成一个
    指向用户仓库 ``src`` 的 junction，再调 ``manager.reset(ws)``——用户未提交的
    ``app.py`` 被 ``git checkout -- .`` 沿着链接用 base 版本覆盖，用户未跟踪的
    ``wip_notes.txt`` 被 ``git clean -fd`` 删掉。之后用户 ``git status`` 是干净
    的，改动没有任何痕迹。

    这里断言的正是那两样东西：一份未提交的修改，和一份未跟踪的文件。
    """
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    origin.write("src/app.py", "def f():\n    return 999  # 用户正在改的东西\n")
    origin.write("src/wip_notes.txt", "用户的草稿\n")

    shutil.rmtree(ws.path / "src")
    _junction(ws.path / "src", origin.path / "src")

    manager.reset(ws)

    assert (origin.path / "src" / "app.py").read_text(encoding="utf-8") == (
        "def f():\n    return 999  # 用户正在改的东西\n"
    ), "reset 穿过 junction 把用户未提交的改动覆盖掉了"
    assert (origin.path / "src" / "wip_notes.txt").exists(), (
        "reset 穿过 junction 删掉了用户未跟踪的文件"
    )


@needs_junction
def test_remove_does_not_delete_through_a_junction(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """**remove 不能穿过 workspace 里的 junction 去删用户的原仓库。**

    实测（修复前）：``git worktree remove --force`` 在 Windows 上把 junction 当
    普通目录，沿它递归删除目标内容，**退出码 0、stderr 为空**。指向
    ``cache/repos/<hash>`` 的话，整个共享缓存连同其他任务的 worktree 注册都会
    被清空。

    这里把 junction 指向一个与仓库无关的目录——"删除别人的文件"这件事本身
    与目标是什么无关。
    """
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    outside = tmp_path / "用户的另一个目录"
    outside.mkdir()
    (outside / "重要文件.txt").write_text("不该被删\n", encoding="utf-8")
    (outside / "sub").mkdir()
    (outside / "sub" / "deep.txt").write_text("也不该被删\n", encoding="utf-8")

    _junction(ws.path / "trap", outside)

    manager.remove(ws)

    assert not ws.path.exists(), "workspace 本身应当被删掉"
    assert (outside / "重要文件.txt").exists(), "remove 穿过 junction 删掉了工作区外的文件"
    assert (outside / "sub" / "deep.txt").exists(), "remove 递归穿过了 junction 的子树"


@needs_junction
def test_reset_removes_a_junction_that_is_not_tracked(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """存活的链接会一直留在那里，所以 reset 必须把它摘掉而不只是绕过。

    如果只是"这次别写穿它"，下一轮同样的事情会再来一遍——而 workspace 是要
    交给 agent 反复使用的。摘掉才是终态：base commit 里没有这个链接，回退到
    base 就应当没有它。
    """
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    _junction(ws.path / "sneaky", outside)

    manager.reset(ws)

    assert not (ws.path / "sneaky").is_symlink()
    assert not os.path.lexists(ws.path / "sneaky"), "junction 没有被摘掉，下一轮还会在那里"
    assert outside.is_dir(), "摘链接不该连带删掉目标目录"


# --------------------------------------------------------------- reset / remove


def test_reset_restores_modified_and_deleted_files(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    (ws.path / "src" / "app.py").write_text("modified\n", encoding="utf-8")
    (ws.path / "src" / "util.py").unlink()
    (ws.path / "added.py").write_text("added\n", encoding="utf-8")

    manager.reset(ws)

    assert dirty_paths(ws.path) == []
    assert (ws.path / "src" / "app.py").read_text(encoding="utf-8").startswith("def f()")
    assert (ws.path / "src" / "util.py").exists()
    assert not (ws.path / "added.py").exists()


def test_reset_keeps_ignored_files(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """``clean -fd`` 不带 ``-x``：``.venv`` 这类构建产物是环境搭建的成果，
    回退代码不该把它们删掉，否则下一轮要重装依赖。"""
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    venv = ws.path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "site.py").write_text("# 已装好的依赖\n", encoding="utf-8")

    manager.reset(ws)

    assert (venv / "site.py").exists()


def test_reset_does_not_move_the_base_commit(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    manager.reset(ws)
    assert _git(ws.path, "rev-parse", "HEAD").strip() == origin.head


def test_dirty_workspace_is_detected(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    (ws.path / "src" / "app.py").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="不干净"):
        ws.assert_clean()


def test_remove_deletes_the_workspace_and_deregisters_the_worktree(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    ws = manager.create(
        source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws", mode="worktree"
    )
    cache, _ = manager.ensure_cache(str(origin.path))

    manager.remove(ws)

    assert not ws.path.exists()
    listing = _git(cache, "worktree", "list", "--porcelain")
    assert str(ws.path).replace("\\", "/") not in listing.replace("\\", "/")


def test_remove_is_idempotent(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    manager.remove(ws)
    manager.remove(ws)  # 第二次不应抛异常


# --------------------------------------------------------------- 收尾


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, f"git {' '.join(args)} 失败: {completed.stderr}"
    return completed.stdout


def _repo_config(cwd: Path, key: str) -> str | None:
    """读仓库**自己**的配置，不注入 ``-c``，也不合并全局配置。

    不能用 ``_git(cwd, "config", key)``：``_git`` 固定注入
    ``-c core.autocrlf=false``，而 ``git config <key>`` 会把命令行 ``-c``
    当成生效值打印出来。于是那条断言实际断言的只是它自己传的 ``-c``——
    把生产代码里写配置的那行删掉，测试照样全绿。

    ``--local`` 让全局配置也参与不进来：开发机上 autocrlf 常常是 ``true``，
    读到全局值会让这条断言在本地与 CI 上给出不同结论。
    """
    completed = subprocess.run(
        ["git", "config", "--local", "--get", key],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    # 退出码 1 就是"这个键没设置"，是有效答案而不是错误。
    return completed.stdout.strip() if completed.returncode == 0 else None


# --------------------------------------------------------------- 缓存完整性与并发
#
# 这一组对应一轮对抗性审查提出的、当时**无人验证**的发现。每一条都先在本机
# 复现过（脚本与实测输出记在 docs/05 §5.2），再修的——不是照着描述改代码。


def test_concurrent_ensure_cache_in_one_process(tmp_path: Path, origin: FixtureRepo) -> None:
    """同一进程里多个线程同时处理同一个仓库，**每一个都要成功**。

    默认部署是进程内 worker 并行跑 benchmark，所以这条路径是常走的。修复前
    暂存目录名是 ``<hash>.tmp-<pid>``——同进程里 pid 相同，于是先到的线程看到
    这个路径"已存在"就按"上次崩溃的残骸"把它删了，而那是另一个线程正在写的
    克隆。实测 4 线程时 3 个失败，报错是
    ``destination path '...tmp-<pid>' already exists``，指向一个临时目录名，
    排查的人完全看不出真实原因。
    """
    import threading

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def work() -> None:
        barrier.wait()  # 尽量让它们真的撞在一起
        try:
            manager.ensure_cache(str(origin.path))
            result = "ok"
        except Exception as exc:  # 要把失败类型带回主线程，否则线程里的异常
            # 只会打到 stderr 而测试照样绿。
            result = type(exc).__name__
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes == ["ok"] * 6, f"并发 ensure_cache 有失败: {outcomes}"
    residue = [p.name for p in (tmp_path / "cache").iterdir() if ".tmp-" in p.name]
    assert residue == [], f"暂存目录没有被收干净: {residue}"


def test_stale_staging_from_a_dead_process_is_swept(tmp_path: Path, origin: FixtureRepo) -> None:
    """别的进程崩溃留下的残骸要被清掉——每一份都是一整个 bare clone。

    原来的清理只认 ``.tmp-<当前pid>`` 这一个名字，于是别的 pid 留下的永远
    没人管，磁盘悄悄涨。这里用一个几乎不可能存活的 pid 来代表"崩溃的进程"。
    """
    from codepilot.fsutil import pid_alive

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    cache, _ = manager.ensure_cache(str(origin.path))

    dead_pid = 999_999_999
    assert not pid_alive(dead_pid), "前置条件：这个 pid 不该是活的"
    stale = cache.with_name(f"{cache.name}.tmp-{dead_pid}-deadbeef")
    stale.mkdir()
    (stale / "leftover").write_text("half a bare clone\n", encoding="utf-8")

    # 换个 cache_root 也会命中同一路径，这里直接再调一次让它去扫。
    manager.ensure_cache(str(origin.path))

    assert not stale.exists(), "崩溃进程留下的暂存目录没有被清掉"


def test_staging_of_a_live_process_is_not_swept(tmp_path: Path, origin: FixtureRepo) -> None:
    """还活着的进程的暂存目录不能碰——那正是并发下互相破坏的来源。

    这条与上一条是一对：只清"没有主的"，不能因为名字像残骸就删。
    """
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    cache, _ = manager.ensure_cache(str(origin.path))

    live = cache.with_name(f"{cache.name}.tmp-{os.getpid()}-cafebabe")
    live.mkdir()
    (live / "in-progress").write_text("someone is cloning\n", encoding="utf-8")

    manager.ensure_cache(str(origin.path))

    assert live.exists(), "删掉了本进程自己的暂存目录（并发时会破坏正在写的克隆）"
    remove_tree(live, ignore_errors=True)


def test_damaged_cache_is_rejected_and_healed(tmp_path: Path, origin: FixtureRepo) -> None:
    """缓存损坏要能自愈，而不是从此每次都失败。

    修复前 ``_cache_usable`` 只看 ``HEAD``/``objects``/``refs`` 三个目录在不在。
    实测把 base commit 的 loose object 删掉之后它仍然返回 ``True``，于是残缺
    缓存被长期接受，每次 ``create()`` 都以 "base_commit 不在缓存中" 失败——
    **永不自愈**，而报错提示"该 commit 可能在克隆之后才被推送"，把人引向远端。
    """
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    cache, _ = manager.ensure_cache(str(origin.path))

    loose = cache / "objects" / origin.head[:2] / origin.head[2:]
    assert loose.exists(), "前置条件：base 是 loose object（新克隆的仓库都是）"
    loose.chmod(0o666)  # git 写的对象是只读的
    loose.unlink()

    assert not manager._cache_usable(cache), "残缺缓存仍被判为可用"

    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")
    assert (ws.path / "src" / "util.py").exists(), "缓存没能自愈重建"


def test_cache_does_not_share_object_files_with_a_local_origin(
    tmp_path: Path, origin: FixtureRepo
) -> None:
    """缓存与用户仓库不能共享对象文件的 inode。

    ``git clone`` 对本地路径默认做硬链接。实测修复前缓存里的对象与用户
    ``.git/objects`` 里的是同一个 inode、``nlink=2``——"缓存只读、原仓库绝不
    修改"就只在命令层面成立：缓存按设计必须可写（worktree 的 index 与 commit
    都写在缓存里），而 ``remove_tree`` 为了删只读对象会 ``chmod``，那是在改
    **共享 inode**，一次误操作就静默改坏用户的对象库。
    """
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    cache, _ = manager.ensure_cache(str(origin.path))

    assert not (_object_inodes(cache) & _object_inodes(origin.path)), (
        "缓存硬链接了用户仓库的对象文件——缓存按设计必须可写，"
        "一次对共享 inode 的 chmod 或删除就会静默改坏用户的对象库"
    )


def test_rebuilding_a_cache_in_use_is_refused(tmp_path: Path, origin: FixtureRepo) -> None:
    """缓存坏了、但上面还挂着活跃 workspace 时，**拒绝重建**。

    实测修复前：任务 A 拿着 workspace，任务 B 发现缓存不可用走了修复分支，
    ``remove_tree`` 把缓存删掉再克隆——A 的文件还在，但它的 ``.git`` 没了，
    ``git status`` 变成 ``fatal: not a git repository``。A 已有的改动再也出不了
    diff，而它自己完全不知道。宁可让 B 失败：B 的报错会写明是哪个 workspace
    挡着，而 A 的数据损坏是无声的。
    """
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    # 只有 worktree 模式依赖缓存活着；fetch / copy 的 workspace 自带对象库。
    ws = manager.create(
        source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws", mode="worktree"
    )
    cache = manager.cache_path(str(origin.path))

    (cache / "HEAD").unlink()  # 让缓存不可用，触发修复分支

    with pytest.raises(WorkspaceError, match="活跃 workspace"):
        manager.ensure_cache(str(origin.path))

    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws.path, capture_output=True
        ).returncode
        == 0
    ), "任务 A 的 workspace 被弄坏了"


def test_failed_create_leaves_nothing_behind(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create()`` 中途失败要把目录和 worktree 注册一起收掉。

    实测修复前：worktree 建好之后后段失败，target 目录留在磁盘上、注册也留在
    缓存里。后果是用同一个 target 重试会撞上"目标目录已存在且非空"，而缓存的
    ``worktrees/`` 会一次次堆积。
    """
    cache, _ = manager.ensure_cache(str(origin.path))
    target = tmp_path / "runs" / "ws"

    def boom(self: Workspace) -> None:
        raise WorkspaceError("模拟 create() 后段失败")

    # 打在 assert_clean 上：它正好是"worktree 已经建好、目录已经注册"之后的第一
    # 步，也就是回滚逻辑要覆盖的那个位置。
    monkeypatch.setattr(Workspace, "assert_clean", boom)

    with pytest.raises(WorkspaceError, match="模拟"):
        manager.create(
            source=str(origin.path), base_commit=origin.head, target=target, mode="worktree"
        )

    assert not target.exists(), "失败的 workspace 目录没有被回滚"
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=cache, capture_output=True, text=True
    ).stdout
    # 只剩 bare 缓存自己那一条，没有悬空的 workspace 注册。
    assert listed.count("worktree ") == 1, f"worktree 注册没有回滚:\n{listed}"


# --------------------------------------------------------------- 信息隔离（03 §11.2）
#
# 03 §11.2 是硬性规则：agent 只能看到 base commit 处的仓库**以及截至 base 的
# 历史**。SWE-bench 的 gold patch 就是 base 之后的那个提交，所以这一组断言的
# 不是"结果好不好的问题"——泄露了，resolve 率这个数字就不能报告（03 §2.2）。


def _later_commit(origin: FixtureRepo) -> str:
    """在 base 之后再造一个提交，它就是"答案"。"""
    origin.write("src/app.py", "def f():\n    return 2  # THE FIX\n")
    return origin.commit("fix: return 2")


@pytest.mark.parametrize("mode", ["fetch", "copy"])
def test_self_contained_workspace_has_no_commits_after_base(
    tmp_path: Path, mode: WorkspaceMode
) -> None:
    """自包含模式（fetch / copy）：workspace 里不能存在 base 之后的提交。

    fetch 模式靠"从不传入"，copy 模式靠"传进来再截断"，两条路径都要钉住。
    tag ``v-fix`` 指向答案：fetch 若漏掉 ``--no-tags``，git 的自动跟随会把它
    连同它指向的提交一起拉进来。

    复制模式的 git 目录就在 workspace 内部，``git clone`` 会把远端全部分支与
    标签一起复制进来。实测修复前：

    * ``git log --all`` → ``473f4ad fix: return 2 | 2721cb4 initial``
    * ``git show v-fix:app.py`` → ``'def f():\n    return 2  # THE FIX'``

    而 workspace 是要挂进沙箱的——容器里跑的是仓库自己的测试代码，它能读。
    """
    origin = init_repo(tmp_path / "origin", {"src/app.py": "def f():\n    return 1\n"})
    base_commit = origin.head
    fix = _later_commit(origin)
    origin.git("tag", "v-fix")
    assert fix != base_commit

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    ws = manager.create(
        source=str(origin.path),
        base_commit=base_commit,
        target=tmp_path / "ws",
        mode=mode,
    )

    tags = subprocess.run(["git", "tag"], cwd=ws.path, capture_output=True, text=True).stdout
    assert "v-fix" not in tags.split(), "指向 base 之后的 tag 被带进了 workspace"

    log = subprocess.run(
        ["git", "log", "--all", "--oneline"], cwd=ws.path, capture_output=True, text=True
    ).stdout
    assert fix[:7] not in log, f"workspace 的 --all 里能看到 base 之后的提交:\n{log}"

    show = subprocess.run(
        ["git", "show", f"{fix}:src/app.py"], cwd=ws.path, capture_output=True, text=True
    )
    assert show.returncode != 0, "workspace 里能直接读出 base 之后的文件内容"

    # 只删引用是不够的：对象还在库里，`git cat-file --batch-all-objects` 能列出来。
    objects = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"],
        cwd=ws.path,
        capture_output=True,
        text=True,
    ).stdout
    assert fix not in objects.split(), "base 之后的 commit 对象仍然留在对象库里"


@pytest.mark.parametrize("mode", ["fetch", "copy"])
def test_self_contained_workspace_keeps_the_ancestors_of_base(
    tmp_path: Path, mode: WorkspaceMode
) -> None:
    """截断不能把 base 的祖先也删掉——R-13 要按祖先读修改历史。"""
    origin = init_repo(tmp_path / "origin", {"src/app.py": "def f():\n    return 1\n"})
    first = origin.commits[0]
    origin.write("src/app.py", "def f():\n    return 1  # second\n")
    second = origin.commit("second")
    _later_commit(origin)  # base 之后还有一个提交，它必须消失

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    ws = manager.create(
        source=str(origin.path),
        base_commit=second,
        target=tmp_path / "ws",
        mode=mode,
    )

    for ancestor in (first, second):
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", ancestor], cwd=ws.path, capture_output=True
            ).returncode
            == 0
        ), f"祖先提交 {ancestor[:7]} 被误删了"

    assert (
        subprocess.run(
            ["git", "log", "--oneline", "HEAD"], cwd=ws.path, capture_output=True, text=True
        ).stdout.count("\n")
        == 2
    ), "base 的祖先历史不完整"
    # 截断之后 workspace 仍然是一个能正常工作的仓库。
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws.path, capture_output=True, text=True
        ).returncode
        == 0
    )


def test_worktree_gitdir_lives_outside_the_workspace(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """worktree 模式的隔离**靠的是沙箱只挂载 workspace**，这条断言钉住那个前提。

    worktree 与缓存共享对象库和全部 refs，所以从 workspace 里能看到缓存的引用，
    本地删不干净——那种模式在容器里的安全性完全依赖"git 目录不在挂载范围内"。
    实测 worktree 模式下 ``git log --all`` 与 ``git show <base 之后的提交>``
    都能拿到答案。

    这里断言的是那个前提本身：``.git`` 是一个**文件**（内容指向缓存里的
    gitdir），且它指向的路径在 workspace 之外。若有人改动 worktree 的创建方式
    让 gitdir 落进 workspace 内部，这条会变红——沙箱的挂载策略就不再成立。
    """
    ws = manager.create(
        source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws", mode="worktree"
    )

    dot_git = ws.path / ".git"
    assert dot_git.is_file(), "worktree 模式下 .git 应当是一个指向 gitdir 的文件"

    declared = dot_git.read_text(encoding="utf-8").strip()
    assert declared.startswith("gitdir:"), f".git 内容不是 gitdir 指针: {declared!r}"
    gitdir = Path(declared.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (ws.path / gitdir).resolve()

    assert not gitdir.is_relative_to(ws.path.resolve()), (
        f"worktree 的 gitdir 落在 workspace 内部了: {gitdir}\n"
        "沙箱只挂载 workspace 就不再能挡住对缓存引用的访问，"
        "03 §11.2 的隔离会失效。"
    )
    assert gitdir.exists(), "gitdir 不存在——沙箱里 git 会完全不可用"
    assert ws.self_contained is False, "worktree 的 git 元数据在缓存里，不是自包含的"


def test_self_contained_distinguishes_the_modes(tmp_path: Path) -> None:
    """三种模式在"自包含"这件事上的差别，用一个属性问出来。

    | 模式 | `.git` | 自包含 | 容器内（只挂 /workspace）git 可用 |
    |---|---|---|---|
    | fetch（默认） | 目录 | 是 | 是 |
    | copy | 目录 | 是 | 是 |
    | worktree | 文件（宿主绝对路径） | 否 | **否** |

    worktree 那一行是实测的：容器里执行任何 git 命令都报
    ``fatal: not a git repository: <宿主路径>``，因为 02 §13 只挂载
    ``/workspace``，而 gitdir 在缓存里。做成属性是为了让沙箱层和 R-13 能直接
    断言，而不是靠"记得文档里那条规则"。
    """
    origin = init_repo(tmp_path / "origin", {"src/app.py": "def f():\n    return 1\n"})
    manager = WorkspaceManager(cache_root=tmp_path / "cache")

    def make(mode: WorkspaceMode) -> Workspace:
        return manager.create(
            source=str(origin.path), base_commit=origin.head, target=tmp_path / mode, mode=mode
        )

    assert make("worktree").self_contained is False, (
        "worktree 的 gitdir 在缓存里，不该被当成自包含——"
        "沙箱只挂 workspace 时容器内 git 会完全不可用"
    )
    assert make("copy").self_contained is True, "复制模式的 .git 是目录，应当自包含"
    assert make("fetch").self_contained is True, "fetch 模式的 .git 是目录，应当自包含"


def test_default_mode_is_fetch(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """默认值是 fetch（docs/05 Q13）。用"看不到 base 之后的提交 + 自包含"来认，
    而不是去读参数默认值——要钉的是行为，不是签名。"""
    base = origin.commits[0]
    later = origin.head
    ws = manager.create(source=str(origin.path), base_commit=base, target=tmp_path / "ws")
    assert ws.self_contained is True
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", later], cwd=ws.path, capture_output=True
        ).returncode
        != 0
    ), "默认模式下能读到 base 之后的提交——默认值被改回了 worktree？"


def test_unknown_mode_is_rejected(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    with pytest.raises(WorkspaceError, match="未知的 workspace 模式"):
        manager.create(
            source=str(origin.path),
            base_commit=origin.head,
            target=tmp_path / "ws",
            mode="clone",  # type: ignore[arg-type]
        )


def test_fetch_mode_keeps_tags_merged_into_base(tmp_path: Path) -> None:
    """fetch 模式保留已合入 base 的 tag，``git describe`` 可用。

    依赖它推导版本号的测试（setuptools-scm、versioneer）在 copy 模式下会因为
    ``fatal: No names found`` 失败——copy 删光了全部 ref。实测 requests v2.31.0
    在 fetch 模式下 describe 输出 ``v2.31.0-0-g147c8511``。

    同时有一个指向 base 之后的 tag：它必须**不在**——describe 能用不能以泄露为代价。
    """
    origin = init_repo(tmp_path / "origin", {"src/app.py": "def f():\n    return 1\n"})
    origin.git("tag", "-a", "v1.0", "-m", "release 1.0")
    origin.write("src/app.py", "def f():\n    return 1  # tweak\n")
    base = origin.commit("tweak")
    _later_commit(origin)
    origin.git("tag", "v2.0")

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    ws = manager.create(source=str(origin.path), base_commit=base, target=tmp_path / "ws")

    tags = _git(ws.path, "tag").split()
    assert tags == ["v1.0"], f"tag 集合不对（应当只有已合入 base 的那个）: {tags}"
    assert _git(ws.path, "describe").strip().startswith("v1.0-1-g"), "fetch 模式下 describe 不可用"


def test_fetch_mode_survives_without_the_cache(tmp_path: Path) -> None:
    """fetch 模式的 workspace 不依赖缓存：把缓存挪走（等价于容器里看不到它），
    ``git log`` / ``status`` / ``describe`` 仍然可用。

    这是 worktree 模式做不到的那件事——它的 ``.git`` 指向缓存里的 gitdir。
    """
    origin = init_repo(tmp_path / "origin", {"src/app.py": "def f():\n    return 1\n"})
    origin.git("tag", "-a", "v1.0", "-m", "release")
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")

    (tmp_path / "cache").rename(tmp_path / "cache-hidden")

    for args in (["log", "--oneline"], ["status", "--porcelain"], ["describe"]):
        result = subprocess.run(["git", *args], cwd=ws.path, capture_output=True, text=True)
        assert result.returncode == 0, f"缓存不可见时 git {args[0]} 失败: {result.stderr}"
    assert _git(ws.path, "remote").strip() == "", "fetch 模式不该留下指向缓存的远端"


def test_fetch_mode_does_not_modify_the_cache(
    manager: WorkspaceManager, origin: FixtureRepo, tmp_path: Path
) -> None:
    """缓存是共享只读的：fetch 只从它读，不往它里面写 ref 或 worktree 注册。"""
    cache, _ = manager.ensure_cache(str(origin.path))
    before = _git(cache, "for-each-ref", "--format=%(refname) %(objectname)")

    manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")

    assert _git(cache, "for-each-ref", "--format=%(refname) %(objectname)") == before
    assert not (cache / "worktrees").exists(), "fetch 模式在缓存里注册了 worktree"


def test_fetch_mode_batches_many_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tag 多到超过单条命令行预算时分批 fetch，一个都不能少。

    把预算压到很小来逼出多批，而不是真造上千个 tag。
    """
    import codepilot.repo.workspace as workspace_mod

    origin = init_repo(tmp_path / "origin", {"a.py": "x = 1\n"})
    names = [f"v0.{i}" for i in range(12)]
    for name in names:
        origin.git("tag", "-a", name, "-m", name)
    monkeypatch.setattr(workspace_mod, "_CMDLINE_BUDGET", 60)

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    ws = manager.create(source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws")

    assert sorted(_git(ws.path, "tag").split()) == sorted(names)


def test_batched_respects_the_budget() -> None:
    from codepilot.repo.workspace import _batched

    items = ["a" * 10] * 7
    batches = _batched(items, limit=25)
    assert [x for b in batches for x in b] == items, "分批丢了或改了顺序"
    assert all(sum(len(x) + 1 for x in b) <= 25 for b in batches)
    # 单项超预算也要成批，而不是死循环或丢掉。
    assert _batched(["x" * 100], limit=10) == [["x" * 100]]


def test_worktree_is_the_only_mode_that_exposes_base_descendants(tmp_path: Path) -> None:
    """把"worktree 模式在宿主上会暴露 base 之后的提交"写成一个可执行的事实。

    这不是我们想要的隔离（03 §11.2 要求看不到），而是**已被接受的取舍**：
    worktree 与缓存共享对象库，实测过 ``git clone --reference`` 这条
    alternates 路子，``git show <base 之后的 SHA>`` 仍然返回 0。只要共享对象库，
    就一定枚举得出来。所以默认模式是 fetch，worktree 只作快速但不隔离的选项。

    哪天 worktree 不泄露了、或者 fetch / copy 开始泄露，这条都会变红——
    提示该去更新 docs/05 §23.1 的取舍记录。
    """
    origin = init_repo(tmp_path / "origin", {"src/app.py": "def f():\n    return 1\n"})
    base_commit = origin.head
    fix = _later_commit(origin)
    manager = WorkspaceManager(cache_root=tmp_path / "cache")

    exposed: dict[str, bool] = {}
    for mode in ("worktree", "copy", "fetch"):
        ws = manager.create(
            source=str(origin.path), base_commit=base_commit, target=tmp_path / mode, mode=mode
        )
        show = subprocess.run(
            ["git", "show", f"{fix}:src/app.py"], cwd=ws.path, capture_output=True, text=True
        )
        exposed[mode] = show.returncode == 0

    assert exposed == {"worktree": True, "copy": False, "fetch": False}, (
        f"各模式对 base 之后提交的可见性变了: {exposed}\n请更新 docs/05 §23.1 的取舍记录。"
    )
