"""SQLite Checkpoint 与进程中断恢复（02 §15.7、G-12、G-13）。

## 为什么不能只看 "state 存下来了"

LangGraph 的 checkpoint 只保证 **state** 持久化。Workspace 里的补丁、Docker 里
跑出来的结果、Artifact 目录里的 JUnit 都是 checkpoint **之外**的东西。进程被硬杀
之后完全可能出现"checkpoint 写着第 2 轮测试 PASS，但 Artifact 已经不在了"——
这时如果照单恢复，就等于把一份拿不出证据的 PASS 当成修复成功的结论。

所以恢复流程是三段，缺一不可：

1. :func:`checkpoint_state` —— 按 ``thread_id = run_id`` 读回 State。
2. :func:`verify_recovery` —— 核对 Workspace 是否存在、base commit 是否一致、
   当前补丁哈希是否与 checkpoint 相符、已记录的执行证据是否仍可读。
3. :func:`resume_run` —— 只对**证据仍然成立**的部分做复用；证据失效就关掉复用
   开关，让 ``run_target_tests`` 重新执行并**重新计费**。

## 不宣称 exactly-once

如果崩溃发生在"外部副作用已经做了、checkpoint 还没写"这个窗口里，恢复后这一轮
会重做。幂等身份由 ``run_id`` + iteration + patch hash + 目标测试集合组成，用来
判定"要不要重做"，但它给不了 exactly-once 保证——一个没有两阶段提交的系统这样
宣称就是假的。这里承诺的是 **at-most-once-with-verified-evidence**：证据有效就
不重放，证据无效就重来一次。

## msgpack 白名单

LangGraph 默认用 ormsgpack 序列化 checkpoint，未登记的自定义类型会打印
"will be blocked in a future version"。依赖这个默认行为意味着未来某天升级会
让历史 run 的 checkpoint 突然读不出来。因此显式传入由
:func:`state_allowed_modules` 从 ``RepairState`` 的注解里推导出的白名单，
并用 ``LANGGRAPH_STRICT_MSGPACK=true`` 测一遍真实往返。
"""

from __future__ import annotations

import enum
import importlib
import json
import re
import sqlite3
import typing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel

from codepilot import __version__
from codepilot.graph.builder import (
    _report_recursion_overflow,
    build_graph,
    graph_config,
)
from codepilot.graph.deps import Deps
from codepilot.graph.stop_rules import patch_hash
from codepilot.observability.events import EventType
from codepilot.repo.gitcmd import GitError, run_git
from codepilot.schemas.enums import RunStatus, StopReason
from codepilot.schemas.state import RepairState

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointManifest",
    "RecoveryReport",
    "open_checkpointer",
    "read_manifest",
    "resume_run",
    "run_with_checkpoint",
    "state_allowed_modules",
    "verify_recovery",
]

#: checkpoint 的 schema 版本。RepairState 增加带默认值的可选字段不必 +1；
#: 改字段语义必须 +1，并让旧 run 的恢复显式失败而不是静默误读。
CHECKPOINT_SCHEMA_VERSION = 1

_MANIFEST = "checkpoint_manifest.json"

#: 只匹配明确的高置信秘密形态，避免把十六进制哈希误判成密钥。
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*\S{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*[\"']?[A-Za-z0-9_\-\.]{12,}"
    ),
)


def _walk(annotation: Any, found: set[tuple[str, str]]) -> None:
    """递归收集注解里出现的 Pydantic 模型与枚举类型。"""
    origin = typing.get_origin(annotation)
    if origin is not None:
        for arg in typing.get_args(annotation):
            _walk(arg, found)
        return
    if isinstance(annotation, type) and issubclass(annotation, (BaseModel, enum.Enum)):
        found.add((annotation.__module__, annotation.__name__))


def state_allowed_modules(
    model: type[BaseModel] = RepairState,
) -> tuple[tuple[str, str], ...]:
    """``RepairState`` 可达的全部自定义类型，作为 msgpack 白名单。

    从注解里**推导**而不是手写清单：手写清单会在下一次给 state 加字段时漏掉新类型，
    而漏掉的后果是历史 checkpoint 读不出来——恰好是升级时最不该发生的故障。
    """
    found: set[tuple[str, str]] = {(model.__module__, model.__name__)}
    for field in model.model_fields.values():
        _walk(field.annotation, found)
        if field.default is not None and isinstance(field.default, BaseModel):
            found.add((type(field.default).__module__, type(field.default).__name__))
    for nested in list(found):
        target = _resolve(nested)
        if target is not None and issubclass(target, BaseModel):
            for field in target.model_fields.values():
                _walk(field.annotation, found)
    return tuple(sorted(found))


def _resolve(key: tuple[str, str]) -> type[Any] | None:
    try:
        module = importlib.import_module(key[0])
        return getattr(module, key[1], None)
    except (ImportError, AttributeError):
        return None


def _serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=state_allowed_modules())


@dataclass(slots=True)
class CheckpointHandle:
    """一次运行的 checkpointer 与其文件；必须由持有者关闭。"""

    saver: AsyncSqliteSaver
    connection: aiosqlite.Connection
    path: Path
    run_id: str

    async def close(self) -> None:
        await self.connection.commit()
        await self.connection.close()

    async def __aenter__(self) -> CheckpointHandle:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


async def open_checkpointer(path: Path, run_id: str) -> CheckpointHandle:
    """打开（或创建）某个 run 的 SQLite checkpoint 文件。

    路径稳定：``artifacts/runs/<run_id>/checkpoints.sqlite``。放在 run 目录里而不是
    进程临时目录，是"换进程还能找到它"的前提；也与既有 Artifact 布局一致，
    不会和 ``attempts/`` 抢位置。

    用的是 ``AsyncSqliteSaver``。节点是 ``async def``（03 §5 的硬约束），而
    LangGraph 只有 ``ainvoke`` 能跑异步节点；同步的 ``SqliteSaver`` 在 ``ainvoke``
    下会直接 ``NotImplementedError``。``aiosqlite`` 是 ``langgraph-checkpoint-sqlite``
    自带的依赖，不需要新增包。

    必须是协程：``AsyncSqliteSaver.__init__`` 会抓住当前 running loop，在没有 loop
    的上下文里构造会得到 ``RuntimeError: no running event loop``。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = aiosqlite.connect(str(target))
    saver = AsyncSqliteSaver(connection, serde=_serde())
    return CheckpointHandle(saver=saver, connection=connection, path=target, run_id=run_id)


@dataclass(frozen=True, slots=True)
class CheckpointManifest:
    """写在 checkpoint 旁边的最小配置记录（不含密钥）。

    版本与 base commit 决定了一个 checkpoint 能不能被当前代码安全解释；
    没有这两条，恢复就是在拿旧 schema 猜新代码。
    """

    run_id: str
    schema_version: int
    base_commit: str
    checkpoint_path: str
    code_version: str
    allowed_types: int

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)


def read_manifest(path: Path) -> dict[str, Any] | None:
    target = Path(path).with_name(_MANIFEST)
    if not target.is_file():
        return None
    return dict(json.loads(target.read_text(encoding="utf-8")))


def write_manifest(handle: CheckpointHandle, state: RepairState, *, code_version: str) -> Path:
    manifest = CheckpointManifest(
        run_id=state.run_id,
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        base_commit=state.base_commit,
        checkpoint_path=str(handle.path),
        code_version=code_version,
        allowed_types=len(state_allowed_modules()),
    )
    target = handle.path.with_name(_MANIFEST)
    target.write_text(manifest.to_json(), encoding="utf-8")
    return target


def assert_checkpoint_safe(state: RepairState) -> None:
    """拒绝把疑似未脱敏的秘密写进 checkpoint。

    Checkpoint 文件会和 Artifact 一起被人复制走排查问题；一个躺在 state 里的
    ``sk-...`` 会跟着目录流传到别人的机器上，而它看起来只是"一次调试快照"。

    这里用**模式匹配**而不是"整份 JSON 过一遍 redact 再比较"：后者的误报率太高
    （``sha256``、``patch_hash``、symbol id 都是十六进制），一次误报就让整次运行
    拒绝落盘，比漏报更难排查。节点写入 ``Attempt.patch_diff`` 时已经单独过了
    :func:`redact_text`。
    """
    payload = state.model_dump_json()
    for pattern in _SECRET_PATTERNS:
        if pattern.search(payload):
            raise ValueError(
                f"RepairState appears to contain an unredacted secret ({pattern.pattern}); "
                "refusing to checkpoint"
            )


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """恢复前的证据核对结果。"""

    workspace_present: bool
    base_commit_matches: bool
    patch_identity_matches: bool
    artifacts_present: bool
    reusable_execution_evidence: bool
    problems: tuple[str, ...]
    phase9_consistent: bool = True

    @property
    def can_continue(self) -> bool:
        """Workspace 与 base commit 是继续执行的硬前提。"""
        return self.workspace_present and self.base_commit_matches and self.phase9_consistent

    @property
    def can_reuse_evidence(self) -> bool:
        return (
            self.can_continue and self.reusable_execution_evidence and self.patch_identity_matches
        )


def _workspace_head(deps: Deps) -> str | None:
    try:
        return run_git(["rev-parse", "HEAD"], cwd=deps.require_workspace()).stdout.strip()
    except (GitError, OSError, ValueError):
        return None


def verify_recovery(state: RepairState, deps: Deps) -> RecoveryReport:
    """核对 checkpoint 之外的三类外部证据（G-13 的恢复验证要求）。"""
    problems: list[str] = []

    try:
        present = deps.require_workspace().is_dir()
    except Exception as exc:  # 恢复核对必须把任何失败变成事实陈述，而不是抛出
        present = False
        problems.append(f"workspace unavailable: {type(exc).__name__}: {exc}")

    head = _workspace_head(deps) if present else None
    base_ok = head is not None and head.startswith(state.base_commit)
    if present and not base_ok:
        problems.append(f"workspace HEAD {head!r} does not match base commit {state.base_commit!r}")

    attempt = state.current_attempt
    patch_ok = True
    if present and attempt is not None and attempt.patch_hash:
        try:
            current = patch_hash(_diff_at(deps, state))
        except Exception as exc:
            current = None
            problems.append(f"cannot re-hash workspace patch: {type(exc).__name__}: {exc}")
        patch_ok = current == attempt.patch_hash
        if not patch_ok:
            problems.append(
                f"workspace patch {current!r} differs from checkpoint patch "
                f"{attempt.patch_hash!r}; recorded evidence is stale"
            )

    artifacts_ok = True
    evidence_ok = attempt is not None and attempt.execution is not None
    if evidence_ok and attempt is not None and attempt.execution is not None:
        ref = attempt.execution.artifact_dir
        if not ref or not deps.artifacts.exists(ref):
            evidence_ok = False
            artifacts_ok = False
            problems.append(
                f"artifact evidence {ref!r} for iteration {attempt.iteration} is missing; "
                "its PASS cannot be reused"
            )

    phase9_ok = True
    if (
        (deps.artifacts.run_dir / "operations.sqlite").exists()
        or state.regression is not None
        or state.review is not None
    ):
        from codepilot.graph.operations import verify_journal

        try:
            verify_journal(state, deps)
        except (ValueError, RuntimeError, OSError, sqlite3.DatabaseError) as exc:
            phase9_ok = False
            problems.append(f"Phase 9 evidence invalid: {exc}")

    return RecoveryReport(
        phase9_consistent=phase9_ok,
        workspace_present=present,
        base_commit_matches=base_ok,
        patch_identity_matches=patch_ok,
        artifacts_present=artifacts_ok,
        reusable_execution_evidence=evidence_ok,
        problems=tuple(problems),
    )


def _diff_at(deps: Deps, state: RepairState) -> str:
    from codepilot.patch.diff import generate_git_diff

    return generate_git_diff(
        deps.require_workspace(), state.base_commit, policy=deps.validator.policy
    ).text


async def checkpoint_state(saver: AsyncSqliteSaver, run_id: str) -> RepairState | None:
    """按 ``thread_id = run_id`` 读回最新 checkpoint 里的 ``RepairState``。"""
    tuple_ = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
    if tuple_ is None:
        return None
    values = tuple_.checkpoint.get("channel_values", {})
    if not values:
        return None
    return RepairState.model_validate(dict(values))


async def pending_nodes(deps: Deps, handle: CheckpointHandle, run_id: str) -> tuple[str, ...]:
    """checkpoint 里"接下来要执行的节点"。空元组意味着这一轮已经跑完。

    读的是 ``graph.aget_state().next``，不是原始 ``checkpoint["next"]`` 字段：
    后者在 LangGraph 里并不可靠（实测崩溃后为 ``()``，而 ``get_state`` 能算出
    真正待执行的节点）。编译一次图不执行任何节点，因此这个读操作没有副作用。
    """
    graph = build_graph(deps, checkpointer=handle.saver)
    snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
    return tuple(snapshot.next or ())


async def run_with_checkpoint(
    initial: RepairState,
    deps: Deps,
    handle: CheckpointHandle,
    *,
    recursion_limit: int | None = None,
) -> RepairState:
    """带 checkpoint 的完整执行入口。

    刻意是协程而不是"同步外壳 + :func:`asyncio.run`"：调用方通常已经在一个
    ``async with await open_checkpointer(...)`` 的循环里，再套一层 ``asyncio.run``
    会直接 ``RuntimeError: This event loop is already running``。
    """
    assert_checkpoint_safe(initial)
    write_manifest(handle, initial, code_version=_code_version())
    return await _ainvoke_checkpointed(
        deps,
        handle,
        payload=initial.model_dump(mode="python"),
        recursion_limit=recursion_limit,
    )


async def _ainvoke_checkpointed(
    deps: Deps,
    handle: CheckpointHandle,
    *,
    payload: dict[str, Any] | None,
    recursion_limit: int | None,
) -> RepairState:
    """异步执行。节点是 ``async def``，用同步 ``invoke`` 会直接 TypeError。

    recursion_limit 命中时与 :func:`codepilot.graph.builder.run_graph` 走同一个
    安全终止路径：回读最后一次落盘的状态，把已经发生的用量如实带出来。
    """
    graph = build_graph(deps, checkpointer=handle.saver)
    anchor = RepairState.model_validate(payload) if payload else _anchor(deps)
    config = graph_config(deps, anchor, recursion_limit=recursion_limit)
    try:
        result = await graph.ainvoke(payload, config, durability="sync")
    except GraphRecursionError as exc:
        return await _report_recursion_overflow(anchor, deps, config, exc, graph=graph)
    return RepairState.model_validate(result)


def _anchor(deps: Deps) -> RepairState:
    """恢复路径只需要 run/task/base 来生成 config，不消费其它字段。"""
    return RepairState(
        task_id=deps.task_id,
        run_id=deps.run_id,
        repository_id="",
        base_commit=deps.base_commit,
        issue="",
        started_at=deps.clock(),
    )


async def resume_run(
    deps: Deps,
    handle: CheckpointHandle,
    run_id: str,
    *,
    recursion_limit: int | None = None,
) -> RepairState:
    """从 checkpoint 恢复同一个 run_id，并从不一致的节点继续。

    调用方必须是**新进程**（或至少是重新构造的 Deps）：恢复的意义就在于验证
    "内存里的计数器归零之后还能不能正确接续"，同进程内模拟函数重复调用证明不了它。
    """
    stored = await checkpoint_state(handle.saver, run_id)
    if stored is None:
        raise ValueError(f"run_id={run_id!r} 没有可用的 checkpoint")
    if stored.status is not RunStatus.RUNNING:
        # 已经跑到终态的 run 不需要恢复；重跑会把已停止的外部副作用再来一遍。
        return stored

    report = verify_recovery(stored, deps)
    deps.bind(stored)
    if not report.can_continue:
        # Workspace 没了或指向了别的 commit：继续跑会产出一个和 checkpoint
        # 不属于同一个世界的补丁，安全终止而不是"将就着跑完"。
        message = "; ".join(report.problems) or "recovery verification failed"
        deps.events.emit(node="resume", type=EventType.STOP, status="refused", error=message)
        refused = stored.model_copy(
            update={
                "usage_uncertain": stored.usage_uncertain or not report.phase9_consistent,
                "status": RunStatus.ERROR,
                "stop_reason": StopReason.WORKFLOW_ERROR,
                "failure_reasons": [*stored.failure_reasons, f"resume refused: {message}"],
                "events": [
                    *stored.events,
                    {
                        "seq": len(stored.events) + 1,
                        "node": "resume",
                        "status": "refused",
                        "error": message,
                    },
                ],
            }
        )

        if not report.phase9_consistent:
            from codepilot.report import RunReport, write_run_report

            failure_report = RunReport.from_state(
                refused,
                repository=deps.repository,
                git_diff=refused.current_patch,
                modified_files=[],
                simulated_llm=deps.simulated_llm,
            )
            paths = write_run_report(failure_report, deps.artifacts, refused)
            refused = refused.model_copy(
                update={"report_artifacts": {**refused.report_artifacts, **paths}}
            )
        return refused

    # 证据不成立时关掉复用开关：这一轮会被重新执行并重新计费，
    # 但绝不会把拿不出证据的 PASS 继续当成结论。
    deps.evidence_trusted = report.can_reuse_evidence

    waiting = await pending_nodes(deps, handle, run_id)
    if not waiting:
        # 状态还是 running，但图认为没有待执行节点：这是 checkpoint 与代码
        # 分支不一致（路由被改过、或写入被截断）。LangGraph 这时会抛
        # EmptyInputError，信息量为零，所以提前给出可行动的原因。
        message = (
            f"run_id={run_id!r} is marked running but the graph has no pending node; "
            "refusing to replay a partially-persisted run"
        )
        deps.events.emit(node="resume", type=EventType.STOP, status="refused", error=message)
        return stored.model_copy(
            update={
                "status": RunStatus.ERROR,
                "stop_reason": StopReason.WORKFLOW_ERROR,
                "failure_reasons": [*stored.failure_reasons, message],
            }
        )

    return await _ainvoke_checkpointed(deps, handle, payload=None, recursion_limit=recursion_limit)


def _code_version() -> str:
    """CodePilot 自身的版本标记（03 §27：git commit + dirty flag）。

    记录的是**运行代码**的版本而不是目标仓库的：没有它，事后无法判断某份
    checkpoint 是哪一版状态机写出来的，而 Phase 8 的路由改动相当频繁。
    """
    try:
        root = Path(__file__).resolve().parents[3]
        commit = run_git(["rev-parse", "--short", "HEAD"], cwd=root).stdout.strip()
        dirty = bool(run_git(["status", "--porcelain"], cwd=root).stdout.strip())
    except (GitError, OSError):
        commit, dirty = "unknown", False
    suffix = "-dirty" if dirty else ""
    return f"{__version__}+{commit}{suffix}"
