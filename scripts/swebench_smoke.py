"""E-17 单实例 SWE-bench 完整链路 smoke，支持 dry-run 与 A/B 两次独立运行。

这个脚本不是"再写一个 mini Agent"：真实执行路径必须依次经过

    Loader → SWEBenchInferenceTask → Workspace → Environment → 真实 M2 图
    (InferenceAssembly) → PatchValidator 结果 → PredictionWriter
    → OfficialHarnessRunner → Report

因此本文件只做**装配与记账**：修复逻辑全部来自 ``codepilot.graph`` 的真实图，
补丁校验/规范化来自 Owner A 的 ``swebench_prediction``，官方命令与报告解析来自
Owner D 的 ``swebench_harness``，恢复由 Owner F 的 ``SWEBenchRunner`` 负责。

安全边界（与 Phase 10 的 bench 一致）：

* ``--live-llm`` 默认 False。没有显式授权时**拒绝**构造真实 provider；
* ``--dry-run`` 不调 LLM、不建镜像、不执行官方 harness，只做装配/命令/路径/预算/隔离检查；
* 测试里注入 FakeLLM + FakeSandbox + MockHarness，报告里的
  ``model_run_status`` 始终是 ``LIVE_NOT_RUN``，不会把工程 smoke 冒充成模型效果。

用法::

    # 1) 只验装配（离线、零费用）
    python scripts/swebench_smoke.py --instance-id django__django-11099 --dry-run

    # 2) A/B 两次独立运行（真实模型需要显式 --live-llm 且已授权）
    python scripts/swebench_smoke.py --instance-id django__django-11099 \\
        --repeats 2 --live-llm

    # 3) 断点恢复（走 SWEBenchRunner 的三层恢复）
    python scripts/swebench_smoke.py --instance-id django__django-11099 \\
        --repeats 1 --resume graph
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # pragma: no cover - 直接执行脚本时
    sys.path.insert(0, str(ROOT / "src"))

DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
HARNESS_VERSION = "4.1.0"
DEFAULT_SNAPSHOT_ROOT = ROOT / "cache" / "swebench"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "swebench"
DEFAULT_SPEC_SNAPSHOT = ROOT / "configs" / "swebench" / "env-specs-4.1.0.json"

#: 工程 smoke 的真实模型状态。本阶段**只允许**这个值；真实模型一次都不跑。
LIVE_NOT_RUN = "LIVE_NOT_RUN"
#: 只有显式 --live-llm 且 provider 不是 FakeLLM 时才允许出现；本轮交付不产生该状态。
LIVE_RUN = "LIVE_RUN"

SPLITS = ("dev", "test")
RESUME_LEVELS = ("task", "graph", "evaluation")
_RUN_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$")


class SmokeError(RuntimeError):
    """smoke 无法安全启动或无法给出可信结论。"""


# --------------------------------------------------------------------------- 配置


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    """一次 smoke 的全部输入。默认值刻意保守：不调真实模型、不建镜像。"""

    instance_id: str
    split: str = "test"
    revision: str = DATASET_REVISION
    provider: str = "bigmodel"
    model: str | None = None
    budget: float | None = None
    seed: int = 0
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout: int = 1800
    docker_cpus: float = 2.0
    docker_memory: str = "4g"
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    snapshot_root: Path = DEFAULT_SNAPSHOT_ROOT
    spec_snapshot: Path = DEFAULT_SPEC_SNAPSHOT
    harness_python: Path | None = None
    run_label: str = "A"
    dry_run: bool = False
    live_llm: bool = False
    resume: str | None = None
    record_llm: bool = False
    budget_currency: str = "CNY"

    def __post_init__(self) -> None:
        if _INSTANCE_ID.fullmatch(self.instance_id) is None:
            raise SmokeError(f"非法 instance_id（需要 owner__repo-编号）: {self.instance_id!r}")
        if self.split not in SPLITS:
            raise SmokeError(f"split 必须是 {SPLITS}，收到 {self.split!r}")
        if len(self.revision) != 40 or any(ch not in "0123456789abcdef" for ch in self.revision):
            raise SmokeError(f"revision 必须是 40 位小写 hex: {self.revision!r}")
        if self.seed < 0:
            raise SmokeError("seed 不能为负")
        if self.timeout <= 0:
            raise SmokeError("timeout 必须为正")
        if self.max_output_tokens <= 0:
            raise SmokeError("max-output-tokens 必须为正")
        if self.temperature < 0:
            raise SmokeError("temperature 不能为负")
        if self.budget is not None and self.budget < 0:
            raise SmokeError("budget 不能为负")
        if self.docker_cpus <= 0:
            raise SmokeError("docker-cpus 必须为正")
        if _RUN_LABEL.fullmatch(self.run_label) is None:
            raise SmokeError(f"非法 run-label: {self.run_label!r}")
        if self.resume is not None and self.resume not in RESUME_LEVELS:
            raise SmokeError(f"resume 必须是 {RESUME_LEVELS}，收到 {self.resume!r}")
        if self.resume is not None and self.dry_run:
            raise SmokeError("--resume 与 --dry-run 不能同时使用（dry-run 没有可恢复的现场）")

    @property
    def model_identity(self) -> str:
        """预测文件与官方 harness 必须使用同一个模型标识。

        真实运行时优先用 ``--model`` 指定的别名，其次用 provider 名；两者都不写时
        用一个显式的 ``unspecified``，让报告里能看出"没有记录模型身份"而不是猜。
        """
        return self.model or self.provider or "unspecified"

    def identity_payload(self) -> dict[str, Any]:
        """参与 run_id 推导的配置身份；改预算/模型/seed 必须得到不同的现场。"""
        return {
            "instance_id": self.instance_id,
            "split": self.split,
            "revision": self.revision,
            "provider": self.provider,
            "model": self.model,
            "budget": self.budget,
            "seed": self.seed,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "budget_currency": self.budget_currency,
        }

    def config_hash(self) -> str:
        payload = json.dumps(self.identity_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SmokePaths:
    """一次 run 的独立产物路径。A/B 两次运行除了 artifact_root 外完全不共享。"""

    artifact_root: Path
    run_id: str
    run_dir: Path
    workspace: Path
    checkpoint: Path
    prediction: Path
    evaluation_dir: Path
    usage: Path
    cost: Path
    report: Path
    journal: Path

    @classmethod
    def build(cls, config: SmokeConfig) -> SmokePaths:
        root = Path(config.artifact_root).expanduser().absolute()
        instance_slug = config.instance_id.replace("/", "__")
        run_id = (
            f"swebench-smoke-{config.run_label}-{instance_slug}"
            f"-s{config.seed}-{config.config_hash()[:8]}"
        )
        run_dir = root / "runs" / run_id
        return cls(
            artifact_root=root,
            run_id=run_id,
            run_dir=run_dir,
            workspace=run_dir / "workspace",
            checkpoint=run_dir / "checkpoints.sqlite",
            prediction=root / "predictions" / f"{run_id}.jsonl",
            evaluation_dir=root / "eval" / run_id,
            usage=root / "usage" / f"{run_id}.json",
            cost=root / "cost" / f"{run_id}.json",
            report=root / "reports" / f"{run_id}.json",
            journal=root / "journal" / f"{run_id}.sqlite",
        )


# --------------------------------------------------------------------------- 注入点


@dataclass(slots=True)
class SmokeDeps:
    """测试与离线 smoke 的注入点；生产路径全部为 None，按契约装配真实实现。

    刻意不用 monkeypatch 修改模块内部状态：把替身从构造参数传进来，报告里就能
    如实记录 `simulated_llm` / `sandbox_backend` / MockHarness 身份。
    """

    loader: Any = None
    environment_adapter: Any = None
    sandbox_factory: Callable[[Any], Any] | None = None
    provider_bundle: Any = None
    provider_factory: Callable[[], Any] | None = None
    harness_runner: Any = None
    workspace_manager: Any = None
    inference_executor: Callable[[Any, SmokeConfig], Any] | None = None
    prediction_writer_factory: Callable[..., Any] | None = None
    runner: Any = None
    platform: Any = None

    def provider_for_assembly(self) -> Any:
        if self.provider_factory is not None:
            return self.provider_factory()
        return self.provider_bundle


# --------------------------------------------------------------------------- 通用小工具


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _jsonable(value: Any) -> Any:
    """把 pydantic / dataclass / Path / enum 统一转成可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json"))
    if hasattr(value, "__dataclass_fields__"):
        return {
            str(field_name): _jsonable(getattr(value, field_name))
            for field_name in value.__dataclass_fields__
        }
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return str(value)


def _atomic_write_json(path: Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)
    return path


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _import_module(name: str) -> Any:
    import importlib

    try:
        return importlib.import_module(name)
    except Exception as exc:  # pragma: no cover - 依赖缺失时的明确边界
        raise SmokeError(f"无法 import {name}: {exc}") from exc


def _require_authored(config: SmokeConfig, deps: SmokeDeps) -> None:
    """未授权真实模型时拒绝发起调用；注入 FakeLLM 的离线路径不受影响。"""
    if config.live_llm or config.dry_run:
        return
    if deps.provider_for_assembly() is not None:
        return
    raise SmokeError(
        "拒绝发起真实模型调用：--live-llm 默认 False。"
        "真实运行必须显式传 --live-llm（并确认预算/授权）；"
        "离线 smoke 请注入 FakeLLM ProviderBundle。"
    )


# --------------------------------------------------------------------------- 真实组件装配


def _default_loader(config: SmokeConfig) -> Any:
    module = _import_module("codepilot.eval.swebench")
    return module.SWEBenchLoader(
        Path(config.snapshot_root),
        dataset_name=DATASET_NAME,
        revision=config.revision,
        env_spec_snapshot=Path(config.spec_snapshot),
    )


def _default_environment_adapter(config: SmokeConfig, *, allow_build: bool = False) -> Any:
    module = _import_module("codepilot.sandbox.swebench_env")
    docker = None if config.dry_run else module.DockerSdkBackend()
    return module.SWEBenchEnvironmentAdapter(
        docker=docker,
        spec_snapshot=Path(config.spec_snapshot),
        cache_dir=Path(config.artifact_root) / "env-cache",
        allow_build=allow_build,
        disk_limit_gb=None,
    )


def _default_sandbox_factory(config: SmokeConfig) -> Callable[[Any], Any]:
    from codepilot.sandbox.base import EnvironmentBuildResult
    from codepilot.sandbox.docker import DockerSandbox
    from codepilot.sandbox.swebench_env import instance_image_key
    from codepilot.schemas.enums import ExecutionStatus

    class CachedOfficialSandbox(DockerSandbox):
        def build_env(self, **kwargs: Any) -> Any:
            from codepilot.repo.gitcmd import run_git

            head = run_git(["rev-parse", "HEAD"], cwd=kwargs["workspace"]).stdout.strip()
            if head != kwargs["base_commit"]:
                raise SmokeError("official sandbox workspace base commit mismatch")
            # No builds or pulls: use the same cached official instance environment.
            image_ref = instance_image_key(config.instance_id)
            image = self.client.images.get(image_ref)
            return EnvironmentBuildResult(
                status=ExecutionStatus.PASS,
                image_ref=image_ref,
                image_id=image.id,
                cache_key=f"official:{image.id}",
                from_cache=True,
            )

        def run_tests(self, **kwargs: Any) -> Any:
            spec = kwargs["env_spec"]
            command = re.sub(
                r"^(?:python(?:3)? -m )?pytest\b",
                "/opt/miniconda3/envs/testbed/bin/python -m pytest",
                spec.test_command,
            )
            kwargs["env_spec"] = spec.model_copy(
                update={
                    "test_command": command,
                    # 工作区以卷的形式出现在 /testbed，与被测镜像原本的布局一致。
                    "env": {**spec.env, "PYTHONPATH": "/testbed/src:/testbed"},
                }
            )
            return super().run_tests(**kwargs)

    def factory(artifacts: Any) -> Any:
        return CachedOfficialSandbox(
            artifacts=artifacts,
            allowed_workspace_root=Path(config.artifact_root),
            # 覆盖镜像自带的 checkout（含它的 Git 历史），只留推理工作区的内容。
            workspace_target="/testbed",
        )

    return factory


def _default_workspace_manager(config: SmokeConfig) -> Any:
    from codepilot.repo.workspace import WorkspaceManager

    return WorkspaceManager(cache_root=Path(config.artifact_root) / "repo-cache")


def _load_llm_config(config: SmokeConfig) -> Any:
    from codepilot.config import get_settings
    from codepilot.llm.config import load_llm_config
    from codepilot.llm.glm import apply_glm_env

    resolved = apply_glm_env(
        load_llm_config(get_settings().resolved_llm_config),
        platform=None,
    )
    if config.model:
        resolved = resolved.bind_all_roles(config.model)
    return resolved


def _default_provider_bundle(config: SmokeConfig) -> Any:
    if not config.live_llm:
        raise SmokeError("未授权真实模型：不能构造真实 Provider")
    from codepilot.eval.assembly import ProviderBundle

    llm_config = _load_llm_config(config)
    return ProviderBundle.live(
        llm_config,
        budget_currency=config.budget_currency,
        record_llm=config.record_llm,
    )


def _wrap_request_overrides(bundle: Any, config: SmokeConfig) -> Any:
    """把 ``--temperature`` / ``--max-output-tokens`` 应用到每个请求。

    图里的 Agent 自己决定请求参数，没有全局覆盖口；但 smoke 契约要求这两个参数
    真的影响这次运行。用 provider 装饰器实现，避免为了参数去改真实图。
    """
    from codepilot.llm.base import LLMProvider  # noqa: F401 - 仅用于说明协议
    from codepilot.llm.types import LLMRequest, LLMResponse

    class _Override:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def complete(self, request: LLMRequest) -> LLMResponse:
            overridden = request.model_copy(
                update={
                    "temperature": config.temperature,
                    "max_output_tokens": min(request.max_output_tokens, config.max_output_tokens),
                }
            )
            return self.inner.complete(overridden)

        def bind_sink(self, *args: Any, **kwargs: Any) -> Any:
            binder = getattr(self.inner, "bind_sink", None)
            return binder(*args, **kwargs) if callable(binder) else None

        def close(self) -> None:
            self.inner.close()

    return replace(
        bundle, roles={role: _Override(provider) for role, provider in bundle.roles.items()}
    )


def _default_harness_runner(
    config: SmokeConfig, paths: SmokePaths, *, loader: Any, deps: SmokeDeps
) -> Any:
    module = _import_module("codepilot.eval.swebench_harness")
    harness_python = config.harness_python or module.default_harness_python(ROOT)
    snapshot_dir: Path | None = None
    try:
        # 官方 CLI 的 -d 接受本地数据集文件；传整目录会被 HarnessConfig 拒绝。
        snapshot_dir = Path(loader.snapshot_dir) / f"{config.split}.jsonl"
    except Exception:  # pragma: no cover - FakeLoader 可以没有 snapshot_dir
        snapshot_dir = None
    harness_config = module.HarnessConfig(
        predictions_path=paths.prediction,
        run_id=paths.run_id,
        report_dir=paths.evaluation_dir,
        max_workers=1,
        timeout_seconds=config.timeout,
        dataset_name=DATASET_NAME,
        split=config.split,
        instance_ids=(config.instance_id,),
        harness_python=harness_python,
        harness_version=HARNESS_VERSION,
        model_name_or_path=config.model_identity,
        dataset_revision=config.revision,
        dataset_snapshot=snapshot_dir,
        cache_level="instance",
    )
    return module.OfficialHarnessRunner(
        harness_config,
        platform=deps.platform,
        # 证据落 <artifact_root>/runs/<run_id>/（与推理现场同一 run 目录，便于追溯）。
        artifacts=_artifact_store(paths.artifact_root, paths.run_id),
    )


# --------------------------------------------------------------------------- 预算 / 隔离


def _build_budget(config: SmokeConfig) -> Any:
    from codepilot.schemas.budget import RepairBudget

    return RepairBudget(
        max_cost=config.budget,
        budget_currency=config.budget_currency,
        max_runtime_seconds=config.timeout,
    )


def _budget_payload(config: SmokeConfig, budget: Any) -> dict[str, Any]:
    payload = _jsonable(budget)
    payload.update(
        {
            "budget_currency": config.budget_currency,
            "max_cost_from_cli": config.budget,
            "cost_limit_enforced": config.budget is not None,
        }
    )
    return payload


def _resources_payload(config: SmokeConfig) -> dict[str, Any]:
    """Docker 资源请求值 vs 当前 M2 沙箱实际生效值。

    M2 的容器限额在 ``sandbox/docker.py`` 的 ``container_config`` 里固定（2 CPU / 4g），
    smoke 的 ``--docker-cpus/--docker-memory`` 只作为**请求值**记录，不能声称已生效，
    否则报告里的资源口径会和实际执行的容器不一致。
    """
    return {
        "requested": {
            "docker_cpus": config.docker_cpus,
            "docker_memory": config.docker_memory,
            "timeout_seconds": config.timeout,
        },
        "effective": {
            "nano_cpus": 2_000_000_000,
            "mem_limit": "4g",
            "source": "codepilot.sandbox.docker.container_config（固定策略）",
        },
        "honest_note": "请求值只记录，不声称已改变沙箱限额；真实运行前需先改沙箱策略",
    }


_GOLD_FIELD_NAMES = frozenset(
    {
        "patch",
        "gold_patch",
        "test_patch",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "fail_to_pass",
        "pass_to_pass",
        "hints_text",
        "gold_patch_sha256",
        "test_patch_sha256",
    }
)


def _assert_inference_task_clean(task: Any) -> dict[str, Any]:
    """推理侧对象不得带 Gold/隐藏测试字段（fail closed）。"""
    model_fields = getattr(type(task), "model_fields", {}) or {}
    leaked = sorted(_GOLD_FIELD_NAMES.intersection(model_fields))
    if leaked:
        raise SmokeError(f"推理任务对象含评测字段，拒绝开跑: {leaked}")
    serialized = getattr(task, "model_dump", None)
    if callable(serialized):
        text = json.dumps(_jsonable(serialized(mode="json")), ensure_ascii=False)
        leaked_keys = sorted(name for name in _GOLD_FIELD_NAMES if f'"{name}"' in text)
        if leaked_keys:
            raise SmokeError(f"推理任务序列化里出现评测字段，拒绝开跑: {leaked_keys}")
    return {
        "checked_fields": len(model_fields),
        "gold_fields_present": [],
        "forbidden_field_names": sorted(_GOLD_FIELD_NAMES),
    }


# --------------------------------------------------------------------------- 预测


def _classify_prediction(outcome: Any) -> Any:
    from codepilot.eval.swebench_prediction import PredictionStatus

    patch = str(getattr(outcome, "final_patch", "") or "")
    status = str(getattr(outcome, "status", "") or "")
    stop_reason = str(getattr(outcome, "stop_reason", "") or "")
    error_class = str(getattr(outcome, "error_class", "") or "")
    empty = bool(getattr(outcome, "empty_patch", not patch.strip()))
    invalid = bool(getattr(outcome, "invalid_patch", False))

    if not patch.strip() or empty:
        if error_class in {"env_error", "sandbox_error"} or status == "sandbox_error":
            return PredictionStatus.ENV_ERROR
        if status == "budget_exhausted" or stop_reason.startswith("budget_"):
            return PredictionStatus.BUDGET_EXHAUSTED
        if status in {"plan_rejected"}:
            return PredictionStatus.MODEL_OUTPUT_INVALID
        # 正常停止但没产出补丁就是 EMPTY_PATCH；只有工作流自身失败/中断才归
        # WORKFLOW_STOPPED，否则"模型改不动"会被误算成基础设施故障。
        if status in {"error", "workflow_error"} or error_class:
            return PredictionStatus.WORKFLOW_STOPPED
        return PredictionStatus.EMPTY_PATCH
    if invalid:
        return PredictionStatus.INVALID_PATCH
    return PredictionStatus.PATCH_READY


def _default_write_prediction(
    config: SmokeConfig, paths: SmokePaths, task: Any, outcome: Any, *, allow_existing: bool
) -> dict[str, Any]:
    module = _import_module("codepilot.eval.swebench_prediction")
    status = _classify_prediction(outcome)
    patch = str(getattr(outcome, "final_patch", "") or "")
    writer = module.PredictionWriter(
        paths.prediction,
        model_name_or_path=config.model_identity,
        run_id=paths.run_id,
        dataset=task.dataset,
        allow_existing=allow_existing,
    )
    if status is module.PredictionStatus.PATCH_READY:
        entry = module.PredictionEntry.patch_ready(
            instance_id=config.instance_id,
            run_id=paths.run_id,
            model_name_or_path=config.model_identity,
            patch=patch,
            source="swebench_smoke/inference",
        )
    else:
        entry = module.PredictionEntry.not_ready(
            instance_id=config.instance_id,
            run_id=paths.run_id,
            model_name_or_path=config.model_identity,
            status=status,
            error_kind=str(getattr(outcome, "error_class", "") or "") or None,
            error_detail="; ".join(getattr(outcome, "failure_reasons", ()) or ()) or None,
            source="swebench_smoke/inference",
        )
    writer.add(entry)
    manifest = writer.write()
    return {
        "status": status.value,
        "path": str(paths.prediction),
        "predictions_sha256": manifest.predictions_sha256,
        "predictions_bytes": manifest.predictions_bytes,
        "written": manifest.written,
        "skipped": manifest.skipped,
        "status_counts": dict(manifest.status_counts),
        "manifest_path": str(paths.prediction.with_suffix(".manifest.json")),
        "manifest": _jsonable(manifest),
        "patch_sha256": _sha256_text(patch) if patch.strip() else None,
    }


# --------------------------------------------------------------------------- 环境 / 推理


def _environment_payload(identity: Any) -> dict[str, Any]:
    return _jsonable(identity)


#: Owner C 环境身份里参与"镜像身份摘要"的字段。只取镜像 + spec 哈希，不取
#: base_commit/instance_id：同一个镜像可以服务多个实例，换补丁不该让环境身份变化。
_ENV_IDENTITY_FIELDS = (
    "instance_image_key",
    "env_image_key",
    "base_image_key",
    "environment_hash",
)


def _environment_identity_digest(environment: Mapping[str, Any]) -> str | None:
    """把 Owner C 的环境身份压成稳定摘要，供 F 的评测信封与 D 的缓存身份使用。"""
    fields = {name: environment.get(name) for name in _ENV_IDENTITY_FIELDS}
    if not all(isinstance(value, str) and value for value in fields.values()):
        return None
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _image_ids_for_environment(
    instance_id: str, environment: Mapping[str, Any]
) -> dict[str, str] | None:
    """D 的 ``image_ids``：同一实例的 instance/env/base 三层镜像身份。"""
    instance_key = environment.get("instance_image_key")
    env_key = environment.get("env_image_key")
    base_key = environment.get("base_image_key")
    if not all(isinstance(value, str) and value for value in (instance_key, env_key, base_key)):
        return None
    return {instance_id: f"{instance_key}|{env_key}|{base_key}"}


def _artifact_store(root: Path, run_id: str) -> Any:
    """构造官方 harness 用的 ArtifactStore：证据落在 ``<root>/runs/<run_id>/``。"""
    from codepilot.artifacts import ArtifactStore

    return ArtifactStore(Path(root), run_id)


def _failure_payload(exc: BaseException) -> dict[str, Any]:
    """异常 → 归因字段；未知异常默认 HARNESS_INTERNAL_ERROR（不替 Harness 免责）。"""
    from codepilot.errors import classify_exception

    failure = classify_exception(exc)
    raw_kind = getattr(exc, "kind", None)
    return {
        "error_class": failure.failure_class.value,
        "error_origin": failure.origin.value,
        "error_kind": str(raw_kind) if raw_kind is not None else failure.failure_class.value,
        "error_detail": failure.message,
    }


def _run_inference(
    config: SmokeConfig,
    paths: SmokePaths,
    task: Any,
    *,
    deps: SmokeDeps,
    budget: Any,
    bundle: Any,
) -> Any:
    if deps.inference_executor is not None:
        return deps.inference_executor(task, config)
    from codepilot.eval.assembly import InferenceAssembly

    manager = deps.workspace_manager or _default_workspace_manager(config)
    sandbox_factory = deps.sandbox_factory or _default_sandbox_factory(config)
    assembly = InferenceAssembly(
        task=task,
        workspace_manager=manager,
        work_root=paths.artifact_root,
        sandbox_factory=sandbox_factory,
        provider_bundle=bundle,
        budget=budget,
        with_reproducer=config.live_llm,
        allow_new_tests=False,
        record_llm=config.record_llm,
        # 图级恢复由 runner 决定；本脚本的普通链路一律开 checkpoint，便于人工
        # 中断后由 `--resume graph` 接上，而不是每次都从头再花一遍钱。
        checkpoint=True,
    )
    return assembly.run(run_id=paths.run_id, seed=config.seed)


def _ensure_environment(
    config: SmokeConfig, task: Any, deps: SmokeDeps
) -> tuple[dict[str, Any], Any, str | None]:
    """返回 (环境报告, adapter, 错误信息)。错误只记录，不在这里伪造结论。"""
    adapter = deps.environment_adapter or _default_environment_adapter(config)
    try:
        identity = adapter.identity(task)
    except Exception as exc:
        return {"error": str(exc), "kind": getattr(exc, "kind", None)}, adapter, str(exc)
    payload = _environment_payload(identity)
    payload["identity_digest"] = _environment_identity_digest(payload)
    if config.dry_run:
        payload["ensured"] = False
        payload["identity_verified"] = False
        payload["note"] = "dry-run 不建镜像/不 ensure；只校验官方规格与身份可计算"
        return payload, adapter, None
    try:
        handle = adapter.ensure(identity)
    except Exception as exc:
        payload["ensured"] = False
        payload["identity_verified"] = False
        payload["error"] = str(exc)
        payload["kind"] = getattr(exc, "kind", None)
        return payload, adapter, str(exc)
    payload["ensured"] = True
    payload["handle"] = _jsonable(handle)
    # 只有 identity digest 存在**且** ensure 真的校验过镜像，才算"环境身份已验证"；
    # 否则 D/F 的缓存里 image_ids/environment_identity 为空，不能声称已绑定镜像。
    payload["identity_verified"] = payload["identity_digest"] is not None
    return payload, adapter, None


def _harness_payload(execution: Any) -> dict[str, Any]:
    verdicts = []
    for item in getattr(execution, "verdicts", ()) or ():
        verdicts.append(
            {
                "instance_id": getattr(item, "instance_id", None),
                "verdict": _jsonable(getattr(item, "verdict", None)),
                "resolved": getattr(item, "resolved", None),
                "report_path": _jsonable(getattr(item, "report_path", None)),
                "report_sha256": getattr(item, "report_sha256", None),
                "error_kind": _jsonable(getattr(item, "error_kind", None)),
                "error_detail": getattr(item, "error_detail", None),
                "runtime_seconds": getattr(item, "runtime_seconds", None),
            }
        )
    report = getattr(execution, "report", None)
    return {
        "run_id": getattr(execution, "run_id", None),
        "command": _jsonable(getattr(execution, "command", None)),
        "decision": _jsonable(getattr(execution, "decision", None)),
        "exit_code": getattr(execution, "exit_code", None),
        "timed_out": getattr(execution, "timed_out", None),
        "runtime_seconds": getattr(execution, "runtime_seconds", None),
        "error_kind": _jsonable(getattr(execution, "error_kind", None)),
        "error_detail": getattr(execution, "error_detail", None),
        "reused": getattr(execution, "reused", None),
        "reused_reason": getattr(execution, "reused_reason", None),
        "warnings": list(getattr(execution, "warnings", ()) or ()),
        "verdicts": verdicts,
        "report_path": _jsonable(getattr(execution, "report_path", None)),
        "report_sha256": getattr(execution, "report_sha256", None),
        "report": _jsonable(report),
        "counts_toward_resolved_rate": getattr(execution, "included_in_resolved_rate", True),
    }


def _official_verdict(harness: Mapping[str, Any]) -> str:
    for item in harness.get("verdicts", []):
        if item.get("instance_id") == harness_instance(harness):
            return str(item.get("verdict"))
    return "NOT_EVALUATED"


def harness_instance(harness: Mapping[str, Any]) -> str | None:
    for item in harness.get("verdicts", []):
        return str(item.get("instance_id"))
    return None


def _m2_vs_official(outcome: Any, harness: Mapping[str, Any] | None) -> dict[str, Any]:
    """内部 M2 声称与官方结论的对照；官方结论永远优先。"""
    claims = bool(getattr(outcome, "agent_claims_success", False))
    m2 = "M2_CLAIMS_SUCCESS" if claims else "M2_DOES_NOT_CLAIM_SUCCESS"
    if harness is None:
        return {
            "internal_m2": m2,
            "official": "NOT_EVALUATED",
            "discrepancy": "OFFICIAL_NOT_EVALUATED",
            "official_authoritative": True,
        }
    official = _official_verdict(harness)
    if official == "RESOLVED":
        discrepancy = "CONSISTENT" if claims else "INTERNAL_NOT_CLAIM_OFFICIAL_RESOLVED"
    elif official == "UNRESOLVED":
        discrepancy = "CONSISTENT" if not claims else "INTERNAL_CLAIM_OFFICIAL_UNRESOLVED"
    else:
        discrepancy = "OFFICIAL_NOT_EVALUATED"
    return {
        "internal_m2": m2,
        "official": official,
        "discrepancy": discrepancy,
        "official_authoritative": True,
        "note": "官方 resolved 是唯一结论来源；内部 M2 状态不参与判定",
    }


# --------------------------------------------------------------------------- 主链路


def _paths_existing(paths: SmokePaths) -> list[Path]:
    return [
        path
        for path in (paths.run_dir, paths.prediction, paths.evaluation_dir)
        if Path(path).exists()
    ]


def _prepare_paths(config: SmokeConfig, paths: SmokePaths) -> None:
    """检查产物路径：fresh 运行不得静默覆盖旧现场；恢复时允许旧现场存在。"""
    if not config.dry_run and config.resume is None:
        existing = _paths_existing(paths)
        if existing:
            raise SmokeError(
                "目标产物已存在，拒绝静默覆盖；换 --run-label 或显式 --resume: "
                + ", ".join(str(item) for item in existing)
            )
    for path in (
        paths.artifact_root,
        paths.prediction.parent,
        paths.evaluation_dir,
        paths.usage.parent,
        paths.cost.parent,
        paths.report.parent,
        paths.journal.parent,
    ):
        Path(path).mkdir(parents=True, exist_ok=True)


def _write_dry_run_prediction(config: SmokeConfig, paths: SmokePaths) -> dict[str, Any]:
    """dry-run 用**显式标记**的占位预测文件验证官方命令拼装，随后删除。"""
    patch = (
        "diff --git a/DRY-RUN-NO-REAL-PATCH b/DRY-RUN-NO-REAL-PATCH\n"
        "--- a/DRY-RUN-NO-REAL-PATCH\n"
        "+++ b/DRY-RUN-NO-REAL-PATCH\n"
        "@@ -1 +1 @@\n"
        "-dry run placeholder\n"
        "+dry run placeholder\n"
    )
    record = {
        "instance_id": config.instance_id,
        "model_name_or_path": config.model_identity,
        "model_patch": patch,
    }
    paths.prediction.parent.mkdir(parents=True, exist_ok=True)
    paths.prediction.write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return {"patch": patch, "sha256": _sha256_text(patch), "path": str(paths.prediction)}


def _dry_run_report(
    config: SmokeConfig,
    paths: SmokePaths,
    *,
    loader: Any,
    task: Any,
    environment: Mapping[str, Any],
    budget: Any,
    deps: SmokeDeps,
    started: float,
) -> dict[str, Any]:
    """dry-run：只构造官方命令与检查路径，不写 prediction、不执行子进程。

    Owner D 提供了 ``preview_command()``（不读 predictions、不 import swebench），
    因此这里优先用它；只有注入的 MockHarness 没有该接口时才退回"占位预测文件 +
    ``run(dry_run=True)``"，且占位文件一定会被删除。
    """
    harness = deps.harness_runner or _default_harness_runner(
        config, paths, loader=loader, deps=deps
    )
    preview = getattr(harness, "preview_command", None)
    placeholder: dict[str, Any] | None = None
    if callable(preview):
        command = _jsonable(preview())
        harness_payload: dict[str, Any] = {
            "command": command,
            "verdicts": [],
            "decision": "DRY_RUN",
            "error_kind": None,
            "error_detail": "preview_command：未执行官方 harness",
        }
    else:  # pragma: no cover - 真实 runner 有 preview_command；仅为注入替身保留
        placeholder = _write_dry_run_prediction(config, paths)
        try:
            execution = harness.run(dry_run=True)
            harness_payload = _harness_payload(execution)
        finally:
            paths.prediction.unlink(missing_ok=True)
            paths.prediction.with_suffix(".manifest.json").unlink(missing_ok=True)
        command = harness_payload.get("command") or {}
    argv = list(command.get("argv") or [])
    expected = [
        "-m",
        "swebench.harness.run_evaluation",
        "-d",
        "-s",
        "-i",
        "-p",
        "-id",
        "--max_workers",
        "-t",
        "--report_dir",
    ]
    missing = [item for item in expected if item not in argv]
    if missing:
        raise SmokeError(f"官方 harness 命令缺少参数 {missing}: {argv}")
    return {
        "status": "DRY_RUN",
        "instance": _instance_payload(task),
        "environment": dict(environment),
        "budget": _budget_payload(config, budget),
        "paths": _paths_payload(paths, dry_run=True),
        "dry_run_checks": {
            "prediction_placeholder": placeholder,
            "placeholder_removed": placeholder is None or not paths.prediction.exists(),
            "checks": {
                "command_argv_has_official_flags": not missing,
                "command_source": "preview_command" if placeholder is None else "run(dry_run=True)",
                "expected_report": command.get("expected_report"),
                "predictions_path": str(paths.prediction),
                "llm_called": False,
                "docker_ensure_called": False,
                "workspace_created": False,
                "harness_executed": False,
            },
        },
        "harness": harness_payload,
        "prediction": {
            "status": "DRY_RUN_ONLY",
            "path": str(paths.prediction),
            "note": "dry-run 不生成 prediction；占位文件（若用到）已在结束时删除",
        },
        "isolation": {
            "gold_fields_absent": True,
            "inference_task_clean": True,
            "workspace_eval_dir_absent": True,
        },
        "latency_seconds": round(time.perf_counter() - started, 3),
    }


def _instance_payload(task: Any) -> dict[str, Any]:
    issue = str(getattr(task, "issue", "") or "")
    dataset = getattr(task, "dataset", None)
    return {
        "instance_id": str(getattr(task, "task_id", "")),
        "repo": str(getattr(task, "repo", "")),
        "base_commit": str(getattr(task, "base_commit", "")),
        "version": str(getattr(task, "version", "")),
        "mode": str(getattr(task, "mode", "")),
        "split": str(getattr(getattr(task, "dataset", None), "split", "") or ""),
        "dataset": _jsonable(dataset),
        "problem_statement_chars": len(issue),
        "problem_statement_sha256": _sha256_text(issue),
    }


def _paths_payload(paths: SmokePaths, *, dry_run: bool) -> dict[str, Any]:
    return {
        "artifact_root": str(paths.artifact_root),
        "run_id": paths.run_id,
        "run_dir": str(paths.run_dir),
        "workspace": str(paths.workspace),
        "checkpoint": str(paths.checkpoint),
        "prediction": str(paths.prediction),
        "evaluation_dir": str(paths.evaluation_dir),
        "usage": str(paths.usage),
        "cost": str(paths.cost),
        "report": str(paths.report),
        "journal": str(paths.journal),
        "created_for_dry_run": dry_run,
    }


def _write_usage_and_cost(
    config: SmokeConfig, paths: SmokePaths, outcome: Any
) -> tuple[Any, dict[str, Any]]:
    usage = getattr(outcome, "usage", None)
    usage_payload = _jsonable(usage) if usage is not None else {}
    cost = {
        "budget_currency": config.budget_currency,
        "known_cost": getattr(usage, "cost", None) if usage is not None else None,
        "cost_is_lower_bound": bool(getattr(usage, "cost_is_lower_bound", False)),
        "unknown_cost_calls": getattr(usage, "cost_unknown_calls", None),
        "budget_max_cost": config.budget,
        "simulated_llm": bool(getattr(outcome, "simulated_llm", True)),
        # 这句话在真实运行里是错的：把"记账链路已接通"的免责写成"这不是真钱"，会让一份
        # 真实费用的报告被读成演示数据。按引擎分叉，两种场合各自说自己的事实。
        "note": (
            "simulated_llm=true：这里的 cost 只证明记账链路接通，不构成真实费用证据"
            if bool(getattr(outcome, "simulated_llm", True))
            else "真实 provider 记账：金额由服务商回传的用量与配置单价算出；"
            "unknown_cost_calls>0 时它是下界而不是总额"
        ),
    }
    _atomic_write_json(paths.usage, usage_payload)
    _atomic_write_json(paths.cost, cost)
    return usage, cost


def _model_run_status(config: SmokeConfig, bundle: Any) -> str:
    """真实模型是否真的被调用过；FakeLLM/dry-run 一律 LIVE_NOT_RUN。"""
    if config.dry_run or bundle is None:
        return LIVE_NOT_RUN
    if config.live_llm and not bool(getattr(bundle, "simulated", True)):
        return LIVE_RUN
    return LIVE_NOT_RUN


def _runtime_payload(config: SmokeConfig, bundle: Any) -> dict[str, Any]:
    roles = getattr(bundle, "roles", {}) or {}
    return {
        "provider": config.provider,
        "model": config.model,
        "model_identity": config.model_identity,
        "temperature": config.temperature,
        "max_output_tokens": config.max_output_tokens,
        "simulated_llm": bool(getattr(bundle, "simulated", True)),
        "provider_model_identity": getattr(bundle, "model_identity", None),
        "roles": sorted(roles) if roles else [],
        "live_llm_requested": config.live_llm,
    }


def run_smoke(config: SmokeConfig, *, deps: SmokeDeps | None = None) -> dict[str, Any]:
    """跑一次完整的单实例 smoke，返回写盘后的报告 payload。

    ``deps`` 为空 = 生产路径（真实 Loader/Environment/M2 图/Harness）。
    离线测试注入 FakeLLM/FakeSandbox/MockHarness，但调用链本身不变。
    """
    deps = deps or SmokeDeps()
    started = time.perf_counter()
    _require_authored(config, deps)
    paths = SmokePaths.build(config)
    _prepare_paths(config, paths)

    base: dict[str, Any] = {
        "schema_version": 1,
        "kind": "swebench_smoke",
        "generated_at": _now(),
        "run_id": paths.run_id,
        "run_label": config.run_label,
        "dry_run": config.dry_run,
        "live_llm_requested": config.live_llm,
        "model_run_status": LIVE_NOT_RUN,
        "dataset": {
            "name": DATASET_NAME,
            "revision": config.revision,
            "harness_version": HARNESS_VERSION,
            "split": config.split,
        },
        "paths": _paths_payload(paths, dry_run=config.dry_run),
        "budget": _budget_payload(config, _build_budget(config)),
        "resources": _resources_payload(config),
        "errors": [],
    }

    try:
        loader = deps.loader or _default_loader(config)
        loader.load_dataset(config.split)
        task = loader.get_inference_task(config.split, config.instance_id)
        isolation = {"inference_task": _assert_inference_task_clean(task)}
        environment, _adapter, environment_error = _ensure_environment(config, task, deps)

        if environment_error is not None and not config.dry_run:
            base.update(
                {
                    "status": "ENV_ERROR",
                    "attribution": _attribution_value("ENV_ERROR"),
                    "attribution_detail": {
                        "reason": "环境未就绪：推理与官方评测均未开始",
                        "environment_error": environment_error,
                        "environment_kind": environment.get("kind"),
                    },
                    "instance": _instance_payload(task),
                    "environment": environment,
                    "isolation": isolation,
                    "errors": [f"环境未就绪: {environment_error}"],
                    "latency_seconds": round(time.perf_counter() - started, 3),
                }
            )
            _atomic_write_json(paths.report, base)
            return {**base, "report_path": str(paths.report)}

        if config.dry_run:
            payload = _dry_run_report(
                config,
                paths,
                loader=loader,
                task=task,
                environment=environment,
                budget=_build_budget(config),
                deps=deps,
                started=started,
            )
            base.update(payload)
            base["isolation"] = {**base.get("isolation", {}), **payload.pop("isolation", {})}
            base["model_run_status"] = LIVE_NOT_RUN
            # dry-run 没有执行任何评测，归因字段显式留空而不是伪装成某种失败。
            base.setdefault("attribution", None)
            base.setdefault(
                "attribution_detail", {"reason": "dry-run：只验证装配/命令/路径，未执行评测"}
            )
            _atomic_write_json(paths.report, base)
            return {**base, "report_path": str(paths.report)}

        if config.resume is not None:
            return _run_resume(
                config,
                paths,
                deps=deps,
                loader=loader,
                task=task,
                environment=environment,
                isolation=isolation,
                started=started,
            )

        bundle = deps.provider_for_assembly()
        if bundle is None:
            _require_authored(config, SmokeDeps())
            bundle = _default_provider_bundle(config)
        bundle = _wrap_request_overrides(bundle, config)
        base["model_run_status"] = _model_run_status(config, bundle)
        budget = _build_budget(config)
        outcome = _run_inference(config, paths, task, deps=deps, budget=budget, bundle=bundle)
        allow_existing = config.resume == "evaluation"
        if deps.prediction_writer_factory is not None:
            prediction = deps.prediction_writer_factory(
                config, paths, task, outcome, allow_existing=allow_existing
            )
        else:
            prediction = _default_write_prediction(
                config, paths, task, outcome, allow_existing=allow_existing
            )
        usage, cost = _write_usage_and_cost(config, paths, outcome)
        if (
            cost["known_cost"] is not None
            and config.budget is not None
            and float(cost["known_cost"]) > config.budget
        ):
            base["errors"].append(
                f"费用 {cost['known_cost']} 超过 --budget {config.budget}（记录但不改判）"
            )

        harness_payload: dict[str, Any] | None = None
        harness_error: str | None = None
        if prediction["status"] == "PATCH_READY":
            try:
                harness = deps.harness_runner or _default_harness_runner(
                    config, paths, loader=loader, deps=deps
                )
                execution = harness.run(dry_run=False)
                harness_payload = _harness_payload(execution)
            except Exception as exc:
                # 异常路径必须保留可归因的 error_kind/error_class（P1-3），不能只留
                # 一句字符串；未知异常按 HARNESS_INTERNAL_ERROR 归因。
                failure = _failure_payload(exc)
                harness_error = failure["error_detail"]
                harness_payload = {
                    "error_detail": harness_error,
                    "error_kind": failure["error_kind"],
                    "error_class": failure["error_class"],
                    "error_origin": failure["error_origin"],
                    "verdicts": [],
                }
        else:
            harness_payload = {
                "verdicts": [
                    {
                        "instance_id": config.instance_id,
                        "verdict": "NOT_EVALUATED",
                        "resolved": None,
                        "error_kind": None,
                        "error_detail": (
                            f"prediction status={prediction['status']}，"
                            "没有可提交的合法补丁，未执行官方 harness"
                        ),
                    }
                ],
                "skipped": True,
            }

        attribution, attribution_detail = _attribution(
            outcome=outcome,
            prediction=prediction,
            harness=harness_payload,
            harness_error=harness_error,
        )
        base.update(
            {
                "status": _overall_status(prediction, harness_payload, harness_error),
                "attribution": attribution,
                "attribution_detail": attribution_detail,
                "instance": _instance_payload(task),
                "environment": environment,
                "isolation": isolation,
                "runtime": _runtime_payload(config, bundle),
                "inference": _inference_payload(outcome),
                "prediction": prediction,
                "harness": harness_payload,
                "m2_vs_official": _m2_vs_official(outcome, harness_payload),
                "usage": _jsonable(usage) if usage is not None else {},
                "cost": cost,
                "latency_seconds": round(time.perf_counter() - started, 3),
            }
        )
        if harness_error:
            base["errors"].append(f"官方 harness 调用失败: {harness_error}")
        _atomic_write_json(paths.report, base)
        return {**base, "report_path": str(paths.report)}
    except SmokeError:
        raise
    except Exception as exc:
        failure = _failure_payload(exc)
        payload = {
            **base,
            "status": "SMOKE_FAILED",
            "attribution": _attribution_value("HARNESS_INTERNAL_ERROR"),
            "attribution_detail": {
                **failure,
                "reason": "smoke 执行期未捕获异常；未知错误默认按 harness 内部错误归因",
            },
            "error_class": failure["error_class"],
            "error_origin": failure["error_origin"],
            "errors": [*base.get("errors", []), str(exc)],
            "latency_seconds": round(time.perf_counter() - started, 3),
        }
        _atomic_write_json(paths.report, payload)
        raise SmokeError(f"smoke 执行失败: {exc}") from exc


def _overall_status(
    prediction: Mapping[str, Any], harness: Mapping[str, Any] | None, harness_error: str | None
) -> str:
    if prediction.get("status") != "PATCH_READY":
        return "SMOKE_COMPLETED_NO_PATCH"
    if harness_error is not None:
        return "HARNESS_ERROR"
    error_kind = str(harness.get("error_kind") or "") if harness else ""
    if error_kind and error_kind != "None":
        return "HARNESS_ERROR"
    verdict = _official_verdict(harness or {})
    if verdict == "RESOLVED":
        return "SMOKE_COMPLETED_RESOLVED"
    if verdict == "UNRESOLVED":
        return "SMOKE_COMPLETED_UNRESOLVED"
    return "SMOKE_COMPLETED"


def _attribution_value(name: str) -> str:
    """取 F 的 SWEBenchAttribution 值（按成员名，枚举值是小写），避免再造一套字符串。

    跨模块枚举成员名若后续改名，这里回退到 ``HARNESS_INTERNAL_ERROR`` 而不是抛
    ValueError——归因字段缺失/未知时默认按基础设施内部错误处理，不能把报告写失败。
    """
    from codepilot.eval.swebench_runner import SWEBenchAttribution

    try:
        return SWEBenchAttribution[name].value
    except KeyError:  # pragma: no cover - F 改名时的安全兜底
        return SWEBenchAttribution.HARNESS_INTERNAL_ERROR.value


def _attribution(
    *,
    outcome: Any,
    prediction: Mapping[str, Any] | None,
    harness: Mapping[str, Any] | None,
    harness_error: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """把失败压成 F 的 ``SWEBenchAttribution`` 一级字段（P1-3）。

    判定顺序与 F 的 ``classify_outcome`` 一致：先看环境/接入是否根本没跑成，再看
    官方结论（官方优先），最后才是补丁形态。``attribution_detail`` 保留原始
    ``prediction.status`` / ``harness.error_kind``，不替代它们。
    """
    pred_status = str((prediction or {}).get("status") or "")
    harness_error_kind = str((harness or {}).get("error_kind") or "")
    official = _official_verdict(harness or {}) if harness else "NOT_EVALUATED"
    detail = {
        "prediction_status": pred_status or None,
        "harness_error_kind": harness_error_kind or None,
        "harness_error_detail": (harness or {}).get("error_detail"),
        "official_verdict": official,
        "m2_claims_success": getattr(outcome, "agent_claims_success", None),
    }

    if harness_error is not None:
        return _attribution_value("HARNESS_INTERNAL_ERROR"), {
            **detail,
            "reason": "官方 harness 调用抛异常，按基础设施内部错误归因",
        }
    if pred_status == "ENV_ERROR":
        return _attribution_value("ENV_ERROR"), {**detail, "reason": "环境未就绪，推理未开始"}
    if pred_status == "BUDGET_EXHAUSTED":
        return _attribution_value("BUDGET_STOPPED"), {**detail, "reason": "预算停止"}
    if pred_status in {"INVALID_PATCH", "MODEL_OUTPUT_INVALID"}:
        return _attribution_value("PATCH_INVALID"), {**detail, "reason": "补丁非法/未通过校验"}
    if pred_status != "PATCH_READY":
        error_class = str(getattr(outcome, "error_class", "") or "")
        if error_class == "harness_internal_error":
            return _attribution_value("HARNESS_INTERNAL_ERROR"), {
                **detail,
                "reason": "推理工作流因 harness 内部错误终止，没有可提交补丁",
            }
        return _attribution_value("MODEL_NOT_RESOLVED"), {
            **detail,
            "reason": "模型没有产出可提交的合法补丁",
        }

    # PATCH_READY：官方结论优先。
    if official == "RESOLVED":
        return _attribution_value("OFFICIAL_RESOLVED"), detail
    if official == "UNRESOLVED":
        return _attribution_value("OFFICIAL_NOT_RESOLVED"), detail
    if official == "PATCH_APPLY_FAILED":
        return _attribution_value("PATCH_INVALID"), {**detail, "reason": "官方报告补丁应用失败"}
    kind = harness_error_kind.lower()
    if kind in {"env_error", "resource_blocked", "docker_unavailable", "platform_blocked"}:
        return _attribution_value("ENV_ERROR"), detail
    if kind:
        return _attribution_value("OFFICIAL_EVALUATION_ERROR"), detail
    return _attribution_value("MODEL_NOT_RESOLVED"), {
        **detail,
        "reason": "有合法补丁但官方未给出可判定结论",
    }


def _inference_payload(outcome: Any) -> dict[str, Any]:
    return {
        "run_id": getattr(outcome, "run_id", None),
        "status": getattr(outcome, "status", None),
        "stop_reason": getattr(outcome, "stop_reason", None),
        "iterations": getattr(outcome, "iterations", None),
        "attempts_saved": getattr(outcome, "attempts_saved", None),
        "test_runs": getattr(outcome, "test_runs", None),
        "runtime_seconds": getattr(outcome, "runtime_seconds", None),
        "empty_patch": getattr(outcome, "empty_patch", None),
        "invalid_patch": getattr(outcome, "invalid_patch", None),
        "agent_claims_success": getattr(outcome, "agent_claims_success", None),
        "verification_mode": getattr(outcome, "verification_mode", None),
        "simulated_llm": getattr(outcome, "simulated_llm", None),
        "sandbox_backend": getattr(outcome, "sandbox_backend", None),
        "final_patch_sha256": getattr(outcome, "final_patch_sha256", None),
        "final_patch_bytes": len(str(getattr(outcome, "final_patch", "") or "")),
        "error_class": getattr(outcome, "error_class", None),
        "error_origin": getattr(outcome, "error_origin", None),
        "failure_reasons": list(getattr(outcome, "failure_reasons", ()) or ()),
        "artifact_dir": getattr(outcome, "artifact_dir", None),
        "usage_uncertain": getattr(outcome, "usage_uncertain", None),
        "resumed_from_checkpoint": getattr(outcome, "resumed_from_checkpoint", None),
    }


# --------------------------------------------------------------------------- 恢复（委托 runner）


def _make_evaluator(
    config: SmokeConfig,
    deps: SmokeDeps,
    loader: Any,
    environment: Mapping[str, Any] | None = None,
) -> Callable[..., Any]:
    """runner 的单实例 evaluator 适配层：写 predictions → 跑官方 harness → 规范化。"""
    runner_module = _import_module("codepilot.eval.swebench_runner")
    prediction_module = _import_module("codepilot.eval.swebench_prediction")
    environment = environment or {}

    def evaluate(
        *,
        instance_id: str,
        evaluation_task: Any,
        patch: str,
        patch_sha256: str,
        seed: int,
        harness_run_id: str,
        run_dir: Path,
    ) -> Any:
        del evaluation_task, seed
        run_dir = Path(run_dir)
        predictions_path = run_dir / "predictions.jsonl"
        # P2-10：resume 路径也必须走 Owner A 的 normalize，避免两套写出口径漂移。
        # 但 F 传来的 patch_sha256 是身份绑定字段：若规范化会改变哈希，就不能悄悄
        # 改写内容（那会让 harness 的 patch hash 校验与 F 的记录不一致），保留原补丁。
        normalized = prediction_module.normalize_patch(patch)
        model_patch = normalized if _sha256_text(normalized) == patch_sha256 else patch
        record = {
            "instance_id": instance_id,
            "model_name_or_path": config.model_identity,
            "model_patch": model_patch,
        }
        predictions_path.parent.mkdir(parents=True, exist_ok=True)
        predictions_path.write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
        harness_module = _import_module("codepilot.eval.swebench_harness")
        harness_config = harness_module.HarnessConfig(
            predictions_path=predictions_path,
            run_id=harness_run_id,
            report_dir=run_dir / "official-report",
            max_workers=1,
            timeout_seconds=config.timeout,
            split=config.split,
            instance_ids=(instance_id,),
            harness_python=config.harness_python or harness_module.default_harness_python(ROOT),
            model_name_or_path=config.model_identity,
            dataset_revision=config.revision,
            dataset_snapshot=(
                Path(loader.snapshot_dir) / f"{config.split}.jsonl"
                if hasattr(loader, "snapshot_dir")
                else None
            ),
        )
        runner = harness_module.OfficialHarnessRunner(
            harness_config,
            platform=deps.platform,
            artifacts=_artifact_store(run_dir.parent.parent, run_dir.name),
            image_ids=_image_ids_for_environment(instance_id, environment),
        )
        execution = runner.run()
        verdict_value = str(execution.verdict_for(instance_id))
        execution_error_kind = str(execution.error_kind) if execution.error_kind else None
        # P1-1：F 的 evaluation_state 只在 verdict==ERROR 时记 ERROR。评测阶段失败
        # （平台不可执行/Docker 不可用/TIMEOUT/报告缺失）必须映射成 ERROR，否则会被
        # 记成"没跑过"，与"评测失败"混为一谈。
        if execution_error_kind is not None and verdict_value not in {"RESOLVED", "UNRESOLVED"}:
            verdict_value = "ERROR"
            resolved: bool | None = None
        else:
            resolved = {"RESOLVED": True, "UNRESOLVED": False}.get(verdict_value)
        harness_verdict = runner_module.OfficialVerdict(verdict_value)
        # P2-1：real_docker 由实际执行后端决定，不再硬编码。REUSED 报告不一定来自
        # 真实 Docker，因此只有"本次 EXECUTED/REBUILT + 成功退出 + 环境 ensure 过"
        # 才声称真实 Docker。
        real_docker = bool(
            environment.get("ensured")
            and _jsonable(getattr(execution, "decision", None)) in {"EXECUTED", "REBUILT"}
            and getattr(execution, "exit_code", None) == 0
        )
        return runner_module.SWEBenchEvaluation(
            verdict=harness_verdict,
            instance_id=instance_id,
            resolved=resolved,
            harness_run_id=harness_run_id,
            patch_sha256=patch_sha256,
            harness_version=HARNESS_VERSION,
            dataset_revision=config.revision,
            report_path=execution.report_path,
            report_sha256=execution.report_sha256,
            error_kind=execution_error_kind,
            error_detail=execution.error_detail,
            runtime_seconds=execution.runtime_seconds,
            real_docker=real_docker,
            reused=execution.reused,
            from_cache=execution.reused,
        )

    return evaluate


def _run_resume(
    config: SmokeConfig,
    paths: SmokePaths,
    *,
    deps: SmokeDeps,
    loader: Any,
    task: Any,
    environment: Mapping[str, Any],
    isolation: Mapping[str, Any],
    started: float,
) -> dict[str, Any]:
    """单实例恢复：委托 SWEBenchRunner，保持三层语义只实现一份。"""
    runner_module = _import_module("codepilot.eval.swebench_runner")
    assert config.resume is not None
    level = runner_module.ResumeLevel(config.resume)
    bundle_provider = deps.provider_factory or (
        (lambda: deps.provider_bundle) if deps.provider_bundle is not None else None
    )
    if bundle_provider is None:
        if not config.live_llm:
            raise SmokeError("恢复运行需要显式 --live-llm 或注入离线 ProviderBundle")
        bundle_provider = lambda: _default_provider_bundle(config)  # noqa: E731

    def inference_factory(task: Any, seed: int, *, run_id: str, resume: Any) -> Any:
        # runner 会用自己的确定性 run_id 调 assembly.run；工厂只负责按 resume 档位
        # 决定是否开 checkpoint（TASK 级根本不会走到这里）。
        del seed, run_id
        from codepilot.eval.assembly import InferenceAssembly

        manager = deps.workspace_manager or _default_workspace_manager(config)
        sandbox_factory = deps.sandbox_factory or _default_sandbox_factory(config)
        return InferenceAssembly(
            task=task,
            workspace_manager=manager,
            work_root=paths.artifact_root,
            sandbox_factory=sandbox_factory,
            provider_bundle=_wrap_request_overrides(bundle_provider(), config),
            budget=_build_budget(config),
            record_llm=config.record_llm,
            checkpoint=resume.enables(runner_module.ResumeLevel.GRAPH),
        )

    environment_identity = _environment_identity_digest(environment)
    run_config = runner_module.SWEBenchRunConfig(
        split=config.split,
        revision=config.revision,
        model_identity=config.model_identity,
        provider=config.provider,
        model=config.model or config.model_identity,
        provider_config_hash=config.config_hash(),
        budget_currency=config.budget_currency,
        max_total_cost=None,
        task_reservation=0.0,
        per_task_budget=_build_budget(config),
        run_label=f"swebench-smoke-{config.run_label}",
        # P1-2：把 Owner C 的环境身份注入 F 的评测信封；不同镜像/重建镜像时
        # 评测级恢复会拒绝复用旧报告，而不是 None==None 的弱校验。
        environment_identity=environment_identity,
    )
    if deps.runner is not None:
        report = deps.runner.run([config.instance_id], seed=config.seed, resume=level)
    else:
        runner = runner_module.SWEBenchRunner(
            loader=loader,
            inference_factory=inference_factory,
            evaluator=_make_evaluator(config, deps, loader, environment),
            journal_path=paths.journal,
            artifact_root=paths.artifact_root,
            config=run_config,
        )
        report = runner.run([config.instance_id], seed=config.seed, resume=level)
    metrics = report.metrics() if hasattr(report, "metrics") else {}
    payload = {
        "schema_version": 1,
        "kind": "swebench_smoke",
        "mode": "runner_resume",
        "generated_at": _now(),
        "run_id": paths.run_id,
        "run_label": config.run_label,
        "resume": config.resume,
        "resume_level": level.value,
        "live_llm_requested": config.live_llm,
        "model_run_status": (
            _model_run_status(config, deps.provider_bundle)
            if deps.provider_bundle is not None
            else (LIVE_RUN if config.live_llm else LIVE_NOT_RUN)
        ),
        "dataset": {
            "name": DATASET_NAME,
            "revision": config.revision,
            "harness_version": HARNESS_VERSION,
            "split": config.split,
        },
        "instance": _instance_payload(task),
        "environment": dict(environment),
        "environment_identity": {
            "digest": environment_identity,
            "verified": bool(environment.get("identity_verified"))
            and environment_identity is not None,
            "image_keys": {name: environment.get(name) for name in _ENV_IDENTITY_FIELDS[:3]},
            "injected_into": [
                "SWEBenchRunConfig.environment_identity",
                "OfficialHarnessRunner.image_ids",
            ],
        },
        "isolation": dict(isolation),
        "paths": _paths_payload(paths, dry_run=False),
        "budget": _budget_payload(config, _build_budget(config)),
        "resources": _resources_payload(config),
        "runner": {
            "run_id": getattr(report, "run_id", None),
            "stop_reason": getattr(report, "stop_reason", None),
            "reuse": _jsonable(getattr(report, "reuse", None)),
            "counts": _jsonable(report.counts()) if hasattr(report, "counts") else None,
            "records": _jsonable(getattr(report, "records", ())),
        },
        "metrics": _jsonable(metrics),
        "status": "RESUME_COMPLETED",
        # resume 的逐实例归因由 F 的 runner 统一记录；这里给出定位入口，不重复造。
        "attribution": None,
        "attribution_detail": {
            "note": "resume 模式：细粒度归因见 runner.records[].attribution 与 metrics.attribution_counts"
        },
        "errors": [],
        "latency_seconds": round(time.perf_counter() - started, 3),
    }
    _atomic_write_json(paths.report, payload)
    return {**payload, "report_path": str(paths.report)}


# --------------------------------------------------------------------------- A/B 两次运行


def run_smoke_set(
    config: SmokeConfig, *, repeats: int, deps: SmokeDeps | None = None
) -> list[dict[str, Any]]:
    """按 ``--repeats`` 跑 A/B 两次独立运行，并写对比报告。

    两次运行使用不同的 run_label → 不同 run_id → 不同 checkpoint / prediction /
    eval / usage / cost / artifact，互不共享；对比报告只汇总，不合并分母。
    """
    if repeats not in (1, 2):
        raise SmokeError("--repeats 只支持 1 或 2（A/B 两次独立运行）")
    deps = deps or SmokeDeps()
    labels = ["A", "B"] if repeats == 2 else [config.run_label]
    reports: list[dict[str, Any]] = []
    for label in labels:
        run_config = replace(config, run_label=label)
        reports.append(run_smoke(run_config, deps=deps))
    if repeats == 2:
        comparison = _comparison_payload(config, reports)
        pair_path = (
            Path(config.artifact_root).expanduser().absolute()
            / "comparisons"
            / f"swebench-smoke-{config.instance_id}-{config.config_hash()[:8]}-AB.json"
        )
        _atomic_write_json(pair_path, comparison)
        for report in reports:
            # P2-4：run_smoke() 已把报告写盘；只改内存返回字典会让"从磁盘重读
            # report"的验收脚本看不到 comparison_path。这里回写磁盘副本。
            report_path = Path(str(report.get("report_path") or ""))
            if report_path.is_file():
                on_disk = json.loads(report_path.read_text(encoding="utf-8"))
                on_disk["comparison_path"] = str(pair_path)
                _atomic_write_json(report_path, on_disk)
            report["comparison_path"] = str(pair_path)
    return reports


def _comparison_payload(
    config: SmokeConfig, reports: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    by_label = {str(report.get("run_label")): report for report in reports}
    entries = []
    for label, report in sorted(by_label.items()):
        inference = report.get("inference") or {}
        harness = report.get("harness") or {}
        entries.append(
            {
                "run_label": label,
                "run_id": report.get("run_id"),
                "status": report.get("status"),
                "report_path": report.get("report_path"),
                "prediction": (report.get("prediction") or {}).get("status"),
                "inference_status": inference.get("status"),
                "final_patch_sha256": inference.get("final_patch_sha256"),
                "official_verdict": _official_verdict(harness),
                "sandbox_backend": inference.get("sandbox_backend"),
                "simulated_llm": inference.get("simulated_llm"),
                "usage": report.get("usage"),
                "cost": report.get("cost"),
            }
        )
    patch_hashes = {entry["final_patch_sha256"] for entry in entries}
    equality = {
        # 没有补丁（dry-run/失败）时"是否相同"不适用，不能把 {None} 读成一致。
        "same_final_patch": (None if patch_hashes == {None} else len(patch_hashes) == 1),
        "distinct_run_ids": len({entry["run_id"] for entry in entries}) == len(entries),
        "independent_artifacts": len({json.dumps(entry["report_path"]) for entry in entries})
        == len(entries),
    }
    return {
        "schema_version": 1,
        "kind": "swebench_smoke_ab_comparison",
        "generated_at": _now(),
        "instance_id": config.instance_id,
        "split": config.split,
        "seed": config.seed,
        "runs": entries,
        "checks": equality,
        "note": "A/B 是两次独立运行，不合并分母；同一配置下 patch 不同不是错误",
    }


# --------------------------------------------------------------------------- CLI


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SWE-bench Lite 单实例完整链路 smoke（E-17）")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--split", default="test", choices=SPLITS)
    parser.add_argument("--revision", default=DATASET_REVISION)
    parser.add_argument("--provider", default="bigmodel")
    parser.add_argument("--model", default=None)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--docker-cpus", type=float, default=2.0)
    parser.add_argument("--docker-memory", default="4g")
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--spec-snapshot", type=Path, default=DEFAULT_SPEC_SNAPSHOT)
    parser.add_argument("--harness-python", type=Path, default=None)
    parser.add_argument("--run-label", default="A")
    parser.add_argument("--repeats", type=int, default=1, choices=(1, 2))
    parser.add_argument("--resume", nargs="?", const="task", choices=RESUME_LEVELS, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live-llm", action="store_true")
    parser.add_argument("--record-llm", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def _config_from_args(args: argparse.Namespace) -> SmokeConfig:
    return SmokeConfig(
        instance_id=args.instance_id,
        split=args.split,
        revision=args.revision,
        provider=args.provider,
        model=args.model,
        budget=args.budget,
        seed=args.seed,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
        timeout=args.timeout,
        docker_cpus=args.docker_cpus,
        docker_memory=args.docker_memory,
        artifact_root=args.artifact_root,
        snapshot_root=args.snapshot_root,
        spec_snapshot=args.spec_snapshot,
        harness_python=args.harness_python,
        run_label=args.run_label,
        dry_run=args.dry_run,
        live_llm=args.live_llm,
        resume=args.resume,
        record_llm=args.record_llm,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = _config_from_args(args)
        reports = run_smoke_set(config, repeats=args.repeats)
    except SmokeError as exc:
        print(f"[smoke] 失败: {exc}", file=sys.stderr)
        return 2
    summary = {
        # 顶部状态取所有 run 的最大口径：只要有一次真实模型运行就写 LIVE_RUN；
        # 本阶段交付的 FakeLLM/dry-run 永远是 LIVE_NOT_RUN。
        "model_run_status": (
            LIVE_RUN
            if any(report.get("model_run_status") == LIVE_RUN for report in reports)
            else LIVE_NOT_RUN
        ),
        "runs": [
            {
                "run_label": report.get("run_label"),
                "run_id": report.get("run_id"),
                "status": report.get("status"),
                "model_run_status": report.get("model_run_status", LIVE_NOT_RUN),
                "report_path": report.get("report_path"),
                "prediction": (report.get("prediction") or {}).get("status"),
                "official_verdict": _official_verdict(report.get("harness") or {}),
            }
            for report in reports
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
