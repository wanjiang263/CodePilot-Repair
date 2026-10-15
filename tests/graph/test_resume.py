"""G-12 / G-13 / G-23：SQLite Checkpoint、崩溃恢复与恢复验证。

分两层：

1. **读回**（G-12）：证明 ``thread_id = run_id``、多 run 隔离、``RepairState``
   能序列化并还原、Schema 版本有记录、疑似密钥不会进 checkpoint。
2. **独立进程崩溃恢复**（G-13 / G-23）：子进程跑到第 1 轮目标测试落盘后用
   ``os._exit`` 自杀，再由另一个新进程读同一份 SQLite 继续跑完。这一层才谈得上
   "进程中断恢复"，同进程内模拟函数重复调用证明不了任何事。

checkpoint 这套 API 全异步：节点是 ``async def``（03 §5），LangGraph 只有
``ainvoke`` 能跑异步节点，而 ``AsyncSqliteSaver`` 又必须在 running loop 里构造。
下面这些 ``asyncio.run`` 外壳只是让测试可读，每块内部都是同一个事件循环。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from harness import (
    FINISH,
    ROUND1_FIXTURE,
    assertion_failure,
    build_harness,
    diagnosis,
    passed_result,
)

from codepilot.graph.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    assert_checkpoint_safe,
    checkpoint_state,
    open_checkpointer,
    pending_nodes,
    read_manifest,
    resume_run,
    run_with_checkpoint,
    state_allowed_modules,
    verify_recovery,
)
from codepilot.graph.routing import Node
from codepilot.schemas.enums import RunStatus
from codepilot.schemas.state import RepairState

WORKER = Path(__file__).with_name("_phase8_resume_worker.py")
RUN_ID = "phase8-run"


def _harness(tmp_path: Path):
    return build_harness(
        tmp_path,
        results=[
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
            passed_result("tests/test_core.py::test_normalize"),
        ],
        coder_rounds=[
            [ROUND1_FIXTURE, FINISH],
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
            ],
        ],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
    )


def _checkpoint(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "runs" / RUN_ID / "checkpoints.sqlite"


def _run(tmp_path: Path):
    """跑完整张图并落 checkpoint，返回 ``(harness, 图自己的最终状态)``。

    只能装配一次：第二次 ``build_harness(reuse=False)`` 会在同一个 ``tmp_path`` 上
    重新 ``mkdir`` 夹具仓库，Windows 直接抛 ``FileExistsError``。
    """
    harness = _harness(tmp_path)

    async def go() -> RepairState:
        async with await open_checkpointer(_checkpoint(tmp_path), RUN_ID) as handle:
            return await run_with_checkpoint(harness.initial, harness.deps, handle)

    return harness, asyncio.run(go())


def _read(tmp_path: Path, harness, *, name: str = RUN_ID) -> tuple[Any, tuple[str, ...]]:
    """在**另一次连接**里读回状态与待执行节点。"""
    harness_ref = [harness]

    async def look() -> tuple[Any, tuple[str, ...]]:
        async with await open_checkpointer(_checkpoint(tmp_path), RUN_ID) as handle:
            state = await checkpoint_state(handle.saver, name)
            waiting = await pending_nodes(harness_ref[0].deps, handle, name)
        return state, waiting

    return asyncio.run(look())


def _resume(harness, tmp_path: Path) -> RepairState:
    async def go() -> RepairState:
        async with await open_checkpointer(_checkpoint(tmp_path), RUN_ID) as handle:
            return await resume_run(harness.deps, handle, RUN_ID)

    return asyncio.run(go())


# ------------------------------------------------------------------ G-12


def test_checkpoint_can_be_read_back_as_the_same_state(tmp_path: Path) -> None:
    harness, final = _run(tmp_path)
    stored, waiting = _read(tmp_path, harness)

    assert stored is not None, "checkpoint held no RepairState at all"
    assert stored.status is RunStatus.PROVISIONALLY_VERIFIED
    assert len(stored.attempts) == 2
    assert stored.attempts[0].patch_hash != stored.attempts[1].patch_hash
    assert stored.attempts[0].execution is not None
    assert stored.attempts[0].execution.status.value == "FAIL"
    assert stored.plan is not None and stored.issue_analysis is not None
    # 从 checkpoint 读回来的状态与图返回的完全同一份。
    assert stored.model_dump_json() == final.model_dump_json()
    assert waiting == ()


def test_thread_id_is_the_run_id_so_runs_cannot_see_each_other(tmp_path: Path) -> None:
    harness, _ = _run(tmp_path)
    stored, _ = _read(tmp_path, harness, name="some-other-run")
    assert stored is None


def test_graph_finishes_and_leaves_no_pending_node(tmp_path: Path) -> None:
    harness, _ = _run(tmp_path)
    _, waiting = _read(tmp_path, harness)
    assert waiting == ()


def test_checkpoint_manifest_records_schema_and_code_version(tmp_path: Path) -> None:
    _, final = _run(tmp_path)
    manifest = read_manifest(_checkpoint(tmp_path))
    assert manifest is not None
    assert manifest["schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert manifest["run_id"] == final.run_id
    assert manifest["base_commit"] == final.base_commit
    assert manifest["code_version"]


def test_two_runs_stay_isolated_in_one_checkpoint_file(tmp_path: Path) -> None:
    """多个 run_id 共用一个文件也必须互不可见（benchmark 会并发跑）。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result("tests/test_core.py::test_normalize")],
        coder_rounds=[[ROUND1_FIXTURE, FINISH]],
    )
    shared = tmp_path / "shared-checkpoints.sqlite"

    async def go() -> tuple[Any, Any]:
        async with await open_checkpointer(shared, harness.deps.run_id) as handle:
            await run_with_checkpoint(harness.initial, harness.deps, handle)
            mine = await checkpoint_state(handle.saver, harness.deps.run_id)
            theirs = await checkpoint_state(handle.saver, "run-that-never-ran")
        return mine, theirs

    mine, theirs = asyncio.run(go())
    assert mine is not None and theirs is None


def test_api_keys_never_reach_the_checkpoint() -> None:
    """checkpoint 文件会和 artifacts 一起被别人复制走，明文密钥等于外泄。"""
    leaked = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="token leak: sk-abcdefghijklmnopqrstuvwxyz123456",
        started_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="unredacted secret"):
        assert_checkpoint_safe(leaked)

    clean = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="normalize returns one too small",
        started_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert_checkpoint_safe(clean)


def test_allowed_modules_cover_every_state_type() -> None:
    """白名单从注解推导，因此给 state 加字段不会悄悄漏掉新类型。"""
    names = {name for _, name in state_allowed_modules()}
    for expected in ("RepairState", "Attempt", "Usage", "RepairBudget", "StopReason"):
        assert expected in names, expected


# ------------------------------------------------------------------ G-13 恢复验证


def test_recovery_verification_accepts_a_consistent_scene(tmp_path: Path) -> None:
    harness, _ = _run(tmp_path)
    stored, _ = _read(tmp_path, harness)
    report = verify_recovery(stored, harness.deps)
    assert report.can_continue is True
    assert report.can_reuse_evidence is True
    assert report.problems == ()


def _rmtree(path: Path) -> None:
    """删掉一棵树，包括 git 在 Windows 上标成只读的对象文件。"""

    def _clear(func: Any, target: str, exc: Any) -> None:
        Path(target).chmod(stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_clear)


def test_recovery_verification_refuses_a_missing_workspace(tmp_path: Path) -> None:
    """Workspace 不在了就不能继续跑：否则补丁和 checkpoint 不是同一个世界。"""
    harness, _ = _run(tmp_path)
    stored, _ = _read(tmp_path, harness)
    _rmtree(harness.workspace)
    report = verify_recovery(stored, harness.deps)
    assert report.can_continue is False
    assert any("workspace" in item.lower() for item in report.problems)


def test_recovery_verification_notices_a_different_base_commit(tmp_path: Path) -> None:
    harness, _ = _run(tmp_path)
    stored, _ = _read(tmp_path, harness)
    drifted = stored.model_copy(update={"base_commit": "0" * 40})
    report = verify_recovery(drifted, harness.deps)
    assert report.base_commit_matches is False
    assert report.can_continue is False


def test_recovery_verification_detects_a_stale_workspace_patch(tmp_path: Path) -> None:
    """Workspace 被外部改动过 → checkpoint 里的补丁身份不再成立。"""
    harness, _ = _run(tmp_path)
    stored, _ = _read(tmp_path, harness)
    (harness.workspace / "src/pkg/core.py").write_text(
        'def normalize(value: int) -> int:\n    return "someone else edited this"\n',
        encoding="utf-8",
    )
    assert verify_recovery(stored, harness.deps).patch_identity_matches is False


def test_lost_artifact_is_not_allowed_to_masquerade_as_passing_evidence(
    tmp_path: Path,
) -> None:
    """Artifact 被清理掉之后，那条 PASS 不能继续被当作"已验证"复用。"""
    harness, _ = _run(tmp_path)
    stored, _ = _read(tmp_path, harness)
    assert stored.attempts[-1].execution is not None

    shutil.rmtree(harness.deps.artifacts.run_dir / "attempts")
    report = verify_recovery(stored, harness.deps)
    assert report.reusable_execution_evidence is False
    assert any("cannot be reused" in item for item in report.problems)


def test_resuming_a_finished_run_does_not_replay_anything(tmp_path: Path) -> None:
    """终态 run 的"恢复"必须原样返回，而不是把外部副作用再来一遍。"""
    harness, final = _run(tmp_path)
    # "不重放"的正确断言是**没有新增**执行，而不是计数器为 0：这个 harness 就是
    # 刚才把图跑完的那个，它本来就执行过 4 次。
    runs_before = harness.deps.test_runs
    again = _resume(harness, tmp_path)

    assert again.status is final.status
    assert [item.patch_hash for item in again.attempts] == [
        item.patch_hash for item in final.attempts
    ]
    assert harness.deps.test_runs == runs_before, "a finished run must not re-run the sandbox"


def test_resuming_an_unknown_run_id_is_an_error_not_a_fresh_start(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    async def go() -> RepairState:
        async with await open_checkpointer(tmp_path / "other.sqlite", "nope") as handle:
            return await resume_run(harness.deps, handle, "nope")

    with pytest.raises(ValueError, match="checkpoint"):
        asyncio.run(go())


# ------------------------------------------------------------------ G-13 / G-23 真崩溃


def _spawn(mode: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WORKER), mode, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=900,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _worker_report(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "resume_report.json").read_text(encoding="utf-8"))


def test_process_is_killed_after_run_tests_and_resumes_in_a_new_process(
    tmp_path: Path,
) -> None:
    crashed = _spawn("crash", tmp_path)
    # 7 = worker 自己 os._exit 的退出码；不是 Python 异常（那会是 1）。
    assert crashed.returncode == 7, crashed.stderr[-3000:]

    assert _spawn("inspect", tmp_path).returncode == 0
    after_crash = _worker_report(tmp_path)
    assert after_crash["has_state"] is True
    # 崩溃点：第 1 轮目标测试已经落盘，等待执行的是 diagnose。
    assert after_crash["pending"] == [Node.DIAGNOSE.value]
    assert after_crash["attempts"] == 1
    assert after_crash["test_runs"] == 3, after_crash  # 基线 2 次 + 目标测试 1 次
    assert after_crash["status"] == RunStatus.RUNNING.value
    assert Node.UNDERSTAND_ISSUE.value in after_crash["events"]

    assert _spawn("resume", tmp_path).returncode == 0
    result = _worker_report(tmp_path)

    # 从下一个未完成的节点继续，并把整条链跑完。
    assert result["status"] == RunStatus.PROVISIONALLY_VERIFIED.value, result
    assert [item["iteration"] for item in result["attempts"]] == [1, 2]
    assert result["attempts"][0]["executed"] == "FAIL"
    assert result["attempts"][1]["executed"] == "PASS"
    assert result["stop_reason"] is None

    # 不重放：崩溃前那次目标测试没有被再执行一次。
    # 3（崩溃前）+ 1（第 2 轮目标测试）= 4；若重放会变成 5。
    assert result["test_runs"] == 4, result

    # 不重复计费：崩溃前已完成的 LLM 节点没有被重新调用。
    # 新进程只补做了"崩溃时还没做成的那几件事"。
    assert result["purposes"] == ["diagnoser", "coder", "coder"], result["purposes"]

    # 不重复追加 Attempt，且两轮补丁身份不同。
    assert len({item["patch_hash"] for item in result["attempts"]}) == 2

    # Workspace 与 checkpoint 一致：最终文件确实是修好的那一份。
    assert result["workspace_source"] == "def normalize(value: int) -> int:\n    return value\n"

    events = result["events"]
    for node in (
        Node.UNDERSTAND_ISSUE,
        Node.RETRIEVE,
        Node.LOCALIZE,
        Node.PLAN,
        Node.GENERATE_PATCH,
    ):
        assert events.count(node.value) == 1, (node.value, events)
    assert events.count(Node.RUN_TARGET_TESTS.value) == 2
    assert events.count(Node.REPAIR_PATCH.value) == 1
    assert events.count(Node.FINALIZE.value) == 1


def _reattach(tmp_path: Path):
    """崩溃之后"新进程"侧的 Deps。

    ``pending_nodes`` 要算出待执行节点，必须有一张已编译的图（图本身不执行任何
    节点，所以这一步没有副作用）。这里用 ``reuse=True``，接回的是崩溃进程留下的
    那个 Workspace 与索引库，而不是重新搭一遍现场。
    """
    return build_harness(tmp_path, results=[], baseline_results=[], coder_rounds=[], reuse=True)


def test_crash_checkpoint_survives_without_any_clean_shutdown(tmp_path: Path) -> None:
    """崩溃进程没有 close()、没有 commit 收尾，恢复侧仍然能读到状态。"""
    _spawn("crash", tmp_path)
    stored, waiting = _read(tmp_path, _reattach(tmp_path))

    assert stored is not None
    assert stored.attempts[0].execution is not None
    assert stored.attempts[0].execution.status.value == "FAIL"
    assert stored.test_runs == 3
    assert waiting == (Node.DIAGNOSE.value,)
