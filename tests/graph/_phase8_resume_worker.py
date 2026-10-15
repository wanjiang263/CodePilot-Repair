"""G-13 / G-23 的子进程 worker：真实地把进程杀掉，再在**新进程**里恢复。

必须由子进程跑，因为"恢复"要证的东西正是"内存全没了还能不能接上"：
同一进程内重新调用一次函数，``deps.usage``、Sandbox 计数器、LangGraph 的编译产物
全都还在内存里，那种测试会通过，但对真实崩溃没有任何证明力。

``crash`` 模式在第 1 轮目标测试已经落盘之后、``diagnose`` 节点真正开始工作之前用
``os._exit`` 杀掉自己 —— 不等清理、不走 ``finally``、不 commit 任何东西。
崩溃点选在这里不是随便挑的：它是"外部副作用已完成 + checkpoint 已写入"这一侧最
有意义的时机，也正是 ``durability="sync"`` 必须保证的东西。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    FINISH,
    ROUND1_FIXTURE,
    assertion_failure,
    build_harness,
    diagnosis,
    passed_result,
)

from codepilot.graph.checkpoint import (
    checkpoint_state,
    open_checkpointer,
    pending_nodes,
    resume_run,
    run_with_checkpoint,
)

SPEC = "resume_report.json"


class _KillSwitch:
    """被调用时直接把进程打死，模拟"节点刚开始就没了"。"""

    def diagnose(self, *args: object, **kwargs: object) -> object:
        sys.stdout.flush()
        os._exit(7)


def _checkpoint_path(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "runs" / "phase8-run" / "checkpoints.sqlite"


def report(tmp_path: Path, **payload: object) -> None:
    (tmp_path / SPEC).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def crash(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3")],
        coder_rounds=[[ROUND1_FIXTURE, FINISH]],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
    )
    harness.deps.diagnoser = _KillSwitch()

    async def go() -> None:
        async with await open_checkpointer(_checkpoint_path(tmp_path), "phase8-run") as handle:
            await run_with_checkpoint(harness.initial, harness.deps, handle)

    asyncio.run(go())


def inspect(tmp_path: Path) -> None:
    """只读 checkpoint，不跑图：崩溃后"到底存下了什么"。"""
    # pending_nodes 需要一张已编译的图来算"下一步该跑哪个节点"；接回的是崩溃进程
    # 留下的 Workspace 与索引，不是重新搭一份现场。
    probe = build_harness(tmp_path, results=[], baseline_results=[], coder_rounds=[], reuse=True)

    async def look() -> dict[str, object]:
        async with await open_checkpointer(_checkpoint_path(tmp_path), "phase8-run") as handle:
            return {
                "state": await checkpoint_state(handle.saver, "phase8-run"),
                "pending": await pending_nodes(probe.deps, handle, "phase8-run"),
            }

    seen = asyncio.run(look())
    state = seen["state"]
    report(
        tmp_path,
        has_state=state is not None,
        pending=list(seen["pending"]),
        attempts=len(state.attempts) if state else 0,
        test_runs=state.test_runs if state else None,
        llm_calls=state.usage.llm_calls if state else None,
        cost=state.usage.cost if state else None,
        status=state.status.value if state else None,
        events=[item["node"] for item in state.events] if state else [],
        patch_hash=state.attempts[-1].patch_hash if state and state.attempts else None,
    )


def resume(tmp_path: Path) -> None:
    """新进程：接回同一份 checkpoint 继续跑完。"""
    harness = build_harness(
        tmp_path,
        # 崩溃后还没执行过的那一次 diagnose + 第 2 轮目标测试。
        # 基线不再重复：它在崩溃前已经完成并计费。
        results=[passed_result("tests/test_core.py::test_normalize")],
        baseline_results=[],
        reuse=True,
        coder_rounds=[
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 2",
                        "replace": "    return value",
                    },
                },
                FINISH,
            ]
        ],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
    )

    async def go() -> object:
        async with await open_checkpointer(_checkpoint_path(tmp_path), "phase8-run") as handle:
            return await resume_run(harness.deps, handle, "phase8-run")

    final = asyncio.run(go())
    report(
        tmp_path,
        status=final.status.value,
        stop_reason=final.stop_reason.value if final.stop_reason else None,
        attempts=[
            {
                "iteration": item.iteration,
                "patch_hash": item.patch_hash,
                "executed": item.execution.status.value if item.execution else None,
            }
            for item in final.attempts
        ],
        test_runs=final.test_runs,
        llm_calls=final.usage.llm_calls,
        events=[item["node"] for item in final.events],
        workspace_source=(harness.workspace / "src/pkg/core.py").read_text(encoding="utf-8"),
        purposes=harness.llm.purposes(),
    )


if __name__ == "__main__":
    mode, root = sys.argv[1], Path(sys.argv[2])
    {"crash": crash, "inspect": inspect, "resume": resume}[mode](root)
