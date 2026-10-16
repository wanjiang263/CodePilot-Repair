"""E-07：答案泄漏检查。

这个模块回答一个具体的问题：**推理可见的内容里，有没有出现只有知道答案才写得出的
东西**。它不回答"这个 Issue 是否公平"——那需要人读。

分层与判据：

* **ERROR**（结构可判定，必须修）：Issue 逐字包含 gold 新增的代码行；metadata 里
  出现 gold 文件清单或实现片段；推理 workspace 里存在 ``eval/``；工具能把
  ``eval/*`` 解析出来；评测目录通过 symlink 或路径别名可达。
* **WARNING**（启发式，交给人判断）：Issue 使用了只在 gold/隐藏测试里出现、base
  里完全不存在的标识符；Issue 里出现"改成/应当改为/加上"一类直接instruction。

规范化匹配的原因：Issue 作者会把一行代码折行、改缩进、加反引号。只比字面量的话，
``return value - 1`` 换成两个空格就绕过检查，而它对模型仍然是同一个答案。这里同时
比"原样"与"压缩空白后"两种形态。

刻意保留的下界：短行（``return`` / ``)`` / ``x = 1``）不参与精确匹配。把 30 个
合法 Issue 全报成泄漏的检查器，只会让人学会忽略它的输出——那比没有检查更糟。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from codepilot.eval.synthetic import (
    EVALUATION_DIRECTORY,
    INFERENCE_DENIED_DIRECTORIES,
    SyntheticFixture,
    patch_added_lines,
)
from codepilot.fsutil import is_link
from codepilot.tools.base import PathAccessError, WorkspacePathPolicy

__all__ = [
    "LeakageFinding",
    "LeakageReport",
    "LeakageSeverity",
    "audit_fixture",
    "audit_suite",
    "check_artifact_contamination",
    "check_inference_projection",
    "check_issue_text",
    "check_metadata",
    "check_symlink_escape",
    "check_tool_reachability",
    "check_workspace_contents",
    "normalize_code_line",
]

#: 短于这么多个"实质字符"的行太常见，不足以证明泄漏。
MIN_CODE_LINE_SIGNATURE = 12

#: 直接给出修复动作的措辞。命中只报 WARNING：中文技术描述里"返回/改为"是正常词汇，
#: 把它当 ERROR 会把几乎所有合法 Issue 都标红。
_FIX_INSTRUCTION = re.compile(
    r"(?:应该|应当|需要|必须)?\s*(?:改为|改成|换成|替换为|调整为|加上|补上|增加一行|删掉这行)",
    re.IGNORECASE,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_KEYWORDS = frozenset(
    {
        "def",
        "return",
        "None",
        "True",
        "False",
        "self",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "if",
        "else",
        "elif",
        "for",
        "while",
        "import",
        "from",
        "class",
        "with",
        "pass",
        "lambda",
        "not",
        "and",
        "or",
        "in",
        "is",
        "try",
        "except",
        "raise",
        "value",
        "values",
        "item",
        "items",
        "args",
        "kwargs",
        "key",
        "keys",
        "note",
        "path",
        "name",
        "type",
        "NoneError",
    }
)


class LeakageSeverity(StrEnum):
    __test__: ClassVar[bool] = False

    ERROR = "error"
    WARNING = "warning"


class LeakageFinding:
    """一条泄漏发现。``check`` 是稳定的机器可读名字，供 CI 与报表分组。"""

    def __init__(self, task_id: str, check: str, severity: LeakageSeverity, detail: str) -> None:
        self.task_id = task_id
        self.check = check
        self.severity = severity
        self.detail = detail

    def __repr__(self) -> str:
        return f"<{self.severity.value} {self.task_id}/{self.check}: {self.detail}>"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "check": self.check,
            "severity": self.severity.value,
            "detail": self.detail,
        }


class LeakageReport:
    """一次审计的结果。``clean`` 只代表"结构检查没抓到 ERROR"。"""

    def __init__(
        self,
        findings: Sequence[LeakageFinding] = (),
        checked_tasks: Sequence[str] = (),
        skipped_tasks: Sequence[str] = (),
    ) -> None:
        self.findings = list(findings)
        self.checked_tasks = list(checked_tasks)
        self.skipped_tasks = list(skipped_tasks)

    @property
    def errors(self) -> list[LeakageFinding]:
        return [item for item in self.findings if item.severity is LeakageSeverity.ERROR]

    @property
    def warnings(self) -> list[LeakageFinding]:
        return [item for item in self.findings if item.severity is LeakageSeverity.WARNING]

    @property
    def clean(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_tasks": len(self.checked_tasks),
            "skipped_tasks": list(self.skipped_tasks),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [item.as_dict() for item in self.findings],
            "note": (
                "本工具只能判定**结构性**泄漏（逐字代码、eval/ 可达、字段投影）。"
                "改写过的、语义层面的答案泄漏需要人工复核，clean 不等于无泄漏。"
            ),
        }


def normalize_code_line(text: str) -> str:
    """压缩空白、去掉行尾分号与反引号包裹，保留缩进语义之外的部分。

    只做**比较用**的归一化，不改存储内容。反引号要去掉是因为 Issue 里代码常被写成
    `` `return value - 1` `` ——那是同一段答案，不是"提到过这个概念"。
    """
    stripped = text.strip().strip("`").strip()
    collapsed = re.sub(r"\s+", " ", stripped)
    return collapsed.rstrip(";").strip()


def _substantive(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _significance(text: str) -> int:
    return len(_substantive(text))


#: 隐藏测试里构成"判分标准"的行。只有这些行算泄漏：断言的期望值就是答案本身，
#: 而"构造一个 RetryPolicy(attempts=0)"只是描述症状的正常 Issue 写法。
_EXPECTATION_MARKERS = ("assert", "==", "pytest.raises", "raises ", "def test_")


def _disclosed_lines(patch: str, *, is_test_patch: bool) -> list[str]:
    """补丁新增行里，哪些**不该**出现在推理可见文本中。

    gold 的实现行全部算；test.patch 只算携带期望值的那些（``assert x == y``、
    ``pytest.raises(...)``、测试函数名）。不加这个区分的话，"Issue 描述里出现了和
    隐藏测试setup 相同的调用写法"会被报成泄漏，而那正是诚实的 Issue 写法。
    """
    lines: list[str] = []
    for raw in patch_added_lines(patch):
        normalized = normalize_code_line(raw)
        if _significance(normalized) < MIN_CODE_LINE_SIGNATURE:
            continue
        if is_test_patch and not any(marker in normalized for marker in _EXPECTATION_MARKERS):
            continue
        lines.append(normalized)
    return lines


def check_issue_text(
    task_id: str,
    issue: str,
    gold_patch: str,
    *,
    test_patch: str = "",
) -> list[LeakageFinding]:
    """Issue 是否逐字包含 gold 新增行，或隐藏测试的期望值。

    隐藏测试的期望值比 gold 片段更要紧：把它写进 Issue，等于把判分标准直接交给模型。
    """
    findings: list[LeakageFinding] = []
    haystack_raw = issue
    haystack_norm = normalize_code_line(issue)
    haystack_compact = _substantive(issue).casefold()
    for label, patch, is_test in (
        ("gold", gold_patch, False),
        ("test", test_patch, True),
    ):
        for normalized in _disclosed_lines(patch, is_test_patch=is_test):
            where = f"{label}.patch"
            if normalized in haystack_raw or normalized in haystack_norm:
                findings.append(
                    LeakageFinding(
                        task_id,
                        f"{where}_added_line_in_issue",
                        LeakageSeverity.ERROR,
                        f"Issue 逐字包含 {where} 新增行: {normalized!r}",
                    )
                )
            elif _significance(normalized) >= 24 and normalized.casefold() in haystack_compact:
                findings.append(
                    LeakageFinding(
                        task_id,
                        f"{where}_added_line_normalized",
                        LeakageSeverity.ERROR,
                        f"Issue 以变体写法包含 {where} 新增行: {normalized!r}",
                    )
                )
    return findings


def check_fix_instruction(task_id: str, issue: str) -> list[LeakageFinding]:
    """Issue 是否直接下达修复动作。WARNING 级，交人工判定。"""
    hits = [match.group(0).strip() for match in _FIX_INSTRUCTION.finditer(issue)]
    if not hits:
        return []
    return [
        LeakageFinding(
            task_id,
            "prescriptive_fix_language",
            LeakageSeverity.WARNING,
            f"Issue 含有直接指示修复写法的措辞: {sorted(set(hits))} —— 需人工确认是否构成答案",
        )
    ]


def _code_like(name: str) -> bool:
    """这个名字看起来像**代码符号**，而不是领域词汇。

    ``Rent``、``lunch``、``currency`` 既出现在 gold 的字符串字面量里，也必然出现在
    一个诚实描述现象的 Issue 里。把它们当"答案词汇"汇报，等于把合法 Issue 全部标红，
    而一个误报率高的检查器只会教会别人忽略它。真正的符号泄漏长这样：
    ``EXPORT_CURRENCY``、``UnknownToken``、``render_row_cached``——带下划线、驼峰或
    全大写。
    """
    if "_" in name or name.isupper():
        return True
    return name[0].isupper() and len(name) >= 4 and name != name.upper()


def check_answer_vocabulary(
    task_id: str,
    issue: str,
    *,
    base_files: dict[str, str],
    gold_patch: str,
) -> list[LeakageFinding]:
    """Issue 是否使用了 base 里不存在、只在 gold 里出现的**符号名**。

    只看 gold 的新增行，不看隐藏测试：隐藏测试里的 ``test_*`` / ``pytest`` /
    ``raises`` 是测试脚手架而不是修复方案，把它们算进"答案词汇"会让每条 Issue 都
    命中。
    """
    base_text = "\n".join(base_files.values())
    issue_identifiers = {item for item in _IDENTIFIER.findall(issue) if item not in _KEYWORDS}
    if not issue_identifiers:
        return []
    added = "\n".join(patch_added_lines(gold_patch))
    answer_identifiers = {item for item in _IDENTIFIER.findall(added) if item not in _KEYWORDS}
    leaked = sorted(
        item
        for item in issue_identifiers & answer_identifiers
        if _code_like(item) and item not in base_text
    )
    if not leaked:
        return []
    return [
        LeakageFinding(
            task_id,
            "answer_only_identifier_in_issue",
            LeakageSeverity.WARNING,
            f"Issue 使用了 base 中不存在、只在 gold 里出现的符号名: {leaked} —— 需人工确认",
        )
    ]


#: ``repo_files`` 是 base 的文件清单，这些路径本来就在推理可见的仓库里，列出来不
#: 构成泄漏；真正不能出现的是"哪个文件被 gold 改了"这类自由文本指路。
_METADATA_PATH_FREE_KEYS = frozenset({"repo_files", "base_commit", "task_id", "schema_version"})


def check_metadata(
    task_id: str, metadata: dict[str, Any], gold_files: Sequence[str]
) -> list[LeakageFinding]:
    """推理可见的 metadata 是否携带 gold 实现细节。

    Schema 的 ``extra="forbid"`` 已经挡住了"新增一个 gold_files 字段"这条路；这里查
    的是**值**里带没带答案。检查范围只限自由文本字段——把 base 里本来就存在的文件
    路径当成泄漏，会把每一个合法 fixture 都报成错误。
    """
    findings: list[LeakageFinding] = []
    forbidden_keys = {
        "gold",
        "gold_files",
        "gold_patch",
        "fix",
        "solution",
        "answer",
        "patch",
        "hint",
    }
    for key in metadata:
        if key.casefold() in forbidden_keys:
            findings.append(
                LeakageFinding(
                    task_id,
                    "metadata_gold_key",
                    LeakageSeverity.ERROR,
                    f"metadata 含有泄漏答案的字段名: {key}",
                )
            )
    for key, value in metadata.items():
        if key in _METADATA_PATH_FREE_KEYS or not isinstance(value, str):
            continue
        for path in gold_files:
            if path.casefold() in value.casefold():
                findings.append(
                    LeakageFinding(
                        task_id,
                        "metadata_gold_path_value",
                        LeakageSeverity.ERROR,
                        f"metadata[{key}] 的值里指明被修改的文件: {path}",
                    )
                )
    return findings


def check_inference_projection(task_id: str, fixture: SyntheticFixture) -> list[LeakageFinding]:
    """推理视图本身是否携带评测内容。

    这是"隔离靠类型而不是靠约定"的那条线：只要 ``InferenceTask`` 的序列化结果里能
    找到 gold patch 的任何一段实质内容，投影就是破的。
    """
    evaluation = fixture.evaluation
    findings: list[LeakageFinding] = []
    inference = fixture.inference(fixture.directory / "repo")
    raw = "\n".join(
        (inference.issue, inference.model_dump_json(), fixture.env_spec.canonical_json())
    )
    # 三种 haystack 都备一份：只比原样的话，Issue 里把答案折个行、多打两个空格
    # 就绕过去了，而对模型来说那仍然是同一个答案。check_issue_text 用同样的三路
    # 匹配，两处必须一致，否则"Issue 检查严格、投影检查宽松"会留下一个更窄的口子。
    haystacks = (raw, normalize_code_line(raw), _substantive(raw).casefold())
    for label, patch, is_test in (
        ("gold", evaluation.gold_patch, False),
        ("test", evaluation.test_patch, True),
    ):
        for normalized in _disclosed_lines(patch, is_test_patch=is_test):
            compact = normalized.casefold()
            if any(
                needle in haystack
                for needle, haystack in zip(
                    (normalized, normalized, compact), haystacks, strict=True
                )
            ):
                findings.append(
                    LeakageFinding(
                        task_id,
                        "inference_view_carries_patch_line",
                        LeakageSeverity.ERROR,
                        f"推理视图里出现 {label} 新增行: {normalized!r}",
                    )
                )
    for node in (*evaluation.tests.fail_to_pass, *evaluation.tests.pass_to_pass):
        if node in raw:
            findings.append(
                LeakageFinding(
                    task_id,
                    "inference_view_carries_test_node_id",
                    LeakageSeverity.ERROR,
                    f"推理视图里出现评测节点 id: {node}",
                )
            )
    if evaluation.fixture_version in raw:
        findings.append(
            LeakageFinding(
                task_id,
                "inference_view_carries_fixture_version",
                LeakageSeverity.ERROR,
                "推理视图里出现 fixture 版本摘要（它由 gold 内容算出）",
            )
        )
    return findings


def check_workspace_contents(task_id: str, workspace: Path) -> list[LeakageFinding]:
    """推理 workspace 里是否存在 ``eval/`` 或评测文件名。"""
    findings: list[LeakageFinding] = []
    root = Path(workspace)
    if (root / EVALUATION_DIRECTORY).exists():
        findings.append(
            LeakageFinding(
                task_id,
                "eval_directory_in_workspace",
                LeakageSeverity.ERROR,
                f"workspace 内存在 {EVALUATION_DIRECTORY}/",
            )
        )
    for name in ("gold.patch", "test.patch", "tests.json"):
        hits = [path for path in root.rglob(name) if path.is_file()]
        if hits:
            findings.append(
                LeakageFinding(
                    task_id,
                    "eval_file_in_workspace",
                    LeakageSeverity.ERROR,
                    f"workspace 内出现评测文件: {[str(p.relative_to(root)) for p in hits]}",
                )
            )
    return findings


def check_tool_reachability(
    task_id: str,
    workspace: Path,
    fixture: SyntheticFixture,
    *,
    deny_directories: frozenset[str] | None = None,
) -> list[LeakageFinding]:
    """工具层能否读到评测内容。

    ``deny_directories`` 默认取 runner 实际使用的 ``INFERENCE_DENIED_DIRECTORIES``，
    **不是**在本函数里另写一份。这个区别决定了本检查有没有意义：硬编码一份"总是
    挡住"的名单，函数就永远返回空，看起来像"检查通过"，实际上是在自我循环论证；
    而一旦那个常量被改掉，也没有任何东西会报警。引用同一个常量之后，名单里少了
    ``eval`` 会立刻在这里暴露。

    尝试的路径含 ``./``、``eval/eval/../``、``../<task>/eval/`` 这类别名：只试规范
    路径会漏掉"按前缀匹配、换个写法就绕过"的情况。
    """
    effective = (
        WorkspacePathPolicy.DEFAULT_DENY_DIRECTORIES | INFERENCE_DENIED_DIRECTORIES
        if deny_directories is None
        else deny_directories
    )
    findings: list[LeakageFinding] = []
    if EVALUATION_DIRECTORY not in effective:
        findings.append(
            LeakageFinding(
                task_id,
                "eval_not_in_denied_directories",
                LeakageSeverity.ERROR,
                f"推理工具策略的拒绝名单里没有 {EVALUATION_DIRECTORY}",
            )
        )
    policy = WorkspacePathPolicy(workspace, deny_directories=effective)
    attempts = (
        f"{EVALUATION_DIRECTORY}/gold.patch",
        f"{EVALUATION_DIRECTORY}/tests.json",
        f"./{EVALUATION_DIRECTORY}/gold.patch",
        f"{EVALUATION_DIRECTORY}/{EVALUATION_DIRECTORY}/../gold.patch",
        f"{EVALUATION_DIRECTORY}/./gold.patch",
        f"../{fixture.task_id}/{EVALUATION_DIRECTORY}/gold.patch",
    )
    for attempt in attempts:
        try:
            resolved = policy.resolve(attempt, must_exist=False)
        except PathAccessError:
            continue
        findings.append(
            LeakageFinding(
                task_id,
                "tool_path_reaches_eval",
                LeakageSeverity.ERROR,
                f"工具策略把 {attempt!r} 解析到了 workspace 内: {resolved}",
            )
        )
    return findings


def check_symlink_escape(
    task_id: str, fixture_directory: Path, workspace: Path
) -> list[LeakageFinding]:
    """评测内容是否会通过 symlink / 重解析点被带进 workspace。"""
    findings: list[LeakageFinding] = []
    for root in (fixture_directory / "repo", workspace):
        if not root.is_dir():
            continue
        offenders = [
            path.relative_to(root).as_posix() for path in sorted(root.rglob("*")) if is_link(path)
        ]
        if offenders:
            findings.append(
                LeakageFinding(
                    task_id,
                    "symlink_inside_inference_tree",
                    LeakageSeverity.ERROR,
                    f"{root.name} 里存在链接（可指向 eval/）: {offenders}",
                )
            )
    return findings


def check_artifact_contamination(
    task_id: str, artifact_files: Iterable[Path], gold_patch: str
) -> list[LeakageFinding]:
    """输出 Artifact 是否混入了评测答案，从而被后续轮次的 Agent 读回去。

    这条针对一个具体事故：Agent 的 ``read_file`` 只能挡住 workspace，而一旦评测器
    把 gold 或目标测试清单写进同一个 run 目录（比如 ``report.json`` 里带
    ``fail_to_pass``），后续轮次的工具调用就能把它读回来。隔离不是"起点干净"就够的。

    扫描全部 gold 新增行而不是第一行：只比第一行的话，第二行开始的答案一概查不到。
    """
    needles = _disclosed_lines(gold_patch, is_test_patch=False)
    findings: list[LeakageFinding] = []
    if not needles:
        return findings
    for path in artifact_files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        candidates = (text, normalize_code_line(text), _substantive(text).casefold())
        for needle in needles:
            hit = any(
                value in haystack
                for value, haystack in zip(
                    (needle, needle, needle.casefold()), candidates, strict=True
                )
            )
            if hit:
                findings.append(
                    LeakageFinding(
                        task_id,
                        "artifact_contains_gold_content",
                        LeakageSeverity.ERROR,
                        f"artifact {path.name} 内含 gold 新增行，可能被后续 Agent 轮次读回: {needle!r}",
                    )
                )
                break
    return findings


def audit_fixture(fixture: SyntheticFixture, *, workspace: Path | None = None) -> LeakageReport:
    """对单个 fixture 跑全部结构检查。"""
    evaluation = fixture.evaluation
    findings: list[LeakageFinding] = [
        *check_issue_text(
            fixture.task_id,
            fixture.issue,
            evaluation.gold_patch,
            test_patch=evaluation.test_patch,
        ),
        *check_fix_instruction(fixture.task_id, fixture.issue),
        *check_answer_vocabulary(
            fixture.task_id,
            fixture.issue,
            base_files={
                relative: (fixture.directory / "repo" / relative).read_text(
                    encoding="utf-8", errors="replace"
                )
                for relative in fixture.metadata.repo_files
            },
            gold_patch=evaluation.gold_patch,
        ),
        *check_metadata(
            fixture.task_id, fixture.metadata.model_dump(mode="json"), evaluation.gold_files
        ),
        *check_inference_projection(fixture.task_id, fixture),
    ]
    if workspace is not None:
        findings.extend(check_workspace_contents(fixture.task_id, workspace))
        findings.extend(check_tool_reachability(fixture.task_id, workspace, fixture))
        findings.extend(check_symlink_escape(fixture.task_id, fixture.directory, workspace))
    return LeakageReport(findings, checked_tasks=[fixture.task_id])


def audit_suite(
    fixtures: Sequence[SyntheticFixture],
    *,
    materialize: bool = False,
    work_root: Path | None = None,
) -> LeakageReport:
    """整个 suite 的泄漏审计。``materialize=True`` 时额外检查真实 workspace。"""
    findings: list[LeakageFinding] = []
    checked: list[str] = []
    for fixture in fixtures:
        workspace: Path | None = None
        if materialize and work_root is not None:
            target = work_root / PurePosixPath(fixture.task_id).name
            if target.exists():
                import shutil

                shutil.rmtree(target, ignore_errors=True)
            workspace = fixture.materialize(target)
        report = audit_fixture(fixture, workspace=workspace)
        findings.extend(report.findings)
        checked.append(fixture.task_id)
        if workspace is not None:
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)
    return LeakageReport(findings, checked_tasks=checked)
