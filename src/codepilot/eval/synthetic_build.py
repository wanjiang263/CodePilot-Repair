"""Fixture 生成器：把任务定义写成 02 §17.3 的目录布局（E-05 的作者期工具）。

放在 ``src`` 而不是 ``scripts`` 有两个原因：

* 单元测试与构建脚本必须用**同一套**写盘逻辑。分开写会出现"测试里的 fixture
  和 benchmarks/ 里的不是同一个东西"，那正是隔离测试最没用的那种漂移。
* 它受 mypy strict 管辖。补丁生成里 ``git diff --cached`` 的输出形态是要被
  :func:`codepilot.eval.synthetic.patch_modified_files` 解析的，类型跑偏应当
  在提交前被发现。

生成过程刻意复用 git 本身来产生补丁，而不是手工拼 unified diff：hunk 行数、
上下文、新文件头这些细节一旦算错，``git apply --check`` 就会拒绝，而手工拼的
diff 错得很安静。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from codepilot.eval.synthetic import (
    ENV_FILE,
    EVALUATION_DIRECTORY,
    GOLD_PATCH_FILE,
    ISSUE_FILE,
    METADATA_FILE,
    REPOSITORY_DIRECTORY,
    TEST_PATCH_FILE,
    TESTS_FILE,
    BenchmarkCategory,
    FixtureValidationError,
    compute_base_commit,
    directory_digests,
    patch_modified_files,
)
from codepilot.repo.gitcmd import run_git
from codepilot.tools.base import is_test_path

__all__ = ["TaskSpec", "build_fixture", "write_repository_tree"]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """一个合成任务的完整定义。

    ``base_files`` 是**含 bug** 的仓库内容，``gold_files`` 只给出被修复文件的
    完整新内容（不写 diff）。这样作者改动一行时改的是一个字符串，而补丁由 git
    重新生成——手写 unified diff 的行计数错误是一类很难在 review 中发现的 bug。
    """

    task_id: str
    category: BenchmarkCategory
    difficulty: str
    issue: str
    base_files: Mapping[str, str]
    gold_files: Mapping[str, str]
    hidden_test_files: Mapping[str, str]
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...] = ()
    env: dict[str, Any] = field(default_factory=dict)
    """``env.yaml`` 的原始载荷；至少需要 ``install`` 或 ``pre_install``。"""

    def __post_init__(self) -> None:
        problems: list[str] = []
        if not self.task_id or PurePosixPath(self.task_id).name != self.task_id:
            problems.append(f"task_id 必须是单个目录名，收到 {self.task_id!r}")
        if not self.issue.strip():
            problems.append("issue 不能为空")
        if not self.base_files:
            problems.append("base_files 不能为空")
        if not self.gold_files:
            problems.append("gold_files 不能为空：没有修复的 fixture 无法判定有效性")
        if not self.hidden_test_files:
            problems.append("hidden_test_files 不能为空：目标测试必须由 test.patch 引入")
        if self.difficulty not in ("easy", "medium", "hard"):
            problems.append(f"difficulty 非法: {self.difficulty!r}")
        for path, content in self.gold_files.items():
            if path in self.base_files and content == self.base_files[path]:
                problems.append(f"gold_files[{path!r}] 与 base 内容相同，补丁会是空的")
            if _looks_like_test(path):
                problems.append(f"gold_files 不得包含测试文件: {path}")
        for path in self.hidden_test_files:
            if not _looks_like_test(path):
                problems.append(f"hidden_test_files 必须都是测试文件: {path}")
        for node in self.fail_to_pass:
            file_part = node.split("::", 1)[0]
            if file_part not in self.hidden_test_files:
                problems.append(f"fail_to_pass[{node!r}] 的测试文件不在 hidden_test_files 里")
        for node in (*self.fail_to_pass, *self.pass_to_pass):
            file_part = node.split("::", 1)[0]
            if file_part not in self.hidden_test_files and file_part not in self.base_files:
                problems.append(f"目标测试 {node!r} 指向一个不存在的文件")
        if not self.fail_to_pass:
            problems.append("fail_to_pass 不能为空")
        if problems:
            raise ValueError(f"TaskSpec {self.task_id}: " + "; ".join(problems))

    @property
    def multi_file(self) -> bool:
        """gold 是否真的需要改动多个**源**文件。

        判据来自补丁本身而不是作者的手填声明：E-06 明确不许为了凑数在无意义地
        多改一个文件，所以这个值必须由 :class:`TaskSpec` 的内容推出来。
        """
        return len(self.gold_files) > 1

    @property
    def has_traceback(self) -> bool:
        return "Traceback (most recent call last)" in self.issue


def _looks_like_test(path: str) -> bool:
    return is_test_path(path) or PurePosixPath(path).name == "conftest.py"


def write_repository_tree(root: Path, files: Mapping[str, str]) -> None:
    """按 LF 写入一棵普通文件树。

    统一 LF 是可复现 SHA 的前提：Windows 上 ``Path.write_text`` 默认会把
    ``\\n`` 写成 ``\\r\\n``，于是同一份定义在两台机器上 commit 出两个 base_commit。
    """
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def build_fixture(spec: TaskSpec, suite_root: Path, *, scratch_root: Path) -> Path:
    """写出 ``<suite_root>/<task_id>/`` 并返回该目录。

    base commit 与两份补丁都在一次性 scratch 目录里由 git 算出，``repo/`` 则直接
    由 :class:`TaskSpec` 的内容写入。顺序很重要：先写再 commit 会把 ``.git/``
    留在 ``benchmarks/`` 里，而嵌套 git 目录既提交不进外层仓库，又会让 loader 的
    "``repo/`` 里不许有 .git" 检查立刻失败。
    """
    target = Path(suite_root) / spec.task_id
    work = Path(scratch_root) / spec.task_id
    if target.exists():
        shutil.rmtree(target)
    if work.exists():
        shutil.rmtree(work)
    target.mkdir(parents=True)
    work.mkdir(parents=True)
    try:
        write_repository_tree(target / REPOSITORY_DIRECTORY, spec.base_files)
        base_commit = compute_base_commit(_scratch_tree(work / "base", spec.base_files))
        gold_patch = _staged_patch(work / "gold", spec.base_files, spec.gold_files)
        test_patch = _staged_patch(work / "tests", spec.base_files, spec.hidden_test_files)
        gold_targets = patch_modified_files(gold_patch)
        test_targets = patch_modified_files(test_patch)
        _assert_patch_shapes(gold_targets, test_targets, spec)

        evaluation = target / EVALUATION_DIRECTORY
        evaluation.mkdir(parents=True, exist_ok=True)
        (target / ISSUE_FILE).write_text(spec.issue, encoding="utf-8", newline="\n")
        (target / ENV_FILE).write_text(
            yaml.safe_dump(spec.env, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
            newline="\n",
        )
        (evaluation / GOLD_PATCH_FILE).write_text(gold_patch, encoding="utf-8", newline="\n")
        (evaluation / TEST_PATCH_FILE).write_text(test_patch, encoding="utf-8", newline="\n")
        (evaluation / TESTS_FILE).write_text(
            json.dumps(
                {
                    "fail_to_pass": list(spec.fail_to_pass),
                    "pass_to_pass": list(spec.pass_to_pass),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        metadata = {
            "schema_version": 1,
            "task_id": spec.task_id,
            "category": spec.category.value,
            "difficulty": spec.difficulty,
            "multi_file": spec.multi_file,
            "has_traceback": spec.has_traceback,
            "base_commit": base_commit,
            "repo_files": directory_digests(target / REPOSITORY_DIRECTORY),
        }
        (target / METADATA_FILE).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _scratch_tree(root: Path, files: Mapping[str, str]) -> Path:
    tree = Path(root)
    write_repository_tree(tree, files)
    return tree


def _staged_patch(work: Path, base: Mapping[str, str], changed: Mapping[str, str]) -> str:
    """base 树 commit 后覆盖 ``changed``，再用 ``git diff --cached`` 生成补丁。"""
    tree = _scratch_tree(work, base)
    compute_base_commit(tree)
    write_repository_tree(tree, changed)
    run_git(["add", "-A"], cwd=tree)
    patch = run_git(["diff", "--cached", "--no-color"], cwd=tree).stdout
    if not patch.strip():
        raise FixtureValidationError(Path(work).name, ["补丁为空：改动后的文件与 base 相同"])
    return patch


def _assert_patch_shapes(
    gold_targets: tuple[str, ...], test_targets: tuple[str, ...], spec: TaskSpec
) -> None:
    reasons: list[str] = []
    if set(gold_targets) != set(spec.gold_files):
        reasons.append(f"gold.patch 文件集不符: {sorted(set(gold_targets) ^ set(spec.gold_files))}")
    if set(test_targets) != set(spec.hidden_test_files):
        reasons.append(
            f"test.patch 文件集不符: {sorted(set(test_targets) ^ set(spec.hidden_test_files))}"
        )
    if reasons:
        raise FixtureValidationError(spec.task_id, reasons)
