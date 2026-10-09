"""R-13 / R-14：GitHistory 只读 base 祖先；文件级与 symbol 级修改频率。

全部用真实 git 仓库（tests/support/gitfixture），不用 mock——历史语义
（numstat 的 -z 格式、merge 不占条目、rename 条目形态）只有真 git 能证明。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codepilot.repo.git_history import (
    GitHistory,
    GitHistoryError,
    _parse_numstat,
    _parse_patch,
    _unquote_diff_path,
)
from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.symbols import extract_symbols
from support.gitfixture import FixtureRepo, init_repo

A_V1 = "X = 1\n\n\ndef f():\n    return 1\n\n\ndef g():\n    return 5\n    return 6\n    return 7\n"
A_V2 = "X = 1\n# 模块级注释，不在任何 symbol 内\n\n\ndef f():\n    return 10\n\n\ndef g():\n    return 5\n    return 6\n    return 7\n"
A_V3 = "X = 1\n# 模块级注释，不在任何 symbol 内\n\n\ndef f():\n    return 10\n\n\ndef g():\n    return 5\n    return 7\n"


def _symbols_of(path: str, content: str) -> tuple:
    parser = TreeSitterParser()
    return extract_symbols(parser.parse(content.encode(), path=path), path=path, repository_id="t")


def _base_symbols(repo: FixtureRepo, files: dict[str, str]) -> tuple:
    return tuple(symbol for path, content in files.items() for symbol in _symbols_of(path, content))


def test_file_churn_counts_and_distances(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1, "README.md": "# r\n"})
    repo.write("a.py", A_V2)
    repo.commit("c2")
    repo.write("README.md", "# r v2\n")
    base = repo.commit("c3-readme")
    repo.write("b.py", "def h():\n    return 9\n")
    repo.commit("c4-new-file")
    repo.write("a.py", A_V3)
    repo.commit("c5")

    churn = GitHistory(repo.path, base).file_churn()

    # log 顺序：[c3(0), c2(1), c1(2)]；c4/c5 是 base 之后的提交，绝不能出现。
    assert churn["a.py"].modifications == 2
    assert churn["a.py"].last_modified_distance == 1
    assert churn["README.md"].modifications == 2
    assert churn["README.md"].last_modified_distance == 0
    assert "b.py" not in churn


def test_post_base_commits_never_leak_into_file_churn(tmp_path: Path) -> None:
    """04 FR-03：base 之后的提交不出现在历史特征中。

    构造一个极端的对照：base 之后对同一文件连改三次。如果它们泄露进来，
    修改次数会多 3、距离会变成 0——两个字段都会错。
    """
    repo = init_repo(tmp_path / "r", {"a.py": A_V1})
    repo.write("a.py", A_V2)
    repo.commit("c2")
    repo.write("README.md", "# r v2\n")
    base = repo.commit("c3-readme")
    for i in range(3):
        repo.write("a.py", f"X = {i}\n")
        repo.commit(f"post-{i}")

    churn = GitHistory(repo.path, base).file_churn()
    assert churn["a.py"].modifications == 2
    assert churn["a.py"].last_modified_distance == 1


def test_file_churn_root_commit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1})
    churn = GitHistory(repo.path, repo.head).file_churn()
    assert churn["a.py"].modifications == 1
    assert churn["a.py"].last_modified_distance == 0


def test_file_churn_window_limit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1, "README.md": "# r\n"})
    repo.write("a.py", A_V2)
    base = repo.commit("c2")
    churn = GitHistory(repo.path, base).file_churn(max_commits=1)
    # 窗口只有 base 自己：只有 c2 触及的文件，各 1 次、距离 0。
    assert set(churn) == {"a.py"}
    assert churn["a.py"].modifications == 1
    assert churn["a.py"].last_modified_distance == 0


def test_file_churn_rename_records_the_new_path(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1})
    repo.git("mv", "a.py", "renamed.py")
    base = repo.commit("c2-rename")
    churn = GitHistory(repo.path, base).file_churn()
    # rename 不追随：新路径只记 rename 这一次；旧路径保留改名前的历史。
    assert churn["renamed.py"].modifications == 1
    assert churn["renamed.py"].last_modified_distance == 0
    assert churn["a.py"].modifications == 1
    assert churn["a.py"].last_modified_distance == 1


def test_file_churn_binary_file_counts(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1})
    (repo.path / "blob.bin").write_bytes(b"\x00\x01\x02")
    base = repo.commit("c2-binary")
    churn = GitHistory(repo.path, base).file_churn()
    # numstat 对二进制输出 "-\t-\tpath"，仍算一次修改。
    assert churn["blob.bin"].modifications == 1


def test_merge_commit_occupies_a_distance_but_touches_no_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1})
    repo.git("checkout", "--quiet", "-b", "side")
    repo.write("side.py", "def s():\n    return 0\n")
    _commit_with_date(repo, "c2-side", "2026-01-02T00:00:00+00:00")
    repo.git("checkout", "--quiet", "main")
    _git_with_date(
        repo,
        "2026-01-03T00:00:00+00:00",
        "merge",
        "--no-ff",
        "--quiet",
        "-m",
        "merge side",
        "side",
    )
    base = repo.git("rev-parse", "HEAD").strip()

    churn = GitHistory(repo.path, base).file_churn()
    # log 顺序（提交时间不同，确定）：[merge(0), side(1), c1(2)]。
    assert churn["side.py"].modifications == 1
    assert churn["side.py"].last_modified_distance == 1
    assert churn["a.py"].modifications == 1
    assert churn["a.py"].last_modified_distance == 2


def test_non_full_sha_is_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    with pytest.raises(GitHistoryError, match="完整 SHA"):
        GitHistory(repo.path, "main")
    with pytest.raises(GitHistoryError, match="完整 SHA"):
        GitHistory(repo.path, "HEAD~1")


def test_unknown_commit_is_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    with pytest.raises(GitHistoryError, match="不在仓库中"):
        GitHistory(repo.path, "0" * 40)


def _commit_with_date(repo: FixtureRepo, message: str, date: str) -> str:
    """用一个不同的提交日期提交（gitfixture 固定日期，merge 顺序需要区分）。"""
    repo.git("add", "-A")
    _git_with_date(repo, date, "commit", "--quiet", "-m", message)
    return repo.git("rev-parse", "HEAD").strip()


def _git_with_date(repo: FixtureRepo, date: str, *args: str) -> None:
    import os

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "CodePilot Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "CodePilot Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *args],
        cwd=repo.path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------- R-14 symbol 级


def _build_history_repo(tmp_path: Path) -> tuple[FixtureRepo, str]:
    """c1 建 a.py → c2 改 f 并加模块级注释、建 temp.py → c3(base) 删 temp.py
    → c4 再改 f、建 b.py（base 之后）。"""
    repo = init_repo(tmp_path / "r", {"a.py": A_V1, "README.md": "# r\n"})
    repo.write("a.py", A_V2)
    repo.write("temp.py", "def t():\n    return 0\n")
    repo.commit("c2")
    repo.git("rm", "--quiet", "temp.py")
    base = repo.commit("c3-rm-temp")
    repo.write("a.py", A_V3)
    repo.write("b.py", "def h():\n    return 9\n")
    repo.commit("c4-post-base")
    return repo, base


def test_symbol_churn_maps_hunks_to_base_symbols(tmp_path: Path) -> None:
    repo, base = _build_history_repo(tmp_path)
    symbols = _base_symbols(repo, {"a.py": A_V2})
    by_name = {s.qualified_name: s for s in symbols}

    result = GitHistory(repo.path, base).symbol_churn(symbols)

    f = result.churn[by_name["a.f"].id]
    # c1 创建（距离 2）+ c2 修改（距离 1）；c4 的再修改绝不能计入。
    assert f.modifications == 2
    assert f.last_modified_distance == 1
    g = result.churn[by_name["a.g"].id]
    assert g.modifications == 1
    assert g.last_modified_distance == 2
    # module symbol 不参与归属（它横跨整个文件，会吞掉所有 hunk）。
    assert by_name["a"].id not in result.churn
    # c2 加的那行模块级注释不在任何 symbol 内 → 未归属。
    assert result.unattributed["a.py"] == 1
    # temp.py：c2 的创建（不在 base 符号表）与 c3 的删除各记一次 skipped。
    assert result.skipped["temp.py"] == 2
    # c4 是 base 之后：b.py 绝不出现，a.py 的 f/g 计数不受影响（上面已断言）。
    assert "b.py" not in result.skipped
    assert result.commits_examined == 3


def test_symbol_churn_post_base_edits_do_not_change_counts(tmp_path: Path) -> None:
    repo, base = _build_history_repo(tmp_path)
    symbols = _base_symbols(repo, {"a.py": A_V2})
    first = GitHistory(repo.path, base).symbol_churn(symbols)
    # 继续在 main 上提交，再用同一个 base SHA 统计：结果必须逐字节一致。
    repo.write("a.py", "def f():\n    return 999\n")
    repo.commit("c5-more")
    second = GitHistory(repo.path, base).symbol_churn(symbols)
    assert first == second


def test_symbol_churn_pure_deletion_attributes_to_the_line_before(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1})
    repo.write("a.py", A_V2)
    repo.commit("c2")
    repo.write("a.py", A_V3)  # c3 只删掉 g 中间的 "return 6"
    base = repo.commit("c3-delete-line")
    symbols = _base_symbols(repo, {"a.py": A_V3})
    by_name = {s.qualified_name: s for s in symbols}

    result = GitHistory(repo.path, base).symbol_churn(symbols)
    # 纯删除 hunk（+c,0）归属到删除点之前那行，仍在 g 的范围内。c1 创建 + c3 删除。
    assert result.churn[by_name["a.g"].id].modifications == 2
    assert result.churn[by_name["a.g"].id].last_modified_distance == 0
    # f 被 c1 创建（距离 2）与 c2 修改（距离 1）触及，c3 的删除不涉及它。
    assert result.churn[by_name["a.f"].id].modifications == 2
    assert result.churn[by_name["a.f"].id].last_modified_distance == 1


def test_symbol_churn_quoted_non_ascii_path(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"rénzhì.py": "def h():\n    return 1\n"})
    repo.write("rénzhì.py", "def h():\n    return 2\n")
    base = repo.commit("c2")
    symbols = _base_symbols(repo, {"rénzhì.py": "def h():\n    return 2\n"})
    by_name = {s.qualified_name: s for s in symbols}

    result = GitHistory(repo.path, base).symbol_churn(symbols)
    # diff 头里的 C-style quoting 必须解码，否则这个文件永远是 skipped。
    assert result.churn[by_name["rénzhì.h"].id].modifications == 2
    assert not result.skipped


def test_symbol_churn_without_symbols_attributes_nothing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r", {"a.py": A_V1})
    result = GitHistory(repo.path, repo.head).symbol_churn(())
    assert result.churn == {}
    assert result.skipped == {"a.py": 1}


# ---------------------------------------------------------------- 解析器直连用例


def test_parse_numstat_rename_entry() -> None:
    sha = "a" * 40
    output = f"COMMIT:{sha}\x00\n0\t0\t\x00old.py\x00new.py\x001\t2\tplain.py\x00"
    commits = _parse_numstat(output)
    assert len(commits) == 1
    assert commits[0].paths == ["new.py", "plain.py"]


def test_parse_patch_deleted_file_uses_old_path() -> None:
    sha = "b" * 40
    output = (
        f"COMMIT:{sha}\n"
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-x = 1\n"
    )
    commits = _parse_patch(output)
    assert commits[0].deleted == ["gone.py"]
    assert commits[0].hunks == {}


def test_unquote_diff_path() -> None:
    assert _unquote_diff_path("b/src/app.py") == "src/app.py"
    assert _unquote_diff_path('"b/r\\303\\251nzh\\303\\254.py"') == "rénzhì.py"
    assert _unquote_diff_path("plain.py") == "plain.py"
