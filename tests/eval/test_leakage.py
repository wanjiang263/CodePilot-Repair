"""E-07：泄漏检查的正负控制。

一个从未报出问题的泄漏检查器和一个没有检查器等价。所以每个"合法 fixture 干净"的
断言都必须配一个"注入泄漏后必须失败"的断言，而且注入要覆盖检查器声称能查的每一类。

这里刻意用**真实 suite** 做负控制，因为"我自己写的检查器对我自己写的数据集全绿"
正是最需要被独立证伪的命题。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codepilot.eval.leakage import (
    LeakageSeverity,
    audit_fixture,
    audit_suite,
    check_answer_vocabulary,
    check_artifact_contamination,
    check_inference_projection,
    check_issue_text,
    check_metadata,
    check_symlink_escape,
    check_tool_reachability,
    check_workspace_contents,
    normalize_code_line,
)
from codepilot.eval.synthetic import INFERENCE_DENIED_DIRECTORIES, load_fixture, validate_suite
from codepilot.eval.synthetic_build import build_fixture
from support.synth import GOLD_SENTINEL_LINE, build_and_load, demo_spec, gold_sentinel_spec

SUITE_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "synthetic"
EVALUATION_DIR = "eval"


def gold_diff(*added: str) -> str:
    """一段格式合法的 unified diff。

    ``@@`` 头必须带行数。写成裸 ``@@`` 就不是合法补丁，``patch_added_lines`` 读不出
    任何新增行，"正控制"于是静默退化成空跑 —— 第一版就是这么错的，测试全绿而什么
    都没检查。
    """
    body = "".join(f"+{line}\n" for line in added)
    return (
        "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
        "--- /dev/null\n"
        "+++ b/src/pkg/core.py\n"
        f"@@ -0,0 +1,{len(added)} @@\n" + body
    )


def _fixture(tmp_path: Path):  # type: ignore[no-untyped-def]
    return build_and_load(tmp_path, demo_spec())


def _rewrite_issue(directory: Path, text: str) -> None:
    (directory / "issue.md").write_text(text, encoding="utf-8", newline="\n")


# ============================================================= 正控制：必须抓到


def test_positive_verbatim_gold_line(tmp_path: Path) -> None:
    """Issue 逐字包含 gold 新增行。"""
    fixture = build_and_load(tmp_path, gold_sentinel_spec())
    issue = f"Just use:\n\n{GOLD_SENTINEL_LINE.strip()}\n"
    findings = check_issue_text("t", issue, fixture.evaluation.gold_patch)
    assert findings, "逐字出现的 gold 新增行必须被判为泄漏"
    assert all(item.severity is LeakageSeverity.ERROR for item in findings)
    assert "gold" in findings[0].check


def test_positive_whitespace_rewrapped_line_is_still_caught(tmp_path: Path) -> None:
    """折行 / 多空格 / 反引号包裹不能绕过 —— 只比字面量的检查器一折行就失效。"""
    fixture = build_and_load(tmp_path, gold_sentinel_spec())
    marker = GOLD_SENTINEL_LINE.split("# ", 1)[1]
    rewrapped = f"`return   value    # {marker}`"
    assert rewrapped != GOLD_SENTINEL_LINE.strip(), "变体必须与原行字面不同，否则什么都没测"
    findings = check_issue_text("t", f"Write {rewrapped} instead.", fixture.evaluation.gold_patch)
    assert findings, "折叠空白之后与 gold 新增行一致的内容，对模型仍然是同一个答案"


def test_positive_hidden_test_expectation(tmp_path: Path) -> None:
    """隐藏测试的期望值写进 Issue，比泄漏实现片段更糟：那就是判分标准。"""
    fixture = _fixture(tmp_path)
    leaked = "The suite then checks `assert normalize(3) == 3` and passes."
    findings = check_issue_text(
        "t", leaked, fixture.evaluation.gold_patch, test_patch=fixture.evaluation.test_patch
    )
    assert findings, "隐藏测试的断言原文必须被判为泄漏"
    assert any("test" in item.check for item in findings)


def test_positive_plain_setup_line_is_not_an_expectation(tmp_path: Path) -> None:
    """隐藏测试里"构造输入"的行不算答案，把它算进去会误杀诚实的 Issue。"""
    fixture = build_and_load(tmp_path, demo_spec())
    setup_only = (
        "diff --git a/tests/t.py b/tests/t.py\n--- /dev/null\n+++ b/tests/t.py\n"
        "@@ -0,0 +1,1 @@\n+RetryPolicy(attempts=0)\n"
    )
    findings = check_issue_text(
        "t",
        "`RetryPolicy(attempts=0)` is accepted today.",
        fixture.evaluation.gold_patch,
        test_patch=setup_only,
    )
    assert findings == [], "描述症状的调用写法是合法 Issue，不是判分标准"


def test_positive_metadata_names_modified_file(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    metadata = fixture.metadata.model_dump(mode="json")
    metadata["difficulty"] = "fix src/pkg/core.py"
    findings = check_metadata("t", metadata, fixture.evaluation.gold_files)
    assert any(item.check == "metadata_gold_path_value" for item in findings)


def test_positive_metadata_forbidden_key(tmp_path: Path) -> None:
    metadata = dict(_fixture(tmp_path).metadata.model_dump(mode="json"))
    metadata["hint"] = "drop the minus one"
    findings = check_metadata("t", metadata, ("src/pkg/core.py",))
    assert any(item.check == "metadata_gold_key" for item in findings)


def test_positive_workspace_with_eval_directory(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    workspace = fixture.materialize(tmp_path / "ws")
    shutil.copytree(fixture.directory / EVALUATION_DIR, workspace / EVALUATION_DIR)
    checks = {item.check for item in check_workspace_contents(fixture.task_id, workspace)}
    assert "eval_directory_in_workspace" in checks
    assert "eval_file_in_workspace" in checks


def test_positive_tool_reachability_when_denylist_omits_eval(tmp_path: Path) -> None:
    """拒绝名单里没有 ``eval`` 时必须报出来。

    检查器如果自带一份"总是挡住"的名单，它就永远返回空 —— 那是自我循环论证，名单
    被改掉也不会有任何反应。所以名单作为参数传入，让它检查 **runner 真正使用的那份
    配置**。
    """
    fixture = _fixture(tmp_path)
    workspace = fixture.materialize(tmp_path / "ws")
    assert EVALUATION_DIR in INFERENCE_DENIED_DIRECTORIES
    assert check_tool_reachability(fixture.task_id, workspace, fixture) == []
    findings = check_tool_reachability(
        fixture.task_id, workspace, fixture, deny_directories=frozenset()
    )
    assert findings, "拒绝名单去掉 eval 之后，可达性检查必须响"
    assert any(item.check == "eval_not_in_denied_directories" for item in findings)


def test_positive_symlink_escape(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    workspace = fixture.materialize(tmp_path / "ws")
    try:
        (workspace / "sneaky").symlink_to(fixture.directory / EVALUATION_DIR)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    findings = check_symlink_escape(fixture.task_id, fixture.directory, workspace)
    assert any(item.check == "symlink_inside_inference_tree" for item in findings)


def test_positive_artifact_contamination(tmp_path: Path) -> None:
    """评测答案被写进 run 目录、又被后续轮次读回去 —— 起点干净不等于全程干净。"""
    artifact = tmp_path / "report.json"
    artifact.write_text(
        json.dumps({"note": f"the fix is: {GOLD_SENTINEL_LINE.strip()}"}),
        encoding="utf-8",
        newline="\n",
    )
    findings = check_artifact_contamination("t", [artifact], gold_diff(GOLD_SENTINEL_LINE))
    assert findings, "artifact 里含 gold 新增行时必须报出来"
    assert findings[0].check == "artifact_contains_gold_content"


def test_positive_artifact_contamination_checks_every_line(tmp_path: Path) -> None:
    """扫描必须覆盖全部新增行：只看第一行的话，第二行起的答案一概查不到。"""
    artifact = tmp_path / "report.json"
    artifact.write_text(
        json.dumps({"cached": GOLD_SENTINEL_LINE.strip()}), encoding="utf-8", newline="\n"
    )
    patch = gold_diff("CACHED_TABLE: dict[str, int] = {}", GOLD_SENTINEL_LINE)
    assert "CACHED_TABLE" not in artifact.read_text(encoding="utf-8"), "命中必须来自第二行"
    assert check_artifact_contamination("t", [artifact], patch), (
        "gold 第二行同样不得留在 artifact 里"
    )


def test_positive_inference_view_carries_gold_line(tmp_path: Path) -> None:
    """把 gold 行塞进 Issue 后，推理投影检查也必须独立报出来。"""
    directory = build_fixture(gold_sentinel_spec(), tmp_path / "suite", scratch_root=tmp_path / "s")
    _rewrite_issue(directory, f"Use `{GOLD_SENTINEL_LINE.strip()}` here.\n")
    broken = load_fixture(directory)
    findings = check_inference_projection(broken.task_id, broken)
    assert findings, "推理视图携带 gold 内容，投影检查必须独立报出"
    assert all(item.severity is LeakageSeverity.ERROR for item in findings)


def test_positive_inference_view_carries_test_node_id(tmp_path: Path) -> None:
    """目标测试的节点 id 本身就是评测信息，出现在推理侧就是泄漏。"""
    fixture = _fixture(tmp_path)
    node = fixture.evaluation.tests.fail_to_pass[0]
    _rewrite_issue(fixture.directory, f"Runs {node} and it should pass.\n")
    broken = load_fixture(fixture.directory)
    findings = check_inference_projection(broken.task_id, broken)
    assert any(item.check == "inference_view_carries_test_node_id" for item in findings)


def test_positive_answer_only_symbol_name() -> None:
    """Issue 出现只在 gold 里存在、base 里完全没有的**符号名**。"""
    base = {"src/pkg/core.py": "def normalize(value: int) -> int:\n    return value - 1\n"}
    findings = check_answer_vocabulary(
        "t",
        "The helper normalize_cached should be used.",
        base_files=base,
        gold_patch=gold_diff("    return normalize_cached(value)"),
    )
    assert findings and findings[0].check == "answer_only_identifier_in_issue"


def test_positive_audit_fixture_end_to_end(tmp_path: Path) -> None:
    directory = build_fixture(gold_sentinel_spec(), tmp_path / "suite", scratch_root=tmp_path / "s")
    _rewrite_issue(directory, f"Just write {GOLD_SENTINEL_LINE.strip()}\n")
    fixture = load_fixture(directory)
    workspace = fixture.materialize(tmp_path / "ws")
    report = audit_fixture(fixture, workspace=workspace)
    assert not report.clean
    assert len(report.errors) >= 1


# ============================================================= 负控制：不许误报


def test_negative_clean_fixture_has_no_errors(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    workspace = fixture.materialize(tmp_path / "ws")
    report = audit_fixture(fixture, workspace=workspace)
    assert report.errors == [], [str(item) for item in report.errors]
    assert report.warnings == [], "合法 fixture 不应触发任何人工复核线索"


def test_negative_tool_cannot_reach_eval_in_real_workspace(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    workspace = fixture.materialize(tmp_path / "ws")
    assert check_tool_reachability(fixture.task_id, workspace, fixture) == []
    assert check_workspace_contents(fixture.task_id, workspace) == []
    assert check_symlink_escape(fixture.task_id, fixture.directory, workspace) == []


def test_negative_short_common_line_is_not_a_leak() -> None:
    """``return`` / ``x = 1`` 这类短行到处都是，拿它报泄漏会把合法 Issue 全标红。"""
    assert (
        check_issue_text("t", "It should return unchanged.", gold_diff("    return", "x = 1")) == []
    )


def test_negative_domain_words_are_not_answer_symbols() -> None:
    """Issue 里的领域词汇（Rent）与 gold 字符串字面量重合是正常现象。

    把这类命中报成泄漏，误报率会让所有人忽略这个检查器 —— 那比没有检查更糟。
    """
    base = {"src/budget/rules.py": 'RECURRING_KEYWORDS = ("rent", "subscription")\n'}
    findings = check_answer_vocabulary(
        "t",
        "A note like Rent April is recurring.",
        base_files=base,
        gold_patch=gold_diff("    return 'rent' in lowered"),
    )
    assert findings == []


def test_negative_repo_files_manifest_is_not_a_leak(tmp_path: Path) -> None:
    """metadata 里的 base 文件清单本来就对模型可见，不该被当成"指明被改文件"。"""
    fixture = _fixture(tmp_path)
    metadata = fixture.metadata.model_dump(mode="json")
    assert check_metadata("t", metadata, fixture.evaluation.gold_files) == []


# ============================================================= 真实 suite


@pytest.mark.parametrize("materialize", [False, True], ids=["structure-only", "with-workspace"])
def test_real_suite_has_no_structural_leakage(tmp_path: Path, materialize: bool) -> None:
    """对整个 suite 跑审计。

    ``with-workspace`` 那一路真的材料化每一个推理仓库，逐个查 eval/ 可达性与 symlink
    ——"目录里没有 eval/"对每个 fixture 都要成立一次，而不是只在玩具目录里成立一次。
    """
    if not SUITE_ROOT.is_dir():
        pytest.skip("benchmarks/synthetic 尚未构建")
    suite = validate_suite(SUITE_ROOT)
    assert suite.fixtures and not suite.invalid
    work_root = tmp_path / "ws" if materialize else None
    report = audit_suite(suite.fixtures, materialize=materialize, work_root=work_root)
    assert report.errors == [], [str(item) for item in report.errors][:8]


def test_real_suite_issues_do_not_quote_hidden_expectations() -> None:
    """专门盯一条最容易发生、后果最重的泄漏：Issue 里贴了隐藏测试的断言。"""
    if not SUITE_ROOT.is_dir():
        pytest.skip("benchmarks/synthetic 尚未构建")
    offenders: list[str] = []
    for fixture in validate_suite(SUITE_ROOT).fixtures:
        findings = check_issue_text(
            fixture.task_id,
            fixture.issue,
            fixture.evaluation.gold_patch,
            test_patch=fixture.evaluation.test_patch,
        )
        offenders.extend(f"{item.task_id}: {item.detail}" for item in findings)
    assert offenders == [], "隐藏测试的判分标准出现在了推理可见的 Issue 里\n" + "\n".join(offenders)


def test_traceback_blocks_never_quote_hidden_tests() -> None:
    """带 traceback 的 Issue 只能引用 base 源码，不能出现 ``tests/`` 路径。

    第一版把 pytest 的失败输出整段粘进 Issue，于是隐藏测试的文件名、行号和断言原文
    一起进了推理输入。这条测试就是为了钉死那次事故不再回来。
    """
    if not SUITE_ROOT.is_dir():
        pytest.skip("benchmarks/synthetic 尚未构建")
    with_traceback = [
        item for item in validate_suite(SUITE_ROOT).fixtures if item.metadata.has_traceback
    ]
    assert len(with_traceback) >= 5, "suite 应当有足够的 traceback 多样性"
    for fixture in with_traceback:
        assert "tests/" not in fixture.issue, fixture.task_id
        assert not any(
            line.strip().startswith(("assert ", "E       assert"))
            for line in fixture.issue.splitlines()
        ), f"{fixture.task_id} 的 Issue 含有断言差异输出，那就是判分标准"


def test_normalize_code_line_collapses_wrapping() -> None:
    assert normalize_code_line("  return    value  ") == "return value"
    assert normalize_code_line("`return value`") == "return value"
    assert normalize_code_line("return value;") == "return value"
