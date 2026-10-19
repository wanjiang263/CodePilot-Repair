"""E-19：SWE-bench 三层断点恢复、运行身份、预算与官方 Harness 恢复。

覆盖任务书 §13.7 的全部条目，并把最容易"看起来通过但其实错了"的几条做成可执行断言：

* **身份绑定**：身份里换任何一项都不能命中旧 journal；只有补丁哈希变化时
  ``journal_key`` 必须保持不变（否则"同一任务重跑出同一补丁"会被迫重跑）。
* **三层恢复**：任务级跳过、图级 checkpoint 续跑、评测级复用推理证据与完整报告；
  每层都验证"没有重跑已经付过钱的工作"。
* **现场核对**：存在 / base 一致 / 补丁一致 / 索引可用 / 产物可读 / checkpoint schema /
  配置一致，逐项构造失败现场并断言拒绝理由里有证据。
* **官方报告不冒充新结果**：报告哈希被改动、补丁不同、run_id 不同、报告缺失时都不得
  复用；缺失报告的可判定结论会被降级成官方评测错误，而不是 resolved。
* **预算/费用**：恢复不重复扣费；未知费用保持未知；单任务预算耗尽不影响其他任务；
  整批预算耗尽才停止派发。
* **两个分母**：端到端口径 vs 实际完成官方评测口径同时出现，比例带分子分母。

另有一个**独立进程**恢复测试（``subprocess`` + ``os._exit``）：同进程内重复调用
证明不了"内存里的计数器归零之后还能不能续跑"，而那份现场是跨进程才能验的。
本文件同时充当 worker 入口（``python test_swebench_runner.py <mode> <root>``），
与 ``tests/eval/_bench_checkpoint_worker.py`` 的思路一致，但不修改该文件。

所有测试离线：不联网、不调真实 LLM、不构建 Docker 镜像、不 import swebench。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from codepilot.eval.assembly import InferenceOutcome  # noqa: E402
from codepilot.eval.runner import JournalCorruptionError  # noqa: E402
from codepilot.eval.swebench_harness import (  # noqa: E402
    HarnessCacheError,
    HarnessCacheIdentity,
    HarnessConfig,
    HarnessErrorKind,
    HarnessPlatformError,
    OfficialHarnessRunner,
    derive_run_id,
    patch_set_digest,
)
from codepilot.eval.swebench_runner import (  # noqa: E402
    DATASET_NAME,
    DATASET_REVISION,
    HARNESS_VERSION,
    LEGACY_ATTRIBUTION,
    OfficialVerdict,
    ResumeLevel,
    RunIdentity,
    SWEBenchAttribution,
    SWEBenchEvaluation,
    SWEBenchRunConfig,
    SWEBenchRunner,
    audit_official_evidence,
    audit_workspace,
    classify_outcome,
    harness_run_id,
    recover_inference_evidence,
    subset_hash,
)
from codepilot.graph.checkpoint import CHECKPOINT_SCHEMA_VERSION  # noqa: E402
from codepilot.schemas.usage import Usage  # noqa: E402
from support.gitfixture import init_repo  # noqa: E402

# --------------------------------------------------------------------------- 夹具常量

INSTANCE_A = "alpha__repo-1"
INSTANCE_B = "beta__repo-2"
INSTANCE_C = "gamma__repo-3"

#: 与 ``init_repo`` 固定时间戳配合，同一内容得到同一 base commit，因此 workspace 可以
#: 直接拷贝 source 仓库而 HEAD 仍然等于任务声明的 base。
SOURCE_FILES = {"calc.py": "def add(a, b):\n    return a - b\n"}
BASE_FILE = SOURCE_FILES["calc.py"]
PATCHED_FILE = "def add(a, b):\n    return a + b\n"
UNRELATED_FILE = "def add(a, b):\n    return a * b\n"

KNOWN_USAGE = Usage(input_tokens=100, output_tokens=20, cost=0.5, llm_calls=3, currency="CNY")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _force_rmtree(path: Path) -> None:
    """删除含 git 对象的目录。Windows 上对象文件常带只读位，直接 rmtree 会 EACCES。"""

    def _onexc(func: Callable[..., Any], target: str, _exc: BaseException) -> None:
        Path(target).chmod(0o700)
        func(target)

    shutil.rmtree(path, onexc=_onexc)


def _write_sqlite(path: Path, table: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()


def _journal_rows(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM results").fetchone()[0])
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class FakeDataset:
    name: str
    revision: str
    harness_version: str
    split: str


@dataclass(frozen=True, slots=True)
class FakeTask:
    instance_id: str
    base_commit: str
    repo: str = "owner/repo"
    dataset: FakeDataset | None = None


class FakeLoader:
    def __init__(self, base_commit: str, *, ids: tuple[str, ...] = (INSTANCE_A,)) -> None:
        self.base_commit = base_commit
        self.ids = ids
        self.inference_calls: list[str] = []
        self.evaluation_calls: list[str] = []

    def get_inference_task(self, split: str, instance_id: str) -> FakeTask:
        if instance_id not in self.ids:
            raise KeyError(instance_id)
        self.inference_calls.append(instance_id)
        return FakeTask(instance_id=instance_id, base_commit=self.base_commit)

    def get_evaluation_task(self, split: str, instance_id: str) -> dict[str, str]:
        self.evaluation_calls.append(instance_id)
        # 故意带上 gold 字段：它只能交给评测侧，绝不能进推理/记录。
        return {"instance_id": instance_id, "gold_patch": "GOLD-SHOULD-NOT-LEAK"}


@dataclass(frozen=True, slots=True)
class RunCall:
    task: FakeTask
    seed: int
    run_id: str
    resume: ResumeLevel


Handler = Callable[["FakeFactory", RunCall], InferenceOutcome]
EvalHandler = Callable[["FakeEvaluator", "EvalCall"], SWEBenchEvaluation]


def _materialize(factory: FakeFactory, call: RunCall, patch_content: str) -> tuple[Path, str]:
    """在 ``artifact_root/runs/<run_id>/`` 建立与生产装配一致的现场。

    真实 ``InferenceAssembly`` 会做同样的事：workspace（含 .git）、events.jsonl、
    index.sqlite3、checkpoint。runner 的现场核对只认这些**文件事实**，不用替身
    伪造结论。
    """
    from codepilot.patch.diff import generate_git_diff

    run_dir = factory.root / "runs" / call.run_id
    workspace = run_dir / "workspace"
    if not workspace.is_dir():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(factory.source_repo, workspace)
        (run_dir / "events.jsonl").write_text('{"event": "started"}\n', encoding="utf-8")
        _write_sqlite(run_dir / "index.sqlite3", "symbols")
        (run_dir / "workspace_identity.json").write_text(
            json.dumps(
                {
                    "workspace": str(workspace),
                    "base_commit": factory.base_commit,
                    "repository_id": "repo_fake",
                }
            ),
            encoding="utf-8",
        )
    if factory.checkpoint_enabled(call.resume):
        _write_sqlite(run_dir / "checkpoints.sqlite", "checkpoints")
        (run_dir / "checkpoint_manifest.json").write_text(
            json.dumps(
                {
                    "run_id": call.run_id,
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "base_commit": factory.base_commit,
                    "code_version": "test",
                }
            ),
            encoding="utf-8",
        )
    (workspace / "calc.py").write_text(patch_content, encoding="utf-8", newline="\n")
    return run_dir, generate_git_diff(workspace, factory.base_commit).text


def _build_outcome(
    factory: FakeFactory,
    call: RunCall,
    *,
    patch: str,
    resumed: bool,
    status: str | None = None,
    stop_reason: str | None = None,
) -> InferenceOutcome:
    run_dir = factory.root / "runs" / call.run_id
    return InferenceOutcome(
        run_id=call.run_id,
        task_id=call.task.instance_id,
        seed=call.seed,
        base_commit=call.task.base_commit,
        repository_id="repo_fake",
        status=status or factory.status,
        stop_reason=stop_reason if stop_reason is not None else factory.stop_reason,
        final_patch=patch,
        final_patch_sha256=_sha(patch),
        first_attempt_patch=None,
        first_attempt_patch_sha256=None,
        iterations=1,
        attempts_saved=int(bool(patch)),
        usage=factory.usage.model_copy(deep=True),
        usage_uncertain=factory.usage_uncertain,
        test_runs=2,
        runtime_seconds=2.5,
        localization_files=(),
        localization_symbols=(),
        agent_claims_success=factory.claims_success,
        empty_patch=not patch.strip(),
        invalid_patch=factory.invalid_patch,
        simulated_llm=True,
        sandbox_backend="fake",
        verification_mode="full" if patch.strip() else "none",
        artifact_dir=str(run_dir),
        checkpointed=factory.checkpoint_enabled(call.resume),
        resumed_from_checkpoint=resumed,
    )


class _FakeAssembly:
    def __init__(self, factory: FakeFactory, call: RunCall) -> None:
        self._factory = factory
        self._call = call

    def run(self, *, run_id: str, seed: int) -> InferenceOutcome:
        assert run_id == self._call.run_id and seed == self._call.seed
        return self._factory.dispatch(self._call)


class FakeFactory:
    """按调用顺序记录真实发生过的推理；handler 决定每次的产出。"""

    def __init__(
        self,
        root: Path,
        *,
        base_commit: str,
        source_repo: Path,
        log_path: Path | None = None,
    ) -> None:
        self.root = root
        self.base_commit = base_commit
        self.source_repo = source_repo
        self.log_path = log_path
        self.calls: list[RunCall] = []
        self.handlers: dict[str, Handler] = {}
        self.handler_override: Handler | None = None
        self.patch_content = PATCHED_FILE
        self.usage = KNOWN_USAGE
        self.usage_uncertain = False
        self.status = "succeeded"
        self.stop_reason: str | None = None
        self.claims_success = True
        self.invalid_patch = False

    def checkpoint_enabled(self, resume: ResumeLevel) -> bool:
        """模拟生产装配：只有 GRAPH/EVALUATION 才开启 checkpoint。"""
        return resume.rank >= ResumeLevel.GRAPH.rank

    def __call__(
        self, task: FakeTask, seed: int, *, run_id: str, resume: ResumeLevel
    ) -> _FakeAssembly:
        call = RunCall(task=task, seed=seed, run_id=run_id, resume=resume)
        self.calls.append(call)
        return _FakeAssembly(self, call)

    def dispatch(self, call: RunCall) -> InferenceOutcome:
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "instance_id": call.task.instance_id,
                            "run_id": call.run_id,
                            "resume": call.resume.value,
                        }
                    )
                    + "\n"
                )
        handler = self.handlers.get(call.task.instance_id) or self.handler_override
        return handler(self, call) if handler is not None else self._success(call)

    def _success(self, call: RunCall) -> InferenceOutcome:
        run_dir = self.root / "runs" / call.run_id
        checkpoint_before = (run_dir / "checkpoints.sqlite").is_file()
        _run_dir, patch = _materialize(self, call, self.patch_content)
        resumed = checkpoint_before and self.checkpoint_enabled(call.resume)
        return _build_outcome(self, call, patch=patch, resumed=resumed)

    def _run_one(self, instance_id: str, *, seed: int = 0, resume: ResumeLevel) -> None:
        """仅供 handler 之外的单实例调试。"""
        raise NotImplementedError(instance_id)


@dataclass(frozen=True, slots=True)
class EvalCall:
    instance_id: str
    evaluation_task: Any
    patch: str
    patch_sha256: str
    seed: int
    harness_run_id: str
    run_dir: Path


class FakeEvaluator:
    """默认给出 RESOLVED，并写一份可被 runner 复制/校验的"官方报告"。"""

    def __init__(self) -> None:
        self.calls: list[EvalCall] = []
        self.handlers: dict[str, EvalHandler] = {}
        self.verdict = OfficialVerdict.RESOLVED
        self.resolved: bool | None = True
        self.error_kind: str | None = None
        self.error_detail: str | None = None
        self.write_report = True
        self.report_missing = False
        self.report_sha_mismatch = False
        self.patch_sha_override: str | None = None
        self.harness_run_id_override: str | None = None
        self.instance_override: str | None = None
        self.harness_version_override: str | None = None
        self.dataset_revision_override: str | None = None
        self.exception: BaseException | None = None
        self.real_docker = False

    def fail(self, *, kind: str | None = None, detail: str | None = None) -> None:
        self.verdict = OfficialVerdict.ERROR
        self.resolved = None
        self.error_kind = kind
        self.error_detail = detail

    def __call__(
        self,
        *,
        instance_id: str,
        evaluation_task: Any,
        patch: str,
        patch_sha256: str,
        seed: int,
        harness_run_id: str,
        run_dir: Path,
    ) -> SWEBenchEvaluation:
        call = EvalCall(
            instance_id=instance_id,
            evaluation_task=evaluation_task,
            patch=patch,
            patch_sha256=patch_sha256,
            seed=seed,
            harness_run_id=harness_run_id,
            run_dir=run_dir,
        )
        self.calls.append(call)
        handler = self.handlers.get(instance_id)
        if handler is not None:
            return handler(self, call)
        return self._default(call)

    def _default(self, call: EvalCall) -> SWEBenchEvaluation:
        if self.exception is not None:
            raise self.exception
        report_path: Path | None = None
        report_sha256: str | None = None
        if self.write_report:
            report_path = call.run_dir / "harness-report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps({"instance_id": call.instance_id, "resolved": self.resolved}),
                encoding="utf-8",
            )
            report_sha256 = _sha(report_path.read_text(encoding="utf-8"))
            if self.report_sha_mismatch:
                report_sha256 = "0" * 64
        resolved = (
            self.resolved
            if self.verdict in {OfficialVerdict.RESOLVED, OfficialVerdict.UNRESOLVED}
            else None
        )
        return SWEBenchEvaluation(
            verdict=self.verdict,
            instance_id=self.instance_override or call.instance_id,
            resolved=resolved,
            harness_run_id=self.harness_run_id_override or call.harness_run_id,
            patch_sha256=self.patch_sha_override or call.patch_sha256,
            harness_version=self.harness_version_override or HARNESS_VERSION,
            dataset_revision=self.dataset_revision_override or DATASET_REVISION,
            report_path=None if self.report_missing else report_path,
            report_sha256=None if self.report_missing else report_sha256,
            error_kind=self.error_kind,
            error_detail=self.error_detail,
            runtime_seconds=1.25,
            real_docker=self.real_docker,
        )


@dataclass
class Harness:
    tmp_path: Path
    source: Path
    base_commit: str
    loader: FakeLoader
    factory: FakeFactory
    evaluator: FakeEvaluator
    runner: SWEBenchRunner
    config: SWEBenchRunConfig
    journal_path: Path
    artifact_root: Path

    def rebuild(
        self,
        *,
        config: SWEBenchRunConfig | None = None,
        code_commit: str | None = "test-commit",
    ) -> SWEBenchRunner:
        return SWEBenchRunner(
            loader=self.loader,
            inference_factory=self.factory,
            evaluator=self.evaluator,
            journal_path=self.journal_path,
            artifact_root=self.artifact_root,
            config=config or self.config,
            code_commit=code_commit,
        )

    def run(
        self,
        instance_ids: tuple[str, ...] | None = None,
        *,
        seed: int = 0,
        resume: ResumeLevel = ResumeLevel.EVALUATION,
    ) -> Any:
        return self.runner.run(list(instance_ids or self.loader.ids), seed=seed, resume=resume)


def _make_harness(
    tmp_path: Path,
    *,
    ids: tuple[str, ...] = (INSTANCE_A, INSTANCE_B),
    config: SWEBenchRunConfig | None = None,
    source_files: dict[str, str] | None = None,
    log_path: Path | None = None,
) -> Harness:
    source = init_repo(tmp_path / "source", files=source_files or dict(SOURCE_FILES))
    base_commit = source.commits[0]
    loader = FakeLoader(base_commit, ids=ids)
    artifact_root = tmp_path / "artifacts"
    factory = FakeFactory(
        artifact_root,
        base_commit=base_commit,
        source_repo=source.path,
        log_path=log_path,
    )
    evaluator = FakeEvaluator()
    resolved_config = config or SWEBenchRunConfig(
        model_identity="FakeLLM", provider="fake", model="FakeLLM"
    )
    journal_path = tmp_path / "journal.sqlite"
    runner = SWEBenchRunner(
        loader=loader,
        inference_factory=factory,
        evaluator=evaluator,
        journal_path=journal_path,
        artifact_root=artifact_root,
        config=resolved_config,
        code_commit="test-commit",
    )
    return Harness(
        tmp_path=tmp_path,
        source=source.path,
        base_commit=base_commit,
        loader=loader,
        factory=factory,
        evaluator=evaluator,
        runner=runner,
        config=resolved_config,
        journal_path=journal_path,
        artifact_root=artifact_root,
    )


def _record_run_dir(harness: Harness, record: Any) -> Path:
    return harness.artifact_root / str(record.artifact_refs["run_dir"])


def _base_identity(harness: Harness, instance_id: str, seed: int = 0) -> RunIdentity:
    task = harness.loader.get_inference_task("test", instance_id)
    return harness.runner._base_identity(task, seed, subset_hash([instance_id]))


# =========================================================================== 身份绑定


def test_identity_changes_with_every_bound_field() -> None:
    base = RunIdentity(
        dataset=DATASET_NAME,
        revision=DATASET_REVISION,
        split="test",
        subset_hash=subset_hash([INSTANCE_A]),
        instance_id=INSTANCE_A,
        repo="owner/repo",
        base_commit="a" * 40,
        provider="fake",
        model="FakeLLM",
        config_hash="c" * 32,
        code_commit="deadbeefcafe",
        seed=0,
        patch_hash="",
        harness_version=HARNESS_VERSION,
    )
    updates: dict[str, str] = {
        "dataset": "other/dataset",
        "revision": "b" * 40,
        "split": "dev",
        "subset_hash": "f" * 32,
        "instance_id": INSTANCE_B,
        "repo": "owner/other",
        "base_commit": "b" * 40,
        "provider": "bigmodel",
        "model": "glm-5.3-flash",
        "config_hash": "d" * 32,
        "code_commit": "001122334455",
        "harness_version": "4.0.0",
    }
    for field_name, value in updates.items():
        changed = base.model_copy(update={field_name: value})
        assert changed.key != base.key, f"{field_name} 没有进入完整身份"
        assert changed.journal_key != base.journal_key, f"{field_name} 没有进入 journal 身份"
    assert base.model_copy(update={"seed": 1}).journal_key != base.journal_key

    # 补丁哈希是"同一任务的不同结果"，不是"另一条任务"：完整身份要变，journal 键不变。
    with_patch = base.model_copy(update={"patch_hash": "e" * 64})
    assert with_patch.key != base.key
    assert with_patch.journal_key == base.journal_key


def test_subset_hash_is_order_independent() -> None:
    assert subset_hash([INSTANCE_A, INSTANCE_B]) == subset_hash([INSTANCE_B, INSTANCE_A])
    assert subset_hash([INSTANCE_A]) != subset_hash([INSTANCE_A, INSTANCE_B])


def test_harness_run_id_binds_patch_and_seed() -> None:
    patch_one = "1" * 64
    patch_two = "2" * 64
    ours = harness_run_id(INSTANCE_A, patch_one, 0)
    assert ours != harness_run_id(INSTANCE_A, patch_two, 0)
    assert ours != harness_run_id(INSTANCE_A, patch_one, 1)
    # 唯一真源是 D 的 derive_run_id：run_id 里的 12 位是**补丁集合摘要**，
    # 不是"补丁哈希前 12 位"。D 在启动 harness 前会按同一算法校验，不一致就会
    # 抛 HarnessCacheError（CACHE_IDENTITY_MISMATCH）。
    assert ours == derive_run_id({INSTANCE_A: patch_one}, seed="0")
    assert ours.split("-")[1] == patch_set_digest({INSTANCE_A: patch_one})[:12]
    with pytest.raises(ValueError):
        harness_run_id(INSTANCE_A, "not-a-hash", 0)


def test_evaluator_receives_a_run_id_the_official_harness_accepts(tmp_path: Path) -> None:
    """P0 回归：runner 传给 evaluator 的 run_id 必须能被 D 的摘要校验接受。

    ``OfficialHarnessRunner._enforce_run_id_digest`` 在启动官方 harness **之前**校验
    ``run_id.split("-")[1] == patch_set_digest(predictions)[:12]``；这里直接复算 D 的
    两个公开派生函数，确保两边不会各算一套。
    """
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.run(resume=ResumeLevel.EVALUATION)
    call = harness.evaluator.calls[0]
    expected = derive_run_id({INSTANCE_A: call.patch_sha256}, seed="0")
    assert call.harness_run_id == expected
    assert (
        call.harness_run_id.split("-")[1] == patch_set_digest({INSTANCE_A: call.patch_sha256})[:12]
    )

    # 直接调用 D 的校验（而不是只比公式）：同一 run_id 必须通过它的预检。
    config = HarnessConfig(
        predictions_path=tmp_path / "predictions.jsonl",
        run_id=call.harness_run_id,
        report_dir=tmp_path / "official",
        model_name_or_path="FakeLLM",
        dataset_revision=DATASET_REVISION,
    )
    identity = HarnessCacheIdentity(
        run_id=call.harness_run_id,
        patch_hashes=((INSTANCE_A, call.patch_sha256),),
        model_name_or_path="FakeLLM",
        dataset_name=DATASET_NAME,
        dataset_revision=DATASET_REVISION,
        split="test",
        harness_version=HARNESS_VERSION,
    )
    # 不抛异常即被接受；旧算法（补丁哈希前 12 位）会被它拒绝。
    OfficialHarnessRunner(config)._enforce_run_id_digest(identity)


def test_dataset_identity_from_loader_must_match_config(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    task = FakeTask(
        instance_id=INSTANCE_A,
        base_commit=harness.base_commit,
        dataset=FakeDataset(
            name=DATASET_NAME,
            revision="f" * 40,  # 与固定 revision 不同
            harness_version=HARNESS_VERSION,
            split="test",
        ),
    )
    harness.loader.get_inference_task = lambda split, iid: task  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="数据集身份"):
        harness.runner.run([INSTANCE_A], seed=0, resume=ResumeLevel.TASK)


def test_unknown_instance_is_rejected_before_any_dispatch(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    with pytest.raises(KeyError):
        harness.runner.run(["missing__repo-9"], seed=0, resume=ResumeLevel.TASK)
    assert harness.factory.calls == []


# =========================================================================== 任务级恢复


def test_task_level_resume_skips_completed_without_rebilling(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    first = harness.run(resume=ResumeLevel.EVALUATION)
    assert len(harness.factory.calls) == 2
    assert len(harness.evaluator.calls) == 2
    first_metrics = first.metrics()
    assert first_metrics["counts"]["official_resolved"] == 2
    # 未注入环境身份 → 必须显式报告"未验证"，不能静默当成已验证。
    assert first_metrics["counts"]["environment_identity_unverified_evaluations"] == 2
    assert abs(first_metrics["cost"]["total_cost"] - 1.0) < 1e-9
    assert abs(first_metrics["budget"]["spent"] - 1.0) < 1e-9

    second = harness.run(resume=ResumeLevel.TASK)
    # 没有重跑推理，也没有重跑评测。
    assert len(harness.factory.calls) == 2
    assert len(harness.evaluator.calls) == 2
    assert second.reuse["reused_tasks"] == 2
    metrics = second.metrics()
    assert metrics["counts"]["official_resolved"] == 2
    # 记录累计费用只算一次；本次会话没有新花费。
    assert abs(metrics["cost"]["total_cost"] - 1.0) < 1e-9
    assert metrics["budget"]["spent"] == 0.0
    assert _journal_rows(harness.journal_path) == 2


def test_task_level_resume_refuses_to_reuse_a_config_from_another_model(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.run(resume=ResumeLevel.EVALUATION)
    other = SWEBenchRunConfig(model_identity="other-model", provider="fake", model="other-model")
    runner = harness.rebuild(config=other)
    report = runner.run([INSTANCE_A], seed=0, resume=ResumeLevel.TASK)
    # 换了模型就必须重跑：旧结果属于另一个模型。
    assert len(harness.factory.calls) == 2
    assert report.reuse["reused_tasks"] == 0
    assert _journal_rows(harness.journal_path) == 2


def test_corrupted_journal_is_refused_not_silently_rerun(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.run(resume=ResumeLevel.EVALUATION)
    connection = sqlite3.connect(harness.journal_path)
    try:
        connection.execute("UPDATE results SET payload = payload || ' '")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(JournalCorruptionError):
        harness.run(resume=ResumeLevel.TASK)
    assert len(harness.factory.calls) == 1


# =========================================================================== 图级恢复


def test_graph_level_resume_continues_the_crashed_run_in_place(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))

    def crash_once(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
        _materialize(factory, call, factory.patch_content)
        factory.handler_override = None
        raise RuntimeError("simulated crash mid-graph")

    harness.factory.handler_override = crash_once
    first = harness.run(resume=ResumeLevel.GRAPH)
    assert len(harness.factory.calls) == 1
    crashed_record = first.records[0]
    assert crashed_record.execution_state == "inference_error"
    crashed_run_id = crashed_record.run_id
    assert (harness.artifact_root / "runs" / crashed_run_id / "checkpoints.sqlite").is_file()

    second = harness.run(resume=ResumeLevel.GRAPH)
    assert len(harness.factory.calls) == 2
    resumed_call = harness.factory.calls[-1]
    assert resumed_call.run_id == crashed_run_id, "图级恢复必须接同一现场"
    assert resumed_call.resume is ResumeLevel.GRAPH
    metrics = second.metrics()
    assert metrics["counts"]["graph_resumed"] == 1
    assert metrics["counts"]["official_resolved"] == 1
    assert second.reuse["graph_resumed"] == 1
    assert second.records[0].resolved is True
    assert second.records[0].resumed_from_checkpoint is True


def test_graph_level_resume_refuses_a_workspace_on_another_base(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))

    def crash_once(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
        _materialize(factory, call, factory.patch_content)
        factory.handler_override = None
        raise RuntimeError("simulated crash mid-graph")

    harness.factory.handler_override = crash_once
    first = harness.run(resume=ResumeLevel.GRAPH)
    run_dir = harness.artifact_root / "runs" / first.records[0].run_id
    # 把现场换成另一个 base 的仓库：继续跑只会产出与 checkpoint 不属于同一个世界的补丁。
    _force_rmtree(run_dir / "workspace")
    init_repo(run_dir / "workspace", files={"calc.py": UNRELATED_FILE})

    second = harness.run(resume=ResumeLevel.GRAPH)
    assert len(harness.factory.calls) == 1, "现场核对不过时不得再跑推理"
    record = second.records[0]
    assert record.resolved is None
    assert record.artifact_refs["swebench_attribution"] == SWEBenchAttribution.WORKSPACE_ERROR.value
    assert any("HEAD" in detail for detail in record.error_details)
    assert any("evidence=" in detail for detail in record.error_details)
    metrics = second.metrics()
    assert metrics["counts"]["env_error"] == 1
    assert metrics["ratios"]["official_resolved_rate"]["value"] == 0.0
    audit = second.workspace_audits[INSTANCE_A]
    assert audit["ok"] is False
    assert audit["base_commit_matches"] is False


# --------------------------------------------------------- 现场核对逐项失败


def _mutate_config(harness: Harness, run_dir: Path) -> None:
    manifest_path = run_dir / "swebench_run.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["identity"]["config_hash"] = "f" * 32
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


def _mutate_patch_hash(harness: Harness, run_dir: Path) -> None:
    manifest_path = run_dir / "swebench_run.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["identity"]["patch_hash"] = "f" * 64
    payload["patch_sha256"] = "f" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")


def _remove_workspace(harness: Harness, run_dir: Path) -> None:
    _force_rmtree(run_dir / "workspace")


def _corrupt_index(harness: Harness, run_dir: Path) -> None:
    (run_dir / "index.sqlite3").write_bytes(b"this is not a sqlite database")


def _corrupt_checkpoint_manifest(harness: Harness, run_dir: Path) -> None:
    (run_dir / "checkpoint_manifest.json").write_text("{ not json", encoding="utf-8")


def _wrong_checkpoint_schema(harness: Harness, run_dir: Path) -> None:
    (run_dir / "checkpoint_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION + 7,
                "base_commit": harness.base_commit,
            }
        ),
        encoding="utf-8",
    )


def _corrupt_manifest(harness: Harness, run_dir: Path) -> None:
    (run_dir / "swebench_run.json").write_text("{ not json", encoding="utf-8")


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        pytest.param(_remove_workspace, "workspace 不存在", id="missing-workspace"),
        pytest.param(_mutate_config, "现场配置与本次运行不一致", id="config-mismatch"),
        pytest.param(_mutate_patch_hash, "补丁", id="patch-mismatch"),
        pytest.param(_corrupt_index, "索引库损坏", id="corrupt-index"),
        pytest.param(
            _corrupt_checkpoint_manifest,
            "checkpoint 缺少可读的 manifest",
            id="checkpoint-manifest-missing",
        ),
        pytest.param(_wrong_checkpoint_schema, "checkpoint schema", id="checkpoint-schema"),
        pytest.param(_corrupt_manifest, "现场 manifest 不可读", id="unreadable-manifest"),
    ],
)
def test_workspace_recovery_audit_rejects_each_inconsistency(
    tmp_path: Path, mutate: Any, needle: str
) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    report = harness.run(resume=ResumeLevel.EVALUATION)
    record = report.records[0]
    run_dir = _record_run_dir(harness, record)
    base = _base_identity(harness, INSTANCE_A)

    intact = audit_workspace(
        artifact_root=harness.artifact_root, run_id=run_dir.name, expected_identity=base
    )
    assert intact.ok, intact.problems
    assert intact.patch_state == "match"
    assert intact.checkpoint_state == "ok"

    mutate(harness, run_dir)
    broken = audit_workspace(
        artifact_root=harness.artifact_root, run_id=run_dir.name, expected_identity=base
    )
    assert broken.ok is False
    assert any(needle in problem for problem in broken.problems), broken.problems
    # 证据字段必须能用来复盘：拒绝理由不能只有一句"不一致"。
    assert broken.evidence
    assert broken.as_dict()["ok"] is False


def test_missing_index_is_a_note_not_a_refusal(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    report = harness.run(resume=ResumeLevel.EVALUATION)
    run_dir = _record_run_dir(harness, report.records[0])
    (run_dir / "index.sqlite3").unlink()
    audit = audit_workspace(
        artifact_root=harness.artifact_root,
        run_id=run_dir.name,
        expected_identity=_base_identity(harness, INSTANCE_A),
    )
    # 索引缺失会重建索引，不改变补丁证据；损坏才拒绝。
    assert audit.ok is True
    assert audit.index_state == "missing"
    assert any("索引库缺失" in note for note in audit.notes)


def test_crashed_run_without_finished_inference_cannot_lend_its_patch(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))

    def crash_once(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
        _materialize(factory, call, factory.patch_content)
        factory.handler_override = None
        raise RuntimeError("simulated crash mid-graph")

    harness.factory.handler_override = crash_once
    first = harness.run(resume=ResumeLevel.GRAPH)
    run_dir = harness.artifact_root / "runs" / first.records[0].run_id
    evidence = recover_inference_evidence(
        artifact_root=harness.artifact_root,
        run_id=run_dir.name,
        expected_identity=_base_identity(harness, INSTANCE_A),
    )
    assert evidence.usable is False
    assert any("推理完成态" in problem or "推理摘要" in problem for problem in evidence.problems)


# =========================================================================== 评测级恢复


def test_evaluation_level_recovery_reuses_inference_without_rerunning_it(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.evaluator.exception = RuntimeError("official harness crashed")
    first = harness.run(resume=ResumeLevel.EVALUATION)
    assert len(harness.factory.calls) == 1
    assert first.records[0].execution_state == "completed"
    assert first.records[0].resolved is None
    assert first.records[0].evaluation_state == "error"
    assert first.metrics()["counts"]["harness_error"] == 1

    # 现场里已经有完整推理证据：恢复时只补评测，不重跑推理。
    harness.evaluator.exception = None
    before_inference = len(harness.factory.calls)
    second = harness.run(resume=ResumeLevel.EVALUATION)
    assert len(harness.factory.calls) == before_inference
    assert len(harness.evaluator.calls) == 2
    assert second.records[0].resolved is True
    metrics = second.metrics()
    assert metrics["counts"]["official_resolved"] == 1
    # 推理费用已在上一进程记过，恢复不重复扣费。
    assert abs(metrics["cost"]["total_cost"] - 0.5) < 1e-9
    assert metrics["budget"]["spent"] == 0.0


def test_evaluation_level_recovers_evaluation_written_before_journal_commit(
    tmp_path: Path,
) -> None:
    harness = _make_harness(tmp_path)
    first = harness.run(resume=ResumeLevel.EVALUATION)
    assert first.metrics()["counts"]["official_resolved"] == 2
    # 模拟"评测报告已落盘、journal 还没提交"的崩溃：结论在 journal 里消失，
    # 但现场里的补丁 + 评测信封 + 报告都完整。
    connection = sqlite3.connect(harness.journal_path)
    try:
        connection.execute("DELETE FROM results")
        connection.commit()
    finally:
        connection.close()

    before_inference = len(harness.factory.calls)
    before_eval = len(harness.evaluator.calls)
    second = harness.run(resume=ResumeLevel.EVALUATION)
    assert len(harness.factory.calls) == before_inference
    assert len(harness.evaluator.calls) == before_eval
    assert second.reuse["evaluation_reused"] == 2
    metrics = second.metrics()
    assert metrics["counts"]["official_resolved"] == 2
    assert metrics["counts"]["evaluation_reused"] == 2
    # 费用只算记录里的那一份，恢复不重复计费。
    assert abs(metrics["cost"]["total_cost"] - 1.0) < 1e-9
    assert metrics["budget"]["spent"] == 0.0
    assert _journal_rows(harness.journal_path) == 2


def test_broken_official_report_is_not_adopted_as_a_new_result(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.run(resume=ResumeLevel.EVALUATION)
    record = harness.runner.run([INSTANCE_A], seed=0, resume=ResumeLevel.TASK).records[0]
    run_dir = _record_run_dir(harness, record)
    # 报告内容被改动：信封里的哈希对不上，旧报告不得冒充新结果。
    (run_dir / "official_report.json").write_bytes(b'{"resolved": ["tampered"]}')
    base = _base_identity(harness, INSTANCE_A)
    assert record.patch_sha256 is not None
    audit = audit_official_evidence(
        artifact_root=harness.artifact_root,
        run_id=run_dir.name,
        expected_instance_id=INSTANCE_A,
        expected_patch_sha256=record.patch_sha256,
        expected_harness_run_id=harness_run_id(INSTANCE_A, str(record.patch_sha256), 0),
        expected_harness_version=HARNESS_VERSION,
        expected_dataset_revision=DATASET_REVISION,
        expected_seed=0,
        expected_config_hash=base.config_hash,
    )
    assert audit.reusable is False
    assert any("哈希" in problem for problem in audit.problems)


def test_environment_identity_mismatch_prevents_report_reuse(tmp_path: Path) -> None:
    config = SWEBenchRunConfig(
        model_identity="FakeLLM",
        provider="fake",
        model="FakeLLM",
        environment_identity="env-a",
    )
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,), config=config)
    report = harness.run(resume=ResumeLevel.EVALUATION)
    record = report.records[0]
    # 注入了环境身份 → 报告必须显示"已验证"（0 个未验证）。
    assert report.metrics()["counts"]["environment_identity_unverified_evaluations"] == 0
    run_dir = _record_run_dir(harness, record)
    base = _base_identity(harness, INSTANCE_A)
    common: dict[str, Any] = {
        "artifact_root": harness.artifact_root,
        "run_id": run_dir.name,
        "expected_instance_id": INSTANCE_A,
        "expected_patch_sha256": record.patch_sha256,
        "expected_harness_run_id": harness_run_id(INSTANCE_A, str(record.patch_sha256), 0),
        "expected_harness_version": HARNESS_VERSION,
        "expected_dataset_revision": DATASET_REVISION,
        "expected_seed": 0,
        "expected_config_hash": base.config_hash,
    }
    same = audit_official_evidence(**common, expected_environment_identity="env-a")
    assert same.reusable is True
    assert same.environment_identity_verified is True
    other = audit_official_evidence(**common, expected_environment_identity="env-b")
    assert other.reusable is False
    assert any("环境身份" in problem for problem in other.problems)
    # 未注入环境身份时仍可复用，但必须明确"未验证"，不能假装验证过。
    unverified = audit_official_evidence(**common)
    assert unverified.reusable is True
    assert unverified.environment_identity_verified is False


def test_environment_identity_change_reevaluates_but_does_not_reinfer(
    tmp_path: Path,
) -> None:
    config_a = SWEBenchRunConfig(
        model_identity="FakeLLM",
        provider="fake",
        model="FakeLLM",
        environment_identity="env-a",
    )
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,), config=config_a)
    first = harness.run(resume=ResumeLevel.EVALUATION)
    run_dir = _record_run_dir(harness, first.records[0])
    connection = sqlite3.connect(harness.journal_path)
    try:
        connection.execute("DELETE FROM results")
        connection.commit()
    finally:
        connection.close()

    config_b = SWEBenchRunConfig(
        model_identity="FakeLLM",
        provider="fake",
        model="FakeLLM",
        environment_identity="env-b",
    )
    before_inference = len(harness.factory.calls)
    before_eval = len(harness.evaluator.calls)
    report = harness.rebuild(config=config_b).run(
        [INSTANCE_A], seed=0, resume=ResumeLevel.EVALUATION
    )
    # 推理证据照常复用（补丁/身份没变），但镜像身份变了 → 旧官方报告不得复用。
    assert len(harness.factory.calls) == before_inference
    assert len(harness.evaluator.calls) == before_eval + 1
    assert report.reuse["evaluation_reused"] == 0
    assert report.records[0].resolved is True
    envelope = json.loads((run_dir / "official_eval.json").read_text(encoding="utf-8"))
    assert envelope["environment_identity"] == "env-b"


def test_recovered_official_evaluation_keeps_real_docker_flag(tmp_path: Path) -> None:
    """P2 回归：信封缺 real_docker 会让恢复后的报告把 Fake 评测说成真 Docker。"""
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.evaluator.real_docker = True
    first = harness.run(resume=ResumeLevel.EVALUATION)
    assert first.records[0].real_docker_evaluation is True

    connection = sqlite3.connect(harness.journal_path)
    try:
        connection.execute("DELETE FROM results")
        connection.commit()
    finally:
        connection.close()
    second = harness.run(resume=ResumeLevel.EVALUATION)
    assert second.reuse["evaluation_reused"] == 1
    assert second.records[0].real_docker_evaluation is True


def test_environment_identity_must_be_non_empty_when_injected() -> None:
    with pytest.raises(ValueError, match="environment_identity"):
        SWEBenchRunConfig(environment_identity="   ")


def test_platform_blocked_harness_result_maps_to_env_error(tmp_path: Path) -> None:
    """D 的 PLATFORM_BLOCKED（原生 Windows）不能记成"模型未解决"。"""

    def platform_blocked(evaluator: FakeEvaluator, call: EvalCall) -> SWEBenchEvaluation:
        # 与 scripts/swebench_smoke.py 的适配层一致：decision=PLATFORM_BLOCKED 时
        # D 的 verdict=NOT_EVALUATED、error_kind 是大写的 ENV_ERROR。
        return SWEBenchEvaluation(
            verdict=OfficialVerdict.NOT_EVALUATED,
            instance_id=call.instance_id,
            resolved=None,
            harness_run_id=call.harness_run_id,
            patch_sha256=call.patch_sha256,
            harness_version=HARNESS_VERSION,
            dataset_revision=DATASET_REVISION,
            error_kind=HarnessErrorKind.ENV_ERROR.value,
            error_detail="原生 Windows 无法 import resource；需要 WSL2/Linux",
        )

    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.evaluator.handlers[INSTANCE_A] = platform_blocked
    report = harness.run(resume=ResumeLevel.EVALUATION)
    record = report.records[0]
    assert record.resolved is None
    assert record.evaluation_state == "error"
    assert record.error_class == "env_error"
    assert record.artifact_refs["swebench_attribution"] == SWEBenchAttribution.ENV_ERROR.value
    metrics = report.metrics()
    assert metrics["counts"]["env_error"] == 1
    assert metrics["counts"]["harness_error"] == 0
    assert metrics["counts"]["official_resolved"] == 0
    assert metrics["counts"]["officially_evaluated"] == 0


def test_harness_cache_error_is_an_evaluation_error_not_a_model_failure(
    tmp_path: Path,
) -> None:
    def cache_error(evaluator: FakeEvaluator, call: EvalCall) -> SWEBenchEvaluation:
        raise HarnessCacheError(
            HarnessErrorKind.CACHE_IDENTITY_MISMATCH,
            "报告缓存身份与当前补丁不符，拒绝复用旧报告",
        )

    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.evaluator.handlers[INSTANCE_A] = cache_error
    report = harness.run(resume=ResumeLevel.EVALUATION)
    record = report.records[0]
    assert record.resolved is None
    assert record.artifact_refs["swebench_attribution"] == (
        SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR.value
    )
    metrics = report.metrics()
    assert metrics["counts"]["harness_error"] == 1
    assert metrics["counts"]["official_resolved"] == 0
    # 评测级恢复仍可用：补丁证据在，缺的是评测结论。
    assert record.execution_state == "completed"


def test_harness_platform_error_is_env_error(tmp_path: Path) -> None:
    def platform_error(evaluator: FakeEvaluator, call: EvalCall) -> SWEBenchEvaluation:
        raise HarnessPlatformError(
            HarnessErrorKind.RESOURCE_BLOCKED,
            "找不到 WSL2 可用发行版",
        )

    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.evaluator.handlers[INSTANCE_A] = platform_error
    report = harness.run(resume=ResumeLevel.EVALUATION)
    assert report.records[0].artifact_refs["swebench_attribution"] == (
        SWEBenchAttribution.ENV_ERROR.value
    )
    assert report.metrics()["counts"]["env_error"] == 1


def test_official_report_identity_mismatch_never_becomes_resolved(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    # 适配层把报告标成了另一个补丁的：身份核对必须让它降级为评测错误。
    harness.evaluator.patch_sha_override = "f" * 64
    report = harness.run(resume=ResumeLevel.EVALUATION)
    record = report.records[0]
    assert record.resolved is None
    assert record.artifact_refs["swebench_attribution"] == (
        SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR.value
    )
    assert report.metrics()["counts"]["official_resolved"] == 0
    assert any(
        "identity_mismatch" in detail or "不一致" in detail for detail in record.error_details
    )


def test_resolved_verdict_without_a_complete_report_is_demoted(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.evaluator.report_missing = True
    report = harness.run(resume=ResumeLevel.EVALUATION)
    record = report.records[0]
    assert record.resolved is None
    assert record.artifact_refs["swebench_attribution"] == (
        SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR.value
    )
    metrics = report.metrics()
    assert metrics["counts"]["official_resolved"] == 0
    assert metrics["counts"]["harness_error"] == 1


def test_report_hash_mismatch_from_adapter_is_rejected(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.evaluator.report_sha_mismatch = True
    report = harness.run(resume=ResumeLevel.EVALUATION)
    record = report.records[0]
    assert record.resolved is None
    assert any("报告哈希" in detail for detail in record.error_details)


def test_different_patch_gets_a_different_harness_run_id() -> None:
    patch_one = "a" * 64
    patch_two = "b" * 64
    assert harness_run_id(INSTANCE_A, patch_one, 0) != harness_run_id(INSTANCE_A, patch_two, 0)


# =========================================================================== 预算与费用


def test_resume_does_not_rebill_tokens_or_cost(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)
    first = harness.run(resume=ResumeLevel.EVALUATION)
    metrics = first.metrics()
    assert metrics["tokens"]["total"] == 240  # 2 个任务 × (100 + 20)
    assert metrics["cost"]["total_llm_calls"] == 6

    second = harness.run(resume=ResumeLevel.TASK)
    assert second.metrics()["tokens"]["total"] == 240
    assert second.metrics()["cost"]["total_llm_calls"] == 6
    assert second.metrics()["budget"]["spent"] == 0.0


def test_unknown_cost_stays_unknown_across_resume(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    harness.factory.usage = Usage(
        input_tokens=100,
        output_tokens=20,
        cost=0.0,
        cost_unknown_calls=1,
        llm_calls=3,
        currency="CNY",
    )
    first = harness.run(resume=ResumeLevel.EVALUATION)
    metrics = first.metrics()
    assert first.records[0].cost is None, "未知费用不是 0"
    assert metrics["cost"]["total_cost"] is None
    assert metrics["cost"]["total_cost_is_lower_bound"] is True
    assert metrics["counts"]["unknown_cost_tasks"] == 1
    assert metrics["budget"]["unknown_cost_tasks"] == 1

    second = harness.run(resume=ResumeLevel.TASK)
    resumed = second.metrics()
    assert resumed["cost"]["total_cost"] is None, "恢复不能把未知变成已知"
    assert resumed["counts"]["unknown_cost_tasks"] == 1
    assert resumed["budget"]["unknown_cost_tasks"] == 0  # 本次会话没有未知花费


def test_partial_unknown_cost_marks_the_batch_total_as_lower_bound(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path)

    def unknown_cost(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
        factory.usage = Usage(
            input_tokens=50,
            output_tokens=10,
            cost=0.0,
            cost_unknown_calls=1,
            llm_calls=1,
            currency="CNY",
        )
        try:
            return factory._success(call)
        finally:
            factory.usage = KNOWN_USAGE

    harness.factory.handlers[INSTANCE_B] = unknown_cost
    report = harness.run(resume=ResumeLevel.EVALUATION)
    metrics = report.metrics()
    assert metrics["cost"]["total_cost"] is not None
    assert metrics["cost"]["total_cost_is_lower_bound"] is True
    assert metrics["counts"]["unknown_cost_tasks"] == 1


def test_single_task_budget_exhaustion_does_not_stop_the_batch(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A, INSTANCE_B, INSTANCE_C))

    def budget_stop(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
        run_dir = factory.root / "runs" / call.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        _materialize(factory, call, BASE_FILE)  # 没有实际改动 → 空补丁
        return _build_outcome(
            factory,
            call,
            patch="",
            resumed=False,
            status="budget_exhausted",
            stop_reason="budget_iterations",
        )

    harness.factory.handlers[INSTANCE_A] = budget_stop
    report = harness.run(resume=ResumeLevel.EVALUATION)
    assert len(harness.factory.calls) == 3, "单任务预算耗尽不能让整批停止派发"
    metrics = report.metrics()
    assert metrics["counts"]["budget_stopped"] == 1
    assert metrics["counts"]["official_resolved"] == 2
    assert report.attribution_counts()["budget_stopped"] == 1


def test_batch_budget_exhaustion_stops_further_dispatch(tmp_path: Path) -> None:
    config = SWEBenchRunConfig(
        model_identity="FakeLLM",
        provider="fake",
        model="FakeLLM",
        max_total_cost=1.0,
        task_reservation=0.6,
    )
    harness = _make_harness(tmp_path, ids=(INSTANCE_A, INSTANCE_B, INSTANCE_C), config=config)
    report = harness.run(resume=ResumeLevel.EVALUATION)
    # 第一个任务花掉 0.6；第二个任务的预留会让总额超过 1.0，于是不再派发。
    assert len(harness.factory.calls) == 1
    metrics = report.metrics()
    assert metrics["counts"]["budget_stopped"] == 2
    assert metrics["counts"]["batch_budget_blocked"] == 2
    assert metrics["counts"]["official_resolved"] == 1
    assert report.stop_reason == "batch_budget_exhausted"
    assert metrics["denominators"]["all_target_tasks"] == 3
    assert metrics["ratios"]["official_resolved_rate"]["value"] == pytest.approx(1 / 3)
    assert metrics["budget"]["blocked_dispatches"] == 2


# =========================================================================== 归因映射


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "execution_state": "budget_blocked",
                "official_verdict": None,
                "official_resolved": None,
                "patch_valid": False,
                "empty_patch": True,
                "invalid_patch": False,
            },
            SWEBenchAttribution.BUDGET_STOPPED,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "RESOLVED",
                "official_resolved": True,
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.OFFICIAL_RESOLVED,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "UNRESOLVED",
                "official_resolved": False,
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.OFFICIAL_NOT_RESOLVED,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "PATCH_APPLY_FAILED",
                "official_resolved": None,
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.PATCH_INVALID,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "ERROR",
                "official_resolved": None,
                "official_error_kind": "env_error",
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.ENV_ERROR,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "ERROR",
                "official_resolved": None,
                "official_error_kind": "platform_blocked",
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.ENV_ERROR,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "ERROR",
                "official_resolved": None,
                # D 的 HarnessErrorKind 是大写枚举值，适配层可能原样透传；
                # patch 哈希不匹配属于评测级错误，不是模型失败。
                "official_error_kind": "CACHE_IDENTITY_MISMATCH",
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "ERROR",
                "official_resolved": None,
                "official_error_kind": "sandbox_error",
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.SANDBOX_ERROR,
        ),
        (
            {
                "execution_state": "inference_error",
                "official_verdict": None,
                "official_resolved": None,
                "error_origin": "workspace",
                "patch_valid": False,
                "empty_patch": True,
                "invalid_patch": False,
            },
            SWEBenchAttribution.WORKSPACE_ERROR,
        ),
        (
            {
                "execution_state": "inference_error",
                "official_verdict": None,
                "official_resolved": None,
                "error_class": "harness_internal_error",
                "patch_valid": False,
                "empty_patch": True,
                "invalid_patch": False,
            },
            SWEBenchAttribution.HARNESS_INTERNAL_ERROR,
        ),
        (
            {
                "execution_state": "inference_error",
                "official_verdict": None,
                "official_resolved": None,
                "error_class": "llm_provider_error",
                "patch_valid": False,
                "empty_patch": True,
                "invalid_patch": False,
            },
            SWEBenchAttribution.PROVIDER_ERROR,
        ),
        (
            {
                "execution_state": "inference_error",
                "official_verdict": None,
                "official_resolved": None,
                "error_class": "plan_semantic_invalid",
                "patch_valid": False,
                "empty_patch": True,
                "invalid_patch": False,
            },
            SWEBenchAttribution.MODEL_INVALID_PLAN,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": "ERROR",
                "official_resolved": None,
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.OFFICIAL_EVALUATION_ERROR,
        ),
        (
            {
                "execution_state": "completed",
                "official_verdict": None,
                "official_resolved": None,
                "patch_valid": True,
                "empty_patch": False,
                "invalid_patch": False,
            },
            SWEBenchAttribution.MODEL_NOT_RESOLVED,
        ),
    ],
)
def test_classify_outcome_matrix(kwargs: dict[str, Any], expected: SWEBenchAttribution) -> None:
    assert classify_outcome(**kwargs) is expected


def test_every_swebench_attribution_maps_to_a_legacy_attribution() -> None:
    assert set(LEGACY_ATTRIBUTION) == set(SWEBenchAttribution)
    for value in LEGACY_ATTRIBUTION.values():
        assert value in set(
            __import__("codepilot.eval.attribution", fromlist=["Attribution"]).Attribution
        )


def test_official_resolution_wins_over_model_side_stop_reason() -> None:
    attribution = classify_outcome(
        execution_state="completed",
        official_verdict="RESOLVED",
        official_resolved=True,
        patch_valid=True,
        empty_patch=False,
        invalid_patch=False,
        stop_reason="budget_iterations",
    )
    assert attribution is SWEBenchAttribution.OFFICIAL_RESOLVED


# =========================================================================== 报告指标


def test_report_counts_and_two_denominators(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A, INSTANCE_B, INSTANCE_C))

    def factory_crash(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
        raise RuntimeError("workspace build blew up")

    def env_error_eval(evaluator: FakeEvaluator, call: EvalCall) -> SWEBenchEvaluation:
        return SWEBenchEvaluation(
            verdict=OfficialVerdict.ERROR,
            instance_id=call.instance_id,
            resolved=None,
            harness_run_id=call.harness_run_id,
            patch_sha256=call.patch_sha256,
            harness_version=HARNESS_VERSION,
            dataset_revision=DATASET_REVISION,
            error_kind="resource_blocked",
            error_detail="镜像层不可用（模拟 ENV_ERROR）",
        )

    harness.factory.handlers[INSTANCE_B] = factory_crash
    harness.evaluator.handlers[INSTANCE_C] = env_error_eval
    report = harness.run(resume=ResumeLevel.EVALUATION)
    metrics = report.metrics()
    counts = metrics["counts"]
    assert counts["dispatched"] == 3
    assert counts["completed_inference"] == 2
    assert counts["valid_predictions"] == 2
    assert counts["officially_evaluated"] == 1
    assert counts["official_resolved"] == 1
    assert counts["official_unresolved"] == 0
    assert counts["harness_error"] == 1
    assert counts["env_error"] == 1

    denominators = metrics["denominators"]
    assert denominators["all_target_tasks"] == 3
    assert denominators["officially_evaluated"] == 1
    assert metrics["ratios"]["evaluation_coverage"]["value"] == pytest.approx(1 / 3)
    assert metrics["ratios"]["official_resolved_rate"]["value"] == pytest.approx(1 / 3)
    assert metrics["ratios"]["conditional_official_resolved_rate"]["value"] == pytest.approx(1.0)
    assert metrics["ratios"]["patch_validity"]["value"] == pytest.approx(1.0)
    # 未评测的任务不能给出"模型没修好"的结论。
    for record in report.records:
        if record.task_id != INSTANCE_A:
            assert record.resolved is None
    assert metrics["attribution_counts"]["official_resolved"] == 1
    assert metrics["attribution_counts"]["harness_internal_error"] == 1
    assert metrics["attribution_counts"]["env_error"] == 1


def test_model_claimed_success_never_becomes_official_resolution(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))

    def unresolved(evaluator: FakeEvaluator, call: EvalCall) -> SWEBenchEvaluation:
        report_path = call.run_dir / "harness-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text('{"resolved": []}', encoding="utf-8")
        return SWEBenchEvaluation(
            verdict=OfficialVerdict.UNRESOLVED,
            instance_id=call.instance_id,
            resolved=False,
            harness_run_id=call.harness_run_id,
            patch_sha256=call.patch_sha256,
            harness_version=HARNESS_VERSION,
            dataset_revision=DATASET_REVISION,
            report_path=report_path,
            report_sha256=_sha(report_path.read_text(encoding="utf-8")),
        )

    harness.evaluator.handlers[INSTANCE_A] = unresolved
    report = harness.run(resume=ResumeLevel.EVALUATION)
    metrics = report.metrics()
    assert report.records[0].agent_claims_success is True
    assert report.records[0].resolved is False
    assert metrics["counts"]["model_claimed_success"] == 1
    assert metrics["counts"]["official_resolved"] == 0
    assert metrics["counts"]["official_unresolved"] == 1


def test_cost_and_token_aggregation_per_resolved_task(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A, INSTANCE_B))

    def cheaper_unresolved(evaluator: FakeEvaluator, call: EvalCall) -> SWEBenchEvaluation:
        report_path = call.run_dir / "harness-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("{}", encoding="utf-8")
        return SWEBenchEvaluation(
            verdict=OfficialVerdict.UNRESOLVED,
            instance_id=call.instance_id,
            resolved=False,
            harness_run_id=call.harness_run_id,
            patch_sha256=call.patch_sha256,
            harness_version=HARNESS_VERSION,
            dataset_revision=DATASET_REVISION,
            report_path=report_path,
            report_sha256=_sha("{}"),
            runtime_seconds=0.5,
        )

    original_usage = harness.factory.usage

    def cheaper(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
        factory.usage = Usage(input_tokens=40, output_tokens=10, cost=0.3, llm_calls=1)
        try:
            return factory._success(call)
        finally:
            factory.usage = original_usage

    harness.evaluator.handlers[INSTANCE_B] = cheaper_unresolved
    harness.factory.handlers[INSTANCE_B] = cheaper
    report = harness.run(resume=ResumeLevel.EVALUATION)
    metrics = report.metrics()
    assert abs(metrics["cost"]["total_cost"] - 0.8) < 1e-9
    assert abs(metrics["cost"]["mean_cost_per_task"] - 0.4) < 1e-9
    assert abs(metrics["cost"]["mean_cost_per_resolved_task"] - 0.5) < 1e-9
    assert metrics["tokens"]["total"] == 170
    assert metrics["counts"]["official_resolved"] == 1
    assert metrics["counts"]["official_unresolved"] == 1


def test_empty_instance_list_renders_undefined_ratios_as_none(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    report = harness.runner.run([], seed=0, resume=ResumeLevel.TASK)
    metrics = report.metrics()
    assert metrics["counts"]["dispatched"] == 0
    assert report.stop_reason == "empty_target"
    # 分母为 0 的比例是 None，不是 0%/100%。
    assert metrics["ratios"]["official_resolved_rate"]["value"] is None
    assert metrics["ratios"]["evaluation_coverage"]["value"] is None
    assert report.records == ()


def test_report_write_produces_json_with_same_counts(tmp_path: Path) -> None:
    harness = _make_harness(tmp_path, ids=(INSTANCE_A,))
    report = harness.run(resume=ResumeLevel.EVALUATION)
    target = report.write(tmp_path / "out")
    assert target.name == "swebench-report.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["counts"] == report.metrics()["counts"]
    assert payload["denominators"]["all_target_tasks"] == 1


def test_swbench_evaluation_rejects_inconsistent_verdicts() -> None:
    with pytest.raises(ValueError, match="resolved"):
        SWEBenchEvaluation(
            verdict=OfficialVerdict.ERROR,
            instance_id=INSTANCE_A,
            resolved=True,
            harness_run_id="cp-x-0",
            patch_sha256="a" * 64,
            harness_version=HARNESS_VERSION,
            dataset_revision=DATASET_REVISION,
        )
    with pytest.raises(ValueError, match="resolved"):
        SWEBenchEvaluation(
            verdict=OfficialVerdict.RESOLVED,
            instance_id=INSTANCE_A,
            resolved=None,
            harness_run_id="cp-x-0",
            patch_sha256="a" * 64,
            harness_version=HARNESS_VERSION,
            dataset_revision=DATASET_REVISION,
        )


# =========================================================================== 跨进程恢复


def _worker_source(root: Path) -> tuple[Path, str]:
    source = root / "source"
    if (source / ".git").is_dir():
        from codepilot.repo.gitcmd import run_git

        head = run_git(["rev-parse", "HEAD"], cwd=source).stdout.strip()
        return source, head
    repo = init_repo(source, files=dict(SOURCE_FILES))
    return source, repo.commits[0]


def _worker_harness(root: Path) -> Harness:
    source, base_commit = _worker_source(root)
    loader = FakeLoader(base_commit, ids=(INSTANCE_A, INSTANCE_B))
    artifact_root = root / "artifacts"
    factory = FakeFactory(
        artifact_root,
        base_commit=base_commit,
        source_repo=source,
        log_path=root / "inference_calls.jsonl",
    )
    evaluator = FakeEvaluator()
    config = SWEBenchRunConfig(model_identity="FakeLLM", provider="fake", model="FakeLLM")
    journal_path = root / "journal.sqlite"
    runner = SWEBenchRunner(
        loader=loader,
        inference_factory=factory,
        evaluator=evaluator,
        journal_path=journal_path,
        artifact_root=artifact_root,
        config=config,
        code_commit="test-commit",
    )
    return Harness(
        tmp_path=root,
        source=source,
        base_commit=base_commit,
        loader=loader,
        factory=factory,
        evaluator=evaluator,
        runner=runner,
        config=config,
        journal_path=journal_path,
        artifact_root=artifact_root,
    )


def _write_worker_summary(path: Path, report: Any, harness: Harness) -> None:
    payload = {
        "reuse": dict(report.reuse),
        "counts": report.metrics()["counts"],
        "denominators": report.metrics()["denominators"],
        "ratios": report.metrics()["ratios"],
        "cost": report.metrics()["cost"],
        "budget": report.metrics()["budget"],
        "stop_reason": report.stop_reason,
        "resolved": [record.task_id for record in report.records if record.resolved is True],
        "inference_calls_this_process": [call.task.instance_id for call in harness.factory.calls],
        "eval_calls_this_process": [call.instance_id for call in harness.evaluator.calls],
        "journal_rows": _journal_rows(harness.journal_path),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _worker_main(mode: str, root: Path) -> int:
    harness = _worker_harness(root)
    if mode == "crash":
        # 第二个 instance 的推理刚开始就硬退出：第一个任务的结论已落 journal，
        # 第二个既没有结论也没有 checkpoint（模拟"死在批次中间"）。
        def crash_before_work(factory: FakeFactory, call: RunCall) -> InferenceOutcome:
            os._exit(7)

        harness.factory.handlers[INSTANCE_B] = crash_before_work
        harness.runner.run([INSTANCE_A, INSTANCE_B], seed=0, resume=ResumeLevel.EVALUATION)
        return 9
    if mode == "eval-error":

        def eval_crash(evaluator: FakeEvaluator, call: EvalCall) -> SWEBenchEvaluation:
            raise RuntimeError("official harness crashed (simulated)")

        harness.evaluator.handlers[INSTANCE_B] = eval_crash
        report = harness.runner.run([INSTANCE_A, INSTANCE_B], seed=0, resume=ResumeLevel.EVALUATION)
        _write_worker_summary(root / "eval-error.json", report, harness)
        return 0
    if mode == "resume":
        report = harness.runner.run([INSTANCE_A, INSTANCE_B], seed=0, resume=ResumeLevel.EVALUATION)
        _write_worker_summary(root / "resumed.json", report, harness)
        return 0
    if mode == "resume-eval":
        report = harness.runner.run([INSTANCE_A, INSTANCE_B], seed=0, resume=ResumeLevel.EVALUATION)
        _write_worker_summary(root / "resumed-eval.json", report, harness)
        return 0
    raise SystemExit(f"unknown worker mode: {mode}")


def _run_worker(mode: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode, str(root)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(ROOT),
    )


def _read_summary(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / name).read_text(encoding="utf-8"))


def test_resume_across_processes_does_not_redo_or_rebill(tmp_path: Path) -> None:
    root = tmp_path
    crashed = _run_worker("crash", root)
    assert crashed.returncode == 7, crashed.stdout + crashed.stderr
    log_path = root / "inference_calls.jsonl"
    crashed_calls = log_path.read_text(encoding="utf-8").splitlines()
    assert len(crashed_calls) == 2, crashed_calls
    assert _journal_rows(root / "journal.sqlite") == 1

    resumed = _run_worker("resume", root)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    payload = _read_summary(root, "resumed.json")
    assert payload["reuse"]["reused_tasks"] == 1
    assert payload["inference_calls_this_process"] == [INSTANCE_B]
    assert sorted(payload["resolved"]) == [INSTANCE_A, INSTANCE_B]
    # 第一个任务在崩溃进程里已经花过 0.5，恢复进程只新花第二个任务的 0.5。
    assert abs(payload["cost"]["total_cost"] - 1.0) < 1e-9
    assert abs(payload["budget"]["spent"] - 0.5) < 1e-9
    assert payload["journal_rows"] == 2
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 3


def test_evaluation_level_resume_across_processes_only_reruns_evaluation(
    tmp_path: Path,
) -> None:
    root = tmp_path
    first = _run_worker("eval-error", root)
    assert first.returncode == 0, first.stdout + first.stderr
    first_payload = _read_summary(root, "eval-error.json")
    assert first_payload["counts"]["official_resolved"] == 1
    assert first_payload["counts"]["harness_error"] == 1
    assert first_payload["journal_rows"] == 2

    resumed = _run_worker("resume-eval", root)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    payload = _read_summary(root, "resumed-eval.json")
    assert payload["reuse"]["reused_tasks"] == 1
    # 恢复进程没有重跑任何推理；只对缺评测的那个实例补跑了一次官方评测。
    assert payload["inference_calls_this_process"] == []
    assert payload["eval_calls_this_process"] == [INSTANCE_B]
    assert sorted(payload["resolved"]) == [INSTANCE_A, INSTANCE_B]
    assert abs(payload["cost"]["total_cost"] - 1.0) < 1e-9
    assert payload["budget"]["spent"] == 0.0
    assert payload["journal_rows"] == 2


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: test_swebench_runner.py <mode> <root>")
    raise SystemExit(_worker_main(sys.argv[1], Path(sys.argv[2])))
