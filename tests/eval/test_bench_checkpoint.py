"""Benchmark 的节点级 checkpoint 接入与跨进程恢复（Harness Hardening 修复五）。

任务级续跑（:class:`ResultJournal`）早就有了，但它只记得"哪些 (task, seed) 已经有
结论"。单任务真实实验最容易死在**一张图跑到一半**：那时既没有结论可写 journal，
也没有 checkpoint 可接，于是每次重启都要把已经付过钱的 LLM 请求与已经跑完的
Docker 测试再来一遍。

这里用独立进程（``subprocess`` + ``os._exit``）验的是那一种死法。
不宣称 exactly-once：崩溃落在"外部副作用已完成、checkpoint 还没写"的窗口里时，
那一轮会重做并重新计费 —— 这是 at-most-once-with-verified-evidence，代码注释与
文档都按这个口径写。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from codepilot.eval.assembly import InferenceAssembly, ProviderBundle
from codepilot.eval.runner import BenchmarkConfig, BenchmarkRunner
from codepilot.eval.synthetic import load_fixture
from codepilot.eval.synthetic_build import build_fixture
from codepilot.llm.registry import LLMMode
from support.gitfixture import init_repo
from support.synth import demo_spec

WORKER = Path(__file__).with_name("_bench_checkpoint_worker.py")


def _suite(tmp_path: Path) -> Path:
    """在 root/suite 下建一份合成 fixture，并把路径告诉子进程 worker。"""
    directory = build_fixture(
        demo_spec(task_id="bench-cp"),
        tmp_path / "suite",
        scratch_root=tmp_path / "scratch",
    )
    (tmp_path / "suite_dir.txt").write_text(str(directory), encoding="utf-8")
    return directory


def _worker(mode: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WORKER), mode, str(root)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(Path(__file__).resolve().parents[2]),
    )


def _payload(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / name).read_text(encoding="utf-8"))


def test_assembly_refuses_to_reopen_an_unidentified_or_other_base_workspace(
    tmp_path: Path,
) -> None:
    """节点级恢复的两条硬前提：现场身份必须认得回来，且必须是同一个 base。"""
    from codepilot.artifacts import ArtifactStore
    from codepilot.eval.assembly import WORKSPACE_IDENTITY

    directory = _suite(tmp_path)
    fixture = load_fixture(directory)
    artifacts = ArtifactStore(tmp_path / "work", "run-x")
    # 旧现场必须是个真的 git 仓库，否则 rev-parse 先失败，测不到 base 比对那一支。
    old_workspace = init_repo(tmp_path / "old-workspace")
    bundle = ProviderBundle.scripted({"planner": []})
    task = fixture.inference(directory / "repo")
    assembly = InferenceAssembly(
        task=task,
        workspace_manager=None,  # type: ignore[arg-type]
        work_root=tmp_path / "work",
        sandbox_factory=lambda artifacts: None,  # type: ignore[arg-type]
        provider_bundle=bundle,
        checkpoint=True,
    )

    with pytest.raises(RuntimeError, match=WORKSPACE_IDENTITY):
        assembly._reopen(artifacts)

    artifacts.write_json(
        WORKSPACE_IDENTITY,
        {
            "workspace": str(old_workspace.path),
            "source": str(task.repository),
            "base_commit": "1" * 40,
            "repository_id": "repo_deadbeef",
        },
    )
    with pytest.raises(RuntimeError, match="base commit"):
        assembly._reopen(artifacts)

    # 身份对得上时才允许接着用：记录的 base、workspace 的 HEAD 与本次任务的 base
    # 三者必须一致，否则恢复出来的是另一个世界的现场。
    assembly_for_old = InferenceAssembly(
        task=task.model_copy(update={"base_commit": old_workspace.commits[0]}),
        workspace_manager=None,  # type: ignore[arg-type]
        work_root=tmp_path / "work",
        sandbox_factory=lambda artifacts: None,  # type: ignore[arg-type]
        provider_bundle=bundle,
        checkpoint=True,
    )
    artifacts.write_json(
        WORKSPACE_IDENTITY,
        {
            "workspace": str(old_workspace.path),
            "source": str(task.repository),
            "base_commit": old_workspace.commits[0],
            "repository_id": "repo_real",
        },
    )
    reopened = assembly_for_old._reopen(artifacts)
    assert reopened.repository_id == "repo_real"
    assert reopened.base_commit == old_workspace.commits[0]


# ------------------------------------------------------- 跨进程中断与恢复


@pytest.mark.parametrize("mode", ["crash:planner", "crash:coder"])
def test_benchmark_resumes_across_processes(tmp_path: Path, mode: str) -> None:
    root = tmp_path
    _suite(root)
    crashed = _worker(mode, root)
    assert crashed.returncode == 7, crashed.stdout + crashed.stderr

    checkpoints = list((root / "work").glob("runs/*/checkpoints.sqlite"))
    assert checkpoints, "checkpoint 必须落在每个 run 自己的目录里"

    resumed = _worker("resume", root)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    payload = _payload(root, "resumed.json")
    assert payload["resumed"] is True
    assert payload["status"] in {"provisionally_verified", "succeeded"}, payload["failure_reasons"]
    assert payload["usage_reconciles"] is True
    # 恢复后仍然把图跑完并产出补丁。
    assert payload["patch_bytes"] > 0


def test_resume_does_not_re_run_confirmed_external_operations(tmp_path: Path) -> None:
    """崩溃点在生成阶段：恢复后已 checkpoint 的节点不再重跑，也不再问模型。"""
    root = tmp_path
    _suite(root)
    crashed = _worker("crash:coder", root)
    assert crashed.returncode == 7, crashed.stdout + crashed.stderr

    resumed = _worker("resume", root)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    payload = _payload(root, "resumed.json")
    assert payload["resumed"] is True
    assert payload["status"] in {"provisionally_verified", "succeeded"}, payload["failure_reasons"]
    # 计划已经确认完成：恢复进程重跑的是 coder 之后那一步。
    assert payload["sandbox_target_test_runs"] >= 1
    # 预算从 checkpoint 续上：恢复后的 test_runs 不少于崩溃前已计费的次数。
    assert payload["test_runs"] >= 3, payload
    assert payload["usage_reconciles"] is True


def test_dangling_external_intent_refuses_resume(tmp_path: Path) -> None:
    """崩溃在外部操作的"意图已写入、结果还没提交"窗口里：恢复必须拒绝盲目重放。

    这不是恢复失败，而是 Phase 9 Operation Journal 的设计承诺：一次拿不出已提交
    结果的外部操作，状态就是**不确定**的，重放等于承认"我们不知道它到底跑没跑"
    却按跑过处理。
    """
    root = tmp_path
    _suite(root)
    crashed = _worker("crash:reviewer", root)
    assert crashed.returncode == 7, crashed.stdout + crashed.stderr

    resumed = _worker("resume", root)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    payload = _payload(root, "resumed.json")
    assert payload["resumed"] is True
    assert payload["status"] == "error"
    assert any("resume refused" in item for item in payload["failure_reasons"]), payload[
        "failure_reasons"
    ]
    # 拒绝重放的同时不重复烧沙箱：目标测试一次都不该再跑。
    assert payload["sandbox_target_test_runs"] == 0


def test_fresh_run_of_the_same_task_is_not_confused_with_a_resume(tmp_path: Path) -> None:
    root = tmp_path
    _suite(root)
    first = _worker("run", root)
    assert first.returncode == 0, first.stdout + first.stderr
    payload = _payload(root, "crashed-run.json")
    assert payload["resumed"] is False
    assert payload["status"] in {"provisionally_verified", "succeeded"}, payload
    assert payload["plan_tool_audit"]["records_present"] is True
    assert payload["llm_requests"]["total_requests"] >= 4
    assert payload["usage_reconciles"] is True


# ------------------------------------------------------ 与任务级 journal 协调


def test_resume_inherits_the_previous_run_id_only_for_the_same_config(
    tmp_path: Path,
) -> None:
    """续跑必须沿用原 run_id，否则节点级恢复找不到旧现场；配置换了则不许继承。"""
    fixtures = [load_fixture(_suite(tmp_path))]
    config = BenchmarkConfig(
        seeds=(0,), model_identity="FakeLLM", out_dir=tmp_path / "out", run_label="bench"
    )
    directory = tmp_path / "journal"
    directory.mkdir(parents=True, exist_ok=True)

    def build(cfg: BenchmarkConfig) -> BenchmarkRunner:
        return BenchmarkRunner(
            config=cfg,
            fixtures=fixtures,
            assembly_factory=lambda fixture, seed: None,  # type: ignore[arg-type]
            evaluator=None,  # type: ignore[arg-type]
            journal_directory=directory,
        )

    (directory / "manifest.json").write_text(
        json.dumps({"run": {"run_id": "bench-abc123"}, "config": {"hash": config.config_hash}}),
        encoding="utf-8",
    )
    assert build(config)._run_id == "bench-abc123"

    other = BenchmarkConfig(
        seeds=(0, 1), model_identity="FakeLLM", out_dir=tmp_path / "out", run_label="bench"
    )
    assert build(other)._run_id != "bench-abc123", "换了配置就不能接旧现场"


def test_record_mode_is_selected_from_settings_for_live_bundles(monkeypatch: Any) -> None:
    monkeypatch.setenv("REG_KEY_LIVE", "sk-test")
    from codepilot.eval.assembly import ProviderBundle
    from codepilot.llm.config import KNOWN_ROLES, LLMConfig

    config = LLMConfig.model_validate(
        {
            "providers": {
                "z": {
                    "name": "z",
                    "base_url": "https://fake.test/v1",
                    "api_key_env": "REG_KEY_LIVE",
                }
            },
            "models": {"m": {"alias": "m", "provider": "z", "model": "glm-4.7"}},
            "roles": dict.fromkeys(KNOWN_ROLES, "m"),
            "exchange_rates": {"CNY": 1.0},
        }
    )
    assert ProviderBundle.live(config).registry.mode is LLMMode.LIVE
    assert ProviderBundle.live(config, record_llm=True).registry.mode is LLMMode.RECORD


def test_external_execution_finished_before_checkpoint_is_redone(tmp_path: Path) -> None:
    """要求的第三个受控崩溃点：外部执行已经成功、checkpoint 还没写。

    这一轮会**重做并重新计费**。这不是缺陷被放过，而是"没有两阶段提交的系统不能
    宣称 exactly-once"这条承诺的可执行证明：恢复口径是
    at-most-once-with-verified-evidence，证据没落盘就重来，绝不把没证据的结果当结论。
    """
    root = tmp_path
    _suite(root)
    crashed = _worker("crash:after-external", root)
    assert crashed.returncode == 7, crashed.stdout + crashed.stderr

    resumed = _worker("resume", root)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    payload = _payload(root, "resumed.json")
    assert payload["resumed"] is True
    assert payload["status"] in {"provisionally_verified", "succeeded"}, payload["failure_reasons"]
    # 目标测试在崩溃前真的跑过一次，但没有进入 checkpoint：恢复后重跑。
    assert payload["sandbox_target_test_runs"] >= 1, payload
    # 重跑要如实计费，不把已经花掉的测试次数清零或重复累计到总账之外。
    assert payload["test_runs"] >= 3, payload
    assert payload["usage_reconciles"] is True
