"""Synthetic fixture Schema、Loader 与推理/评测隔离（02 §17.3，E-01）。

这个模块存在的唯一理由是 03 §11.2 的硬规则：**违反它就作废全部报告数字**。
因此隔离不是靠 Prompt 里写"不要看答案"实现的，而是靠三条结构性约束：

1. **加载即分裂**。``SyntheticFixture`` 一旦构建完成就只剩两个视图：
   :class:`InferenceTask`（只有 Issue、仓库、base commit、环境）和
   :class:`EvaluationTask`（只有 gold patch、隐藏测试、目标测试清单）。
   两者的字段集合被刻意设计成不相交（除了 ``task_id`` / ``base_commit``
   这两个身份字段），有测试逐字段核对。
2. **``eval/`` 永远不进 workspace**。推理用的 git 仓库由 :meth:`materialize`
   从 ``repo/`` 的**普通文件**重新构建，只含 base commit 这一个提交；
   ``eval/`` 从来不是那棵树的成员，因此既不在工作区里，也不在对象库里。
3. **fail closed**。Schema 不合、清单哈希不符、出现 symlink 或路径穿越，
   一律抛 :class:`FixtureValidationError` 而不是"记个警告继续跑"。一个静默
   接受的坏 fixture 会在几百次运行之后表现为"成功率莫名偏高"。

磁盘布局（02 §17.3）::

    benchmarks/synthetic/<task_id>/
    ├── repo/            # base 状态的可运行仓库（不含 .git）
    ├── issue.md         # 只描述现象
    ├── env.yaml         # EnvSpec
    ├── metadata.json    # 推理可见的分类信息
    └── eval/            # 评测专用，绝不进 workspace
        ├── gold.patch
        ├── test.patch
        └── tests.json

``repo/`` 里刻意**不提交** ``.git``：嵌套 git 目录无法进入外层仓库。base commit
因此由内容确定——固定身份与提交时间后重新 ``git init && commit``，得到的 SHA
必须与 ``metadata.json`` 记录的一致。这顺带成了整棵 ``repo/`` 的完整性校验：
改一个字节，SHA 就变。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from codepilot.fsutil import is_link
from codepilot.repo.gitcmd import run_git
from codepilot.sandbox.env_spec import EnvSpec, EnvSpecError, load_env_spec
from codepilot.tools.base import is_test_path

__all__ = [
    "EVALUATION_DIRECTORY",
    "FIXTURE_SCHEMA_VERSION",
    "INFERENCE_DENIED_DIRECTORIES",
    "BenchmarkCategory",
    "EvalTests",
    "EvaluationTask",
    "FixtureValidationError",
    "InferenceTask",
    "SyntheticFixture",
    "SyntheticSuite",
    "compute_base_commit",
    "directory_digests",
    "load_fixture",
    "patch_added_lines",
    "patch_modified_files",
    "validate_suite",
]

#: 02 §17.3 的十类别。用 StrEnum 而不是裸 str，是为了让"新写一个类别名结果
#: 拼错了"在加载时就炸，而不是在聚合报表里表现为一个谁也注意不到的分类。
FIXTURE_SCHEMA_VERSION = 1
EVALUATION_DIRECTORY = "eval"
REPOSITORY_DIRECTORY = "repo"
ISSUE_FILE = "issue.md"
ENV_FILE = "env.yaml"
METADATA_FILE = "metadata.json"
GOLD_PATCH_FILE = "gold.patch"
TEST_PATCH_FILE = "test.patch"
TESTS_FILE = "tests.json"

#: 推理 workspace 必须对工具层拒绝这些目录名（E-01 第 6 条）。
#: ``eval`` 之所以还要在这里出现一次：workspace 里本来就不该有它，但如果
#: 某个仓库自带 ``eval/``，工具层也得挡住，不能让模型借道读到同名内容。
INFERENCE_DENIED_DIRECTORIES: frozenset[str] = frozenset({EVALUATION_DIRECTORY})

_REQUIRED_TOP_LEVEL = (ISSUE_FILE, ENV_FILE, METADATA_FILE, REPOSITORY_DIRECTORY)
_REQUIRED_EVAL_FILES = (GOLD_PATCH_FILE, TEST_PATCH_FILE, TESTS_FILE)

#: base commit 的可复现性来源。身份与时间必须钉死，否则同一份 ``repo/``
#: 在两台机器上会 commit 出两个不同的 SHA，"校验 base commit"就无从谈起。
#: 提交对象只由 tree/parent/author/committer/message 决定；把这些全部固定，
#: SHA 就是 ``repo/` 内容的纯函数。
GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "CodePilot Synthetic Benchmark",
    "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
    "GIT_COMMITTER_NAME": "CodePilot Synthetic Benchmark",
    "GIT_COMMITTER_EMAIL": "benchmark@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}
_COMMIT_MESSAGE = "synthetic fixture base"

_FULL_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
_HUNK_LINE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


class BenchmarkCategory(StrEnum):
    """02 §17.3 的十个缺陷类别。"""

    # 名字以 Test/Benchmark 开头的类会被 pytest 收集；这里是枚举，显式关掉。
    __test__: ClassVar[bool] = False

    WRONG_CONDITION = "wrong_condition"
    MISSING_VALIDATION = "missing_validation"
    WRONG_EXCEPTION = "wrong_exception"
    OFF_BY_ONE = "off_by_one"
    INCORRECT_RETURN = "incorrect_return"
    API_MISUSE = "api_misuse"
    EDGE_CASE = "edge_case"
    CROSS_MODULE = "cross_module_dependency"
    REGRESSION = "regression_bug"
    STATE_MUTATION = "state_mutation_bug"


_DIFFICULTY = Literal["easy", "medium", "hard"]


class FixtureValidationError(ValueError):
    """Fixture 不合规。**必须**让调用方失败，不允许降级继续跑。"""

    def __init__(self, task_id: str, reasons: Sequence[str]) -> None:
        self.task_id = task_id
        self.reasons = list(reasons)
        joined = "\n  ".join(self.reasons) or "(未给出原因)"
        super().__init__(f"fixture {task_id} 无效：\n  {joined}")


def _relative_path(value: str) -> str:
    """仓库内规范相对路径：POSIX 分隔、无绝对路径、无 ``..``、无尾点。"""
    if not isinstance(value, str) or not value:
        raise ValueError("路径必须是非空字符串")
    posix = PurePosixPath(value.replace("\\", "/"))
    if (
        posix.is_absolute()
        or PurePosixPath(value).is_absolute()
        or re.match(r"^[A-Za-z]:", value)
        or ".." in posix.parts
        or "." in posix.parts
        or value != posix.as_posix()
    ):
        raise ValueError(f"需要规范化的仓库内相对 POSIX 路径，收到 {value!r}")
    return posix.as_posix()


def _canonical_test_node(value: str) -> str:
    """``tests/test_x.py::TestClass::test_y`` 形态的节点 id。

    只校验**语法**与"不得指向 eval/"。测试是否真的存在属于 E-02 的职责——
    它要靠真实执行来判定，在纯 Schema 层假装判定等于把 ENV_ERROR 混进语法错误。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("测试节点 id 不能为空")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("测试节点 id 含控制字符")
    parts = value.split("::")
    if len(parts) < 2 or len(parts) > 4:
        raise ValueError(f"测试节点 id 形态不对: {value!r}")
    try:
        file_part = _relative_path(parts[0])
    except ValueError as exc:
        raise ValueError(f"测试节点 id 路径不合法: {value!r}") from exc
    if not file_part.endswith(".py"):
        raise ValueError(f"测试节点 id 必须指向 .py 文件: {value!r}")
    if EVALUATION_DIRECTORY in PurePosixPath(file_part).parts:
        raise ValueError(f"测试节点 id 指向评测目录，越过隔离边界: {value!r}")
    for part in parts[1:]:
        if not part or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\[\].:\-]*", part):
            raise ValueError(f"测试节点 id 含非法选择器: {value!r}")
    return value


class FixtureMetadata(BaseModel):
    """``metadata.json`` ——**推理可见**的分类信息。

    ``extra="forbid"`` 是有实际作用的：任何人想往这里塞 ``gold_files``、
    ``fix_hint`` 之类的字段，会在加载时立刻失败，而不是悄悄把答案喂给模型。
    分类与难度是任务属性；精确修复位置不是（E-06 明确要求无 traceback 的
    任务不得用 metadata 泄漏位置）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    task_id: str
    category: BenchmarkCategory
    difficulty: _DIFFICULTY
    multi_file: bool
    has_traceback: bool
    base_commit: str
    repo_files: dict[str, str]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("schema_version 必须是整数")
        if value != FIXTURE_SCHEMA_VERSION:
            raise ValueError(f"不支持的 fixture schema_version: {value}")
        return value

    @field_validator("task_id")
    @classmethod
    def _task(cls, value: str) -> str:
        if _TASK_ID.fullmatch(value) is None:
            raise ValueError(f"task_id 必须匹配 {_TASK_ID.pattern}，收到 {value!r}")
        return value

    @field_validator("base_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if _FULL_SHA.fullmatch(value) is None:
            raise ValueError("base_commit 必须是 40 或 64 位十六进制完整 SHA")
        return value.lower()

    @field_validator("repo_files")
    @classmethod
    def _files(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for path, digest in value.items():
            try:
                key = _relative_path(path)
            except ValueError as exc:
                raise ValueError(f"repo_files 路径不合法: {path!r}") from exc
            if _SHA256.fullmatch(digest) is None:
                raise ValueError(f"repo_files[{path!r}] 的哈希不是 sha256")
            if key.startswith(f"{EVALUATION_DIRECTORY}/"):
                raise ValueError("repo_files 不得包含评测目录")
            normalized[key] = digest
        if not normalized:
            raise ValueError("repo_files 不能为空")
        return dict(sorted(normalized.items()))


class EvalTests(BaseModel):
    """``eval/tests.json`` ——**评测专用**。含隐藏目标测试清单，永不进入推理侧。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fail_to_pass: list[str]
    pass_to_pass: list[str] = Field(default_factory=list)

    @field_validator("fail_to_pass", "pass_to_pass", mode="before")
    @classmethod
    def _lists(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError("fail_to_pass / pass_to_pass 必须是字符串数组")
        return list(value)

    @field_validator("fail_to_pass", "pass_to_pass")
    @classmethod
    def _nodes(cls, value: list[str]) -> list[str]:
        return [_canonical_test_node(item) for item in value]

    @model_validator(mode="after")
    def _check(self) -> EvalTests:
        if not self.fail_to_pass:
            raise ValueError("fail_to_pass 不能为空：没有目标测试的 fixture 无法判定有效性")
        if len(set(self.fail_to_pass)) != len(self.fail_to_pass):
            raise ValueError("fail_to_pass 含重复项")
        if len(set(self.pass_to_pass)) != len(self.pass_to_pass):
            raise ValueError("pass_to_pass 含重复项")
        overlap = set(self.fail_to_pass) & set(self.pass_to_pass)
        if overlap:
            raise ValueError(f"同一测试不能既要求失败又要求通过: {sorted(overlap)[:3]}")
        return self


class InferenceTask(BaseModel):
    """推理侧唯一允许持有的对象。

    字段集合即信息边界：这里**没有**任何 gold / 隐藏测试 / 目标清单字段。
    新增字段前请先看 :meth:`EvaluationTask` 与 ``test_isolation.py``——
    两边出现同名字段会被测试判为隔离破口。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    issue: str
    base_commit: str
    category: BenchmarkCategory
    difficulty: _DIFFICULTY
    multi_file: bool
    has_traceback: bool
    env_spec: EnvSpec
    repository: Path
    """由 :meth:`SyntheticFixture.materialize` 生成的**只含 base commit** 的 git 仓库。"""

    mode: Literal["synthetic"] = "synthetic"

    @model_validator(mode="after")
    def _check(self) -> InferenceTask:
        if not self.issue.strip():
            raise ValueError("issue 不能为空")
        if not self.repository.is_dir():
            raise ValueError(f"推理仓库不存在: {self.repository}")
        return self


class EvaluationTask(BaseModel):
    """评测侧对象。推理模块不得接收它（有静态导入与构造点测试）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    base_commit: str
    gold_patch: str
    test_patch: str
    tests: EvalTests
    gold_files: tuple[str, ...]
    test_patch_files: tuple[str, ...]
    fixture_version: str
    """整份 fixture（含 eval/）的内容摘要，用于运行身份与断点续跑。"""

    gold_patch_sha256: str
    test_patch_sha256: str

    @property
    def target_tests(self) -> tuple[str, ...]:
        return (*self.tests.fail_to_pass, *self.tests.pass_to_pass)


class SyntheticFixture(BaseModel):
    """一份已校验的合成 fixture；同时持有两个视图，但视图之间不相通。"""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    directory: Path
    metadata: FixtureMetadata
    issue: str
    env_spec: EnvSpec
    evaluation: EvaluationTask

    @property
    def task_id(self) -> str:
        return self.metadata.task_id

    def inference(self, repository: Path) -> InferenceTask:
        """构造推理视图。**只**从 metadata/issue/env 取字段。"""
        return InferenceTask(
            task_id=self.metadata.task_id,
            issue=self.issue,
            base_commit=self.metadata.base_commit,
            category=self.metadata.category,
            difficulty=self.metadata.difficulty,
            multi_file=self.metadata.multi_file,
            has_traceback=self.metadata.has_traceback,
            env_spec=self.env_spec,
            repository=Path(repository),
        )

    # ------------------------------------------------------------------ 装配

    def materialize(self, target: Path) -> Path:
        """把 ``repo/`` 变成只含 base commit 的 git 仓库，并核对 SHA。

        三条防线：

        * 逐个条目拒绝 symlink/重解析点——否则一次 checkout 就能把 workspace
          里的 ``src`` 指到 fixture 目录外，``eval/`` 立刻可读。
        * 复制时按 ``metadata.repo_files`` 的 sha256 逐文件核对，多文件、少文件、
          内容漂移都会在这里失败，而不是在后面表现为"patch 应用不上"。
        * 提交出的 SHA 必须等于 ``metadata.base_commit``。

        ``target`` 已存在且非空时拒绝执行：拿一份旧的材料继续跑，等于把上一个
        fixture 的残留算进这次的结果。
        """
        destination = Path(target)
        if destination.exists() and any(destination.iterdir()):
            raise FixtureValidationError(
                self.task_id, [f"materialize 目标目录已存在且非空: {destination}"]
            )
        staged = destination.with_name(f"{destination.name}.staging")
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True, exist_ok=True)
        try:
            self._copy_repository(staged)
            commit = compute_base_commit(staged)
            if commit != self.metadata.base_commit:
                raise FixtureValidationError(
                    self.task_id,
                    [
                        "base commit 与 metadata.json 不一致；repo/ 的内容被改动过，"
                        "或构建环境改变了提交对象。"
                        f"\n    期望 {self.metadata.base_commit}"
                        f"\n    实得 {commit}"
                    ],
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(destination)
        finally:
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
        return destination

    def _copy_repository(self, staged: Path) -> None:
        source_root = (self.directory / REPOSITORY_DIRECTORY).resolve()
        seen: dict[str, str] = {}
        for path in sorted(source_root.rglob("*")):
            if is_link(path):
                raise FixtureValidationError(
                    self.task_id, [f"repo/ 含符号链接或重解析点: {path.relative_to(source_root)}"]
                )
            if not path.is_file():
                continue
            relative = path.relative_to(source_root).as_posix()
            if relative.startswith(f"{EVALUATION_DIRECTORY}/") or relative == ".git":
                raise FixtureValidationError(
                    self.task_id, [f"repo/ 不应包含 {relative}（评测内容必须留在 eval/）"]
                )
            expected = self.metadata.repo_files.get(relative)
            if expected is None:
                raise FixtureValidationError(
                    self.task_id, [f"repo/ 含 metadata 未登记的文件: {relative}"]
                )
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != expected:
                raise FixtureValidationError(
                    self.task_id, [f"{relative} 内容与 metadata.json 的哈希不符"]
                )
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            seen[relative] = digest
        missing = sorted(set(self.metadata.repo_files) - set(seen))
        if missing:
            raise FixtureValidationError(
                self.task_id, [f"repo/ 缺少已登记的文件: {', '.join(missing[:5])}"]
            )


class SyntheticSuite(BaseModel):
    """一整个 suite 的加载结果。``invalid`` 不为空时调用方必须拒绝运行。"""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    root: Path
    fixtures: list[SyntheticFixture] = Field(default_factory=list)
    invalid: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def task_ids(self) -> list[str]:
        return [item.task_id for item in self.fixtures]

    def __len__(self) -> int:
        return len(self.fixtures)


# --------------------------------------------------------------------- patch 解析


def patch_modified_files(patch: str) -> tuple[str, ...]:
    """统一 diff 里被改动的文件（规范相对路径，去重保序）。"""
    files: list[str] = []
    for header in re.findall(r"^diff --git a/(\S+) b/(\S+)$", patch, flags=re.MULTILINE):
        old, new = header
        if old != new:
            raise ValueError(f"不支持重命名/移动: a/{old} -> b/{new}")
        try:
            relative = _relative_path(new)
        except ValueError as exc:
            raise ValueError(f"patch 路径越界: {new!r}") from exc
        if relative not in files:
            files.append(relative)
    if not files and patch.strip():
        raise ValueError("patch 非空但没有 `diff --git` 头，不是 git 统一 diff 格式")
    return tuple(files)


def patch_added_lines(patch: str) -> tuple[str, ...]:
    """新增行正文（去掉 ``+`` 前缀）。

    E-07 用它做"issue 里是否直接出现 gold 新增代码"的精确匹配；同时返回
    规范化形式由调用方负责，这里刻意保留原始缩进——Python 的缩进是语义。
    """
    added: list[str] = []
    inside_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            inside_hunk = bool(_HUNK_LINE.match(line))
            continue
        if not inside_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return tuple(added)


# --------------------------------------------------------------------- 加载


def compute_base_commit(tree: Path) -> str:
    """把一棵普通文件树转成**唯一确定的** commit SHA。

    这是 base commit 可复现的机制，也是 ``repo/`` 的完整性校验：提交对象只由
    tree、parent、author/committer（含时间）、message 决定，全部钉死后，
    SHA 就是文件内容的纯函数。改一个字节 → SHA 变 → 加载失败。

    用 ``--template=`` 而非默认模板：用户 git 模板目录里的 hook（例如
    ``commit-msg``）会改写提交甚至让它失败，那样 SHA 就不再只由内容决定了。
    """
    root = Path(tree)
    run_git(["init", "-q", "--template="], cwd=root, env=GIT_IDENTITY_ENV)
    run_git(["add", "-A"], cwd=root, env=GIT_IDENTITY_ENV)
    run_git(["commit", "-q", "-m", _COMMIT_MESSAGE], cwd=root, env=GIT_IDENTITY_ENV)
    return run_git(["rev-parse", "HEAD"], cwd=root, env=GIT_IDENTITY_ENV).stdout.strip()


def directory_digests(root: Path) -> dict[str, str]:
    """一棵树里所有普通文件的 sha256，键为 POSIX 相对路径。

    拒绝 symlink/重解析点：链接文件的"内容"取决于它指向哪儿，而那是可以被
    workspace 之外改动的。登记这样一份哈希等于登记了一个随时会变的承诺。
    """
    base = Path(root)
    digests: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if is_link(path):
            raise FixtureValidationError(base.name, [f"含符号链接或重解析点: {path}"])
        if not path.is_file():
            continue
        digests[path.relative_to(base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def load_fixture(directory: Path) -> SyntheticFixture:
    """读取并校验单个 fixture 目录。任何不合规定义为 :class:`FixtureValidationError`。"""
    root = Path(directory)
    if not root.is_dir():
        raise FixtureValidationError(root.name, [f"fixture 目录不存在: {root}"])
    reasons: list[str] = []

    for name in _REQUIRED_TOP_LEVEL:
        if not (root / name).exists():
            reasons.append(f"缺少必需文件/目录: {name}")
    eval_dir = root / EVALUATION_DIRECTORY
    for name in _REQUIRED_EVAL_FILES:
        if not (eval_dir / name).is_file():
            reasons.append(f"缺少评测文件: {EVALUATION_DIRECTORY}/{name}")
    if reasons:
        raise FixtureValidationError(root.name, reasons)

    _reject_links(root)

    metadata = _load_metadata(root, reasons)
    issue = _load_issue(root, reasons)
    env_spec = _load_env(root, reasons)
    tests = _load_tests(eval_dir, reasons)
    gold_text, test_text = _load_patches(eval_dir, reasons)

    if metadata is None or reasons:
        raise FixtureValidationError(root.name, reasons or ["metadata.json 无法解析"])
    assert issue is not None and env_spec is not None and tests is not None
    assert gold_text is not None and test_text is not None

    if metadata.task_id != root.name:
        reasons.append(f"task_id 与目录名不一致: metadata={metadata.task_id} dir={root.name}")

    try:
        repo_digests = _digest_repository(root)
    except FixtureValidationError as exc:
        reasons.extend(exc.reasons)
        repo_digests = {}
    if repo_digests and repo_digests != metadata.repo_files:
        only_here = sorted(set(repo_digests) - set(metadata.repo_files))
        only_there = sorted(set(metadata.repo_files) - set(repo_digests))
        changed = sorted(
            key
            for key in set(repo_digests) & set(metadata.repo_files)
            if repo_digests[key] != metadata.repo_files[key]
        )
        if only_here:
            reasons.append(f"repo/ 含未登记文件: {', '.join(only_here[:5])}")
        if only_there:
            reasons.append(f"repo/ 缺少文件: {', '.join(only_there[:5])}")
        if changed:
            reasons.append(f"repo/ 内容与哈希不符: {', '.join(changed[:5])}")

    gold_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    try:
        gold_files = patch_modified_files(gold_text)
        test_files = patch_modified_files(test_text)
    except ValueError as exc:
        reasons.append(str(exc))

    reasons.extend(
        _patch_reasons(gold_text, test_text, gold_files, test_files, tests, metadata.repo_files)
    )

    if reasons:
        raise FixtureValidationError(root.name, reasons)

    evaluation = EvaluationTask(
        task_id=metadata.task_id,
        base_commit=metadata.base_commit,
        gold_patch=gold_text,
        test_patch=test_text,
        tests=tests,
        gold_files=gold_files,
        test_patch_files=test_files,
        fixture_version=_fixture_version(metadata, issue, env_spec, tests, gold_text, test_text),
        gold_patch_sha256=hashlib.sha256(gold_text.encode("utf-8")).hexdigest(),
        test_patch_sha256=hashlib.sha256(test_text.encode("utf-8")).hexdigest(),
    )
    return SyntheticFixture(
        directory=root,
        metadata=metadata,
        issue=issue,
        env_spec=env_spec,
        evaluation=evaluation,
    )


def validate_suite(root: Path, *, task_ids: Iterable[str] | None = None) -> SyntheticSuite:
    """加载整个 suite。

    返回对象里同时给出 ``fixtures`` 与 ``invalid``：调用方要能在报告里写出
    "32 个里 3 个无效及各自原因"，而不是只看到 29 这个数字。``task_ids`` 用于
    定向复核——已确认有效且未变化的 fixture 可以跳过，避免整轮重跑 Docker。
    """
    suite_root = Path(root)
    if not suite_root.is_dir():
        raise FixtureValidationError(str(suite_root), ["suite 根目录不存在"])
    wanted = set(task_ids) if task_ids is not None else None
    selected = sorted(
        path
        for path in suite_root.iterdir()
        if path.is_dir()
        and not path.name.startswith((".", "_"))
        and (wanted is None or path.name in wanted)
    )
    if wanted:
        missing = sorted(wanted - {path.name for path in selected})
        if missing:
            raise FixtureValidationError(suite_root.name, [f"请求的 fixture 不存在: {missing}"])
    fixtures: list[SyntheticFixture] = []
    invalid: dict[str, list[str]] = {}
    for path in selected:
        try:
            fixtures.append(load_fixture(path))
        except FixtureValidationError as exc:
            invalid[exc.task_id] = exc.reasons
    return SyntheticSuite(root=suite_root, fixtures=fixtures, invalid=invalid)


def _reject_links(root: Path) -> None:
    """整棵 fixture 树都拒绝 symlink/重解析点。

    只挡 ``repo/`` 不够：``eval/gold.patch`` 若是指向别处的链接，评测侧读到的
    就不是它看起来的那个文件，"记录下来的 gold 哈希"与"真正应用的 gold"会分开。
    """
    offenders: list[str] = []
    for path in sorted(root.rglob("*")):
        if is_link(path):
            offenders.append(path.relative_to(root).as_posix())
    if offenders:
        raise FixtureValidationError(root.name, [f"fixture 含符号链接或重解析点: {offenders[:5]}"])


def _load_metadata(root: Path, reasons: list[str]) -> FixtureMetadata | None:
    try:
        payload = json.loads((root / METADATA_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        reasons.append(f"metadata.json 读取失败: {exc}")
        return None
    try:
        return FixtureMetadata.model_validate(payload)
    except ValidationError as exc:
        reasons.extend(f"metadata.json: {item['loc']}: {item['msg']}" for item in exc.errors())
        return None


def _load_issue(root: Path, reasons: list[str]) -> str | None:
    try:
        text = (root / ISSUE_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reasons.append(f"issue.md 读取失败: {exc}")
        return None
    if not text.strip():
        reasons.append("issue.md 为空")
        return None
    return text


def _load_env(root: Path, reasons: list[str]) -> EnvSpec | None:
    try:
        return load_env_spec(root / ENV_FILE)
    except EnvSpecError as exc:
        reasons.append(f"env.yaml 无效: {exc}")
        return None


def _load_tests(eval_dir: Path, reasons: list[str]) -> EvalTests | None:
    try:
        payload: Any = json.loads((eval_dir / TESTS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        reasons.append(f"tests.json 读取失败: {exc}")
        return None
    try:
        return EvalTests.model_validate(payload)
    except (ValidationError, ValueError) as exc:
        reasons.append(f"tests.json 无效: {exc}")
        return None


def _load_patches(eval_dir: Path, reasons: list[str]) -> tuple[str | None, str | None]:
    texts: list[str | None] = []
    for name in (GOLD_PATCH_FILE, TEST_PATCH_FILE):
        try:
            text = (eval_dir / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            reasons.append(f"{name} 读取失败: {exc}")
            texts.append(None)
            continue
        if not text.strip():
            reasons.append(f"{name} 为空")
            texts.append(None)
            continue
        texts.append(text)
    if len(texts) == 2:
        return (texts[0], texts[1])
    return (None, None)


def _patch_reasons(
    gold_text: str,
    test_text: str,
    gold_files: tuple[str, ...],
    test_files: tuple[str, ...],
    tests: EvalTests,
    repo_files: Iterable[str],
) -> list[str]:
    reasons: list[str] = []
    for label, touched in (("gold.patch", gold_files), ("test.patch", test_files)):
        for path in touched:
            if path.startswith(f"{EVALUATION_DIRECTORY}/"):
                reasons.append(f"{label} 试图修改评测目录: {path}")
            if path == ".git" or path.startswith(".git/"):
                reasons.append(f"{label} 试图修改 git 元数据: {path}")
    if gold_files and set(gold_files) & set(test_files):
        reasons.append(
            "gold.patch 与 test.patch 修改了同一文件，无法区分修复与评测测试: "
            f"{sorted(set(gold_files) & set(test_files))[:3]}"
        )
    if gold_text == test_text:
        reasons.append("gold.patch 与 test.patch 内容相同")

    # 角色分离：gold 只改实现，test.patch 只改测试。两条混在一起会让"补丁通过
    # 了评测"变成"补丁顺手改了评测"，而那正是 Self-verified Precision 想暴露的行为。
    test_like = {item for item in test_files if _is_test_like(item)}
    for path in set(test_files) - test_like:
        reasons.append(f"test.patch 修改了非测试文件，评测与修复的角色混在一起: {path}")
    for path in gold_files:
        if _is_test_like(path):
            reasons.append(f"gold.patch 修改了测试文件，应只改实现: {path}")

    present = set(repo_files) | set(test_files)
    hidden = set(test_files)
    for label, nodes in (
        ("fail_to_pass", tests.fail_to_pass),
        ("pass_to_pass", tests.pass_to_pass),
    ):
        for node in nodes:
            file_part = node.split("::", 1)[0]
            if file_part not in present:
                reasons.append(f"{label} 指向一个既不在 repo/ 也不在 test.patch 里的文件: {node!r}")
    # FAIL_TO_PASS 必须由 test.patch 引入或修改。仓库里**已有**的测试对推理侧是
    # 可见的（03 §11.2 明确允许），把它当隐藏目标测试，判据就从"模型是否修好了"
    # 滑向"模型是否注意到了那个失败"，两者不再可比。
    leaked = sorted(
        {item.split("::", 1)[0] for item in tests.fail_to_pass} - hidden,
    )
    if leaked:
        reasons.append(
            f"fail_to_pass 的测试文件没有出现在 test.patch 中，因此不是隐藏评测测试: {leaked[:5]}"
        )
    return reasons


def _is_test_like(path: str) -> bool:
    """判断一个路径是否"看起来是测试"。

    复用工具层同一套判据（``is_test_path``），再额外放行 ``conftest.py``——
    SWE-bench 的 test_patch 经常需要它来注册夹具，而 ``is_test_path`` 只认
    ``tests/`` 目录与 ``test_*.py`` 命名。
    """
    if PurePosixPath(path).name == "conftest.py":
        return True
    return is_test_path(path)


def _digest_repository(root: Path) -> dict[str, str]:
    try:
        return directory_digests(root / REPOSITORY_DIRECTORY)
    except FixtureValidationError as exc:
        raise FixtureValidationError(root.name, [f"repo/{item}" for item in exc.reasons]) from exc


def _fixture_version(
    metadata: FixtureMetadata,
    issue: str,
    env_spec: EnvSpec,
    tests: EvalTests,
    gold_text: str,
    test_text: str,
) -> str:
    payload = {
        "schema_version": metadata.schema_version,
        "task_id": metadata.task_id,
        "category": metadata.category.value,
        "difficulty": metadata.difficulty,
        "multi_file": metadata.multi_file,
        "has_traceback": metadata.has_traceback,
        "base_commit": metadata.base_commit,
        "repo_files": metadata.repo_files,
        "issue": issue,
        "env": env_spec.canonical_json(),
        "fail_to_pass": tests.fail_to_pass,
        "pass_to_pass": tests.pass_to_pass,
        "gold_patch": gold_text,
        "test_patch": test_text,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "synthetic-v1-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
