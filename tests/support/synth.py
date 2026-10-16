"""合成 fixture 的测试夹具：与 ``benchmarks/`` 用同一个构建器，避免两套定义漂移。

这里刻意只提供**最小**任务定义。真实 suite 的 30+ 任务在
``scripts/synthetic_tasks.py``，而本模块的任务是让隔离与校验逻辑可以在
``tmp_path`` 里被反复构造出**坏**样本——把坏样本写进 ``benchmarks/`` 会污染
正式 suite，也会被 E-02 的真实 Docker 校验拒掉。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from codepilot.eval.synthetic import BenchmarkCategory, SyntheticFixture, load_fixture
from codepilot.eval.synthetic_build import TaskSpec, build_fixture

#: 唯一标识。**只**允许出现在评测侧（gold 的新增行、隐藏测试）。放进 base 源码
#: 就等于把它放进推理 workspace，那会同时毁掉两类断言的意义：哨兵"找不到"变得
#: 毫无意义（根本没有地方可找），E-07 的泄漏检查也变得毫无意义（答案本来就在明文里）。
SENTINEL = "GOLD-SENTINEL-5f3a9c1e-do-not-leak"

_MODULE_DOC = '"""Demonstration package with one seeded defect."""'

BUGGY = f"{_MODULE_DOC}\n\n\ndef normalize(value: int) -> int:\n    return value - 1\n"

FIXED = f"{_MODULE_DOC}\n\n\ndef normalize(value: int) -> int:\n    return value\n"

#: gold 的"答案形状"版本：修好的同时在**新增行**里引入一个只有正确实现才有的记号。
#: E-07 用它制造一次真实泄漏（把这一行写进 issue.md），并确认脚本抓得到。
GOLD_SENTINEL_LINE = f"    return value  # {SENTINEL}"
FIXED_WITH_SENTINEL = (
    f"{_MODULE_DOC}\n\n\ndef normalize(value: int) -> int:\n{GOLD_SENTINEL_LINE}\n"
)

EXISTING_TEST = (
    "from pkg.core import normalize\n\n\n"
    "def test_documented_base_behaviour() -> None:\n    assert normalize(3) == 2\n"
)

HIDDEN_TEST = (
    "from pkg.core import normalize\n\n\n"
    "def test_normalize_keeps_value() -> None:\n"
    f"    # 目标断言由 {SENTINEL} 标记\n"
    "    assert normalize(3) == 3\n"
)

PYPROJECT = "[project]\nname='pkg'\nversion='0.0.1'\n"

DEFAULT_ENV: dict[str, object] = {
    "python_version": "3.12",
    "install": ["python -m pip install pytest"],
    "env": {"PYTHONPATH": "/workspace/src"},
    "timeout_seconds": 120,
}

DEFAULT_BASE: dict[str, str] = {
    "pyproject.toml": PYPROJECT,
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": BUGGY,
    "tests/test_public.py": EXISTING_TEST,
}
DEFAULT_GOLD: dict[str, str] = {"src/pkg/core.py": FIXED}
DEFAULT_HIDDEN: dict[str, str] = {"tests/test_hidden.py": HIDDEN_TEST}


def demo_spec(
    *,
    task_id: str = "demo-off-by-one",
    issue: str = "normalize(3) returns 2. The contract is that it returns its argument.\n",
    base_files: Mapping[str, str] | None = None,
    gold_files: Mapping[str, str] | None = None,
    hidden_test_files: Mapping[str, str] | None = None,
    fail_to_pass: tuple[str, ...] = ("tests/test_hidden.py::test_normalize_keeps_value",),
    pass_to_pass: tuple[str, ...] = ("tests/test_public.py::test_documented_base_behaviour",),
    category: BenchmarkCategory = BenchmarkCategory.INCORRECT_RETURN,
    difficulty: str = "easy",
    env: dict[str, object] | None = None,
) -> TaskSpec:
    """一个结构完整、可运行、单文件修复的最小任务。"""
    return TaskSpec(
        task_id=task_id,
        category=category,
        difficulty=difficulty,
        issue=issue,
        base_files=dict(DEFAULT_BASE if base_files is None else base_files),
        gold_files=dict(DEFAULT_GOLD if gold_files is None else gold_files),
        hidden_test_files=dict(DEFAULT_HIDDEN if hidden_test_files is None else hidden_test_files),
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        env=dict(DEFAULT_ENV if env is None else env),
    )


def gold_sentinel_spec(*, task_id: str = "demo-gold-sentinel") -> TaskSpec:
    """gold 的新增行里带唯一记号的任务——泄漏检查的正控制。"""
    return demo_spec(task_id=task_id, gold_files={"src/pkg/core.py": FIXED_WITH_SENTINEL})


def build_and_load(tmp_path: Path, spec: TaskSpec, *, name: str | None = None) -> SyntheticFixture:
    """写出 fixture 再加载回来，返回校验通过的对象。"""
    return load_fixture(write_fixture(tmp_path, spec, name=name))


def write_fixture(tmp_path: Path, spec: TaskSpec, *, name: str | None = None) -> Path:
    """写出 fixture 目录并返回它。``name`` 用于在同一次运行里放多份不冲突的副本。"""
    return build_fixture(
        spec,
        tmp_path / "suite",
        scratch_root=tmp_path / "scratch" / (name if name is not None else "all"),
    )
