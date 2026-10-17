"""Benchmark 节点级 checkpoint 的独立进程 worker。

模式：

``crash:<purpose>``  在指定角色的模型请求发出**之前**硬退出（``os._exit``，不是异常），
                     留下一个 ``running`` 的 checkpoint 与半旧的现场。
``resume``           新进程用同一个 run_id 重新装配，从 checkpoint 继续。

用真 ``InferenceAssembly``（真实 M2 图 + 真 checkpointer + FakeLLM 脚本 + 规则化
沙箱），而不是替身：要验的就是 benchmark 这条路径有没有真的接上节点级恢复。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import make_bench_scripts as gen  # noqa: E402

from codepilot.eval.assembly import InferenceAssembly, ProviderBundle  # noqa: E402
from codepilot.eval.evaluation import GoldEvaluator  # noqa: E402
from codepilot.eval.synthetic import load_fixture  # noqa: E402
from codepilot.repo.workspace import WorkspaceManager  # noqa: E402
from codepilot.sandbox.base import EnvironmentBuildResult  # noqa: E402
from codepilot.schemas.enums import ExecutionStatus, TestOutcome  # noqa: E402
from codepilot.schemas.execution import ExecutionResult, TestCaseResult  # noqa: E402

RUN_ID = "bench-cp-run"


class RuleSandbox:
    """按规则永远返回 PASS 的沙箱，并记录每次调用落在哪些测试上。

    不用固定长度的脚本：恢复进程的执行次数与首次进程本就不同，脚本耗尽会抛
    "没有更多结果"，那会让"恢复到底重不重跑"这个问题被一个无关错误掩盖。
    """

    backend = "fake"

    def __init__(self, node_ids: list[str], *, exit_after_target_run: bool = False) -> None:
        self._nodes = node_ids
        self.run_calls: list[tuple[str, ...]] = []
        self.build_calls = 0
        # 受控崩溃点：外部执行（一次指定测试的真实运行）**已经成功返回**，
        # 但节点还没返回、checkpoint 还没写。这正是"不宣称 exactly-once"要测的窗口。
        self._exit_after_target_run = exit_after_target_run

    def build_env(self, **kwargs: Any) -> EnvironmentBuildResult:
        self.build_calls += 1
        return EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="fake://env",
            image_id="fake-image",
            cache_key="k",
        )

    def run_tests(self, **kwargs: Any) -> ExecutionResult:
        tests = tuple(kwargs.get("tests") or ())
        self.run_calls.append(tests)
        if self._exit_after_target_run and tests:
            os._exit(7)
        cases = [
            TestCaseResult(node_id=node, outcome=TestOutcome.PASSED, duration=0.01)
            for node in (tests or self._nodes)
        ]
        return ExecutionResult(
            status=ExecutionStatus.PASS,
            exit_code=0,
            tests=cases,
            tests_passed=len(cases),
            duration_seconds=0.01,
            diagnostics={"execution_backend": "fake", "container_verified": False},
        )

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:
        return []


def main(mode: str, root: Path) -> int:
    fixture = load_fixture(Path((root / "suite_dir.txt").read_text(encoding="utf-8")))
    sandboxes: list[RuleSandbox] = []

    after_external = mode == "crash:after-external"

    def sandbox_factory(_artifacts: Any) -> RuleSandbox:
        sandbox = RuleSandbox(node_ids, exit_after_target_run=after_external)
        sandboxes.append(sandbox)
        return sandbox

    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=root / "cache"),
        sandbox_factory=sandbox_factory,
        work_root=root / "evalwork",
    )
    repository = evaluator.materialized_repository(fixture)
    # 沙箱只能报告推理现场里**真实存在**的测试。用 fixture 的评测侧 node id 会让
    # 恢复/回归节点去校验隐藏测试路径，那次崩溃与 checkpoint 这件事毫无关系。
    node_ids = sorted(
        path.relative_to(repository).as_posix() for path in (repository / "tests").glob("test_*.py")
    )
    # 每个进程一份独立的索引工作目录：``WorkspaceManager.create`` 拒绝复用非空目录，
    # 恢复进程要面对的旧现场是 run 目录里的 workspace，不是这个只为生成脚本而建的副本。
    index = gen._index(fixture, repository, root / f"idxwork-{mode.replace(':', '_')}")
    script = gen._build_script(fixture, "correct", index)
    bundle = ProviderBundle.scripted(script)

    if mode.startswith("crash:") and mode != "crash:after-external":
        purpose = mode.split(":", 1)[1]
        shared = getattr(bundle.roles["planner"], "inner", None)
        original = shared.complete

        def guarded(request: Any) -> Any:
            if request.purpose == purpose:
                (root / "crash-before.json").write_text(
                    json.dumps({"purpose": purpose, "calls_so_far": len(shared.calls)}),
                    encoding="utf-8",
                )
                os._exit(7)
            return original(request)

        shared.complete = guarded

    assembly = InferenceAssembly(
        task=fixture.inference(repository),
        workspace_manager=WorkspaceManager(cache_root=root / "cache"),
        work_root=root / "work",
        sandbox_factory=sandbox_factory,
        provider_bundle=bundle,
        checkpoint=True,
    )
    outcome = assembly.run(run_id=RUN_ID, seed=0)
    payload = {
        "status": outcome.status,
        "stop_reason": outcome.stop_reason,
        "error_class": outcome.error_class,
        "iterations": outcome.iterations,
        "test_runs": outcome.test_runs,
        "llm_calls": outcome.usage.llm_calls,
        "cost": outcome.usage.cost,
        "usage_reconciles": outcome.usage.role_totals_reconcile(),
        "resumed": outcome.resumed_from_checkpoint,
        "plan_corrections": outcome.plan_corrections,
        "plan_tool_audit": outcome.plan_tool_audit,
        "llm_requests": outcome.llm_requests,
        "patch_bytes": len(outcome.final_patch),
        "failure_reasons": list(outcome.failure_reasons),
        "sandbox_test_runs_in_this_process": sum(len(s.run_calls) for s in sandboxes),
        "sandbox_target_test_runs": sum(
            len([call for call in s.run_calls if call]) for s in sandboxes
        ),
        "sandbox_all_test_runs": sum(len(s.run_calls) for s in sandboxes),
    }
    name = "resumed.json" if mode == "resume" else f"crashed-{mode}.json"
    (root / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], Path(sys.argv[2])))
