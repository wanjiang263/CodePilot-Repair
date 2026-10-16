"""E-05 / E-06：把任务定义写成 ``benchmarks/synthetic/`` 并自证有效性。

    uv run python scripts/build_synthetic_fixtures.py --self-check

``--self-check`` 在宿主机上真跑两遍 pytest（base 与 gold），用 junit XML 读结论，
要求：

1. 目标测试在 **base 上失败**、在 **gold 上通过**；
2. **可见测试在两侧都通过** —— 这条是 PASS_TO_PASS 的定义本身，也是"修好之后不
   该把自己的测试弄红"这个约束。它必须由运行结果保证，而不是由作者断言。

因此 ``pass_to_pass`` 不是手写的，而是**测出来的**：凡是 base 上就红的可见测试，
都会被列成待修问题而不是被悄悄写进清单。这样 E-02 的真实 Docker 校验面对的是
一批已经结构自洽的 fixture，Docker 只用来确认"在容器里也一样"，而不是用来发现
写错了的任务。

``--self-check`` 不能替代 E-02：它跑在宿主机 Python 上，不是受控容器，也没有
核对镜像身份。正式有效性结论只来自 ``scripts/validate_fixtures.py``。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPO_ROOT / "src", REPO_ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from synthetic_hosts import COMMON_ENV, HOSTS  # noqa: E402
from synthetic_tasks import TASKS, Bug, TaskDef  # noqa: E402

from codepilot.eval.synthetic import (  # noqa: E402
    BenchmarkCategory,
    SyntheticFixture,
    load_fixture,
    validate_suite,
)
from codepilot.eval.synthetic_build import TaskSpec, build_fixture  # noqa: E402
from codepilot.sandbox.junit import parse_junit  # noqa: E402
from codepilot.schemas.enums import TestOutcome  # noqa: E402

SUITE_ROOT = REPO_ROOT / "benchmarks" / "synthetic"
PACKAGES = ("__pycache__", ".pytest_cache", ".pytest-tmp")


class CheckError(RuntimeError):
    """自检没通过；带上 task_id 与全部原因。"""

    def __init__(self, task_id: str, reasons: list[str]) -> None:
        self.task_id = task_id
        self.reasons = reasons
        super().__init__(f"{task_id}: " + "; ".join(reasons))


def apply_bug(clean: dict[str, str], bug: Bug) -> dict[str, str]:
    """把一处 ``fixed`` 换成 ``buggy``，产出 base 状态的文件表。"""
    source = clean[bug.file]
    hits = source.count(bug.fixed)
    if hits != 1:
        raise CheckError(bug.file, [f"{bug.file} 里 fixed 片段出现 {hits} 次，需要恰好 1 次"])
    if bug.buggy and bug.buggy == bug.fixed:
        raise CheckError(bug.file, [f"{bug.file}: buggy 与 fixed 相同，种子缺陷不存在"])
    return {**clean, bug.file: source.replace(bug.fixed, bug.buggy)}


def base_files(task: TaskDef) -> dict[str, str]:
    clean = HOSTS[task.host]
    files = dict(clean)
    for bug in task.bugs:
        files = apply_bug(files, bug)
    if files == clean:
        raise CheckError(task.task_id, ["base 与干净源码完全相同"])
    return files


def gold_files(task: TaskDef) -> dict[str, str]:
    return {bug.file: HOSTS[task.host][bug.file] for bug in task.bugs}


def to_spec(
    task: TaskDef,
    *,
    pass_to_pass: tuple[str, ...],
    fail_to_pass: tuple[str, ...],
    traceback_text: str,
) -> TaskSpec:
    issue = task.issue
    if traceback_text:
        issue = f"{issue.rstrip()}\n\nActual behaviour on the current revision:\n\n{traceback_text}"
    return TaskSpec(
        task_id=task.task_id,
        category=task.category,
        difficulty=task.difficulty,
        issue=issue,
        base_files=base_files(task),
        gold_files=gold_files(task),
        hidden_test_files=task.hidden,
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
        env=dict(COMMON_ENV),
    )


# ------------------------------------------------------------------ pytest 自检


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    tree = root / "repo"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)
    for relative, content in files.items():
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return tree


def _run_suite(tree: Path, work: Path) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """在给定文件树上跑 pytest。

    返回 ``node_id -> outcome`` 与 ``node_id -> (失败类型, 失败文本)``。

    结果只从 junit XML 读（03 §13）：终端文本会随 pytest 版本与 -q/-v 而变化，
    而"哪些测试红了"是这个脚本唯一的判据来源。
    """
    import os

    junit = work / "junit.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--tb=long",
        f"--junitxml={junit.as_posix()}",
        "tests",
    ]
    completed = subprocess.run(
        command,
        cwd=tree,
        env={**os.environ, "PYTHONPATH": str(tree / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if not junit.is_file():
        raise CheckError(
            tree.name,
            [f"pytest 没有产出 junit（exit={completed.returncode}）", completed.stdout[-400:]],
        )
    report = parse_junit(junit)
    outcomes = {case.node_id: case.outcome.value for case in report.tests}
    failures = {
        case.node_id: (case.failure_type or "", case.traceback or case.message or "")
        for case in report.tests
        if case.outcome.value in (TestOutcome.FAILED.value, TestOutcome.ERROR.value)
    }
    if not outcomes:
        raise CheckError(
            tree.name,
            [f"junit 里没有任何测试（exit={completed.returncode}）: {completed.stdout[-500:]}"],
        )
    return outcomes, failures


def _assert_compilable(files: dict[str, str], label: str) -> None:
    """先编译一遍再生成文件树。

    宿主源码是用字符串写的，``"\\n"`` 少写一个反斜杠就会变成一个真换行，产出的
    ``report.py`` 于是带一个未闭合的字符串字面量。这种错误由 pytest 报出来时是
    "collection error"，会被误读成任务设计问题；在这里当场编译，报的就是它本身。
    """
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        try:
            compile(content, path, "exec")
        except SyntaxError as exc:
            raise CheckError(
                label, [f"{path} 语法不合法：{exc.msg}（第 {exc.lineno} 行）"]
            ) from exc


#: 这些类别的缺陷表现就是"炸了"，真实 Issue 里带 traceback 是自然的写法。
#: 其余类别的 Issue 只有文字描述，用来满足 E-06 "至少 5 个不含 traceback"，并让
#: suite 同时覆盖"只看描述能不能定位"和"给了栈能不能定位"两种难度。
TRACEBACK_CATEGORIES = frozenset(
    {
        BenchmarkCategory.WRONG_EXCEPTION,
        BenchmarkCategory.API_MISUSE,
        BenchmarkCategory.EDGE_CASE,
    }
)


#: 这些"失败"是断言不等，不是库自己崩了；它们的输出里必然带着期望值。
_ASSERTION_FAILURES = frozenset({"", "AssertionError", "Failed"})


def _traceback_block(failures: dict[str, tuple[str, str]], node: str) -> str:
    """从**真实执行**里取一段失败现场。

    刻意不手写 traceback：手写的栈会指向作者想象中的行号，而 Issue 里的行号是推理
    可见信息，写错了就把模型引到别处去。

    三条过滤都对应第一版的真实泄漏点，均由 ``scripts/check_leakage.py`` 抓到：

    * 丢掉引用 ``tests/`` 的行 —— pytest 会把隐藏测试的断言原文连同路径一起打出来，
      而隐藏测试是评测内容；
    * **断言不等整段都不要** —— ``assert 'a' == 'b'`` 的差异输出把判分标准直接写进
      了推理可见的 Issue，那不是"描述现象"，那就是答案；
    * 路径分隔符归一成 ``/`` —— junit 里带的是构建机的形态，把反斜杠固化进要提交的
      fixture 等于把构建机操作系统写进数据集。

    于是只有库自己抛出的异常（KeyError / IndexError / ZeroDivisionError…）才会带
    traceback，而那些内容只涉及 base 源码——base 本来就对模型可读。
    """
    failure_type, text = failures.get(node, ("", ""))
    text = text.strip().replace("\\", "/")
    if not text or failure_type in _ASSERTION_FAILURES:
        return ""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "tests/" in stripped:
            continue
        if stripped.startswith("E ") or "src/" in stripped:
            kept.append(stripped)
    if not kept:
        return ""
    return "```\nTraceback (most recent call last):\n" + "\n".join(kept[-6:]) + "\n```\n"


def check_task(task: TaskDef, work: Path) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """跑 base 与 gold 两遍，返回 ``(PASS_TO_PASS, FAIL_TO_PASS, 真实 traceback)``。"""
    base = base_files(task)
    gold_overlay = gold_files(task)
    gold = {**base, **gold_overlay}
    hidden_names = set(task.hidden)
    _assert_compilable({**base, **task.hidden}, task.task_id)
    _assert_compilable({**gold, **task.hidden}, task.task_id)

    base_tree = _write_tree(work / "base", {**base, **task.hidden})
    base_outcomes, base_failures = _run_suite(base_tree, work / "base")

    gold_tree = _write_tree(work / "gold", {**gold, **task.hidden})
    gold_outcomes, _ = _run_suite(gold_tree, work / "gold")

    reasons: list[str] = []

    def is_hidden(node: str) -> bool:
        return any(node.startswith(f"{name}::") for name in hidden_names)

    def module_error_nodes(outcomes: dict[str, str], side: str) -> set[str]:
        """没有 ``::`` 的条目是模块级收集错误，不是测试。"""
        bad: set[str] = set()
        for node, outcome in outcomes.items():
            if "::" in node:
                continue
            stem = f"{node.replace('.', '/').rstrip('/')}.py"
            label = "隐藏测试文件" if stem in hidden_names else "可见测试文件"
            reasons.append(f"{side} 的 {label} {node} 无法收集（{outcome}）")
            bad.add(node)
        return bad

    base_errors = module_error_nodes(base_outcomes, "base")
    gold_errors = module_error_nodes(gold_outcomes, "gold")

    visible = sorted(
        node
        for node in base_outcomes
        if "::" in node and not is_hidden(node) and node not in base_errors
    )
    for node in visible:
        if base_outcomes[node] != TestOutcome.PASSED.value:
            reasons.append(f"可见测试在 base 上就是红的: {node} -> {base_outcomes[node]}")
        if gold_outcomes.get(node) != TestOutcome.PASSED.value:
            reasons.append(
                f"可见测试在 gold 上变红（修复弄坏了自己的测试）: {node} -> {gold_outcomes[node]}"
            )

    # FAIL_TO_PASS 是**测量结果**而不是作者声明：隐藏文件里那些两边都通过的断言是
    # 防止"过度修复"的守卫，把它们也列进目标测试，tests.json 里就会出现根本不区分
    # base 与 gold 的条目，Resolved Rate 的分母口径随之失真。
    hidden_nodes = sorted(
        node for node in gold_outcomes if is_hidden(node) and node not in gold_errors
    )
    discriminating = sorted(
        node
        for node in hidden_nodes
        if base_outcomes.get(node) in (TestOutcome.FAILED.value, TestOutcome.ERROR.value)
    )
    if not discriminating:
        reasons.append("隐藏测试里没有任何用例真正区分 base 与 gold")
    # 守卫断言也必须真的通过，否则"目标测试"只是被顺手挑出来的一小部分，同一文件
    # 里剩下的部分坏没坏就没人看。
    for node in set(hidden_nodes) - set(discriminating):
        if gold_outcomes[node] != TestOutcome.PASSED.value:
            reasons.append(f"隐藏守卫断言在 gold 上没通过: {node} -> {gold_outcomes[node]}")

    if reasons:
        raise CheckError(task.task_id, reasons)
    traceback_text = ""
    if task.category in TRACEBACK_CATEGORIES:
        # 取第一个"库自己抛异常"的失败节点。断言不等的节点跳过：它们的输出就是答案。
        for node in discriminating:
            traceback_text = _traceback_block(base_failures, node)
            if traceback_text:
                break
    return tuple(visible), tuple(discriminating), traceback_text


# ------------------------------------------------------------------ 汇总核对


def suite_requirements(tasks: list[TaskDef]) -> list[str]:
    """04 / E-05 / E-06 的数量要求，逐条核对。"""
    reasons: list[str] = []
    if len(tasks) < 30:
        reasons.append(f"任务数 {len(tasks)} < 30")
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.category.value] = counts.get(task.category.value, 0) + 1
    missing = sorted({item.value for item in BenchmarkCategory} - set(counts))
    if missing:
        reasons.append(f"缺少类别: {missing}")
    thin = sorted(f"{key}={value}" for key, value in counts.items() if value < 2)
    if thin:
        reasons.append(f"类别配额不足两个: {thin}")
    multi = sum(1 for task in tasks if len({bug.file for bug in task.bugs}) > 1)
    if multi < 5:
        reasons.append(f"多文件修复任务 {multi} < 5")
    ids = [task.task_id for task in tasks]
    if len(set(ids)) != len(ids):
        reasons.append("存在重复 task_id")
    return reasons


def suite_content_requirements(fixtures: Sequence[SyntheticFixture]) -> list[str]:
    """traceback 多样性只能**建完之后**核对。

    一个缺陷到底会不会让库自己抛异常，是执行出来的事实，不是类别的标签：同属
    edge_case 的两个任务，一个抛 IndexError、另一个只是断言不等，而后者不能带
    traceback（带上去就是漏答案）。所以这里读 metadata 里测得的 has_traceback。
    """
    with_trace = sum(1 for item in fixtures if item.metadata.has_traceback)
    without = sum(1 for item in fixtures if not item.metadata.has_traceback)
    reasons: list[str] = []
    if without < 5:
        reasons.append(f"不含 traceback 的 Issue {without} < 5")
    if with_trace < 5:
        reasons.append(f"含 traceback 的 Issue {with_trace} < 5（现场多样性不足）")
    return reasons


# ------------------------------------------------------------------ 入口


def build_one(task: TaskDef, suite_root: Path, scratch: Path) -> tuple[Path, int, int]:
    """跑自检、写出 fixture，并立刻按正式 Schema 读回来。"""
    pass_to_pass, fail_to_pass, traceback_text = check_task(task, scratch / task.task_id)
    spec = to_spec(
        task,
        pass_to_pass=pass_to_pass,
        fail_to_pass=fail_to_pass,
        traceback_text=traceback_text,
    )
    directory = build_fixture(spec, suite_root, scratch_root=scratch / "patch")
    load_fixture(directory)
    return directory, len(pass_to_pass), len(fail_to_pass)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the synthetic benchmark suite.")
    parser.add_argument("--suite", type=Path, default=SUITE_ROOT)
    parser.add_argument("--only", nargs="*", default=None, help="只构建这些 task_id。")
    parser.add_argument(
        "--keep-existing", action="store_true", help="不清空 suite 目录（用于增量重建）。"
    )
    args = parser.parse_args(argv)

    selected = [task for task in TASKS if args.only is None or task.task_id in args.only]
    # 数量要求是对**整个 suite** 的约束（04：>=30 个、十类各 >=2、>=5 多文件）。
    # 只重建其中几个任务时这些数字必然不达标，把它们当错误会让增量重建根本没法跑。
    if args.only is None:
        problems = suite_requirements(selected)
        if problems:
            for problem in problems:
                print(f"REQ FAIL: {problem}", file=sys.stderr)
            return 2

    if not args.keep_existing and args.only is None and args.suite.is_dir():
        for child in sorted(args.suite.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
    args.suite.mkdir(parents=True, exist_ok=True)

    scratch = Path(tempfile.mkdtemp(prefix="synthetic-build-"))
    failures: list[CheckError] = []
    try:
        for task in selected:
            try:
                directory, p2p, f2p = build_one(task, args.suite, scratch)
            except CheckError as exc:
                failures.append(exc)
                print(f"CHECK FAIL {exc.task_id}")
                for reason in exc.reasons[:10]:
                    print(f"    - {reason}")
                continue
            print(f"  built {task.task_id:<46} f2p={f2p} p2p={p2p} -> {directory.name}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)}/{len(selected)} 个任务未通过自检", file=sys.stderr)
        return 1

    suite = validate_suite(args.suite)
    print(f"\nsuite: {len(suite)} 个 fixture，schema 无效 {len(suite.invalid)}")
    for task_id, reasons in sorted(suite.invalid.items()):
        print(f"  INVALID {task_id}: {reasons[0]}")
    if args.only is None:
        content_problems = suite_content_requirements(suite.fixtures)
        for problem in content_problems:
            print(f"REQ FAIL: {problem}", file=sys.stderr)
        if content_problems:
            return 2
    return 0 if not suite.invalid else 1


if __name__ == "__main__":
    raise SystemExit(main())
