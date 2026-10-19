"""E-13：SWE-bench 专属隔离测试（官方数据 ↔ 推理侧的信息边界）。

与 ``test_isolation.py`` 的分工：那边验通用机制（Gold Sentinel、Import Graph、
运行时装配）；这里验 **SWE-bench 特有的四条边界**，并且用 Owner A 已经落地的
真实模型（``SWEBenchInferenceTask`` / ``SWEBenchEvaluationTask``），不用等价替身：

1. 官方 Test Patch / FAIL_TO_PASS / PASS_TO_PASS 没有字段能进入推理对象，
   也不能进入 prompt；
2. ``evaluation/`` 目录不进入 workspace，也不通过 symlink/junction 逃逸进去；
3. 推理仓库的 git 历史只有 base 及其祖先，base 之后的提交（即使引用已删、
   对象还在）不能用 ``git show`` / ``git log`` 读到；
4. 工具层拒绝父级 ``evaluation/`` 目录，同时 Base 源码 / Base 已有测试 / 检索
   仍然可用（不能靠"一律拒绝"通过）。

刻意不跑真实 Docker、不调真实 LLM、不联网；全部在 ``tmp_path`` 里构造。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.eval.swebench import SWEBenchEvaluationTask
from codepilot.eval.swebench_isolation import (
    GoldSentinel,
    IsolationViolation,
    assert_inference_import_closure,
    audit_git_history,
    audit_inference_request,
    audit_tool_registry,
    audit_workspace,
    build_import_graph,
    import_closure,
)
from codepilot.eval.swebench_task import (
    DATASET_REVISION,
    FORBIDDEN_INFERENCE_FIELDS,
    INFERENCE_IDENTITY_FIELDS,
    SWEBenchDatasetRef,
    SWEBenchInferenceTask,
)
from codepilot.eval.synthetic import INFERENCE_DENIED_DIRECTORIES
from codepilot.repo.gitcmd import run_git
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.read_tools import read_file_tool, search_code_tool
from codepilot.tools.registry import ToolRegistry
from support.gitfixture import init_repo

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "codepilot"

INSTANCE_ID = "django__django-11099"
REPO = "django/django"
BASE_COMMIT = "a" * 40
SETUP_COMMIT = "b" * 40
VERSION = "4.2"

GOLD_PATCH = (
    "diff --git a/django/db/models/query.py b/django/db/models/query.py\n"
    "--- a/django/db/models/query.py\n"
    "+++ b/django/db/models/query.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-        return self._result_cache[0]\n"
    "+        return self._result_cache[0] if self._result_cache else None\n"
)

TEST_PATCH = (
    "diff --git a/tests/test_hidden_query.py b/tests/test_hidden_query.py\n"
    "--- a/tests/test_hidden_query.py\n"
    "+++ b/tests/test_hidden_query.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+def test_first_when_empty():\n"
    "+    assert list(QuerySet([])) == []\n"
)

F2P_NODE = "tests/test_hidden_query.py::test_first_when_empty"
P2P_NODE = "tests/test_public_query.py::test_public_contract"

#: SWE-bench 推理闭包里**不得出现**的模块（Lead 明确给出的集合）。
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "codepilot.eval.swebench",
    "codepilot.eval.swebench_prediction",
    "codepilot.eval.swebench_harness",
    "codepilot.eval.swebench_isolation",
    "codepilot.eval.evaluation",
    "codepilot.sandbox.swebench_env",
)

#: 推理入口（与 test_isolation.py 同一口径，这里只保留与 SWE-bench 相关的核心）。
INFERENCE_ENTRIES: tuple[str, ...] = (
    "codepilot.eval.assembly",
    "codepilot.graph.builder",
    "codepilot.agents.planner",
    "codepilot.agents.coder",
    "codepilot.context.builder",
    "codepilot.tools.registry",
)

DATASET = SWEBenchDatasetRef(revision=DATASET_REVISION, split="test")
ENV_SPEC = EnvSpec(
    python_version="3.12",
    install=("python -m pip install -e .",),
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _inference_task() -> SWEBenchInferenceTask:
    return SWEBenchInferenceTask(
        instance_id=INSTANCE_ID,
        repo=REPO,
        base_commit=BASE_COMMIT,
        problem_statement=(
            "QuerySet.first() raises IndexError on an empty queryset. "
            "It should return None instead."
        ),
        version=VERSION,
        environment_setup_commit=SETUP_COMMIT,
        dataset=DATASET,
        env_spec=ENV_SPEC,
    )


def _evaluation_task() -> SWEBenchEvaluationTask:
    return SWEBenchEvaluationTask(
        instance_id=INSTANCE_ID,
        repo=REPO,
        base_commit=BASE_COMMIT,
        version=VERSION,
        environment_setup_commit=SETUP_COMMIT,
        dataset=DATASET,
        gold_patch=GOLD_PATCH,
        test_patch=TEST_PATCH,
        fail_to_pass=(F2P_NODE,),
        pass_to_pass=(P2P_NODE,),
        gold_patch_sha256=_sha(GOLD_PATCH),
        test_patch_sha256=_sha(TEST_PATCH),
    )


def _make_directory_link(link: Path, target: Path) -> None:
    """Windows 用 junction、其他平台用 symlink（两者都算重解析点）。"""
    if sys.platform == "win32":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"mklink /J 失败: {completed.stdout} {completed.stderr}")
        return
    link.symlink_to(target, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()


# ============================================ 1. 推理对象与 prompt 的信息边界


def test_inference_task_has_no_evaluation_fields() -> None:
    """真实 SWE-bench 推理对象的字段集合 = 信息边界。"""
    inference = _inference_task()
    fields = set(type(inference).model_fields)
    assert FORBIDDEN_INFERENCE_FIELDS & fields == frozenset(), (
        f"推理对象里出现了评测字段: {sorted(FORBIDDEN_INFERENCE_FIELDS & fields)}"
    )
    assert not hasattr(inference, "gold_patch")
    assert not hasattr(inference, "test_patch")
    assert not hasattr(inference, "fail_to_pass")

    evaluation = _evaluation_task()
    evaluation_fields = set(type(evaluation).model_fields)
    # 交集只能是身份字段：两边要能对齐"评的是哪个实例"，但不能交换答案。
    assert fields & evaluation_fields == set(INFERENCE_IDENTITY_FIELDS)
    assert evaluation_fields - fields == {
        "gold_patch",
        "test_patch",
        "fail_to_pass",
        "pass_to_pass",
        "gold_patch_sha256",
        "test_patch_sha256",
    }

    # ``extra="forbid"`` 让"顺手多传一个 gold_patch"在构造期失败，而不是进入 prompt。
    with pytest.raises(ValidationError):
        SWEBenchInferenceTask(
            **inference.model_dump(),
            gold_patch=GOLD_PATCH,  # type: ignore[call-arg]
        )


def test_gold_sentinel_only_planted_on_evaluation_task() -> None:
    sentinel = GoldSentinel.generate(run_id="swe-runtime", instance_id=INSTANCE_ID)
    evaluation = _evaluation_task()
    planted = sentinel.plant_in_evaluation_task(evaluation)
    assert isinstance(planted, SWEBenchEvaluationTask)
    assert sentinel.token in planted.model_dump_json()
    assert sentinel.token not in evaluation.model_dump_json(), "植入必须是副本"

    inference = _inference_task()
    with pytest.raises(IsolationViolation, match="sentinel_scope"):
        sentinel.plant_in_evaluation_task(inference)


def test_official_test_patch_and_f2p_never_enter_prompts() -> None:
    sentinel = GoldSentinel.generate()
    inference = _inference_task()

    # 推理侧合法 prompt：只有 issue 与身份字段。
    clean = f"{inference.issue}\nrepo={inference.repo}\nbase={inference.base_commit}"
    audit_inference_request(clean, where="swebench_prompt")
    # 官方 patch/清单以结构化标记出现时必须被抓。
    for leaked in (
        f"test_patch:\n{TEST_PATCH}",
        f"FAIL_TO_PASS = {F2P_NODE}",
        f"PASS_TO_PASS = {P2P_NODE}",
        f"gold_patch:\n{GOLD_PATCH}",
        f"hint: {sentinel.token}",
    ):
        with pytest.raises(IsolationViolation):
            audit_inference_request(leaked, where="swebench_prompt")

    # 推理对象的序列化里也找不到这些内容。
    payload = inference.model_dump_json()
    sentinel.assert_absent(payload, where="SWEBenchInferenceTask")
    for needle in ("gold_patch", "test_patch", "fail_to_pass", F2P_NODE):
        assert needle not in payload, f"推理对象序列化里出现评测内容: {needle}"


# ==================================================== 2. evaluation/ 不进 workspace


def test_evaluation_directory_never_enters_workspace(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    instance_root = tmp_path / INSTANCE_ID
    workspace = instance_root / "workspace"
    init_repo(
        workspace,
        files={
            "django/db/models/query.py": "def first(qs):\n    return qs[0]\n",
            "tests/test_public_query.py": "def test_public_contract():\n    assert True\n",
        },
    )
    evaluation = instance_root / "evaluation"
    evaluation.mkdir()
    sentinel.plant_in_file(evaluation / "gold.patch")
    sentinel.plant_in_file(evaluation / "test.patch")
    sentinel.plant_in_file(evaluation / "tests.json")

    # 正控制：workspace 干净，评测目录在它外面。
    audit_workspace(workspace, sentinel=sentinel)
    assert not (workspace / "eval").exists()
    assert not (workspace / "evaluation").exists()
    assert sentinel.token in (evaluation / "test.patch").read_text(encoding="utf-8"), (
        "评测目录必须真的含 sentinel，否则'workspace 里读不到'是空跑"
    )
    # Base 源码与 Base 已有测试都可读（契约允许）。
    assert (workspace / "django/db/models/query.py").is_file()
    assert (workspace / "tests/test_public_query.py").is_file()

    # 负控制：把评测目录复制进 workspace → 必须拒绝。
    leaky = tmp_path / "leaky-workspace"
    shutil.copytree(workspace, leaky)
    shutil.copytree(evaluation, leaky / "evaluation")
    with pytest.raises(IsolationViolation, match="evaluation_directory_in_workspace"):
        audit_workspace(leaky, sentinel=sentinel)


# ======================================================== 3. git 历史只有 base


def test_git_history_exposes_only_base_commit(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    workspace = tmp_path / "workspace"
    repo = init_repo(
        workspace,
        files={"django/db/models/query.py": "def first(qs):\n    return qs[0]\n"},
    )
    base = repo.commits[0]
    audit_git_history(workspace, base_commit=base)
    # Base 内容可读（隔离不是禁止读代码）。
    assert (
        "def first" in run_git(["show", f"{base}:django/db/models/query.py"], cwd=workspace).stdout
    )

    # 负控制：造一个 base 之后的"修复提交"，里面带 sentinel。
    (workspace / "django/db/models/query.py").write_text(
        f"def first(qs):\n    return qs[0] if qs else None  # {sentinel.token}\n",
        encoding="utf-8",
    )
    repo.git("add", "-A")
    repo.git("commit", "--quiet", "-m", "gold fix")
    future = run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    with pytest.raises(IsolationViolation, match=r"git_(ref_outside_base|future_commit_object)"):
        audit_git_history(workspace, base_commit=base)

    # 更强的形态：把分支退回去、删掉引用，只留下不可达对象。``git show`` 仍读得到，
    # 所以"引用没了"不等于"提交没了"——对象库全量扫描必须抓住它。
    run_git(["reset", "--hard", base], cwd=workspace)
    for ref in ("refs/heads/master", "refs/heads/main"):
        run_git(["update-ref", "-d", ref], cwd=workspace, check=False)
    assert run_git(["cat-file", "-t", future], cwd=workspace, check=False).returncode == 0, (
        "未来提交对象应当仍留在对象库里；否则这条负控制测不到'引用已删、对象还在'"
    )
    with pytest.raises(IsolationViolation, match=r"git_(future_commit_object|reflog_outside_base)"):
        audit_git_history(workspace, base_commit=base)


# =========================================== 4. symlink / junction 逃逸拒绝


def test_symlink_or_junction_escape_rejected(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "query.py").write_text(
        "def first(qs):\n    return qs[0]\n", encoding="utf-8"
    )
    audit_workspace(workspace, sentinel=sentinel)

    evaluation = tmp_path / "evaluation"
    evaluation.mkdir()
    sentinel.plant_in_file(evaluation / "test.patch")
    link = workspace / "linked-evaluation"
    _make_directory_link(link, evaluation)
    try:
        with pytest.raises(IsolationViolation, match="workspace_link"):
            audit_workspace(workspace, sentinel=sentinel)
    finally:
        _remove_directory_link(link)

    # 链接去掉之后又干净了：拒绝的是链接，不是"含有 src 目录的 workspace"。
    audit_workspace(workspace, sentinel=sentinel)


# ============================================ 5. 工具层拒绝父级 evaluation/


def test_tool_layer_rejects_parent_evaluation_directory(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    instance_root = tmp_path / INSTANCE_ID
    workspace = instance_root / "workspace"
    init_repo(
        workspace,
        files={
            "django/db/models/query.py": "def first(qs):\n    return qs[0]\n",
            "tests/test_public_query.py": "def test_public_contract():\n    assert True\n",
        },
    )
    evaluation = instance_root / "evaluation"
    evaluation.mkdir()
    sentinel.plant_in_file(evaluation / "test.patch")
    sentinel.plant_in_file(evaluation / "gold.patch")

    policy = WorkspacePathPolicy(
        workspace,
        deny_directories=WorkspacePathPolicy.DEFAULT_DENY_DIRECTORIES
        | INFERENCE_DENIED_DIRECTORIES,
    )
    registry = ToolRegistry([read_file_tool(policy), search_code_tool(policy)])

    # ---- 正向：Base 源码、Base 已有测试、检索都可用
    assert registry.execute("read_file", {"path": "django/db/models/query.py"}).ok
    assert registry.execute("read_file", {"path": "tests/test_public_query.py"}).ok
    found = registry.execute("search_code", {"pattern": "def first"})
    assert found.ok and found.data.get("matches"), "检索不得因为隔离而失效"

    # ---- 负向：父级 evaluation/ 目录、绝对路径、反斜杠形态都读不到
    for path in (
        "../evaluation/test.patch",
        "..\\evaluation\\test.patch",
        "../../evaluation/gold.patch",
        str(evaluation / "test.patch"),
        "evaluation/test.patch",
    ):
        response = registry.execute("read_file", {"path": path})
        assert not response.ok, (
            f"read_file 不应读到评测内容: {path} -> {response.model_dump_json()}"
        )
        sentinel.assert_absent(response.model_dump_json(), where=f"tool_response:{path}")

    # 全仓搜索 sentinel 也找不到（它只在 workspace 外的 evaluation/ 里）。
    searched = registry.execute("search_code", {"pattern": sentinel.token})
    assert searched.ok and not searched.data.get("matches")
    sentinel.assert_absent(searched.model_dump_json(), where="search_code")

    # 审计通过；再造一个"拒绝名单被清空、workspace 里恰好有 eval/ 目录"的坏策略，
    # 审计必须能证明它确实读得到——否则"审计通过"只说明检查器没在查。
    audit_tool_registry(registry, sentinel=sentinel)
    leaky_workspace = tmp_path / "leaky-workspace"
    shutil.copytree(workspace, leaky_workspace)
    (leaky_workspace / "eval").mkdir()
    (leaky_workspace / "eval" / "gold.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    leaky = ToolRegistry(
        [read_file_tool(WorkspacePathPolicy(leaky_workspace, deny_directories=()))]
    )
    with pytest.raises(IsolationViolation) as caught:
        audit_tool_registry(leaky, sentinel=sentinel)
    assert caught.value.kind in {"tool_reaches_evaluation", "gold_sentinel_in_inference_side"}


# ================================================== 6. FAIL_TO_PASS 只在评测侧


def test_fail_to_pass_tests_do_not_exist_in_reasoning_workspace(tmp_path: Path) -> None:
    sentinel = GoldSentinel.generate()
    workspace = tmp_path / "workspace"
    init_repo(
        workspace,
        files={
            "django/db/models/query.py": "def first(qs):\n    return qs[0]\n",
            "tests/test_public_query.py": "def test_public_contract():\n    assert True\n",
        },
    )
    planted = sentinel.plant_in_evaluation_task(_evaluation_task())
    # 隐藏测试文件只由 test.patch 引入，base 树里没有它。
    hidden_file = F2P_NODE.split("::", 1)[0]
    assert not (workspace / hidden_file).exists()
    policy = WorkspacePathPolicy(
        workspace,
        deny_directories=WorkspacePathPolicy.DEFAULT_DENY_DIRECTORIES
        | INFERENCE_DENIED_DIRECTORIES,
    )
    registry = ToolRegistry([read_file_tool(policy), search_code_tool(policy)])
    assert not registry.execute("read_file", {"path": hidden_file}).ok
    searched = registry.execute("search_code", {"pattern": "test_first_when_empty"})
    assert searched.ok and not searched.data.get("matches"), "隐藏目标测试不得出现在推理检索里"
    assert sentinel.token in planted.model_dump_json()


# ================================================== 7. Import Graph 方向性


def test_swebench_import_closure_excludes_evaluation_modules() -> None:
    graph = build_import_graph(PACKAGE_ROOT)
    for entry in INFERENCE_ENTRIES:
        assert entry in graph, f"推理入口不在包内: {entry}"

    assert_inference_import_closure(
        graph, forbidden_prefixes=FORBIDDEN_PREFIXES, entries=INFERENCE_ENTRIES
    )
    closure = import_closure(graph, INFERENCE_ENTRIES)
    violations = sorted(
        module
        for module in closure
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_PREFIXES)
    )
    assert violations == [], f"推理闭包里出现 SWE-bench 评测模块: {violations}"
    assert "codepilot.eval.swebench_task" not in closure, (
        "推理装配只依赖 InferenceTaskLike Protocol，不应 import 具体 SWE-bench 任务类型"
    )

    # 反向允许：评测侧可以 import 推理安全模块；推理安全模块不得反向 import 评测。
    assert "codepilot.eval.swebench_task" in import_closure(graph, ["codepilot.eval.swebench"])
    assert_inference_import_closure(
        graph,
        forbidden_prefixes=FORBIDDEN_PREFIXES,
        entries=("codepilot.eval.swebench_task",),
    )

    # 注入违规边 → 同一个断言必须失败（证明检查器不是空转）。
    injected = {name: set(deps) for name, deps in graph.items()}
    injected["codepilot.eval.assembly"].add("codepilot.eval.swebench")
    with pytest.raises(IsolationViolation, match=r"codepilot\.eval\.swebench"):
        assert_inference_import_closure(
            injected, forbidden_prefixes=FORBIDDEN_PREFIXES, entries=INFERENCE_ENTRIES
        )


def test_evaluation_module_uses_real_swebench_task_types() -> None:
    """评测侧对象必须由推理安全模块的类型组成（两模块只在一个方向上依赖）。"""
    evaluation = _evaluation_task()
    assert isinstance(evaluation.dataset, SWEBenchDatasetRef)
    # 评测侧的 fail_to_pass 是 tuple，推理侧连字段都没有；这里断言的是真实类型，
    # 不是"我们承诺过不泄漏"。
    assert evaluation.fail_to_pass == (F2P_NODE,)
    with pytest.raises(ValidationError):
        SWEBenchInferenceTask(**{**_inference_task().model_dump(), "fail_to_pass": (F2P_NODE,)})
