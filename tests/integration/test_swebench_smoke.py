"""E-17：单实例 SWE-bench smoke 的离线端到端测试（任务书 §16.E）。

覆盖任务书 §16.E 的 10 种情形（1 成功 / 2 无效 Patch / 3 无 Patch / 4 环境构建失败 /
5 官方 Harness 错误 / 6 官方未解决 / 7 预算停止 / 8 中断恢复 / 9 Gold 泄漏哨兵 /
10 内部 M2 与官方结论不一致），外加 dry-run 与"未授权拒绝真实模型"两条工程约束。

工程覆盖 vs 真实运行：

* 情形 1/9 走**真实 M2 图**（``InferenceAssembly``）+ FakeLLM + FakeSandbox + 本地
  官方格式小仓库，证明装配与隔离链路真的接上了；
* 情形 2/3/6/7 用注入的 ``InferenceOutcome`` 精确制造分支，再走真实的
  PredictionWriter / MockHarness / 报告链路（避免为了造一个"无效补丁"去驱动整张图，
  那只会把测试变成对图内部实现的脆弱断言）；
* 情形 4/5/8 分别注入环境失败、MockHarness 错误、Mock SWEBenchRunner。

真实 GLM 一次都不跑：全部 FakeLLM/注入，``model_run_status`` 恒为 ``LIVE_NOT_RUN``。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import swebench_smoke as smoke  # noqa: E402

from codepilot.eval.assembly import InferenceOutcome, ProviderBundle  # noqa: E402
from codepilot.eval.swebench_runner import ResumeLevel  # noqa: E402
from codepilot.eval.swebench_task import SWEBenchDatasetRef, SWEBenchInferenceTask  # noqa: E402
from codepilot.repo.index import Indexer, RepositoryIndex  # noqa: E402
from codepilot.repo.workspace import (  # noqa: E402
    Workspace,
    normalize_source,
    repository_id,
)
from codepilot.sandbox.env_spec import EnvSpec  # noqa: E402
from codepilot.sandbox.fake import FakeSandbox, scripted_result  # noqa: E402
from codepilot.sandbox.swebench_env import (  # noqa: E402
    EnvironmentHandle,
    SWEBenchEnvironmentError,
    SWEBenchEnvironmentIdentity,
    SWEBenchEnvironmentKind,
)
from codepilot.schemas.enums import ExecutionStatus, TestOutcome  # noqa: E402
from codepilot.schemas.execution import TestCaseResult  # noqa: E402
from codepilot.schemas.usage import Usage  # noqa: E402
from codepilot.storage.db import (  # noqa: E402
    create_engine_from_url,
    create_session_factory,
    init_db,
)
from codepilot.storage.repository import Stores  # noqa: E402

TARGET_FILE = "pkg/calc.py"
TARGET_SEARCH = "return a - b"
TARGET_REPLACE = "return a + b"
TEST_FILE = "tests/test_calc.py"
GOLD_SENTINEL = "CP-GOLD-SENTINEL-smoke-0001"
BASE_COMMIT = "0" * 40


# --------------------------------------------------------------------------- 本地官方格式小仓库


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=smoke@example.invalid", "-c", "user.name=Smoke", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """一个只有单个 base commit 的真实 git 仓库（满足推理侧隔离断言）。"""
    repo = tmp_path / "fixture-repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(repo, "init", "--quiet")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _make_task(repo: Path, base_commit: str) -> SWEBenchInferenceTask:
    """真实 SWEBenchInferenceTask；``repository`` 在测试里被指到本地仓库。"""
    dataset = SWEBenchDatasetRef(split="test", revision=smoke.DATASET_REVISION)
    return SWEBenchInferenceTask(
        instance_id="smoke__repo-1",
        repo="smoke/repo",
        base_commit=base_commit,
        problem_statement="add(2, 3) returns -1 instead of 5",
        version="1.0",
        environment_setup_commit=base_commit,
        dataset=dataset,
        env_spec=EnvSpec(
            python_version="3.12",
            test_command="python -m pytest -p no:cacheprovider --junitxml=/out/junit.xml",
        ),
    )


def _bound_task(
    repo: Path, base_commit: str, monkeypatch: pytest.MonkeyPatch
) -> SWEBenchInferenceTask:
    """Monkeypatch 推理任务的 ``repository`` 到本地仓库，全程不联网。"""
    task = _make_task(repo, base_commit)
    monkeypatch.setattr(
        SWEBenchInferenceTask,
        "repository",
        property(lambda _self: str(repo)),
    )
    return task


def _build_script(tmp_path: Path, repo: Path, base_commit: str) -> dict[str, list[Any]]:
    """按真实索引生成的 candidate_id 构造 FakeLLM 脚本（与 bench 生成器同法）。"""
    work = tmp_path / "script-index"
    work.mkdir()
    manager = CopyWorkspaceManager()
    workspace = manager.create(
        source=str(repo),
        base_commit=base_commit,
        target=work / "workspace",
    )
    engine = create_engine_from_url(f"sqlite:///{(work / 'index.sqlite3').as_posix()}")
    init_db(engine)
    session = create_session_factory(engine)()
    try:
        stores = Stores(session)
        Indexer().index(stores, workspace)
        session.commit()
        index = RepositoryIndex(stores, workspace.repository_id)
        files = {item.path: item for item in index.files()}
        symbols = [item for item in index.symbol_objects() if item.path == TARGET_FILE]
        symbol = next(item for item in symbols if item.kind == "function")
        file_candidate = files[TARGET_FILE]
        script: dict[str, list[Any]] = {
            "localizer_files": [
                {
                    "files": [
                        {
                            "candidate_id": file_candidate.id,
                            "reason": "issue points at pkg/calc.py",
                        }
                    ]
                }
            ]
            * 6,
            "localizer_symbols": [
                {
                    "symbols": [
                        {
                            "candidate_id": symbol.id,
                            "start_line": symbol.start_line,
                            "end_line": symbol.end_line,
                            "reason": "the add definition",
                        }
                    ]
                }
            ]
            * 6,
            "planner": [
                item
                for _ in range(6)
                for item in (
                    {"tool": "read_file", "arguments": {"path": TARGET_FILE}},
                    {
                        "root_cause_hypothesis": "add subtracts instead of adds",
                        "evidence": [f"{TARGET_FILE}: {TARGET_SEARCH}"],
                        "changes": [
                            {
                                "file": TARGET_FILE,
                                "symbol": symbol.qualified_name,
                                "action": "modify",
                                "description": "add instead of subtract",
                            }
                        ],
                        "tests_to_run": [TEST_FILE],
                        "risk": "low",
                        "alternatives": [],
                    },
                )
            ],
            "coder": [
                item
                for _ in range(6)
                for item in (
                    {
                        "tool": "edit_file",
                        "arguments": {
                            "path": TARGET_FILE,
                            "search": TARGET_SEARCH,
                            "replace": TARGET_REPLACE,
                        },
                    },
                    {"tool": "finish", "arguments": {"summary": "fix add()"}},
                )
            ],
            "diagnoser": [
                {
                    "root_cause": "still wrong",
                    "evidence": [
                        {"kind": "code", "file": TARGET_FILE, "line": 1, "text": TARGET_SEARCH}
                    ],
                    "confidence": 0.5,
                    "is_patch_caused": False,
                    "repair_strategy": "refine",
                    "suspected_symbols": [symbol.qualified_name],
                }
            ]
            * 6,
            "reviewer": [
                {
                    "approved": True,
                    "risk": "low",
                    "issues": [],
                    "summary": "scripted review",
                }
            ]
            * 6,
        }
        return script
    finally:
        session.close()
        engine.dispose()


def _fake_sandbox() -> FakeSandbox:
    """规则化的 PASS 结果；带真实 node_id，回归判定才有东西可比较。"""
    case = TestCaseResult(node_id=TEST_FILE, outcome=TestOutcome.PASSED, duration=0.01)
    return FakeSandbox([scripted_result(ExecutionStatus.PASS, tests=[case]) for _ in range(12)])


class CopyWorkspaceManager:
    """测试材料化器：用文件复制代替 ``git clone``。

    宿主进程沙箱禁止子进程创建 msys 命名对象（``git clone`` 会拉起 ``sh.exe``，
    报 ``NtCreateDirectoryObject ... 0xC0000022``）。直接把本地仓库（含自包含
    ``.git``、仅一个 base commit）复制成 workspace，`repository_id` 仍按真实的
    ``(normalized source, base_commit)`` 计算，因此索引 candidate_id 与真实
    WorkspaceManager 完全一致。这是**宿主限制下的等价材料化**，不是绕过推理隔离：
    ``InferenceAssembly._assert_isolated`` 仍会检查自包含与单一可达提交。
    """

    def __init__(self) -> None:
        self.created: list[Path] = []

    def create(
        self,
        *,
        source: str | object,
        base_commit: str,
        target: Path,
        mode: str = "fetch",
    ) -> Workspace:
        del mode
        src = Path(str(source))
        target = Path(target)
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(f"workspace 已存在且非空: {target}")
        shutil.copytree(src, target)
        normalized = normalize_source(str(src))
        self.created.append(target)
        return Workspace(
            path=target,
            source=normalized,
            base_commit=base_commit,
            repository_id=repository_id(normalized, base_commit),
        )


# --------------------------------------------------------------------------- 环境 / Harness 替身


def _identity(task: Any) -> SWEBenchEnvironmentIdentity:
    return SWEBenchEnvironmentIdentity(
        instance_id=task.instance_id,
        repo=task.repo,
        base_commit=task.base_commit,
        version=task.version,
        environment_setup_commit=task.environment_setup_commit,
        dataset_revision=smoke.DATASET_REVISION,
        harness_version=smoke.HARNESS_VERSION,
        python_version="3.12",
        env_image_key="swebench/sweb.eval.x86_64.smoke:latest",
        instance_image_key="swebench/sweb.eval.x86_64.smoke-1:latest",
        base_image_key="swebench/sweb.eval.x86_64.base:latest",
        spec_sha256="a" * 64,
        environment_hash="b" * 64,
    )


class FakeEnvironmentAdapter:
    """离线环境适配：不碰 Docker；可按需让 ensure 抛环境错误。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.identity_calls: list[str] = []
        self.ensure_calls: list[str] = []

    def identity(self, task: Any) -> SWEBenchEnvironmentIdentity:
        self.identity_calls.append(task.instance_id)
        return _identity(task)

    def ensure(self, identity: SWEBenchEnvironmentIdentity) -> EnvironmentHandle:
        self.ensure_calls.append(identity.instance_id)
        if self.fail:
            raise SWEBenchEnvironmentError(
                "官方实例镜像不存在且 allow_build=False",
                kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED,
            )
        return EnvironmentHandle(
            identity=identity,
            image_key=identity.instance_id,
            image_id="sha256-" + "c" * 64,
            from_cache=True,
            cache_state="hit",
        )


class MockHarness:
    """按指定结论返回 HarnessExecutionResult 形状的对象；不执行子进程。"""

    def __init__(
        self,
        *,
        verdict: str = "RESOLVED",
        error_kind: str | None = None,
        error_detail: str | None = None,
        resolved: bool | None = True,
    ) -> None:
        self.verdict = verdict
        self.error_kind = error_kind
        self.error_detail = error_detail
        self.resolved = resolved
        self.calls: list[dict[str, Any]] = []

    def run(self, *, dry_run: bool = False) -> Any:
        self.calls.append({"dry_run": dry_run})
        if dry_run:
            raise AssertionError("MockHarness 不应被 dry-run 使用")
        verdicts = (
            SimpleNamespace(
                instance_id="smoke__repo-1",
                verdict=self.verdict,
                resolved=self.resolved,
                report_path=Path("report.json"),
                report_sha256="d" * 64,
                error_kind=self.error_kind,
                error_detail=self.error_detail,
                runtime_seconds=0.01,
            ),
        )
        return SimpleNamespace(
            run_id="mock-harness-run",
            command=SimpleNamespace(
                argv=("python", "-m", "swebench.harness.run_evaluation"),
                cwd=Path(),
                env={},
                expected_report=Path("report.json"),
            ),
            decision="EXECUTED",
            exit_code=0 if self.error_kind is None else 1,
            timed_out=False,
            runtime_seconds=0.01,
            error_kind=self.error_kind,
            error_detail=self.error_detail,
            reused=False,
            reused_reason=None,
            warnings=(),
            verdicts=verdicts,
            report=None,
            report_path=Path("report.json"),
            report_sha256="d" * 64,
            included_in_resolved_rate=True,
        )


class FakeLoader:
    """只提供 runner/loader 用到的入口；记录评测侧对象是否被请求。"""

    def __init__(self, task: Any, evaluation_task: Any = None) -> None:
        self.task = task
        self.evaluation_task = evaluation_task
        self.loaded: list[str] = []
        self.evaluation_calls = 0

    def load_dataset(self, split: str) -> None:
        self.loaded.append(split)

    def get_inference_task(self, split: str, instance_id: str) -> Any:
        assert instance_id == self.task.task_id
        return self.task

    def get_evaluation_task(self, split: str, instance_id: str) -> Any:
        self.evaluation_calls += 1
        del split, instance_id
        return self.evaluation_task


class StubRunner:
    """Mock SWEBenchRunner：证明确实按 ResumeLevel 委派，而不是自己再写一层恢复。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, instance_ids: list[str], *, seed: int, resume: ResumeLevel) -> Any:
        self.calls.append({"instance_ids": list(instance_ids), "seed": seed, "resume": resume})
        return SimpleNamespace(
            run_id="cp-mock-resume",
            stop_reason="completed",
            reuse={"task": 1, "graph": 1, "evaluation": 0},
            records=(),
            counts=lambda: {
                "dispatched": 1,
                "completed": 1,
                "resolved": 1,
            },
            metrics=lambda: {
                "counts": {"dispatched": 1, "completed": 1, "resolved": 1},
                "denominators": {"declared_tasks": 1},
            },
        )


# --------------------------------------------------------------------------- 公共 fixture / 配置


def _config(tmp_path: Path, **overrides: Any) -> smoke.SmokeConfig:
    values: dict[str, Any] = {
        "instance_id": "smoke__repo-1",
        "artifact_root": tmp_path / "artifacts",
        "model": "FakeLLM/smoke",
        "budget": 1.0,
        "timeout": 120,
    }
    values.update(overrides)
    return smoke.SmokeConfig(**values)


def _offline_bundle() -> ProviderBundle:
    """只要通过"已注入 provider"的离线断言即可；这些分支不会真正调用模型。"""
    return ProviderBundle.scripted({"planner": []})


def _outcome(
    *,
    final_patch: str = "",
    empty_patch: bool | None = None,
    invalid_patch: bool = False,
    status: str = "succeeded",
    stop_reason: str | None = None,
    agent_claims_success: bool | None = None,
    error_class: str | None = None,
) -> InferenceOutcome:
    patch = final_patch
    claims = (
        status in {"succeeded", "provisionally_verified"}
        if agent_claims_success is None
        else agent_claims_success
    )
    return InferenceOutcome(
        run_id="smoke__repo-1-run",
        task_id="smoke__repo-1",
        seed=0,
        base_commit=BASE_COMMIT,
        repository_id="repo-smoke",
        status=status,
        stop_reason=stop_reason,
        final_patch=patch,
        final_patch_sha256=InferenceOutcome.digest(patch),
        first_attempt_patch=patch or None,
        first_attempt_patch_sha256=(InferenceOutcome.digest(patch) if patch else None),
        iterations=1,
        attempts_saved=1 if patch else 0,
        usage=Usage(llm_calls=3, input_tokens=100, output_tokens=50),
        usage_uncertain=False,
        test_runs=1,
        runtime_seconds=0.5,
        localization_files=(TARGET_FILE,),
        localization_symbols=(),
        agent_claims_success=claims,
        empty_patch=(not patch.strip()) if empty_patch is None else empty_patch,
        invalid_patch=invalid_patch,
        simulated_llm=True,
        sandbox_backend="fake",
        verification_mode="full",
        artifact_dir="artifacts/fake",
        error_class=error_class,
    )


VALID_PATCH = (
    f"diff --git a/{TARGET_FILE} b/{TARGET_FILE}\n"
    f"--- a/{TARGET_FILE}\n"
    f"+++ b/{TARGET_FILE}\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a: int, b: int) -> int:\n"
    f"-    {TARGET_SEARCH}\n"
    f"+    {TARGET_REPLACE}\n"
)


# --------------------------------------------------------------------------- 1) dry-run


def test_dry_run_uses_real_loader_and_builds_official_command(tmp_path: Path) -> None:
    """dry-run：真实 Loader + 真实官方命令拼装，不调 LLM、不建镜像、不建 workspace。"""
    from fetch_swebench_lite import build_snapshot_files

    from codepilot.eval.swebench import SWEBenchLoader

    dataset_name = smoke.DATASET_NAME
    revision = "b" * 40
    rows = {
        split: [
            json.loads(line)
            for line in (ROOT / "tests/fixtures/swebench" / f"{split}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        for split in ("dev", "test")
    }
    snapshot_root = tmp_path / "snapshot"
    snapshot = snapshot_root / f"{dataset_name.replace('/', '__')}__{revision}"
    snapshot.mkdir(parents=True)
    for name, payload in build_snapshot_files(
        rows, dataset_name=dataset_name, revision=revision
    ).items():
        (snapshot / name).write_bytes(payload)
    loader = SWEBenchLoader(snapshot_root, dataset_name=dataset_name, revision=revision)
    config = _config(
        tmp_path,
        instance_id=rows["test"][0]["instance_id"],
        revision=revision,
        snapshot_root=snapshot_root,
        model=None,
        dry_run=True,
    )
    report = smoke.run_smoke(config, deps=smoke.SmokeDeps(loader=loader))
    assert report["status"] == "DRY_RUN"
    assert report["model_run_status"] == smoke.LIVE_NOT_RUN
    assert report["dry_run_checks"]["placeholder_removed"] is True
    # P1-3：dry-run 没有执行评测，归因显式留空而不是伪装成失败。
    assert report["attribution"] is None
    assert "dry-run" in report["attribution_detail"]["reason"]
    # P1-4：默认产物根目录必须是验收要求的 artifacts/swebench/。
    assert smoke.DEFAULT_ARTIFACT_ROOT == ROOT / "artifacts" / "swebench"
    # 官方命令关键参数齐全（命令由 Owner D 的真实 build_command 生成）。
    argv = report["harness"]["command"]["argv"]
    for flag in ("-d", "-s", "-i", "-p", "-id", "--max_workers", "-t", "--report_dir"):
        assert flag in argv, flag
    assert "swebench.harness.run_evaluation" in argv
    assert report["environment"]["environment_hash"]
    assert report["environment"]["ensured"] is False
    # dry-run 只写报告；占位预测文件必须删除，workspace 不能建出来。

    assert not Path(report["paths"]["prediction"]).exists()
    assert not Path(report["paths"]["workspace"]).exists()
    assert Path(report["report_path"]).is_file()
    assert report["isolation"]["inference_task_clean"] is True


def test_unauthorized_live_llm_is_refused_before_any_call(tmp_path: Path) -> None:
    """--live-llm 默认 False：未授权时必须在装配阶段拒绝，不能偷偷发起真实调用。"""
    config = _config(tmp_path, dry_run=False, live_llm=False)
    with pytest.raises(smoke.SmokeError, match="live-llm"):
        smoke.run_smoke(config)
    # 没有留下任何"看起来跑过"的现场。
    assert not (tmp_path / "artifacts" / "runs").exists()


# --------------------------------------------------------------------------- 2) 端到端成功 + 9) Gold 隔离


def test_end_to_end_real_graph_succeeds_with_fake_llm_and_mock_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """情形 1：真实 M2 图 + FakeLLM + FakeSandbox → 合法 patch → MockHarness resolved。"""
    repo, base_commit = _make_repo(tmp_path)
    task = _bound_task(repo, base_commit, monkeypatch)
    loader = FakeLoader(task, evaluation_task=SimpleNamespace(gold_patch=GOLD_SENTINEL))
    bundle = ProviderBundle.scripted(_build_script(tmp_path, repo, base_commit))
    harness = MockHarness(verdict="RESOLVED", resolved=True)
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        workspace_manager=CopyWorkspaceManager(),
        sandbox_factory=lambda _artifacts: _fake_sandbox(),
        provider_bundle=bundle,
        harness_runner=harness,
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)

    assert report["status"] == "SMOKE_COMPLETED_RESOLVED"
    assert report["attribution"] == "official_resolved"
    assert report["attribution_detail"]["official_verdict"] == "RESOLVED"
    assert report["model_run_status"] == smoke.LIVE_NOT_RUN
    assert report["prediction"]["status"] == "PATCH_READY"
    assert report["m2_vs_official"]["discrepancy"] == "CONSISTENT"
    # P1-2：环境身份摘要真的从 Owner C 的身份算了进来，并标记为已验证。
    assert report["environment"]["identity_verified"] is True
    assert len(report["environment"]["identity_digest"]) == 64
    # 官方 predictions 行严格三字段。
    records = [
        json.loads(line)
        for line in Path(report["paths"]["prediction"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert set(records[0]) == {"instance_id", "model_name_or_path", "model_patch"}
    assert records[0]["instance_id"] == "smoke__repo-1"
    # writer 落盘的是规范化后的 patch；manifest 里记录的 sha256 必须与文件内容一致。
    entry = report["prediction"]["manifest"]["entries"][0]
    assert entry["instance_id"] == "smoke__repo-1"
    assert (
        entry["patch_sha256"]
        == hashlib.sha256(records[0]["model_patch"].encode("utf-8")).hexdigest()
    )
    # 现场：workspace / checkpoint / usage / cost / report 都落在本次 run_id 下。
    paths = report["paths"]
    assert Path(paths["workspace"]).is_dir()
    assert Path(paths["checkpoint"]).is_file()
    assert Path(paths["usage"]).is_file()
    assert Path(paths["cost"]).is_file()
    assert report["usage"]["llm_calls"] > 0
    assert report["cost"]["simulated_llm"] is True
    # 推理侧从未向 Loader 要过 evaluation_task（Gold 只在评测侧对象里）。
    assert loader.evaluation_calls == 0
    # Gold 哨兵与 gold 字段没有出现在任何模型 prompt / workspace 文件里。
    sentinel = GOLD_SENTINEL.encode("utf-8")
    assert all(
        sentinel not in prompt.encode("utf-8")
        for prompt in bundle.roles["planner"].inner.prompt_texts()
    )
    for path in Path(paths["workspace"]).rglob("*"):
        if path.is_file():
            assert sentinel not in path.read_bytes()


def test_gold_sentinel_is_absent_from_inference_prompts_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """情形 9：评测侧 Gold 哨兵在读不到的位置——推理任务字段、prompt、artifact 全查。"""
    repo, base_commit = _make_repo(tmp_path)
    task = _bound_task(repo, base_commit, monkeypatch)
    evaluation_task = SimpleNamespace(
        gold_patch=GOLD_SENTINEL, test_patch=GOLD_SENTINEL, fail_to_pass=(GOLD_SENTINEL,)
    )
    loader = FakeLoader(task, evaluation_task=evaluation_task)
    bundle = ProviderBundle.scripted(_build_script(tmp_path, repo, base_commit))
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        workspace_manager=CopyWorkspaceManager(),
        sandbox_factory=lambda _artifacts: _fake_sandbox(),
        provider_bundle=bundle,
        harness_runner=MockHarness(verdict="UNRESOLVED", resolved=False),
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)

    # 推理任务对象本身不允许出现 gold/test 字段。
    for field_name in ("gold_patch", "test_patch", "fail_to_pass", "pass_to_pass"):
        assert field_name not in type(task).model_fields
    # 推理链路从未请求 evaluation_task。
    assert loader.evaluation_calls == 0
    # 所有 FakeLLM prompt 里没有哨兵。
    for provider in bundle.roles.values():
        for prompt in provider.inner.prompt_texts():
            assert GOLD_SENTINEL not in prompt
    # artifact 树里没有哨兵（run 目录 + 报告）。
    sentinel = GOLD_SENTINEL.encode("utf-8")
    for path in Path(report["paths"]["run_dir"]).rglob("*"):
        if path.is_file():
            assert sentinel not in path.read_bytes()
    for path in (
        Path(report["paths"]["report"]),
        Path(report["paths"]["usage"]),
        Path(report["paths"]["cost"]),
        Path(report["paths"]["prediction"]),
    ):
        if path.is_file():
            assert sentinel not in path.read_bytes()


# --------------------------------------------------------------------------- 2/3) 无效与空 Patch


def test_invalid_patch_is_not_submitted_to_official_harness(tmp_path: Path) -> None:
    """情形 2：模型产出未通过校验的 Patch → INVALID_PATCH，不写官方行、不跑 harness。"""
    loader = FakeLoader(_make_task((tmp_path / "unused"), BASE_COMMIT))
    harness = MockHarness(verdict="RESOLVED", resolved=True)
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=_offline_bundle(),
        inference_executor=lambda task, config: _outcome(
            final_patch=VALID_PATCH, invalid_patch=True, status="patch_rejected"
        ),
        harness_runner=harness,
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)
    assert report["prediction"]["status"] == "INVALID_PATCH"
    assert report["status"] == "SMOKE_COMPLETED_NO_PATCH"
    assert report["attribution"] == "patch_invalid"
    assert report["harness"]["skipped"] is True
    assert harness.calls == []
    assert report["harness"]["verdicts"][0]["verdict"] == "NOT_EVALUATED"
    # 官方 predictions 文件里没有可提交行。
    lines = [
        line
        for line in Path(report["paths"]["prediction"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines == []


def test_empty_patch_is_recorded_but_not_written(tmp_path: Path) -> None:
    """情形 3：模型没有产出 Patch → EMPTY_PATCH 只进 manifest，不伪造官方行。"""
    loader = FakeLoader(_make_task(tmp_path / "unused", BASE_COMMIT))
    harness = MockHarness()
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=_offline_bundle(),
        inference_executor=lambda task, config: _outcome(
            final_patch="", empty_patch=True, status="stopped", stop_reason="no_progress"
        ),
        harness_runner=harness,
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)
    assert report["prediction"]["status"] == "EMPTY_PATCH"
    assert report["attribution"] == "model_not_resolved"
    assert report["prediction"]["written"] == 0
    assert report["harness"]["skipped"] is True
    assert harness.calls == []
    assert Path(report["paths"]["prediction"]).read_text(encoding="utf-8").strip() == ""


# --------------------------------------------------------------------------- 4) 环境构建失败


def test_environment_failure_is_attributed_as_env_error(tmp_path: Path) -> None:
    """情形 4：官方环境不可用 → ENV_ERROR，且在启动 M2 图/LLM 之前终止。"""
    loader = FakeLoader(_make_task(tmp_path / "unused", BASE_COMMIT))
    inference_calls: list[int] = []

    def _must_not_run(task: Any, config: Any) -> Any:
        inference_calls.append(1)
        pytest.fail("环境失败时不应进入推理")

    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(fail=True),
        provider_bundle=_offline_bundle(),
        inference_executor=_must_not_run,
        harness_runner=MockHarness(),
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)
    assert report["status"] == "ENV_ERROR"
    assert report["attribution"] == "env_error"
    assert inference_calls == []
    assert report["model_run_status"] == smoke.LIVE_NOT_RUN
    assert report["errors"]
    assert "RESOURCE_BLOCKED" in str(report["environment"].get("kind"))
    assert Path(report["report_path"]).is_file()


# --------------------------------------------------------------------------- 5/6/10) 官方侧结论


def test_official_harness_error_is_recorded_without_crashing(tmp_path: Path) -> None:
    """情形 5：官方 Harness 错误 → HARNESS_ERROR，报告保留 error_kind/detail。"""
    loader = FakeLoader(_make_task(tmp_path / "unused", BASE_COMMIT))
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=_offline_bundle(),
        inference_executor=lambda task, config: _outcome(final_patch=VALID_PATCH),
        harness_runner=MockHarness(
            verdict="NOT_EVALUATED",
            resolved=None,
            error_kind="DOCKER_UNAVAILABLE",
            error_detail="docker daemon 不可用",
        ),
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)
    assert report["status"] == "HARNESS_ERROR"
    assert report["attribution"] == "env_error"
    assert report["harness"]["error_kind"] == "DOCKER_UNAVAILABLE"
    assert report["harness"]["error_detail"] == "docker daemon 不可用"
    assert report["prediction"]["status"] == "PATCH_READY"
    assert report["m2_vs_official"]["official_authoritative"] is True


def test_official_unresolved_is_not_promoted(tmp_path: Path) -> None:
    """情形 6：官方未解决 → 即使内部 patch 合法也只报 UNRESOLVED。"""
    loader = FakeLoader(_make_task(tmp_path / "unused", BASE_COMMIT))
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=_offline_bundle(),
        inference_executor=lambda task, config: _outcome(
            final_patch=VALID_PATCH, agent_claims_success=False
        ),
        harness_runner=MockHarness(verdict="UNRESOLVED", resolved=False),
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)
    assert report["status"] == "SMOKE_COMPLETED_UNRESOLVED"
    assert report["attribution"] == "official_not_resolved"
    assert report["m2_vs_official"]["official"] == "UNRESOLVED"
    assert report["m2_vs_official"]["discrepancy"] == "CONSISTENT"


def test_internal_m2_claim_disagrees_with_official(tmp_path: Path) -> None:
    """情形 10：内部 M2 声称成功、官方 unresolved → 记录差异且官方优先。"""
    loader = FakeLoader(_make_task(tmp_path / "unused", BASE_COMMIT))
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=_offline_bundle(),
        inference_executor=lambda task, config: _outcome(
            final_patch=VALID_PATCH, status="succeeded", agent_claims_success=True
        ),
        harness_runner=MockHarness(verdict="UNRESOLVED", resolved=False),
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)
    assert report["m2_vs_official"]["internal_m2"] == "M2_CLAIMS_SUCCESS"
    assert report["m2_vs_official"]["official"] == "UNRESOLVED"
    assert report["m2_vs_official"]["discrepancy"] == "INTERNAL_CLAIM_OFFICIAL_UNRESOLVED"
    assert report["status"] == "SMOKE_COMPLETED_UNRESOLVED"
    assert report["attribution"] == "official_not_resolved"
    # 报告里不得出现"官方 resolved"字样。
    text = Path(report["report_path"]).read_text(encoding="utf-8")
    assert 'official_resolved": true' not in text


# --------------------------------------------------------------------------- 7) 预算停止


def test_budget_exhausted_stops_before_official_harness(tmp_path: Path) -> None:
    """情形 7：预算停止 → BUDGET_EXHAUSTED，不把没跑出的补丁送去官方 harness。"""
    loader = FakeLoader(_make_task(tmp_path / "unused", BASE_COMMIT))
    harness = MockHarness()
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=_offline_bundle(),
        inference_executor=lambda task, config: _outcome(
            final_patch="", empty_patch=True, status="budget_exhausted", stop_reason="budget_cost"
        ),
        harness_runner=harness,
    )
    report = smoke.run_smoke(_config(tmp_path), deps=deps)
    assert report["prediction"]["status"] == "BUDGET_EXHAUSTED"
    assert report["attribution"] == "budget_stopped"
    assert report["harness"]["skipped"] is True
    assert harness.calls == []


# --------------------------------------------------------------------------- 8) 恢复


def test_resume_delegates_to_swebench_runner_with_resume_levels(tmp_path: Path) -> None:
    """情形 8：--resume 委托 SWEBenchRunner，并正确映射 task/graph/evaluation。"""
    loader = FakeLoader(_make_task(tmp_path / "unused", BASE_COMMIT))
    runner = StubRunner()
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=ProviderBundle.scripted({"planner": []}),
        runner=runner,
    )
    for level in ("task", "graph", "evaluation"):
        config = _config(tmp_path, resume=level)
        report = smoke.run_smoke(config, deps=deps)
        assert report["mode"] == "runner_resume"
        assert report["resume_level"] == level
        assert report["model_run_status"] == smoke.LIVE_NOT_RUN
        assert Path(report["report_path"]).is_file()
    assert [call["resume"] for call in runner.calls] == [
        ResumeLevel.TASK,
        ResumeLevel.GRAPH,
        ResumeLevel.EVALUATION,
    ]
    assert all(call["instance_ids"] == ["smoke__repo-1"] for call in runner.calls)


def test_resume_with_real_swebench_runner_records_windows_env_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """情形 8（真实 runner）：确实走 SWEBenchRunner.run，而不是脚本自造的假恢复。

    官方 harness 在原生 Windows 上不可执行（缺 ``resource``），因此预期记录
    ``evaluation_status=ERROR``；这正是要验证的诚实归因：runner 完成、报告落盘、
    不启动官方子进程、不调真实模型。
    """
    repo, base_commit = _make_repo(tmp_path)
    task = _bound_task(repo, base_commit, monkeypatch)
    loader = FakeLoader(task)
    bundle = ProviderBundle.scripted(_build_script(tmp_path, repo, base_commit))
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        workspace_manager=CopyWorkspaceManager(),
        sandbox_factory=lambda _artifacts: _fake_sandbox(),
        provider_bundle=bundle,
    )
    report = smoke.run_smoke(_config(tmp_path, resume="task"), deps=deps)

    assert report["mode"] == "runner_resume"
    assert report["status"] == "RESUME_COMPLETED"
    assert report["model_run_status"] == smoke.LIVE_NOT_RUN
    assert report["resume_level"] == "task"
    assert report["runner"]["counts"]["completed"] == 1
    records = report["runner"]["records"]
    assert len(records) == 1
    # 内部图确实跑过（有 patch、有 LLM 调用），官方侧因平台限制如实报 ERROR。
    assert records[0]["patch_sha256"]
    assert records[0]["llm_calls"] > 0
    assert records[0]["evaluation_status"] == "ERROR"
    # P1-1：评测阶段失败映射成 ERROR verdict，而不是 NOT_RUN；三态结论必须为 None。
    assert records[0]["resolved"] is None
    # P1-2：环境身份摘要已注入 runner 的评测信封（不同镜像会拒绝复用旧报告）。
    assert report["environment_identity"]["digest"]
    assert len(report["environment_identity"]["digest"]) == 64
    assert report["environment_identity"]["verified"] is True
    assert Path(report["paths"]["journal"]).is_file()
    # runner 的实际现场与 smoke 报告都在同一个 artifact_root 下。
    runner_run_dir = Path(report["paths"]["artifact_root"]) / "runs" / records[0]["run_id"]
    assert runner_run_dir.is_dir()
    assert Path(report["report_path"]).is_file()


# --------------------------------------------------------------------------- A/B 两次独立运行


def test_repeats_two_produce_independent_runs(tmp_path: Path) -> None:
    """--repeats 2：A/B 各自 run_id / prediction / eval / usage / cost / artifact。"""
    loader = FakeLoader(_make_task((tmp_path / "unused"), BASE_COMMIT))
    deps = smoke.SmokeDeps(
        loader=loader,
        environment_adapter=FakeEnvironmentAdapter(),
        provider_bundle=_offline_bundle(),
        inference_executor=lambda task, config: _outcome(final_patch=VALID_PATCH),
        harness_runner=MockHarness(verdict="RESOLVED", resolved=True),
    )
    reports = smoke.run_smoke_set(_config(tmp_path), repeats=2, deps=deps)
    assert [report["run_label"] for report in reports] == ["A", "B"]
    paths = [report["paths"] for report in reports]
    assert len({item["run_id"] for item in paths}) == 2
    for key in ("report", "prediction", "evaluation_dir", "usage", "cost", "workspace"):
        assert len({item[key] for item in paths}) == 2, key
    comparison_path = Path(reports[0]["comparison_path"])
    assert comparison_path.is_file()
    for report in reports:
        on_disk = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
        assert on_disk["comparison_path"] == str(comparison_path)
        assert on_disk["attribution"] == "official_resolved"
    comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
    assert comparison["checks"]["distinct_run_ids"] is True
    assert comparison["checks"]["independent_artifacts"] is True


def test_cost_note_follows_the_engine_and_never_calls_real_money_a_demo(
    tmp_path: Path,
) -> None:
    """真实运行的 cost.json 不许写"这只是记账链路接通"。

    这句免责对 FakeLLM 是对的，对着真实 provider 写就会把一份真实费用读成演示数据；
    反过来也一样，模拟运行不能写成真实花费。两种场合各说各的事实。
    """
    paths = SimpleNamespace(usage=tmp_path / "usage.json", cost=tmp_path / "cost.json")
    config = SimpleNamespace(budget_currency="CNY", budget=10.0)
    usage = Usage(input_tokens=100, output_tokens=10, cost=0.5, llm_calls=1)

    smoke._write_usage_and_cost(config, paths, SimpleNamespace(usage=usage, simulated_llm=False))
    live = json.loads(paths.cost.read_text(encoding="utf-8"))
    assert live["simulated_llm"] is False
    assert "真实 provider 记账" in live["note"]
    assert "不构成真实费用证据" not in live["note"]

    smoke._write_usage_and_cost(config, paths, SimpleNamespace(usage=usage, simulated_llm=True))
    fake = json.loads(paths.cost.read_text(encoding="utf-8"))
    assert fake["simulated_llm"] is True
    assert fake["note"].startswith("simulated_llm=true")
    assert fake["budget_max_cost"] == 10.0
