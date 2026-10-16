"""E-08：BenchmarkRunner —— 多 seed、全局费用上限、断点续跑。

流程与 04 一致，且**复用 Phase 9 的完整 M2 图**（装配在
:mod:`codepilot.eval.assembly`，这里不实现任何修复逻辑）::

    Synthetic Suite → 加载并核对 fixture → InferenceTask → M2 LangGraph
      → 模型补丁 → 独立评测（最终 + 第一轮）→ 指标 → 汇总 → 产物

三件事在这里最容易做错，而且做错了不会报错、只会让数字变好看：

**身份绑定。** 一条结果属于 ``(suite, task_id, seed, 配置 hash, 代码 commit,
fixture 版本)``。少绑一项，续跑就会把"换个模型重跑"的结果当成"已经跑过"跳过，
或者把两个代码版本的分数混进同一张表。

**费用预算。** ``--max-total-cost`` 是**派发前**检查的整批上限，按每任务预留额扣减，
跑完再用实际费用回填。并发时不预留就会超发 —— 等第一个任务结束才发现总额爆了，
那笔钱已经花了。实际费用**未知**的任务不得按 0 记账，那等于宣布"这批没花钱"。

**续跑不重不漏。** 已完成任务从 journal 复用：不重跑、不重复计费、不重复计成功数。
journal 里的证据被改动或摘要不符时**拒绝复用**并报错，而不是"当作没跑过"重跑 ——
后者会让同一份 results.jsonl 混进两个代码版本的结果，而文件看起来是完整的。

**第一轮单独评测。** First Attempt Resolved 用第一轮实际保存的 patch 再评一次，
不从最终结果推断。推断值恒 ≥ Resolved Rate，Repair Gain 于是永远是 0，迭代修复
的贡献就测不出来了。代价是每个任务多一次评测运行，这是这个指标的定义要求的。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from codepilot import __version__ as CODEPILOT_VERSION  # noqa: N812
from codepilot.artifacts import ArtifactStore
from codepilot.eval.assembly import InferenceAssembly, InferenceOutcome, ProviderBundle
from codepilot.eval.evaluation import EvaluationVerdict, GoldEvaluator, PatchEvaluation
from codepilot.eval.metrics import LocalizationSample, TaskOutcome, VerificationMode
from codepilot.eval.synthetic import SyntheticFixture
from codepilot.repo.workspace import WorkspaceManager
from codepilot.schemas.budget import RepairBudget

__all__ = [
    "BenchmarkConfig",
    "BenchmarkRun",
    "BenchmarkRunner",
    "CostLedger",
    "ExecutionState",
    "JournalCorruptionError",
    "ResultJournal",
    "RunIdentity",
    "TaskRecord",
    "assembly_factory",
    "code_identity",
    "provider_config_hash",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def code_identity(root: Path | None = None) -> dict[str, Any]:
    """运行**本程序**的代码版本，不是目标仓库的（03 §27）。

    带 dirty 标记：未提交改动跑出来的结果若也被记成"某个 commit 的结果"，事后按那个
    commit 复现会拿不到同一份数字，而且没人知道为什么。
    """
    from codepilot.repo.gitcmd import GitError, run_git

    base = root or Path(__file__).resolve().parents[3]
    try:
        commit = run_git(["rev-parse", "HEAD"], cwd=base, check=False).stdout.strip() or "unknown"
        dirty = bool(run_git(["status", "--porcelain"], cwd=base, check=False).stdout.strip())
    except (GitError, OSError):
        commit, dirty = "unknown", False
    return {"version": CODEPILOT_VERSION, "commit": commit, "dirty": dirty}


class ExecutionState(StrEnum):
    """一个 (task, seed) 在 runner 视角下的结局。

    与"模型有没有修好"正交。``BUDGET_BLOCKED`` / ``NOT_EXECUTED`` 的任务不进任何
    成功率分子，但**必须**留在报告里，否则分母会悄悄缩小。
    """

    __test__: ClassVar[bool] = False

    COMPLETED = "completed"
    NOT_EXECUTED = "not_executed"
    BUDGET_BLOCKED = "budget_blocked"
    INFERENCE_ERROR = "inference_error"
    EVALUATION_ERROR = "evaluation_error"
    REUSED = "reused"


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """一条结果的身份。断点续跑与跨运行比较都靠它。"""

    suite: str
    task_id: str
    seed: int
    config_hash: str
    code_commit: str
    fixture_version: str

    @property
    def key(self) -> str:
        return _sha(
            _canonical(
                {
                    "suite": self.suite,
                    "task": self.task_id,
                    "seed": self.seed,
                    "config": self.config_hash,
                    "code": self.code_commit,
                    "fixture": self.fixture_version,
                }
            )
        )[:32]

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "task_id": self.task_id,
            "seed": self.seed,
            "config_hash": self.config_hash,
            "code_commit": self.code_commit,
            "fixture_version": self.fixture_version,
            "identity": self.key,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """一次 benchmark 的全部可复现输入。"""

    suite: str = "synthetic"
    seeds: tuple[int, ...] = (0, 1, 2)
    max_total_cost: float | None = None
    task_reservation: float = 0.0
    per_task_budget: RepairBudget = field(default_factory=RepairBudget)
    model_identity: str = "FakeLLM"
    provider_config_hash: str = "unconfigured"
    max_in_flight: int = 1
    budget_currency: str = "CNY"
    out_dir: Path = Path("artifacts") / "phase10"
    run_label: str = "bench"

    def __post_init__(self) -> None:
        if not self.seeds:
            raise ValueError("至少需要一个 seed")
        if min(self.seeds) < 0:
            raise ValueError("seed 不能为负")
        if self.max_in_flight < 1:
            raise ValueError("max_in_flight 至少为 1")
        if self.max_total_cost is not None and self.max_total_cost < 0:
            raise ValueError("max_total_cost 不能为负")
        if self.task_reservation < 0:
            raise ValueError("task_reservation 不能为负")
        if self.max_total_cost is not None and self.task_reservation <= 0:
            raise ValueError(
                "设置了整批上限却没有每任务预留额：并发派发会超发，顺序派发则会在最后"
                "一个任务上直接爆预算。请给 --max-cost-per-task 一个正值。"
            )

    @property
    def config_hash(self) -> str:
        """配置指纹。这里没有密钥字段，因此也就不会被顺手哈希进去。"""
        return _sha(
            _canonical(
                {
                    "suite": self.suite,
                    "seeds": list(self.seeds),
                    "max_total_cost": self.max_total_cost,
                    "task_reservation": self.task_reservation,
                    "budget": self.per_task_budget.model_dump(mode="json"),
                    "model_identity": self.model_identity,
                    "provider_config_hash": self.provider_config_hash,
                    "max_in_flight": self.max_in_flight,
                    "budget_currency": self.budget_currency,
                }
            )
        )[:32]


class CostLedger:
    """整批费用账本：先预留、后回填。

    锁保护的不是"算得准"这一件事，而是"两个 worker 同时看到还剩一份预算"这种只在
    并发下出现、事后完全查不出来的超发。
    """

    def __init__(
        self, *, max_total_cost: float | None, reservation: float, currency: str = "CNY"
    ) -> None:
        self.max_total_cost = max_total_cost
        self.reservation = reservation
        self.currency = currency
        self._spent = 0.0
        self._reserved = 0.0
        self._unknown_cost_tasks = 0
        self._blocked = 0
        self._lock = threading.Lock()

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def committed(self) -> float:
        """已花 + 已预留。派发判断只看这个数。"""
        with self._lock:
            return self._spent + self._reserved

    @property
    def unknown_cost_tasks(self) -> int:
        with self._lock:
            return self._unknown_cost_tasks

    @property
    def blocked(self) -> int:
        with self._lock:
            return self._blocked

    def reserve(self) -> bool:
        """派发前占额度。拿不到就返回 ``False``，调用方记 BUDGET_BLOCKED。"""
        with self._lock:
            if self.max_total_cost is None:
                return True
            if self._spent + self._reserved + self.reservation > self.max_total_cost:
                self._blocked += 1
                return False
            self._reserved += self.reservation
            return True

    def settle(self, actual: float | None) -> None:
        """任务结束后用实际费用替换预留。

        ``actual is None``（费用未知）**不**冲减预留，只计入 ``unknown_cost_tasks``。
        按 0 记账会让预算被系统性高估，整批上限形同虚设。
        """
        with self._lock:
            if self.max_total_cost is None:
                if actual is not None:
                    self._spent += actual
                else:
                    self._unknown_cost_tasks += 1
                return
            self._reserved = max(0.0, self._reserved - self.reservation)
            if actual is None:
                self._unknown_cost_tasks += 1
                self._reserved += self.reservation
                return
            self._spent += actual

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_total_cost": self.max_total_cost,
                "task_reservation": self.reservation,
                "currency": self.currency,
                "spent": self._spent,
                "reserved": self._reserved,
                "unknown_cost_tasks": self._unknown_cost_tasks,
                "blocked_dispatches": self._blocked,
                "exhausted": (
                    self.max_total_cost is not None
                    and self._spent + self._reserved >= self.max_total_cost
                ),
            }


class JournalCorruptionError(RuntimeError):
    """journal 里的证据不完整或被改动过 —— 拒绝复用，也拒绝静默重跑。"""


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """``results.jsonl`` 的一行：一个 (task, seed) 的完整证据。"""

    task_id: str
    seed: int
    category: str
    difficulty: str
    multi_file: bool
    has_traceback: bool
    model_identity: str
    config_hash: str
    code_commit: str
    fixture_version: str
    base_commit: str
    run_id: str
    identity: str
    execution_state: str
    evaluation_status: str
    resolved: bool | None
    first_attempt_resolved: bool | None
    agent_claims_success: bool
    empty_patch: bool
    invalid_patch: bool
    patch_sha256: str | None
    first_attempt_patch_sha256: str | None
    iterations: int | None
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    llm_calls: int | None
    cost: float | None
    cost_currency: str
    runtime_seconds: float | None
    verification_mode: str
    simulated_llm: bool
    sandbox_backend: str
    real_docker_evaluation: bool
    usage_uncertain: bool
    artifact_refs: dict[str, str] = field(default_factory=dict)
    error_details: tuple[str, ...] = ()
    finished_at: str = ""
    localization_files: tuple[str, ...] = ()
    localization_symbols: tuple[str, ...] = ()
    # 推理侧自己给出的定位候选（有序、按模型排名）。只存预测，不存 gold：gold 一旦落进
    # journal 或 ``results.jsonl``，续跑与复盘就会把"该改哪个文件"重新喂进任何读这些
    # 产物的环节。gold 在报告时由评测侧现算。

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["error_details"] = list(self.error_details)
        return payload

    @staticmethod
    def from_dict(payload: Mapping[str, Any]) -> TaskRecord:
        data = dict(payload)
        for key in ("error_details", "localization_files", "localization_symbols"):
            data[key] = tuple(data.get(key, ()))
        return TaskRecord(**data)

    @property
    def counted(self) -> bool:
        """这次观测是否产生了可用于成功率判断的结论。"""
        return self.execution_state == ExecutionState.COMPLETED.value

    def metric_view(self, localization: LocalizationSample | None = None) -> TaskOutcome:
        """转成指标层输入。定位结论由评测侧算好后传入，这里不碰 gold。"""
        return TaskOutcome(
            task_id=self.task_id,
            seed=self.seed,
            category=self.category,
            resolved=self.resolved,
            first_attempt_resolved=self.first_attempt_resolved,
            agent_claims_success=self.agent_claims_success,
            empty_patch=self.empty_patch,
            invalid_patch=self.invalid_patch,
            iterations=self.iterations,
            stop_reason=self.stop_reason,
            environment_failure=(
                self.execution_state == ExecutionState.EVALUATION_ERROR.value
                or self.execution_state == ExecutionState.INFERENCE_ERROR.value
            ),
            verification_mode=VerificationMode(self.verification_mode),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            llm_calls=self.llm_calls,
            cost=self.cost,
            runtime_seconds=self.runtime_seconds,
            cost_currency=self.cost_currency,
            localization=localization,
        )


class ResultJournal:
    """已完成结果的持久账本（沿用 Phase 9 Operation Journal 的思路）。

    与内存缓存的差别只有一条但很关键：进程被杀之后"哪些任务已经有结论"必须还能查到，
    而且查到的东西必须能证明没被动过。所以每条记录连同内容摘要一起提交，复用前先核
    摘要；核不上就抛 :class:`JournalCorruptionError`。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS results ("
                " identity TEXT PRIMARY KEY,"
                " payload TEXT NOT NULL,"
                " digest TEXT NOT NULL,"
                " finished_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def lookup(self, identity: str) -> TaskRecord | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT payload, digest FROM results WHERE identity=?", (identity,)
            ).fetchone()
        if row is None:
            return None
        self._verify(identity, row[0], row[1])
        record = TaskRecord.from_dict(json.loads(row[0]))
        if record.identity != identity:
            raise JournalCorruptionError(
                f"journal 记录身份与键不一致: {record.identity} != {identity}"
            )
        return record

    def commit(self, record: TaskRecord) -> None:
        payload = json.dumps(record.as_dict(), sort_keys=True, ensure_ascii=False, default=str)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO results (identity, payload, digest, finished_at)"
                " VALUES (?, ?, ?, ?)",
                (record.identity, payload, _sha(payload), record.finished_at),
            )

    def all(self) -> list[TaskRecord]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT identity, payload, digest FROM results").fetchall()
        out: list[TaskRecord] = []
        for identity, payload, digest in rows:
            self._verify(identity, payload, digest)
            out.append(TaskRecord.from_dict(json.loads(payload)))
        return sorted(out, key=lambda item: (item.task_id, item.seed))

    @staticmethod
    def _verify(identity: str, payload: str, digest: str) -> None:
        if _sha(payload) != digest:
            raise JournalCorruptionError(
                f"journal 记录摘要不符，证据可能已被改动: identity={identity}"
            )

    def count(self) -> int:
        with self._lock, self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM results").fetchone()[0])


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    """一次 benchmark 的全部结果。"""

    run_id: str
    config: BenchmarkConfig
    identity: dict[str, Any]
    records: tuple[TaskRecord, ...]
    ledger: dict[str, Any]
    declared_observations: int
    started_at: str
    finished_at: str
    integrity: str
    gold_files_by_task: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """``task_id -> gold 修改的文件集合``。只在报告阶段使用，不写进任何产物。

    只有文件级 gold 是从 fixture 直接得到的；符号级 gold 需要把补丁行映射回索引，
    评测侧目前不提供，因此 Function Recall@K 的分母如实为 0（``value=None``），
    而不是 0%。
    """

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self.records:
            out[record.execution_state] = out.get(record.execution_state, 0) + 1
        return dict(sorted(out.items()))

    def metric_inputs(self) -> list[TaskOutcome]:
        outcomes: list[TaskOutcome] = []
        for record in self.records:
            gold = self.gold_files_by_task.get(record.task_id)
            sample = (
                LocalizationSample(
                    task_id=record.task_id,
                    predicted_files=record.localization_files,
                    predicted_symbols=record.localization_symbols,
                    gold_files=gold,
                )
                if gold
                else None
            )
            outcomes.append(record.metric_view(sample))
        return outcomes


AssemblyFactory = Callable[[SyntheticFixture, int], InferenceAssembly]


class BenchmarkRunner:
    """编排：加载 suite → 逐 (task, seed) 推理 → 独立评测 → 汇总。

    ``assembly_factory`` 是隔离边界上的一个刻意设计：每个 (task, seed) 拿到自己的
    :class:`InferenceAssembly` 与 :class:`ProviderBundle`。共用一份会让两个任务写
    同一个 :class:`Usage` 对象，于是 A 的费用记到 B 头上 —— 顺序跑看不出来，只在
    并发下出现。
    """

    def __init__(
        self,
        *,
        config: BenchmarkConfig,
        fixtures: Sequence[SyntheticFixture],
        assembly_factory: AssemblyFactory,
        evaluator: GoldEvaluator,
        journal_directory: Path | None = None,
    ) -> None:
        self._config = config
        self._fixtures = list(fixtures)
        self._assembly_factory = assembly_factory
        self._evaluator = evaluator
        self._ledger = CostLedger(
            max_total_cost=config.max_total_cost,
            reservation=config.task_reservation,
            currency=config.budget_currency,
        )
        self._code = code_identity()
        self._started_at = datetime.now(UTC).isoformat()
        self._run_id = f"{config.run_label}-{uuid.uuid4().hex[:12]}"
        self._journal = (
            ResultJournal(Path(journal_directory) / "journal.sqlite") if journal_directory else None
        )
        self._journal_directory = Path(journal_directory) if journal_directory else None

    # ------------------------------------------------------------------ 入口

    @property
    def run_dir(self) -> Path:
        if self._journal_directory is not None:
            return self._journal_directory
        return Path(self._config.out_dir) / self._run_id

    def run(self) -> BenchmarkRun:
        directory = self.run_dir
        directory.mkdir(parents=True, exist_ok=True)
        self._journal = self._journal or ResultJournal(directory / "journal.sqlite")

        records: list[TaskRecord] = []
        pending: list[tuple[SyntheticFixture, int]] = []
        for fixture, seed in self._plan():
            identity = self._identity(fixture, seed)
            reused = self._journal.lookup(identity.key)
            if reused is not None:
                records.append(reused)
                continue
            pending.append((fixture, seed))

        if pending:
            workers = max(1, min(self._config.max_in_flight, len(pending)))
            if workers == 1:
                produced = [self._run_one(fixture, seed) for fixture, seed in pending]
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    produced = list(pool.map(lambda pair: self._run_one(*pair), pending))
            records.extend(produced)

        records.sort(key=lambda item: (item.task_id, item.seed))
        finished = datetime.now(UTC).isoformat()
        return BenchmarkRun(
            run_id=self._run_id,
            config=self._config,
            identity={
                "run_id": self._run_id,
                "suite": self._config.suite,
                "config_hash": self._config.config_hash,
                "code": self._code,
                "model_identity": self._config.model_identity,
                "provider_config_hash": self._config.provider_config_hash,
                "started_at": self._started_at,
                "finished_at": finished,
            },
            records=tuple(records),
            ledger=self._ledger.as_dict(),
            declared_observations=len(self._fixtures) * len(self._config.seeds),
            started_at=self._started_at,
            finished_at=finished,
            integrity=self._integrity(len(records)),
            gold_files_by_task={
                item.task_id: frozenset(item.evaluation.gold_files) for item in self._fixtures
            },
        )

    # ------------------------------------------------------------------ 内部

    def _plan(self) -> list[tuple[SyntheticFixture, int]]:
        """seed 在外、task 在内的固定顺序。

        顺序本身进不了身份（身份是内容 hash），但固定的派发序列让"同一配置跑两次"
        在日志与预算截停点上也可比。
        """
        return [(fixture, seed) for seed in self._config.seeds for fixture in self._fixtures]

    def _identity(self, fixture: SyntheticFixture, seed: int) -> RunIdentity:
        return RunIdentity(
            suite=self._config.suite,
            task_id=fixture.task_id,
            seed=seed,
            config_hash=self._config.config_hash,
            code_commit=self._commit_label(),
            fixture_version=fixture.evaluation.fixture_version,
        )

    def _commit_label(self) -> str:
        return f"{self._code['commit'][:12]}{'-dirty' if self._code['dirty'] else ''}"

    def _integrity(self, recorded: int) -> str:
        expected = len(self._fixtures) * len(self._config.seeds)
        if recorded == expected and self._ledger.blocked == 0:
            return "full"
        parts = [f"recorded {recorded}/{expected}"]
        if self._ledger.blocked:
            parts.append(f"budget-blocked {self._ledger.blocked}")
        if self._ledger.unknown_cost_tasks:
            parts.append(f"unknown-cost {self._ledger.unknown_cost_tasks}")
        return "partial: " + "; ".join(parts)

    def _run_one(self, fixture: SyntheticFixture, seed: int) -> TaskRecord:
        identity = self._identity(fixture, seed)
        if not self._ledger.reserve():
            return self._record(fixture, identity, state=ExecutionState.BUDGET_BLOCKED, run_id="")
        # run_id 里必须带本次 benchmark 的标识：推理现场（workspace 目录）在运行结束后
        # 刻意保留供事后核查，而 WorkspaceManager 拒绝写入非空目录。只用 task+seed+identity
        # 命名的话，第二次全新运行会一头撞进上一次留下的现场，每一个任务都变成
        # inference_error——而复跑正是评测基础设施最基本的能力要求。
        run_id = f"{self._run_id}-{identity.task_id}-s{seed}-{identity.key[:8]}"
        try:
            outcome = self._assembly_factory(fixture, seed).run(run_id=run_id, seed=seed)
        except Exception as exc:  # 单任务失败不能带走整批
            self._ledger.settle(None)
            return self._record(
                fixture,
                identity,
                state=ExecutionState.INFERENCE_ERROR,
                run_id=run_id,
                errors=(f"{type(exc).__name__}: {exc}",),
            )
        try:
            final = self._evaluator.evaluate(
                fixture, outcome.final_patch, patch_source="model_final", run_id=run_id
            )
            first = None
            if outcome.first_attempt_patch is not None:
                first = self._evaluator.evaluate(
                    fixture,
                    outcome.first_attempt_patch,
                    patch_source="model_first_attempt",
                    run_id=run_id,
                )
        except Exception as exc:  # 评测失败要记录，而不是整批退出
            self._ledger.settle(outcome.usage.cost)
            return self._record(
                fixture,
                identity,
                state=ExecutionState.EVALUATION_ERROR,
                run_id=run_id,
                outcome=outcome,
                errors=(f"{type(exc).__name__}: {exc}",),
            )
        self._ledger.settle(outcome.usage.cost if outcome.usage.llm_calls else None)
        record = self._record(
            fixture,
            identity,
            state=ExecutionState.COMPLETED,
            run_id=run_id,
            outcome=outcome,
            evaluation=final,
            first_attempt=first,
        )
        assert self._journal is not None
        self._journal.commit(record)
        return record

    def _record(
        self,
        fixture: SyntheticFixture,
        identity: RunIdentity,
        *,
        state: ExecutionState,
        run_id: str,
        outcome: InferenceOutcome | None = None,
        evaluation: PatchEvaluation | None = None,
        first_attempt: PatchEvaluation | None = None,
        errors: tuple[str, ...] = (),
    ) -> TaskRecord:
        usage = outcome.usage if outcome else None
        metadata = fixture.metadata
        blocked = state in (ExecutionState.BUDGET_BLOCKED, ExecutionState.NOT_EXECUTED)
        return TaskRecord(
            task_id=metadata.task_id,
            seed=identity.seed,
            category=metadata.category.value,
            difficulty=metadata.difficulty,
            multi_file=metadata.multi_file,
            has_traceback=metadata.has_traceback,
            model_identity=self._config.model_identity,
            config_hash=identity.config_hash,
            code_commit=identity.code_commit,
            fixture_version=identity.fixture_version,
            base_commit=metadata.base_commit,
            run_id=run_id or f"{metadata.task_id}-s{identity.seed}",
            identity=identity.key,
            execution_state=state.value,
            evaluation_status=(
                evaluation.verdict.value
                if evaluation is not None
                else "NOT_EVALUATED"
                if blocked or outcome is None
                else EvaluationVerdict.ENV_ERROR.value
            ),
            resolved=None if blocked else (evaluation.resolved if evaluation else None),
            first_attempt_resolved=(None if first_attempt is None else first_attempt.resolved),
            agent_claims_success=bool(outcome and outcome.agent_claims_success),
            empty_patch=bool(outcome and outcome.empty_patch),
            invalid_patch=bool(outcome and outcome.invalid_patch),
            patch_sha256=outcome.final_patch_sha256 if outcome else None,
            first_attempt_patch_sha256=(outcome.first_attempt_patch_sha256 if outcome else None),
            iterations=outcome.iterations if outcome else None,
            stop_reason=outcome.stop_reason if outcome else None,
            input_tokens=None if blocked else (usage.input_tokens if usage else None),
            output_tokens=None if blocked else (usage.output_tokens if usage else None),
            total_tokens=None if blocked else (usage.total_tokens if usage else None),
            llm_calls=None if blocked else (usage.llm_calls if usage else None),
            # 没跑过 LLM 的任务费用是"未知"，不是 0：0 会让整批成本合计看起来更便宜。
            cost=(usage.cost if usage is not None and usage.llm_calls and not blocked else None),
            cost_currency=self._config.budget_currency,
            runtime_seconds=outcome.runtime_seconds if outcome else None,
            verification_mode=outcome.verification_mode if outcome else VerificationMode.NONE.value,
            simulated_llm=bool(outcome.simulated_llm) if outcome else self._config_is_fake(),
            sandbox_backend=(
                evaluation.sandbox_backend
                if evaluation is not None
                else (outcome.sandbox_backend if outcome else "none")
            ),
            real_docker_evaluation=bool(
                evaluation is not None and evaluation.sandbox_backend.startswith("docker")
            ),
            usage_uncertain=bool(outcome.usage_uncertain) if outcome else True,
            localization_files=tuple(outcome.localization_files) if outcome else (),
            localization_symbols=tuple(outcome.localization_symbols) if outcome else (),
            artifact_refs={
                **({"inference_run_dir": Path(outcome.artifact_dir).name} if outcome else {}),
                **(evaluation.artifact_refs if evaluation else {}),
            },
            error_details=(
                errors
                + tuple(outcome.failure_reasons if outcome else ())
                + tuple(
                    f"missing:{item}" for item in (evaluation.missing_tests if evaluation else ())
                )
            ),
            finished_at=datetime.now(UTC).isoformat(),
        )

    def _config_is_fake(self) -> bool:
        return self._config.model_identity == "FakeLLM"


def provider_config_hash(payload: Mapping[str, Any]) -> str:
    """LLM 配置的指纹。密钥不进来：这里只接受已脱敏的结构。"""
    return _sha(_canonical(payload))[:32]


#: 允许出现在配置文件里的键。多写一个就报错，而不是静默忽略一个拼错的预算字段——
#: 后者表现为"我明明设了 max_total_cost，为什么没拦住"。
_CONFIG_KEYS = frozenset({"suite", "seeds", "budget", "limits", "output"})
_BUDGET_KEYS = frozenset(RepairBudget.model_fields)
_LIMIT_KEYS = frozenset({"max_total_cost", "max_cost_per_task", "max_in_flight"})
_OUTPUT_KEYS = frozenset({"dir", "label"})


def load_bench_config(
    path: Path,
    *,
    seeds_override: Sequence[int] | None = None,
    max_total_cost_override: float | None = None,
    max_in_flight_override: int | None = None,
    model_identity: str = "FakeLLM",
    provider_config: str = "unconfigured",
) -> BenchmarkConfig:
    """读 ``configs/full.yaml`` 并映射成 :class:`BenchmarkConfig`。

    ``max_cost_per_task`` 的缺省值有个真实理由：只给 ``max_total_cost`` 而不给预留额
    会让 :class:`BenchmarkConfig` 直接拒绝构造（那是对的），但用户不该被迫每次都写两个
    数。这里退到"单任务上限"，再退到"上限的 1/8"——一个保守但不为零的占位，
    宁可少跑几个任务，也不要在无人值守时把预算花超。
    """
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件顶层必须是对象: {path}")
    unknown = set(raw) - _CONFIG_KEYS
    if unknown:
        raise ValueError(f"未知配置项: {sorted(unknown)}；支持 {sorted(_CONFIG_KEYS)}")

    budget_payload = dict(raw.get("budget") or {})
    extra_budget = set(budget_payload) - _BUDGET_KEYS
    if extra_budget:
        raise ValueError(f"budget 下未知字段: {sorted(extra_budget)}")
    budget = RepairBudget(**budget_payload)

    limits = dict(raw.get("limits") or {})
    extra_limits = set(limits) - _LIMIT_KEYS
    if extra_limits:
        raise ValueError(f"limits 下未知字段: {sorted(extra_limits)}")
    total_cost = limits.get("max_total_cost")
    if max_total_cost_override is not None:
        total_cost = max_total_cost_override
    reservation = limits.get("max_cost_per_task")
    if reservation is None:
        reservation = budget.max_cost
        if reservation is None and total_cost is not None:
            reservation = float(total_cost) / 8.0
    if reservation is None:
        reservation = 0.0

    output = dict(raw.get("output") or {})
    extra_output = set(output) - _OUTPUT_KEYS
    if extra_output:
        raise ValueError(f"output 下未知字段: {sorted(extra_output)}")
    in_flight = max_in_flight_override or int(limits.get("max_in_flight") or 1)

    seeds = tuple(seeds_override) if seeds_override else tuple(raw.get("seeds") or (0, 1, 2))
    return BenchmarkConfig(
        suite=str(raw.get("suite") or "synthetic"),
        seeds=seeds,
        max_total_cost=None if total_cost is None else float(total_cost),
        task_reservation=float(reservation),
        per_task_budget=budget,
        model_identity=model_identity,
        provider_config_hash=provider_config,
        max_in_flight=in_flight,
        budget_currency=str(budget.budget_currency),
        out_dir=Path(str(output.get("dir") or "artifacts/phase10")),
        run_label=str(output.get("label") or "bench"),
    )


def assembly_factory(
    *,
    work_root: Path,
    cache_root: Path,
    sandbox_factory: Callable[[ArtifactStore], Any],
    providers: Callable[[SyntheticFixture, int], ProviderBundle],
    evaluator: GoldEvaluator,
    budget: RepairBudget | None = None,
) -> AssemblyFactory:
    """生产装配工厂。

    ``repository`` 复用 :meth:`GoldEvaluator.materialized_repository` 而不是自己再
    材料化一次：``materialize`` 拒绝写入非空目录，两边各建一份会在第二次跑同一个
    fixture 时直接抛错，而那份错误看起来像"磁盘上有个坏缓存"，其实只是两处各自
    为政。
    """
    manager = WorkspaceManager(cache_root=cache_root)

    def factory(fixture: SyntheticFixture, seed: int) -> InferenceAssembly:
        # 同一 fixture 的多个 seed 共用同一份只读材料化仓库：材料化按内容寻址，
        # 与 seed 无关，重复产出只会多花时间不会多占磁盘。
        task = fixture.inference(evaluator.materialized_repository(fixture))
        return InferenceAssembly(
            task=task,
            workspace_manager=manager,
            work_root=work_root,
            sandbox_factory=sandbox_factory,
            provider_bundle=providers(fixture, seed),
            budget=budget or RepairBudget(),
        )

    return factory
