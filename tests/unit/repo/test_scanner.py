"""R-04 的证明：RepositoryScanner。

比起 workspace 那一组，这里的失败都不会"伤害用户"，但会**悄悄地改变索引的
内容**——多收一个 40MB 的 JSON 会让索引慢十倍，少收一个 `conftest.py` 会让
回归判定看不见测试基础设施的改动。两种都不会报错，所以只能靠断言钉住。

`.gitignore` 的用例刻意覆盖了几条容易实现错的语义：取反（`!`）的顺序敏感、
尾斜杠只匹配目录、子目录里的 `.gitignore` 只作用于该子树。这些都是"自己写
正则"必然踩到、而交给 `git check-ignore` 就不用管的。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codepilot.repo.scanner import (
    DEFAULT_MAX_FILE_BYTES,
    RepositoryScanner,
    ScanResult,
    is_test_path,
)
from support.gitfixture import FixtureRepo, init_repo

# --------------------------------------------------------------- 基本


@pytest.fixture
def repo(tmp_path: Path) -> FixtureRepo:
    return init_repo(
        tmp_path / "repo",
        {
            "README.md": "# demo\n",
            "src/app.py": "def main():\n    return 0\n",
            "src/util.py": "def helper():\n    return 1\n",
            "tests/test_app.py": "def test_main():\n    assert True\n",
            "Makefile": "all:\n\techo hi\n",
        },
    )


def scan(root: Path, **kwargs: object) -> ScanResult:
    return RepositoryScanner(**kwargs).scan(root)  # type: ignore[arg-type]


def test_scans_the_expected_files(repo: FixtureRepo) -> None:
    result = scan(repo.path)
    assert [f.path for f in result.files] == [
        "Makefile",
        "README.md",
        "src/app.py",
        "src/util.py",
        "tests/test_app.py",
    ]


def test_result_is_sorted_and_repeatable(repo: FixtureRepo) -> None:
    """顺序确定。增量索引要按路径对齐，golden test 要能写死期望值。"""
    first = scan(repo.path)
    second = scan(repo.path)
    assert first.files == second.files
    assert list(first.files) == sorted(first.files, key=lambda f: f.path)


def test_file_metadata_is_accurate(repo: FixtureRepo) -> None:
    result = scan(repo.path)
    app = result.get("src/app.py")
    assert app is not None
    assert app.size == len("def main():\n    return 0\n")
    assert app.line_count == 2
    assert app.is_test is False
    assert len(app.sha256) == 64

    test_file = result.get("tests/test_app.py")
    assert test_file is not None and test_file.is_test is True


def test_sha256_distinguishes_content(repo: FixtureRepo) -> None:
    """哈希是增量索引（R-16）判断"这个文件变了吗"的唯一依据。"""
    before = scan(repo.path).get("src/app.py")
    repo.write("src/app.py", "def main():\n    return 42\n")
    after = scan(repo.path).get("src/app.py")
    assert before is not None and after is not None
    assert before.sha256 != after.sha256


def test_line_count_handles_missing_trailing_newline(repo: FixtureRepo) -> None:
    repo.write("no_newline.py", "a = 1\nb = 2")
    assert scan(repo.path).get("no_newline.py").line_count == 2  # type: ignore[union-attr]


def test_line_count_handles_crlf(repo: FixtureRepo) -> None:
    (repo.path / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    assert scan(repo.path).get("crlf.py").line_count == 2  # type: ignore[union-attr]


def test_result_lookup_and_counts(repo: FixtureRepo) -> None:
    result = scan(repo.path)
    assert len(result) == 5
    assert result.get("does/not/exist.py") is None
    assert result.counts()["files"] == 5
    assert result.skipped == ()


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError, match="不是目录"):
        scan(tmp_path / "nope")


def test_scanning_does_not_modify_the_repository(repo: FixtureRepo) -> None:
    """扫描是只读的。它会调用 git，而 git 会在某些情况下写索引文件，
    所以"扫描之后仓库仍然干净"值得断言一次。"""
    before = repo.git("status", "--porcelain")
    scan(repo.path)
    assert repo.git("status", "--porcelain") == before


# --------------------------------------------------------------- 目录黑名单


@pytest.mark.parametrize(
    "dirname",
    [
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
        "node_modules",
    ],
)
def test_ignored_directories_are_excluded(repo: FixtureRepo, dirname: str) -> None:
    repo.write(f"{dirname}/artifact.py", "x = 1\n")
    result = scan(repo.path)
    assert all(dirname not in f.path for f in result.files)
    assert result.counts()["files"] == 5


def test_ignored_directories_apply_at_any_depth(repo: FixtureRepo) -> None:
    repo.write("src/pkg/build/generated.py", "x = 1\n")
    assert scan(repo.path).get("src/pkg/build/generated.py") is None


def test_a_file_named_build_is_kept(repo: FixtureRepo) -> None:
    """黑名单只作用于**目录**。``build`` 作为文件名是合法的源码。"""
    repo.write("build", "#!/bin/sh\n")
    assert scan(repo.path).get("build") is not None


def test_the_git_directory_is_excluded(repo: FixtureRepo) -> None:
    result = scan(repo.path)
    assert all(".git" not in f.path.split("/") for f in result.files)


def test_a_file_named_dot_git_is_excluded(tmp_path: Path) -> None:
    """worktree 里的 ``.git`` 是**文件**，不是目录。

    只按目录名过滤会漏掉它：那个文件的内容是一行指向缓存仓库的路径，
    会被当成源码索引进仓库的文件列表里。
    """
    plain = tmp_path / "worktree-like"
    plain.mkdir()
    (plain / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = scan(plain)
    assert [f.path for f in result.files] == ["a.py"]


# --------------------------------------------------------------- gitignore


def test_gitignore_patterns_are_applied(repo: FixtureRepo) -> None:
    repo.write(".gitignore", "*.log\nscratch/\n")
    repo.write("debug.log", "noise\n")
    repo.write("scratch/notes.py", "x = 1\n")
    repo.write("kept.py", "x = 1\n")

    result = scan(repo.path)
    assert result.get("debug.log") is None
    assert result.get("scratch/notes.py") is None
    assert result.get("kept.py") is not None
    assert "debug.log" in result.ignored


def test_gitignore_negation_is_honoured(repo: FixtureRepo) -> None:
    """``!keep.log`` 在后面，所以它把前面 ``*.log`` 的排除**撤销**了。
    顺序敏感是 gitignore 最容易实现错的地方。"""
    repo.write(".gitignore", "*.log\n!keep.log\n")
    repo.write("drop.log", "noise\n")
    repo.write("keep.log", "important\n")

    result = scan(repo.path)
    assert result.get("drop.log") is None
    assert result.get("keep.log") is not None


def test_gitignore_trailing_slash_ignores_directories(repo: FixtureRepo) -> None:
    repo.write(".gitignore", "cache/\n")
    repo.write("cache/inner.py", "x = 1\n")

    assert scan(repo.path).get("cache/inner.py") is None


def test_gitignore_trailing_slash_does_not_match_files(repo: FixtureRepo) -> None:
    """``cache/`` 只匹配目录。一个名为 ``cache`` 的**文件**（可执行脚本、数据
    快照都可能是这个名字）不该被它排除掉。"""
    repo.write(".gitignore", "cache/\n")
    repo.write("cache", "not a directory\n")

    result = scan(repo.path)
    assert result.get("cache") is not None
    # 同一个模式不能因为"没匹配上目录"就顺手把文件也算了。
    assert "cache" not in result.ignored


def test_nested_gitignore_applies_to_its_subtree_only(repo: FixtureRepo) -> None:
    repo.write("src/.gitignore", "generated_*.py\n")
    repo.write("src/generated_a.py", "x = 1\n")
    repo.write("generated_b.py", "x = 1\n")

    result = scan(repo.path)
    assert result.get("src/generated_a.py") is None
    assert result.get("generated_b.py") is not None


def test_global_excludes_are_ignored(
    repo: FixtureRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户全局的 ``core.excludesFile`` 必须被屏蔽。

    否则同一份代码在开发机上索引 100 个文件、在 CI 上索引 103 个——因为某人
    的全局 ignore 里写了 ``*.json``。基准数字必须可复现（NFR-01）。
    """
    global_ignore = tmp_path / "global-ignore"
    global_ignore.write_text("*.skipme\n", encoding="utf-8")
    global_config = tmp_path / "gitconfig"
    global_config.write_text(
        f"[core]\n\texcludesFile = {global_ignore.as_posix()}\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    repo.write("data.skipme", "x = 1\n")
    assert scan(repo.path).get("data.skipme") is not None


def test_non_git_directory_scans_without_ignore_rules(tmp_path: Path) -> None:
    """没有 git 的普通目录也能扫（单测与临时目录的场景）。"""
    plain = tmp_path / "plain"
    (plain / "sub").mkdir(parents=True)
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")
    (plain / "sub" / "b.py").write_text("y = 1\n", encoding="utf-8")
    (plain / ".gitignore").write_text("a.py\n", encoding="utf-8")

    result = scan(plain)
    # 没有仓库就没有忽略规则可用，a.py 仍然出现——这是刻意的降级，
    # 不是静默出错：调用方在正常路径上拿到的一定是仓库。
    assert [f.path for f in result.files] == [".gitignore", "a.py", "sub/b.py"]
    assert result.ignored == ()


# --------------------------------------------------------------- 大文件与二进制


def test_oversized_file_is_skipped_and_counted(repo: FixtureRepo) -> None:
    repo.write("big.py", "x = 1\n" * 100)
    result = scan(repo.path, max_file_bytes=64)
    assert result.get("big.py") is None
    assert "big.py" in result.too_large
    assert result.counts()["too_large"] == 1


def test_file_exactly_at_the_limit_is_kept(repo: FixtureRepo) -> None:
    """边界是 ``>`` 而不是 ``>=``：恰好 1MB 的文件应当被收进来。"""
    content = b"a" * 64
    (repo.path / "exact.py").write_bytes(content)
    assert scan(repo.path, max_file_bytes=64).get("exact.py") is not None


def test_default_limit_is_one_megabyte() -> None:
    assert DEFAULT_MAX_FILE_BYTES == 1024 * 1024


def test_binary_file_is_skipped_and_counted(repo: FixtureRepo) -> None:
    (repo.path / "blob.bin").write_bytes(b"PK\x03\x04\x00\x00binary payload")
    result = scan(repo.path)
    assert result.get("blob.bin") is None
    assert "blob.bin" in result.binary


def test_binary_detection_only_sniffs_the_head(repo: FixtureRepo) -> None:
    """只看前 8KB。真实仓库里存在"前面是文本、后面嵌了一段二进制"的文件，
    全文件扫描会把读它们变成索引的主要开销。"""
    (repo.path / "text_then_binary.py").write_bytes(b"x = 1\n" * 2000 + b"\x00\x01\x02")
    assert scan(repo.path).get("text_then_binary.py") is not None


def test_non_utf8_text_file_is_kept(repo: FixtureRepo) -> None:
    """latin-1 编码的源码没有 NUL 字节，属于文本。按扩展名或按能否解码成
    UTF-8 判二进制都会把它误杀。"""
    (repo.path / "latin.py").write_bytes(b"# caf\xe9\nx = 1\n")
    assert scan(repo.path).get("latin.py") is not None


def test_extensionless_text_file_is_kept(repo: FixtureRepo) -> None:
    assert scan(repo.path).get("Makefile") is not None


# --------------------------------------------------------------- 符号链接


def test_symlink_to_a_file_is_skipped(repo: FixtureRepo, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("host secret\n", encoding="utf-8")
    link = repo.path / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 权限受限的环境
        pytest.skip(f"本机不支持创建符号链接: {exc}")

    result = scan(repo.path)
    assert result.get("escape.txt") is None
    assert "escape.txt" in result.symlinks


def test_symlinked_directory_is_not_descended(repo: FixtureRepo, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.py").write_text("secret = 1\n", encoding="utf-8")
    link = repo.path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"本机不支持创建符号链接: {exc}")

    result = scan(repo.path)
    assert result.get("linked/hidden.py") is None
    assert "linked" in result.symlinks


def test_self_referencing_symlink_terminates(repo: FixtureRepo) -> None:
    """指向自己的链接必须让遍历停下来。不停的话扫描会一直走到路径长度上限，
    表现为"索引某个仓库时无限转圈"。"""
    loop = repo.path / "loop"
    try:
        loop.symlink_to(repo.path, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"本机不支持创建符号链接: {exc}")

    result = scan(repo.path)
    assert result.counts()["files"] == 5
    assert "loop" in result.symlinks


@pytest.mark.skipif(sys.platform != "win32", reason="junction 是 Windows 特有的重解析点")
def test_windows_junction_is_not_descended(repo: FixtureRepo, tmp_path: Path) -> None:
    """junction **不是**符号链接：``Path.is_symlink()`` 对它返回 ``False``，
    于是 ``os.walk(followlinks=False)`` 照样会递归进去。

    这条路径在本机是唯一**不需要开发者模式**就能构造的重解析点——符号链接
    要管理员权限或开发者模式，junction 不要。也就是说：一台默认配置的
    Windows 机器上，能构造出来的目录逃逸只有这一种。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.py").write_text("secret = 1\n", encoding="utf-8")

    link = repo.path / "junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:  # pragma: no cover - 受限文件系统
        pytest.skip(f"无法创建 junction: {completed.stderr.strip()}")

    result = scan(repo.path)
    assert result.get("junction/hidden.py") is None, "junction 指向的目录被索引了"
    assert "junction" in result.symlinks


# --------------------------------------------------------------- 读不了的文件


def test_unreadable_file_is_recorded_not_fatal(
    repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个读不了的文件不该让整次索引失败——用户只会在最后发现"没有结果"，
    而原因是一个权限问题。"""
    repo.write("locked.py", "x = 1\n")
    original = Path.read_bytes

    def guarded(self: Path) -> bytes:
        if self.name == "locked.py":
            raise PermissionError(13, "Permission denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", guarded)

    result = scan(repo.path)
    assert result.get("locked.py") is None
    assert "locked.py" in result.unreadable
    assert result.counts()["files"] == 5


def test_unreadable_directory_is_recorded_not_silently_dropped(
    repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**目录**读不了时，整棵子树不能凭空消失。

    ``os.walk`` 的 ``onerror`` 默认是 ``None``——它会把一个目录的 ``scandir``
    失败直接吞掉，连一个空目录都不产出。于是那个子树里的每个文件都从结果里
    消失，而在调用方看来这和"这些文件本来就不存在"完全一样。对定位阶段来说
    这是最坏的一种错：它会理直气壮地把 agent 引向错误结论，且没有任何迹象。

    实测（Windows 11，用 icacls 对子目录 deny RD 后再 scan）：修复前
    ``files`` 只有 `a.py`，``unreadable`` 是空的，``skipped`` 也是空的——
    锁定目录里的文件彻底不存在于任何一栏。
    """
    repo.write("locked/hidden.py", "y = 2\n")
    before = scan(repo.path)
    assert before.get("locked/hidden.py") is not None, "前置条件：修复前它是能被扫到的"

    original = os.scandir

    def guarded(path: object) -> object:
        # os.walk 内部对新目录调 scandir；只对这一个目录失败。
        if str(path).replace("\\", "/").endswith("/locked"):
            raise PermissionError(13, "Permission denied", str(path))
        return original(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", guarded)

    result = scan(repo.path)
    assert result.get("locked/hidden.py") is None
    assert "locked" in result.unreadable, (
        "读不了的目录没有出现在 unreadable 里——它会从 files、unreadable、skipped "
        "三个地方同时消失，调用方无法区分'不存在'和'没能去看'"
    )
    assert "locked" in result.skipped
    assert result.counts()["unreadable"] == 1


def test_unreadable_directory_without_a_filename_still_recorded(
    repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拿不到路径时记成 ``.``，而不是丢掉那条记录。

    CPython 会把 ``scandir`` 的参数放进 ``err.filename``，所以这一支几乎不会
    走到；但"记不清是哪里"和"没有这回事"的区别正是这个字段存在的意义，
    所以宁可粗糙也不能少。
    """
    repo.write("locked/hidden.py", "y = 2\n")
    original = os.scandir

    def guarded(path: object) -> object:
        if str(path).replace("\\", "/").endswith("/locked"):
            error = PermissionError(13, "Permission denied")  # filename 留空
            assert error.filename is None
            raise error
        return original(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "scandir", guarded)

    result = scan(repo.path)
    assert result.unreadable == (".",)


# --------------------------------------------------------------- is_test_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_app.py", True),
        ("test/test_app.py", True),
        ("src/tests/helpers.py", True),
        ("test_app.py", True),
        ("app_test.py", True),
        ("conftest.py", True),
        ("src/app.py", False),
        ("testing/app.py", False),  # 目录名必须是 test/tests，testing 不算
        ("contest.py", False),
        ("tests/data.json", False),  # 非 .py 一律不算
        ("tests/__init__.py", True),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


def test_is_test_path_accepts_posix_separators_only() -> None:
    """扫描产出的路径统一是 POSIX 分隔符，函数按这个约定断言。
    传 Windows 分隔符时 ``Path`` 在 POSIX 上不会拆分，属于调用方的错误。"""
    assert is_test_path("tests/test_app.py") is True


# --------------------------------------------------------------- workspace 集成


def test_scans_a_workspace_created_by_the_manager(tmp_path: Path) -> None:
    """真正的调用路径：先由 WorkspaceManager 建 workspace，再扫描它。

    这里专门验证 worktree 情形——它的 ``.git`` 是文件，且 ``.gitignore``
    的判定要在没有 `.git` 目录的前提下仍然工作。
    """
    from codepilot.repo.workspace import WorkspaceManager

    origin = init_repo(
        tmp_path / "origin",
        {"src/app.py": "def f():\n    return 1\n", ".gitignore": "*.log\n"},
    )
    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    ws = manager.create(
        source=str(origin.path), base_commit=origin.head, target=tmp_path / "ws", mode="worktree"
    )

    (ws.path / "noise.log").write_text("ignored\n", encoding="utf-8")
    result = scan(ws.path)

    assert [f.path for f in result.files] == [".gitignore", "src/app.py"]
    assert "noise.log" in result.ignored
