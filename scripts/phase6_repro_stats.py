"""Phase 6 D-08：synthetic 复现成功率离线评测。

**这个脚本会真实执行生成的复现测试。** 生成出来的测试是不可信代码，因此一律在
Docker 沙箱里运行，绝不在宿主机 import。需要可用的 Docker daemon；daemon 不可用时
脚本直接报告 NOT RUN 并以非零码退出，不会用 FakeSandbox 的结果冒充真实执行。

判定口径（docs/04 Phase 6 验收）：

    复现成功 = 生成的复现测试在 base commit 上 **有效 FAIL**
               且同一个测试在应用 Gold Patch 后 **PASS**

信息隔离（03 §11.2 的硬规则）：

* 推理侧只拿到 ``RetrievalCase.inference(dataset_root)`` 投影出的
  ``RetrievalInput``（task_id / repo / base_commit / issue），加上 base 快照里
  的源码与已有测试；
* gold patch 路径与内容只出现在 ``GoldReproEvaluator`` 里，它在推理完成之后才
  被调用；
* ``--verify-isolation`` 会独立复算这两条边界并把结果写进报告。

用法::

    uv run python scripts/phase6_repro_stats.py                    # 真实 Docker
    uv run python scripts/phase6_repro_stats.py --limit 4
    uv run python scripts/phase6_repro_stats.py --verify-isolation  # 只复算隔离边界，不执行
    uv run python scripts/phase6_repro_stats.py --out artifacts/phase6

输出：``artifacts/phase6/{results.jsonl,metrics.json,summary.md}``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from codepilot.agents.issue_analyzer import IssueAnalyzer  # noqa: E402
from codepilot.agents.reproducer import (  # noqa: E402
    BaseWorkspaceValidator,
    DirectoryBaseProvider,
    ReproductionAgent,
    ReproductionOutcome,
)
from codepilot.artifacts import ArtifactStore  # noqa: E402
from codepilot.llm.fake import FakeLLM  # noqa: E402
from codepilot.repo.parser import default_parser  # noqa: E402
from codepilot.repo.symbols import extract_symbols  # noqa: E402
from codepilot.retrieval.dataset import RetrievalCase, contained, load_dataset  # noqa: E402
from codepilot.retrieval.service import retrieve_symbols  # noqa: E402
from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox  # noqa: E402
from codepilot.sandbox.env_spec import EnvSpec  # noqa: E402
from codepilot.schemas.enums import TestOutcome  # noqa: E402
from codepilot.schemas.localization import (  # noqa: E402
    FileDecision,
    LocalizationResult,
    SymbolDecision,
)
from codepilot.schemas.reproduction import (  # noqa: E402
    REPRO_TEST_DIRECTORY,
    ReproductionStatus,
    is_reproduction_test_path,
    reproduction_node_id,
)
from codepilot.schemas.retrieval import FusionMethod  # noqa: E402
from codepilot.tools.base import WorkspacePathPolicy  # noqa: E402

DATASET = REPO_ROOT / "tests" / "fixtures" / "retrieval_cases.jsonl"
DEFAULT_OUT = REPO_ROOT / "artifacts" / "phase6"

#: mini_auth 夹具是 src 布局的纯 Python 包：容器里只需要 pytest，PYTHONPATH 指到
#: /workspace/src 就能 import miniauth。base 与 gold 共用同一个镜像缓存键。
ENV_SPEC = EnvSpec(
    install=("python -m pip install pytest",),
    env={"PYTHONPATH": "/workspace/src"},
    timeout_seconds=180,
)

GOLD_MARKERS = ("retrieval_gold", "gold_patch", "fail_to_pass", "pass_to_pass", "reference.patch")


@dataclass(slots=True)
class TaskReproRecord:
    """单个 synthetic 任务的复现记录。每个布尔都对应一次真实执行。"""

    task_id: str
    status: str = "not_attempted"
    kind: str | None = None
    attempts: int = 0
    generated: bool = False
    base_valid_failure: bool = False
    gold_passed: bool | None = None
    repro_path: str | None = None
    node_ids: list[str] = field(default_factory=list)
    attempt_kinds: list[str] = field(default_factory=list)
    target_tests: list[str] = field(default_factory=list)
    detail: str = ""
    gold_detail: str = ""


def build_docker_sandbox(work_root: Path, artifacts_root: Path) -> Sandbox:
    from codepilot.sandbox.docker import DockerSandbox

    return DockerSandbox(
        artifacts=ArtifactStore(artifacts_root, "phase6-sandbox"),
        allowed_workspace_root=work_root,
    )


def build_environment(sandbox: Sandbox, work_root: Path) -> EnvironmentBuildResult:
    """base 与 gold 共用一份镜像：两者只有挂载的工作树不同。

    ``workspace`` 传一个空目录——镜像构建阶段只会把工作树拷进构建容器再 pip
    install，真实内容在运行期由 bind mount 提供，没必要把整个工作区（含 artifact）
    打包进那次构建。
    """
    build_context = work_root / "_envbuild"
    build_context.mkdir(parents=True, exist_ok=True)
    image = sandbox.build_env(
        repository="phase6-synthetic-mini_auth",
        base_commit="snapshot",
        workspace=build_context,
        env_spec=ENV_SPEC,
        allow_network=True,
    )
    if not image.succeeded:
        raise RuntimeError(f"环境镜像构建失败：{image.status.value} {image.error}")
    return image


@dataclass(frozen=True, slots=True)
class CandidateScript:
    """离线评测的受控生成脚本。

    这是 **FakeLLM 的脚本**，不是真实模型的输出：内容是按 Issue 描述与 base 源码
    （不含 gold patch）写下的候选复现测试。报告与 metrics 会把这一条写清楚，
    所以这里的数字只能说明"复现链路是否工作"，不能说明"模型会写复现测试"。
    """

    task_id: str
    filename: str
    content: str
    expected_failure: str

    def reply(self) -> dict[str, str]:
        return {
            "filename": self.filename,
            "content": self.content,
            "expected_failure": self.expected_failure,
        }


def is_reproduction_success(record: TaskReproRecord) -> bool:
    """docs/04 Phase 6 的验收口径：base 有效 FAIL **且** gold 上 PASS。

    两处判定都来自真实执行，所以成功永远是被**推导**出来的，而不是某个可以被
    单独写错的布尔位。
    """
    return record.base_valid_failure and record.gold_passed is True


class GoldReproEvaluator:
    """评测侧唯一能看到 gold patch 的接口。

    它在推理结束之后才被调用，输入是 base 目录 + gold patch 路径；产出的只是一个
    布尔判定。推理链路上的任何对象都不持有它的引用。
    """

    def __init__(self, *, validator_factory: Callable[[Path], BaseWorkspaceValidator]) -> None:
        self._validator_factory = validator_factory

    def passes_after_gold(
        self,
        *,
        base_dir: Path,
        gold_dir: Path,
        gold_patch: Path,
        expected_sha256: str,
        filename: str,
        content: str,
        test_names: Sequence[str],
    ) -> tuple[bool, str]:
        patch_bytes = gold_patch.read_bytes()
        digest = hashlib.sha256(patch_bytes).hexdigest()
        if digest != expected_sha256:
            return False, f"gold patch digest mismatch: {digest}"
        prepared = _prepare_gold_workspace(base_dir=base_dir, gold_dir=gold_dir)
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=prepared,
            input=patch_bytes,
            capture_output=True,
            check=False,
        )
        if applied.returncode != 0:
            return False, f"git apply failed: {applied.stderr.decode('utf-8', 'replace')[:400]}"

        node_ids = tuple(
            reproduction_node_id(f"{REPRO_TEST_DIRECTORY}/{filename}", name) for name in test_names
        )
        check = self._validator_factory(prepared).check(
            filename=filename,
            content=content,
            attempt=1,
            test_names=test_names,
        )
        execution = check.execution
        if execution is None:
            return False, f"gold run produced no execution: {check.detail}"
        ran = [case for case in execution.tests if case.node_id in node_ids]
        if len(ran) != len(node_ids):
            return False, f"gold run did not execute every node id: {check.detail}"
        if any(case.outcome is not TestOutcome.PASSED for case in ran):
            return False, f"gold run still failing: {check.detail}"
        return True, f"gold run passed {len(ran)} node ids"


def _prepare_gold_workspace(*, base_dir: Path, gold_dir: Path) -> Path:
    if gold_dir.exists():
        shutil.rmtree(gold_dir)
    shutil.copytree(base_dir, gold_dir)
    shutil.rmtree(gold_dir / REPRO_TEST_DIRECTORY, ignore_errors=True)
    return gold_dir


def build_inference_inputs(case: RetrievalCase, dataset_root: Path) -> tuple[Path, str, str]:
    """推理侧输入：只有 Issue 与 base 快照，字段集合由 dataset 层的投影保证。"""
    inference = case.inference(dataset_root)
    payload = inference.model_dump_json()
    for marker in GOLD_MARKERS:
        if marker in payload:
            raise AssertionError(f"推理侧输入泄漏了评测答案字段: {marker}")
    repo_dir = Path(inference.repo).resolve(strict=True)
    if contained(dataset_root, case.gold_patch).is_relative_to(repo_dir):
        raise AssertionError("gold patch 落在推理可见的仓库目录内")
    return repo_dir, inference.base_commit, inference.issue


def build_localization(
    repo_dir: Path, issue: str, *, task_id: str
) -> tuple[LocalizationResult, str]:
    """用真实 BM25 检索结果确定性构造 Top-1 定位（不调用真实模型）。

    候选来自 base 快照的索引与 Issue 文本；gold 标签完全不参与，因此这里得到的
    定位质量与真实 Localizer 不同，报告里也照实写成"离线受控定位"。
    """
    symbols = []
    for path in sorted(repo_dir.rglob("*.py")):
        relative = path.relative_to(repo_dir).as_posix()
        symbols.extend(
            extract_symbols(
                default_parser().parse(path.read_bytes()),
                path=relative,
                repository_id=f"phase6/{task_id}",
            )
        )
    retrieval = retrieve_symbols(
        repo_dir,
        symbols,
        issue,
        channel="bm25",
        fusion_method=FusionMethod.WEIGHTED,
        channel_weights={"bm25": 1.0},
        snapshot_id=f"phase6-offline-{task_id}",
    )
    # 3 个文件 / 5 个 symbol 是 LocalizationResult 契约自己的上限（schemas.max_length），
    # 取满上限而不是截到前两个：离线夹具的 Top-1 排序经常把真正该改的文件排到第三。
    files = list(retrieval.files)[:3]
    symbols_ranked = [item for item in retrieval.symbols if item.file in {f.path for f in files}][
        :5
    ]
    snapshot_id = retrieval.snapshot_id or f"phase6-offline-{task_id}"
    if not files:
        return (
            LocalizationResult(retrieval_snapshot_id=snapshot_id),
            snapshot_id,
        )
    return (
        LocalizationResult(
            files=files,
            symbols=symbols_ranked,
            file_decisions=[
                FileDecision(
                    candidate_id=f"bm25-file-{index:02d}",
                    path=item.path,
                    reason="BM25 Top 文件（离线受控定位，不调用真实模型）",
                )
                for index, item in enumerate(files, 1)
            ],
            symbol_decisions=[
                SymbolDecision(
                    symbol_id=item.symbol_id,
                    file=item.file,
                    start_line=int(item.start_line or 1),
                    end_line=int(item.end_line or item.start_line or 1),
                    file_line_count=_line_count(repo_dir / item.file),
                    reason="BM25 Top symbol（离线受控定位）",
                )
                for item in symbols_ranked
            ],
            retrieval_snapshot_id=snapshot_id,
        ),
        snapshot_id,
    )


def _line_count(path: Path) -> int:
    try:
        return max(1, len(path.read_text(encoding="utf-8", errors="replace").splitlines()))
    except OSError:
        return 1


def run_reproduction(
    *,
    case: RetrievalCase,
    dataset_root: Path,
    sandbox: Sandbox,
    environment: EnvironmentBuildResult,
    work_root: Path,
    candidate: CandidateScript,
) -> tuple[ReproductionOutcome, Path]:
    """推理侧：Issue → base 快照 → ReproductionAgent（FakeLLM 走正常 Provider 接口）。"""
    repo_dir, base_commit, issue = build_inference_inputs(case, dataset_root)
    base_dir = work_root / case.task_id / "base"
    if base_dir.exists():
        shutil.rmtree(base_dir)
    shutil.copytree(repo_dir, base_dir)

    artifacts = ArtifactStore(work_root / case.task_id / "artifacts", f"phase6-{case.task_id}")
    localization, _ = build_localization(repo_dir, issue, task_id=case.task_id)
    repo_files = [
        path.relative_to(repo_dir).as_posix() for path in repo_dir.rglob("*.py") if path.is_file()
    ]
    analysis = IssueAnalyzer().analyze(issue, repo_files=repo_files, repo_root=repo_dir)
    # default 而不是只给一条脚本：候选被拒绝时 Agent 会按预算重试，脚本化生成的
    # "重试"就是同一份候选再交一次——它不会变好，于是结局如实记为拒绝并耗尽预算。
    llm = FakeLLM([candidate.reply()], default=candidate.reply())
    validator = BaseWorkspaceValidator(
        sandbox=sandbox,
        env_spec=ENV_SPEC,
        provider=DirectoryBaseProvider(
            base_dir, repository=f"phase6/{case.task_id}", base_commit=base_commit
        ),
        environment=environment,
    )
    agent = ReproductionAgent(
        llm,
        policy=WorkspacePathPolicy(base_dir, repro_write=True),
        validator=validator,
        artifacts=artifacts,
    )
    outcome = agent.reproduce(
        issue_text=issue,
        analysis=analysis,
        localization=localization,
        fallback_tests=tuple(_existing_test_node_ids(base_dir)),
    )
    return outcome, base_dir


def _existing_test_node_ids(base_dir: Path) -> list[str]:
    """降级目标 = 仓库**已有**测试（base 快照里本来就在的那些）。"""
    return sorted(
        path.relative_to(base_dir).as_posix()
        for path in base_dir.rglob("test_*.py")
        if not is_reproduction_test_path(path.relative_to(base_dir).as_posix())
    )


def evaluate_case(
    *,
    case: RetrievalCase,
    dataset_root: Path,
    sandbox: Sandbox,
    environment: EnvironmentBuildResult,
    work_root: Path,
    candidate: CandidateScript,
    gold_evaluator: GoldReproEvaluator,
) -> TaskReproRecord:
    record = TaskReproRecord(task_id=case.task_id)
    outcome, base_dir = run_reproduction(
        case=case,
        dataset_root=dataset_root,
        sandbox=sandbox,
        environment=environment,
        work_root=work_root,
        candidate=candidate,
    )
    result = outcome.result
    record.status = result.status.value
    record.kind = None if result.kind is None else result.kind.value
    record.attempts = result.attempts
    record.generated = result.attempts > 0
    record.attempt_kinds = [item.kind.value for item in result.history]
    record.target_tests = list(outcome.target_tests)
    record.detail = result.fail_reason or ""
    if result.test_path is None:
        record.base_valid_failure = False
        record.gold_passed = None
        return record

    record.base_valid_failure = result.status is ReproductionStatus.REPRODUCED
    record.repro_path = result.test_path
    record.node_ids = list(outcome.target_tests)
    if not record.base_valid_failure:
        return record

    content = (base_dir / result.test_path).read_text(encoding="utf-8")
    test_names = [item.rsplit("::", 1)[-1] for item in outcome.target_tests]
    passed, gold_detail = gold_evaluator.passes_after_gold(
        base_dir=base_dir,
        gold_dir=work_root / case.task_id / "gold",
        gold_patch=contained(dataset_root, case.gold_patch),
        expected_sha256=case.patch_sha256,
        filename=Path(result.test_path).name,
        content=content,
        test_names=test_names,
    )
    record.gold_passed = passed
    record.gold_detail = gold_detail
    shutil.rmtree(base_dir / REPRO_TEST_DIRECTORY, ignore_errors=True)
    return record


def aggregate(records: Sequence[TaskReproRecord], *, simulated: bool) -> dict[str, object]:
    """每项指标都带明确分母；没有证据的比值记为 None，不记 0 也不记 1。"""
    total = len(records)
    generated = [item for item in records if item.generated]
    valid_base = [item for item in records if item.base_valid_failure]
    # "成功"按口径现场推导，而不是信任记录里可能被写错的布尔位。
    successes = [item for item in records if item.base_valid_failure and item.gold_passed is True]
    exhausted = [item for item in records if item.attempts >= 4]
    env_errors = [item for item in records if item.status == ReproductionStatus.ENV_ERROR.value]
    failed = [item for item in records if item.status == ReproductionStatus.FAILED.value]
    kind_distribution: dict[str, int] = {}
    for item in records:
        for kind in item.attempt_kinds:
            kind_distribution[kind] = kind_distribution.get(kind, 0) + 1
    attempts = [item.attempts for item in records]

    def rate(numerator: int, denominator: int) -> float | None:
        return None if denominator == 0 else round(numerator / denominator, 4)

    return {
        "denominators": {
            "total_tasks": total,
            "tasks_with_generated_candidate": len(generated),
            "tasks_with_valid_base_failure": len(valid_base),
            "valid_base_failures_evaluated_on_gold": len(
                [item for item in valid_base if item.gold_passed is not None]
            ),
        },
        "counts": {
            "total_tasks": total,
            "generated_candidates": len(generated),
            "valid_base_failures": len(valid_base),
            "gold_passes": len([item for item in records if item.gold_passed is True]),
            "reproduction_successes": len(successes),
            "retries_exhausted": len(exhausted),
            "degraded_to_existing_tests": len(
                [item for item in records if not is_reproduction_success(item)]
            ),
            "environment_errors": len(env_errors),
            "reproduction_failures": len(failed),
        },
        "rates": {
            "generation_rate": rate(len(generated), total),
            "base_valid_failure_rate": rate(len(valid_base), len(generated)),
            "gold_pass_rate": rate(
                len([item for item in records if item.gold_passed is True]), len(valid_base)
            ),
            "reproduction_success_rate": rate(len(successes), total),
            "reproduction_success_rate_over_generated": rate(len(successes), len(generated)),
        },
        "attempts": {
            "mean": round(sum(attempts) / total, 4) if total else None,
            "mean_over_successes": (
                round(sum(item.attempts for item in successes) / len(successes), 4)
                if successes
                else None
            ),
            "max": max(attempts) if attempts else 0,
            "sum": sum(attempts),
        },
        "failure_kind_distribution": dict(sorted(kind_distribution.items())),
        "conditions": {
            "generated_with": "FakeLLM scripted candidate (offline controlled generation)",
            "execution_backend": "fake" if simulated else "docker",
            "simulated_sandbox": simulated,
            "dataset": "tests/fixtures/retrieval_cases.jsonl (synthetic-development)",
            "not_a_model_capability_measurement": True,
        },
    }


def write_outputs(
    out_dir: Path, records: Sequence[TaskReproRecord], metrics: dict[str, object]
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    with results_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
    metrics_path = out_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    summary_path = out_dir / "summary.md"
    summary_path.write_text(render_summary(records, metrics), encoding="utf-8", newline="\n")
    return {"results": results_path, "metrics": metrics_path, "summary": summary_path}


def render_summary(records: Sequence[TaskReproRecord], metrics: dict[str, object]) -> str:
    denominators = metrics["denominators"]
    counts = metrics["counts"]
    rates = metrics["rates"]
    attempts = metrics["attempts"]
    conditions = metrics["conditions"]
    lines = [
        "# Phase 6 复现成功率（synthetic，离线评测）",
        "",
        f"- 数据集规模：**{denominators['total_tasks']}** 个 synthetic-development 任务"
        "（tests/fixtures/retrieval_cases.jsonl，mini_auth 夹具）",
        f"- 生成条件：**{conditions['generated_with']}**；执行后端："
        f"**{conditions['execution_backend']}**（simulated_sandbox="
        f"{conditions['simulated_sandbox']}）",
        "- 判定口径：复现成功 = 同一测试在 base 上有效 FAIL **且** 在应用 Gold Patch 后 PASS。",
        "- 这批数字衡量的是**复现链路的工程正确性**，不是任何真实模型的复现能力。",
        "",
        "## 指标（每项都带分母）",
        "",
        "| 指标 | 值 | 分子 | 分母 |",
        "|---|---|---|---|",
        f"| 生成了候选复现测试 | {counts['generated_candidates']} | — | {denominators['total_tasks']} |",
        f"| base 上有效失败 | {counts['valid_base_failures']} | {counts['valid_base_failures']} | "
        f"{denominators['tasks_with_generated_candidate']} |",
        f"| Gold Patch 后通过 | {counts['gold_passes']} | {counts['gold_passes']} | "
        f"{denominators['tasks_with_valid_base_failure']} |",
        f"| 复现成功（双条件） | {counts['reproduction_successes']} | {counts['reproduction_successes']} | "
        f"{denominators['total_tasks']} |",
        f"| 复现成功率 | {rates['reproduction_success_rate']} | {counts['reproduction_successes']} | "
        f"{denominators['total_tasks']} |",
        f"| 平均尝试次数 | {attempts['mean']} | attempts 合计 {attempts['sum']} | "
        f"{denominators['total_tasks']} |",
        f"| 重试耗尽 | {counts['retries_exhausted']} | {counts['retries_exhausted']} | "
        f"{denominators['total_tasks']} |",
        f"| 环境错误 | {counts['environment_errors']} | {counts['environment_errors']} | "
        f"{denominators['total_tasks']} |",
        f"| 降级为已有测试 | {counts['degraded_to_existing_tests']} | "
        f"{counts['degraded_to_existing_tests']} | {denominators['total_tasks']} |",
        "",
        "## 无效/失败结局分布（按尝试计）",
        "",
    ]
    distribution = metrics["failure_kind_distribution"]
    if distribution:
        lines.append("| kind | 次数 |")
        lines.append("|---|---|")
        for kind, count in distribution.items():
            lines.append(f"| `{kind}` | {count} |")
    else:
        lines.append("（没有产生任何尝试记录）")
    lines.extend(
        [
            "",
            "## 任务级明细",
            "",
            "| task | status | kind | attempts | base FAIL | gold PASS |",
            "|---|---|---|---|---|---|",
        ]
    )
    for record in records:
        lines.append(
            f"| `{record.task_id}` | {record.status} | {record.kind or '-'} | {record.attempts} | "
            f"{'YES' if record.base_valid_failure else 'no'} | "
            f"{'YES' if record.gold_passed else ('n/a' if record.gold_passed is None else 'no')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _docker_available() -> bool:
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=20,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--verify-isolation",
        action="store_true",
        help="只复算推理侧/评测侧的隔离边界，不执行任何测试",
    )
    args = parser.parse_args(argv)

    cases = load_dataset(args.dataset)
    dataset_root = args.dataset.parent
    if args.limit is not None:
        cases = cases[: args.limit]

    if args.verify_isolation:
        for case in cases:
            build_inference_inputs(case, dataset_root)
        print(
            f"isolation verified for {len(cases)} cases; gold patches stay outside inference input"
        )
        return 0

    if not _docker_available():
        print(
            "NOT RUN：Docker daemon 不可用。复现评测必须真实执行生成的测试，"
            "不会用 FakeSandbox 的结果冒充真实执行。",
            file=sys.stderr,
        )
        return 2

    work_root = args.work or args.out / "_work"
    work_root.mkdir(parents=True, exist_ok=True)
    (args.out / "_work").mkdir(parents=True, exist_ok=True)
    sandbox = build_docker_sandbox(work_root, args.out)
    environment = build_environment(sandbox, work_root)
    ArtifactStore(args.out, "phase6-env").write_json(
        "environment.json",
        {
            "status": environment.status.value,
            "image_ref": environment.image_ref,
            "cache_key": environment.cache_key,
            "from_cache": environment.from_cache,
        },
    )
    gold_evaluator = GoldReproEvaluator(
        validator_factory=lambda directory: BaseWorkspaceValidator(
            sandbox=sandbox,
            env_spec=ENV_SPEC,
            provider=DirectoryBaseProvider(
                directory, repository="phase6-gold", base_commit="snapshot"
            ),
            environment=environment,
        )
    )

    records: list[TaskReproRecord] = []
    started = time.time()
    for case in cases:
        candidate = CANDIDATES.get(case.task_id)
        if candidate is None:
            print(f"skip {case.task_id}: 没有受控候选脚本（不虚构生成结果）", file=sys.stderr)
            continue
        records.append(
            evaluate_case(
                case=case,
                dataset_root=dataset_root,
                sandbox=sandbox,
                environment=environment,
                work_root=work_root,
                candidate=candidate,
                gold_evaluator=gold_evaluator,
            )
        )
        last = records[-1]
        print(
            f"{last.task_id}: status={last.status} attempts={last.attempts} "
            f"base_fail={last.base_valid_failure} gold_pass={last.gold_passed}"
        )

    metrics = aggregate(records, simulated=False)
    metrics["conditions"]["wall_seconds"] = round(time.time() - started, 2)
    paths = write_outputs(args.out, records, metrics)
    print(json.dumps(metrics["rates"], ensure_ascii=False, sort_keys=True))
    print("wrote " + ", ".join(str(path) for path in paths.values()))
    return 0


CANDIDATES: dict[str, CandidateScript] = {}


def _register(script: CandidateScript) -> None:
    CANDIDATES[script.task_id] = script


# 以下候选全部按 Issue 描述与 base 源码写成；文件名与内容都属于 FakeLLM 脚本。
_register(
    CandidateScript(
        task_id="01-expiry-boundary",
        filename="test_repro_expiry_boundary.py",
        expected_failure="AssertionError：刚签发的令牌在过期整点被判为无效",
        content='"""Issue: freshly issued tokens stop working at the exact expiry second."""\n'
        "\n"
        "from miniauth import tokens\n"
        "\n"
        "\n"
        "class _Clock:\n"
        "    def __init__(self, now: float) -> None:\n"
        "        self.now = now\n"
        "\n"
        "    def time(self) -> float:\n"
        "        return self.now\n"
        "\n"
        "\n"
        "def test_token_stays_valid_at_the_exact_expiry_second(monkeypatch) -> None:\n"
        "    clock = _Clock(1_700_000_000.0)\n"
        "    monkeypatch.setattr(tokens, 'time', clock)\n"
        "    token = tokens.issue_token('demo', ttl=3600)\n"
        "    clock.now = 1_700_000_000.0 + 3600\n"
        "    assert tokens.verify_token(token) == 'demo'\n",
    )
)
_register(
    CandidateScript(
        task_id="02-malformed-expiry",
        filename="test_repro_malformed_expiry.py",
        expected_failure="ValueError：非数字过期时间让校验直接抛异常",
        content='"""Issue: a correctly signed token with a nonnumeric expiry raises ValueError."""\n'
        "\n"
        "from miniauth.tokens import _sign, verify_token\n"
        "\n"
        "\n"
        "def test_nonnumeric_expiry_is_rejected_instead_of_raising() -> None:\n"
        "    body = b'demo:not-an-int'\n"
        "    token = f'demo:not-an-int:{_sign(body)}'\n"
        "    assert verify_token(token) is None\n",
    )
)
_register(
    CandidateScript(
        task_id="03-zero-ttl",
        filename="test_repro_zero_ttl.py",
        expected_failure="AssertionError（DID NOT RAISE ValueError）：零有效期被接受",
        content='"""Issue: issue_token accepts a zero lifetime instead of rejecting it."""\n'
        "\n"
        "import pytest\n"
        "\n"
        "from miniauth.tokens import issue_token\n"
        "\n"
        "\n"
        "def test_zero_ttl_is_rejected() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        issue_token('demo', ttl=0)\n",
    )
)
_register(
    CandidateScript(
        task_id="04-missing-password",
        filename="test_repro_missing_password.py",
        expected_failure="AssertionError：未知账号传 None 密码时认证成功",
        content='"""Issue: check_password succeeds for an unknown account when password is None."""\n'
        "\n"
        "from miniauth.backends.memory import MemoryBackend\n"
        "\n"
        "\n"
        "def test_unknown_account_cannot_authenticate_with_none() -> None:\n"
        "    backend = MemoryBackend()\n"
        "    assert backend.check_password('ghost', None) is False\n",
    )
)
_register(
    CandidateScript(
        task_id="05-missing-account",
        filename="test_repro_missing_account.py",
        expected_failure="AssertionError（DID NOT RAISE KeyError）：缺失账号静默返回 None",
        content='"""Issue: MemoryBackend.load silently returns None for a missing account."""\n'
        "\n"
        "import pytest\n"
        "\n"
        "from miniauth.backends.memory import MemoryBackend\n"
        "\n"
        "\n"
        "def test_load_reports_a_missing_account() -> None:\n"
        "    backend = MemoryBackend()\n"
        "    with pytest.raises(KeyError):\n"
        "        backend.load('ghost')\n",
    )
)
_register(
    CandidateScript(
        task_id="06-empty-revocation",
        filename="test_repro_empty_revocation.py",
        expected_failure="AssertionError（DID NOT RAISE ValueError）：空令牌被记录为已吊销",
        content='"""Issue: MemoryBackend.revoke accepts an empty token."""\n'
        "\n"
        "import pytest\n"
        "\n"
        "from miniauth.backends.memory import MemoryBackend\n"
        "\n"
        "\n"
        "def test_empty_revocation_is_rejected() -> None:\n"
        "    backend = MemoryBackend()\n"
        "    with pytest.raises(ValueError):\n"
        "        backend.revoke('')\n",
    )
)
_register(
    CandidateScript(
        task_id="07-negative-count",
        filename="test_repro_negative_count.py",
        expected_failure="AssertionError（DID NOT RAISE ValueError）：负数计数返回 0",
        content='"""Issue: count_down returns zero for a negative input."""\n'
        "\n"
        "import pytest\n"
        "\n"
        "from miniauth.app import count_down\n"
        "\n"
        "\n"
        "def test_negative_count_is_rejected() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        count_down(-2)\n",
    )
)
_register(
    CandidateScript(
        task_id="08-role-case",
        filename="test_repro_role_case.py",
        expected_failure="AssertionError：ADMIN 角色拿不到 admin 权限",
        content='"""Issue: a user with role ADMIN is denied; comparison should ignore case."""\n'
        "\n"
        "from miniauth.models import User\n"
        "\n"
        "\n"
        "def test_role_comparison_ignores_case() -> None:\n"
        "    user = User(name='demo', roles=['ADMIN'])\n"
        "    assert user.has_role('admin') is True\n",
    )
)
_register(
    CandidateScript(
        task_id="09-missing-session",
        filename="test_repro_missing_session.py",
        expected_failure="AssertionError（DID NOT RAISE KeyError）：账号消失时返回 None",
        content='"""Issue: build_session returns None for an account that disappeared."""\n'
        "\n"
        "import pytest\n"
        "\n"
        "from miniauth.app import build_session\n"
        "from miniauth.backends.memory import MemoryBackend\n"
        "\n"
        "\n"
        "def test_missing_account_is_reported_explicitly() -> None:\n"
        "    with pytest.raises(KeyError):\n"
        "        build_session(MemoryBackend(), 'ghost')\n",
    )
)
_register(
    CandidateScript(
        task_id="10-empty-logout",
        filename="test_repro_empty_logout.py",
        expected_failure="AssertionError（DID NOT RAISE ValueError）：空白登出被接受",
        content='"""Issue: Session.logout accepts a blank token."""\n'
        "\n"
        "import pytest\n"
        "\n"
        "from miniauth.backends.memory import MemoryBackend\n"
        "from miniauth.models import User\n"
        "from miniauth.session import Session\n"
        "\n"
        "\n"
        "def test_blank_logout_is_rejected() -> None:\n"
        "    session = Session(MemoryBackend(), User(name='demo', roles=['admin']))\n"
        "    with pytest.raises(ValueError):\n"
        "        session.logout('')\n",
    )
)
_register(
    CandidateScript(
        task_id="11-default-key",
        filename="test_repro_default_signing_key.py",
        expected_failure="AssertionError：夹具仍在使用共享的默认签名密钥",
        content='"""Issue: tokens share the same default signing secret across installations."""\n'
        "\n"
        "from miniauth import tokens\n"
        "\n"
        "DEFAULT_SHARED_SECRET = b'not-a-real-secret'\n"
        "\n"
        "\n"
        "def test_fixture_does_not_use_the_shared_default_secret() -> None:\n"
        "    assert tokens.SECRET != DEFAULT_SHARED_SECRET\n",
    )
)
_register(
    CandidateScript(
        task_id="12-retry-zero",
        filename="test_repro_retry_zero.py",
        expected_failure="AssertionError（DID NOT RAISE ValueError）：retry(0) 被接受",
        content='"""Issue: retry(times=0) never calls the wrapped function."""\n'
        "\n"
        "import pytest\n"
        "\n"
        "from miniauth.plugins import retry\n"
        "\n"
        "\n"
        "def test_invalid_retry_count_is_rejected_when_creating_the_decorator() -> None:\n"
        "    with pytest.raises(ValueError):\n"
        "        retry(times=0)\n",
    )
)


if __name__ == "__main__":
    raise SystemExit(main())
