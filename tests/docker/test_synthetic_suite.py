"""Phase 10 E-01/E-02：真实 Docker 下的合成 suite 验收。

这里的每条断言都必须由容器里的 pytest 输出喂。FakeSandbox 预置的 PASS 只能证明
链路跑通，不能证明 fixture 有效——所以本文件整个是 ``@pytest.mark.docker``：没有
daemon 时它 skip，而不是悄悄给出一份"看起来验证过了"的绿。

三段职责：

1. 抽样真实校验（5 个任务，覆盖多文件修复与 traceback 两个稀有形状）；
2. 真实负控制（坏 gold 必须拿到 GOLD_NOT_PASSING / BASE_NOT_FAILING，而不是
   VALID）——一个从不拒绝任何样本的校验器等于没有校验器；
3. 容器内不可达性证明：把推理 workspace 挂进容器，在里面找 gold 的新增行。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from codepilot.artifacts import ArtifactStore
from codepilot.eval.evaluation import GoldEvaluator, ValidationStatus, validate_fixture
from codepilot.eval.synthetic import (
    SyntheticFixture,
    SyntheticSuite,
    load_fixture,
    patch_added_lines,
    validate_suite,
)
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import Sandbox
from codepilot.sandbox.docker import DockerSandbox
from codepilot.schemas.enums import ExecutionStatus
from support.synth import demo_spec, write_fixture

pytestmark = pytest.mark.docker

SUITE_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "synthetic"

#: 抽样口径：5 个宿主包各一个，其中必须包含多文件修复（``cross_module_dependency``）
#: 与带 traceback 的任务——这两个形状各自只占 suite 的一小部分，随机抽样很容易漏掉。
SAMPLE_TASK_IDS = (
    "budget-export-adds-currency-column",
    "httpclient-zero-retries-makes-no-request",
    "inventory-describe-crashes-on-unknown-sku",
    "taskqueue-retry-makes-one-fewer-attempt",
    "textkit-table-keeps-its-own-split-rule",
)


@pytest.fixture(scope="session")
def suite() -> SyntheticSuite:
    if not SUITE_ROOT.is_dir():
        pytest.skip("benchmarks/synthetic 尚未构建")
    return validate_suite(SUITE_ROOT)


@pytest.fixture(scope="session")
def work_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("suite-docker")


@pytest.fixture(scope="session")
def evaluator(work_root: Path) -> GoldEvaluator:
    def factory(artifacts: ArtifactStore) -> Sandbox:
        return DockerSandbox(artifacts=artifacts, allowed_workspace_root=work_root)

    return GoldEvaluator(
        manager=WorkspaceManager(cache_root=work_root / "repo-cache"),
        sandbox_factory=factory,
        work_root=work_root,
    )


def _fixture(suite: SyntheticSuite, task_id: str) -> SyntheticFixture:
    for item in suite.fixtures:
        if item.task_id == task_id:
            return item
    pytest.fail(f"suite 里没有 {task_id}")
    raise AssertionError  # pragma: no cover


# ===================================================== 1. 抽样真实校验


@pytest.mark.parametrize("task_id", SAMPLE_TASK_IDS)
def test_sample_fixture_is_valid_by_real_execution(
    suite: SyntheticSuite, evaluator: GoldEvaluator, task_id: str
) -> None:
    """base 上目标测试真的失败、gold 之后真的通过、环境是真的 Docker。"""
    fixture = _fixture(suite, task_id)
    validation = validate_fixture(evaluator, fixture, run_id=f"pytest-{task_id}")
    assert validation.status is ValidationStatus.VALID, validation.reason
    assert validation.environment.startswith("docker"), (
        f"{task_id} 的结论不是真实容器给的：{validation.environment}"
    )
    assert validation.target_count > 0, task_id
    assert validation.base_target_passed == 0, "目标测试在没有修复的 base 上就通过了"
    assert validation.gold_target_passed == validation.target_count
    assert validation.baseline is not None
    for node in fixture.evaluation.tests.fail_to_pass:
        assert validation.baseline.outcomes[node] == "failed", node


def test_multi_file_sample_actually_touches_several_files(suite: SyntheticSuite) -> None:
    """``multi_file`` 必须是 metadata 与 gold 的一致陈述，而不是自贴的标签。"""
    for task_id in SAMPLE_TASK_IDS:
        fixture = _fixture(suite, task_id)
        if not fixture.metadata.multi_file:
            continue
        assert len(fixture.evaluation.gold_files) > 1, task_id
        return
    pytest.fail("抽样里没有多文件修复任务，多文件形状将不被真实执行覆盖")


# ===================================================== 2. 真实负控制


def test_gold_that_does_not_fix_is_not_valid(evaluator: GoldEvaluator, work_root: Path) -> None:
    """gold 修好了，但目标断言要的是另一个值 ⇒ 必须判 GOLD_NOT_PASSING。

    这条是整份校验逻辑的"检查器真的会说不"证明：如果 GOLD_NOT_PASSING 永远拿不到，
    那么 VALID 也无从可信。
    """
    spec = demo_spec(
        task_id="negative-gold-not-passing",
        pass_to_pass=(),
        hidden_test_files={
            "tests/test_hidden.py": (
                "from pkg.core import normalize\n\n\n"
                "def test_normalize_keeps_value() -> None:\n    assert normalize(3) == 4\n"
            )
        },
    )
    fixture = load_fixture(write_fixture(work_root, spec, name="gold-not-passing"))
    validation = validate_fixture(evaluator, fixture, run_id="pytest-negative-gold")
    assert validation.status is ValidationStatus.GOLD_NOT_PASSING, validation.reason
    assert not validation.usable
    assert not validation.status.is_environment_failure


def test_base_that_already_passes_is_not_valid(evaluator: GoldEvaluator, work_root: Path) -> None:
    """base 里缺陷本来就不存在 ⇒ 必须判 BASE_NOT_FAILING，绝不能算 VALID。

    这类 fixture 对 Resolved Rate 是纯粹的虚高来源：一个什么都不做的补丁也能"通过"。
    """
    from support.synth import FIXED, FIXED_WITH_SENTINEL

    spec = demo_spec(
        task_id="negative-base-not-failing",
        base_files={
            "pyproject.toml": "[project]\nname='pkg'\nversion='0.0.1'\n",
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": FIXED,
            "tests/test_public.py": (
                '"""Base already behaves; the visible test pins the correct contract."""\n'
                "from pkg.core import normalize\n\n\n"
                "def test_documented_base_behaviour() -> None:\n    assert normalize(3) == 3\n"
            ),
        },
        gold_files={"src/pkg/core.py": FIXED_WITH_SENTINEL},
        pass_to_pass=(),
        fail_to_pass=("tests/test_hidden.py::test_normalize_keeps_value",),
    )
    fixture = load_fixture(write_fixture(work_root, spec, name="base-not-failing"))
    validation = validate_fixture(evaluator, fixture, run_id="pytest-negative-base")
    assert validation.status is ValidationStatus.BASE_NOT_FAILING, validation.reason
    assert not validation.usable


def test_unusable_env_spec_reports_environment_failure_not_invalid(
    evaluator: GoldEvaluator, work_root: Path
) -> None:
    """镜像建不起来时必须是 ENV_ERROR/SANDBOX_ERROR，不能混进任务无效。

    混在一起的话，一次网络抖动就会把 fixture 从 suite 里"合法地"删掉。
    """
    spec = demo_spec(
        task_id="negative-env-error",
        env={
            "python_version": "3.12",
            "install": ["python -m pip install no-such-package-5f3a9c1e"],
        },
    )
    fixture = load_fixture(write_fixture(work_root, spec, name="env-error"))
    validation = validate_fixture(evaluator, fixture, run_id="pytest-negative-env")
    assert validation.status.is_environment_failure, validation.status
    assert not validation.usable
    assert validation.status is not ValidationStatus.INVALID_FIXTURE


# ===================================================== 3. 容器内不可达


_PROBE = '''"""在容器内部探测推理 workspace 是否能看到评测侧内容。

期望值以 sha256 形式硬编码在这里：直接写 gold 原文的话，本文件自己就成了被
扫到的那份泄漏。
"""

import hashlib
from pathlib import Path

DIGESTS = {digests!r}

ROOT = Path.cwd()


def test_no_eval_directory_in_the_inference_tree() -> None:
    hits = [str(p) for p in ROOT.rglob("*") if p.name == "eval"]
    assert hits == [], f"推理 workspace 里出现了评测目录: {{hits[:5]}}"


def test_gold_patch_is_not_an_openable_path() -> None:
    import pytest

    with pytest.raises((FileNotFoundError, NotADirectoryError, OSError)):
        Path("eval/gold.patch").read_text(encoding="utf-8")


def test_gold_added_lines_appear_nowhere_on_disk() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            if hashlib.sha256(line.strip().encode("utf-8")).hexdigest() in DIGESTS:
                offenders.append(f"{{path.relative_to(ROOT)}}::{{line.strip()[:60]}}")
    assert offenders == [], f"gold 的新增行在容器内可读: {{offenders[:5]}}"
'''


def test_gold_content_is_unreachable_inside_the_container(
    suite: SyntheticSuite, work_root: Path
) -> None:
    """挂载给容器的只有推理仓库，所以这三条断言只能由真实容器给出否定答案。

    host 侧的 ``check_tool_reachability`` 证明的是"目录里没有 eval/"；这条证明的是
    "跑测试的那个进程，从它的 cwd 出发，也读不到答案"。
    """
    fixture = _fixture(suite, "budget-member-discount-inverted-condition")
    repository = work_root / "probe-repo"
    if repository.exists():
        shutil.rmtree(repository)
    fixture.materialize(repository)

    manager = WorkspaceManager(cache_root=work_root / "probe-cache")
    run_dir = work_root / "probe-run"
    workspace = manager.create(
        source=str(repository),
        base_commit=fixture.metadata.base_commit,
        target=run_dir / "workspace",
    )
    try:
        digests = sorted(
            {
                hashlib.sha256(line.strip().encode("utf-8")).hexdigest()
                for line in patch_added_lines(fixture.evaluation.gold_patch)
                if line.strip()
            }
        )
        assert digests, "gold patch 必须有新增行，否则这条探测什么也没证明"
        (workspace.path / "tests" / "test_eval_visibility_probe.py").write_text(
            _PROBE.format(digests=set(digests)), encoding="utf-8", newline="\n"
        )
        artifacts = ArtifactStore(work_root, "probe-run")
        sandbox = DockerSandbox(artifacts=artifacts, allowed_workspace_root=work_root)
        environment = sandbox.build_env(
            repository=str(repository),
            base_commit=workspace.base_commit,
            workspace=workspace.path,
            env_spec=fixture.env_spec,
            allow_network=True,
        )
        assert environment.succeeded, environment.error
        result = sandbox.run_tests(
            workspace=workspace.path,
            environment=environment,
            env_spec=fixture.env_spec,
            tests=("tests/test_eval_visibility_probe.py",),
        )
    finally:
        manager.remove(workspace)

    assert result.status is ExecutionStatus.PASS, [
        f"{case.node_id}: {case.message or ''}" for case in result.tests
    ]
    assert result.tests_passed == 3, "三条容器内探测必须都真的执行过"
