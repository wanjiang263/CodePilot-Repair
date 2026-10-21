"""真实基线上 779 条授权回归 NodeID 的生产链路验证（零模型成本）。

跑的是**生产代码**：``build_test_baseline`` → ``RegressionRunner`` →
``DockerSandbox``（官方实例镜像）→ ``sandbox.junit`` 解析器。本脚本**不**调用任何
LLM，也**不**改 fixture / Gold / 隐藏测试；它只回答两个问题 —— 授权范围是否被**完整
执行**，结果是否能**准确回读**。

为什么要有这个脚本：FakeLLM 夹具的参数化 ID 全是 ``test_ok`` 这种最规整形式，而真实
marshmallow 基线里有一批含空格与引号的 pytest 参数化 ID。首轮真实 GLM 运行正是在这里
把整段回归炸掉的（``ValueError: test target must be a path or pytest NodeID``）。
只抽验几条参数化用例等于没验证 —— 覆盖缺口本身就是缺陷。

用法（WSL2，仓库副本的 ``.venv`` 内，DOCKER_HOST 指向 Docker Desktop 的 guest socket）：

    .venv/bin/python scripts/verify_baseline_nodeids.py \\
        --site /root/cp-run/phase11-nodeids-20261020 \\
        --mirror /root/cp-run/phase11-glm12-20261020/artifacts/repo-cache/8b675b79e9201815 \\
        --archived-state '/root/cp-run/phase11-glm12-20261020/artifacts/runs/*/state_final.json' \\
        --patch '/root/cp-run/phase11-glm12-20261020/artifacts/runs/*/patch_final.diff' \\
        --expected-image-id sha256:a61197e0b29461e34010cd83570424eb91a7f265574a81c82ff79e55c6c10f29

``--site`` 必须是不存在的新目录：既不覆盖任何历史产物，也让 Operation Journal 从空白
开始（复用同一个 run 目录会把上一次的结果当缓存读回来，那就不是实测了）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any

from codepilot.artifacts import ArtifactStore
from codepilot.clock import utcnow
from codepilot.eval.swebench import SWEBenchLoader
from codepilot.graph.deps import Deps
from codepilot.graph.regression import RegressionRunner
from codepilot.observability.events import EventSink
from codepilot.patch.diff import generate_git_diff
from codepilot.patch.validator import PatchValidator
from codepilot.repo.gitcmd import run_git
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.baseline import build_test_baseline
from codepilot.sandbox.docker import DockerSandbox, _validate_test_selection
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.junit import JUnitParseError, parse_junit
from codepilot.sandbox.swebench_env import instance_image_key
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus
from codepilot.schemas.execution import ExecutionResult
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.write_tools import validate_test_target

INSTANCE = "marshmallow-code__marshmallow-1343"
DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
HARNESS_PIN = "swebench==4.1.0"
EXPECTED_AUTHORIZED = 779
#: 官方镜像里的 pytest 入口：与真实实验的推理装配逐字同构（见下面的沙箱类）。
_OFFICIAL_PYTEST = "/opt/miniconda3/envs/testbed/bin/python -m pytest"

EVIDENCE: dict[str, Any] = {}
LOG: list[str] = []


def note(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    LOG.append(line)
    print(line, flush=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def first_match(pattern: str) -> Path:
    """按 glob 取**第一个**匹配（历史产物路径里带 run_id，只有调用方知道上一层）。

    锚点必须切在第一个含通配符的**路径段**之前：把 ``.../runs/*`` 整段当成根去
    ``glob()`` 永远匹配不到，pathlib 不展开锚点里的通配符。
    """
    candidate = Path(pattern)
    parts = candidate.parts
    anchor_index = next(
        (i for i, part in enumerate(parts) if any(ch in part for ch in "*?[")), None
    )
    if anchor_index is None:
        matches = [candidate] if candidate.is_file() else []
    else:
        root = Path(*parts[:anchor_index]) if anchor_index else Path(candidate.anchor)
        matches = sorted(root.glob(str(Path(*parts[anchor_index:]))))
    if not matches:
        raise SystemExit(f"没有匹配到的输入：{pattern}")
    return matches[0]


def dump(site: Path, name: str, payload: Any) -> Path:
    path = site / name
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return path


def docker_residue() -> dict[str, Any]:
    """本工具自己留下的容器与卷（只读核对；从不删别人的东西，也从不 prune）。

    Docker 不可用时**明确报错**而不是返回空清单：空的残留清单在这里等于"通过"，
    把读不到的东西当成干净是验收里最危险的一种假绿。
    """

    def _run(*cmd: str) -> list[str]:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [f"docker-unavailable: {type(exc).__name__}"]
        if out.returncode != 0:
            return [f"docker-error: {out.stderr.strip()[:200]}"]
        return [line for line in out.stdout.splitlines() if line.strip()]

    volumes = _run("docker", "volume", "ls", "--format", "{{.Name}}")
    containers = _run("docker", "ps", "-a", "--filter", "label=codepilot=1", "--format", "{{.ID}}")
    broken = [item for item in containers + volumes if str(item).startswith("docker-")]
    return {
        "owned_containers": containers,
        "owned_volumes": [name for name in volumes if name.startswith("codepilot-")],
        "readable": not broken,
        "read_error": broken[:2],
    }


class OfficialCachedSandbox(DockerSandbox):
    """与真实实验同一份沙箱语义：只用已缓存且身份核对过的官方实例镜像，绝不 build / pull。

    两条覆写与 ``scripts/swebench_smoke.py`` 里的 ``CachedOfficialSandbox`` 同构：
    ``build_env`` 先核对工作区 HEAD 就是 base commit，再按实例镜像名取**本地已有**镜像
    （镜像 ID 还必须等于批准保留的那一个）；``run_tests`` 把官方 test_command 里的
    ``pytest`` 改写成镜像内绝对解释器入口，并补上 ``PYTHONPATH=/testbed/src:/testbed``。
    """

    def __init__(self, *, instance_id: str, expected_image_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._instance_id = instance_id
        self._expected_image_id = expected_image_id

    def build_env(self, **kwargs: Any) -> EnvironmentBuildResult:
        head = run_git(["rev-parse", "HEAD"], cwd=kwargs["workspace"]).stdout.strip()
        if head != kwargs["base_commit"]:
            raise RuntimeError(f"official sandbox workspace base commit mismatch: {head}")
        image_ref = instance_image_key(self._instance_id)
        image = self.client.images.get(image_ref)
        if image.id != self._expected_image_id:
            raise RuntimeError(
                f"cached image identity changed: {image.id} != {self._expected_image_id}"
            )
        return EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref=image_ref,
            image_id=image.id,
            cache_key=f"official:{image.id}",
            from_cache=True,
        )

    def run_tests(self, **kwargs: Any) -> ExecutionResult:
        spec: EnvSpec = kwargs["env_spec"]
        command = re.sub(r"^(?:python(?:3)? -m )?pytest\b", _OFFICIAL_PYTEST, spec.test_command)
        kwargs["env_spec"] = spec.model_copy(
            update={
                "test_command": command,
                "env": {**spec.env, "PYTHONPATH": "/testbed/src:/testbed"},
            }
        )
        return super().run_tests(**kwargs)


# --------------------------------------------------------------------- 输入


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="779 条授权回归 NodeID 的生产链路验证")
    parser.add_argument("--site", required=True, help="本次验证的根目录（必须不存在）")
    parser.add_argument("--mirror", required=True, help="base-only 裸镜像仓库路径")
    parser.add_argument(
        "--archived-state", required=True, help="历史真实运行的 state_final.json（glob）"
    )
    parser.add_argument(
        "--patch", required=True, help="历史真实运行的候选补丁 patch_final.diff（glob）"
    )
    parser.add_argument("--expected-image-id", required=True, help="已批准保留的官方实例镜像 ID")
    parser.add_argument("--snapshot-root", default="cache/swebench", help="本地数据集快照目录")
    parser.add_argument(
        "--spec-snapshot",
        default="configs/swebench/env-specs-4.1.0.json",
        help="冻结的官方 4.1.0 env specs",
    )
    parser.add_argument("--run-label", default="nodeids", help="run_id 前缀")
    parser.add_argument(
        "--skip-timeout-probe",
        action="store_true",
        help="跳过超时/清理探针（默认执行：特殊字符 ID 小批 + timeout=1s）",
    )
    return parser.parse_args(argv)


def load_archived(pattern: str) -> dict[str, Any]:
    path = first_match(pattern)
    state = json.loads(path.read_text(encoding="utf-8"))
    baseline = state.get("baseline") or {}
    outcomes = baseline.get("outcomes") or {}
    if len(outcomes) != EXPECTED_AUTHORIZED:
        raise SystemExit(f"归档基线条数不是 {EXPECTED_AUTHORIZED}：{len(outcomes)}（{path}）")
    authorized = sorted(outcomes)
    return {
        "state_path": str(path),
        "run_id": state["run_id"],
        "base_commit": state["base_commit"],
        "authorized": authorized,
        "outcomes": outcomes,
        "authorized_sha256": sha256_text(json.dumps(authorized)),
        "flaky": sorted(baseline.get("flaky") or []),
        "duration_seconds": baseline.get("duration_seconds"),
        "complete": bool(baseline.get("complete")),
    }


# ------------------------------------------------------------ 纯判据核对（零容器）


def static_id_checks(authorized: list[str], workspace: Path) -> dict[str, Any]:
    """不花一分钱也要先把「解析 / 转义 / 注入」三条判据**全量**核一遍。"""
    policy = WorkspacePathPolicy(workspace)
    accepted, rejected = [], []
    for node in authorized:
        try:
            validate_test_target(policy, node)
            accepted.append(node)
        except ValueError as exc:
            rejected.append({"node_id": node, "reason": str(exc)})
    selection = _validate_test_selection(tuple(authorized), workspace=workspace)

    specials = [
        node for node in authorized if re.search(r"""[ "'`$;|&()]""", node.split("::", 1)[-1])
    ]
    quoted = " ".join(shlex.quote(node) for node in authorized)
    recovered = shlex.split(quoted)

    #: 注入面负控制：每一条都必须被**至少一层**判据拒掉。
    hostile = [
        "--co",
        "-x",
        "tests/test_validate.py -rf",
        "tests/; rm -rf /",
        "tests/$(id)",
        "tests/`whoami`",
        "tests/test_validate.py::t\nrm -rf /",
        "tests/test_validate.py::t\x00x",
        "/etc/passwd",
        "../outside/test_x.py::t",
        "tests/../../outside/test_x.py::t",
        "tests/test_validate.py::" + "x" * 2000,
        "",
        "   ",
    ]
    detail, leaked = [], []
    for item in hostile:
        record: dict[str, Any] = {"input": item[:80], "length": len(item)}
        for name, check in (
            ("target_validator", partial(validate_test_target, policy, item)),
            ("sandbox_selection", partial(_validate_test_selection, (item,), workspace=workspace)),
        ):
            try:
                check()
                record[f"{name}_verdict"] = "accepted"
            except ValueError as exc:
                record[f"{name}_verdict"] = f"rejected: {exc}"
        if not any(
            str(value).startswith("rejected") for key, value in record.items() if "_verdict" in key
        ):
            leaked.append(record)
        detail.append(record)

    return {
        "authorized_count": len(authorized),
        "accepted_by_target_validator": len(accepted),
        "rejected_by_target_validator": rejected,
        "accepted_by_sandbox_selection": len(selection),
        "ids_with_shell_special_characters": len(specials),
        "special_examples": specials,
        "shlex_roundtrip_exact": recovered == authorized,
        "shlex_recovered_count": len(recovered),
        "command_character_length": len(quoted),
        "hostile_total": len(hostile),
        "hostile_all_rejected": leaked == [],
        "hostile_leaked": leaked,
        "hostile_detail": detail,
    }


# --------------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    site = Path(args.site).resolve()
    if site.exists():
        raise SystemExit(f"--site 必须是不存在的新目录（不许覆盖任何历史产物）：{site}")
    site.mkdir(parents=True)
    started = time.time()
    EVIDENCE["command"] = {
        "argv": sys.argv[1:],
        "python": sys.version.split()[0],
        "cwd": str(Path.cwd()),
    }

    archived = load_archived(args.archived_state)
    patch_path = first_match(args.patch)
    patch_text = patch_path.read_text(encoding="utf-8")
    EVIDENCE["archived_identity"] = {
        "state_path": archived["state_path"],
        "run_id": archived["run_id"],
        "base_commit": archived["base_commit"],
        "authorized_count": len(archived["authorized"]),
        "authorized_sha256": archived["authorized_sha256"],
        "archived_baseline_complete": archived["complete"],
        "archived_flaky": archived["flaky"],
        "patch_source": str(patch_path),
        "patch_sha256": sha256_text(patch_text),
        "patch_bytes": len(patch_text.encode("utf-8")),
        "harness_pin": HARNESS_PIN,
    }
    dump(site, "input-manifest.json", EVIDENCE["archived_identity"])
    note(
        f"归档基线：{len(archived['authorized'])} 条授权 NodeID，"
        f"sha256={archived['authorized_sha256'][:16]}…，候选补丁 "
        f"{EVIDENCE['archived_identity']['patch_bytes']} B"
    )

    loader = SWEBenchLoader(
        Path(args.snapshot_root),
        dataset_name=DATASET_NAME,
        revision=DATASET_REVISION,
        env_spec_snapshot=Path(args.spec_snapshot),
    )
    inference = loader.get_inference_task("dev", INSTANCE)
    evaluation = loader.get_evaluation_task("dev", INSTANCE)
    if inference.base_commit != archived["base_commit"]:
        raise SystemExit("数据集 base commit 与归档基线不一致")
    fail_to_pass = list(evaluation.fail_to_pass)
    EVIDENCE["task_identity"] = {
        "instance_id": INSTANCE,
        "repo": inference.repo,
        "base_commit": inference.base_commit,
        "version": inference.version,
        "dataset_name": DATASET_NAME,
        "dataset_revision": DATASET_REVISION,
        "env_spec_sha256": sha256_text(inference.env_spec.model_dump_json()),
        "test_command": inference.env_spec.test_command,
        "timeout_seconds": inference.env_spec.timeout_seconds,
        "fail_to_pass": fail_to_pass,
        "pass_to_pass_count": len(evaluation.pass_to_pass),
        "note": "Gold / test patch 只被读取用于目标测试身份，未被写入或改写",
    }
    dump(site, "task-identity.json", EVIDENCE["task_identity"])

    manager = WorkspaceManager(cache_root=site / "repo-cache")
    workspace = manager.create(
        source=args.mirror, base_commit=inference.base_commit, target=site / "workspace"
    )
    head = run_git(["rev-parse", "HEAD"], cwd=workspace.path).stdout.strip()
    commits = int(run_git(["rev-list", "--count", "HEAD"], cwd=workspace.path).stdout.strip())
    if head != inference.base_commit:
        raise SystemExit(f"工作区 HEAD {head} != base commit {inference.base_commit}")
    dump(
        site,
        "workspace-identity.json",
        {
            "path": str(workspace.path.resolve()),
            "head": head,
            "commit_count": commits,
            "repository_id": workspace.repository_id,
            "mirror": args.mirror,
            "test_files": sorted(p.name for p in (workspace.path / "tests").glob("test_*.py")),
        },
    )
    note(f"工作区已建立：{workspace.path} HEAD={head[:12]} commits={commits}")

    run_id = f"{args.run_label}-{INSTANCE}"
    artifacts = ArtifactStore(site / "artifacts", run_id)
    sandbox = OfficialCachedSandbox(
        instance_id=INSTANCE,
        expected_image_id=args.expected_image_id,
        artifacts=artifacts,
        allowed_workspace_root=site,
        workspace_target="/testbed",
    )
    before = docker_residue()
    EVIDENCE["docker_before"] = before

    policy = WorkspacePathPolicy(workspace.path)
    environment = sandbox.build_env(
        repository=str(args.mirror),
        base_commit=inference.base_commit,
        workspace=workspace.path,
        env_spec=inference.env_spec,
    )
    if environment.status is not ExecutionStatus.PASS or not environment.from_cache:
        raise SystemExit(f"环境未按批准从缓存镜像就位：{environment.status}")
    deps = Deps(
        run_id=run_id,
        task_id=INSTANCE,
        repository=str(args.mirror),
        workspace=workspace.path,
        base_commit=inference.base_commit,
        index=None,  # type: ignore[arg-type]
        repo_graph=None,  # type: ignore[arg-type]
        retriever=None,  # type: ignore[arg-type]
        issue_analyzer=None,  # type: ignore[arg-type]
        localizer=None,  # type: ignore[arg-type]
        impact_analyzer=None,  # type: ignore[arg-type]
        planner=None,  # type: ignore[arg-type]
        coder=None,  # type: ignore[arg-type]
        diagnoser=None,  # type: ignore[arg-type]
        sandbox=sandbox,
        environment=environment,
        env_spec=inference.env_spec,
        validator=PatchValidator(policy, inference.base_commit),
        tools=None,  # type: ignore[arg-type]
        context_builder=None,  # type: ignore[arg-type]
        artifacts=artifacts,
        events=EventSink(
            run_id=run_id, task_id=INSTANCE, jsonl_path=artifacts.run_dir / "events.jsonl"
        ),
        usage=Usage(),
    )
    EVIDENCE["environment"] = {
        "image_ref": deps.environment.image_ref,
        "image_id": deps.environment.image_id,
        "from_cache": deps.environment.from_cache,
        "cache_key": deps.environment.cache_key,
        "pulled_or_built_by_this_run": False,
    }
    note(f"官方实例镜像就位（未 build / 未 pull）：{deps.environment.image_ref}")

    # --- 1. 从真实基线**重新**取得授权清单（两次全量容器执行，生产 build_test_baseline）
    baseline = build_test_baseline(
        deps.sandbox,
        base_commit=inference.base_commit,
        workspace=workspace.path,
        environment=deps.environment,
        env_spec=inference.env_spec,
        first_artifact_iteration=1001,
    )
    derived = sorted(baseline.outcomes)
    EVIDENCE["baseline_rederivation"] = {
        "complete": baseline.complete,
        "outcomes": len(derived),
        "outcomes_sha256": sha256_text(json.dumps(derived)),
        "run_outcome_counts": [len(run) for run in baseline.run_outcomes],
        "outcome_mix": _mix([outcome.value for outcome in baseline.outcomes.values()]),
        "flaky": sorted(baseline.flaky),
        "duration_seconds": baseline.duration_seconds,
        "evidence": baseline.evidence,
        "matches_archived_set": derived == archived["authorized"],
        "matches_archived_outcomes": {node: baseline.outcomes[node].value for node in derived}
        == archived["outcomes"],
        "missing_vs_archived": sorted(set(archived["authorized"]) - set(derived)),
        "extra_vs_archived": sorted(set(derived) - set(archived["authorized"])),
    }
    dump(site, "baseline-rederivation.json", EVIDENCE["baseline_rederivation"])
    dump(site, "authorized-nodeids.json", derived)
    if not baseline.complete or len(derived) != EXPECTED_AUTHORIZED:
        raise SystemExit(
            f"重新生成的基线不完整或条数不符：{len(derived)} complete={baseline.complete}"
        )
    if derived != archived["authorized"]:
        raise SystemExit("重新生成的授权清单与归档基线不逐字相等")
    note(
        f"基线重新生成：{len(derived)} 条，与归档逐字相等，sha256={EVIDENCE['baseline_rederivation']['outcomes_sha256'][:16]}…"
    )

    # --- 2. 静态判据：解析、转义、注入负控制
    checks = static_id_checks(derived, workspace.path)
    EVIDENCE["static_id_checks"] = checks
    dump(site, "node-id-static-checks.json", checks)
    if not checks["hostile_all_rejected"] or not checks["shlex_roundtrip_exact"]:
        raise SystemExit(f"NodeID 判据或转义回读不合格：leaked={checks['hostile_leaked']}")
    note(
        f"静态判据：{checks['accepted_by_target_validator']}/{checks['authorized_count']} 条被生产判据接受，"
        f"含特殊字符 {checks['ids_with_shell_special_characters']} 条，"
        f"注入负控制 {checks['hostile_total']} 条全部被拒"
    )

    # --- 3. 应用真实候选补丁（历史模型产出，不是 Gold），核对补丁身份
    run_git(["apply", "--whitespace=nowarn", "-"], cwd=workspace.path, input=patch_text)
    canonical = generate_git_diff(
        workspace.path, inference.base_commit, policy=deps.validator.policy
    )
    EVIDENCE["candidate_patch"] = {
        "applied_files": canonical.files,
        "canonical_sha256": sha256_text(canonical.text),
        "canonical_bytes": len(canonical.text.encode("utf-8")),
        "identical_to_archived": canonical.text == patch_text,
    }
    dump(site, "candidate-patch.json", EVIDENCE["candidate_patch"])
    note(
        f"补丁已应用：{canonical.files}（canonical {EVIDENCE['candidate_patch']['canonical_bytes']} B）"
    )

    # --- 4. L1：真实容器执行"与改动模块同名测试文件"的授权 NodeID
    #
    # 官方 FAIL_TO_PASS 在这里**不可用**：它的测试身份来自评测期的隐藏 test patch，
    # 基线工作区里根本没有那个节点（实测报 ``ERROR: not found … no match in any of
    # [<Class TestUnmarshaller>]``，退出码 4）。本验证不得改 fixture / Gold / 隐藏测试，
    # 所以 L1 用**授权清单里真实存在**的测试 —— 与生产图一致：目标测试来自复现或
    # Planner 授权范围，而不是从隐藏测试里抄。F2P 的身份按原样记录在证据里。
    related = sorted(node for node in derived if _related_to(canonical.files, node))
    l1_scope = tuple(related[:12])
    l1 = deps.sandbox.run_tests(
        workspace=workspace.path,
        environment=deps.environment,
        env_spec=inference.env_spec,
        tests=l1_scope,
        artifact_iteration=1003,
    )
    EVIDENCE["l1_target_tests"] = {
        "requested": list(l1_scope),
        "selection": "授权基线中与改动模块同名测试文件的前 12 条（真实存在、可执行）",
        "all_from_authorized_scope": all(node in set(derived) for node in l1_scope),
        "official_fail_to_pass": fail_to_pass,
        "official_fail_to_pass_runnable_at_base": False,
        "why_not_official_ids": (
            "F2P 节点由评测期的隐藏 test patch 创建；base commit 工作区里不存在该节点，"
            "本验证禁止改动隐藏测试，因此改用授权清单内的真实节点"
        ),
        "status": l1.status.value,
        "exit_code": l1.exit_code,
        "duration_seconds": l1.duration_seconds,
        "observed": {t.node_id: t.outcome.value for t in l1.tests},
        "artifact_dir": l1.artifact_dir,
        "junit": _read_junit(artifacts, l1.artifact_dir),
        "execution_backend": l1.diagnostics.get("execution_backend"),
        "test_runs_so_far": deps.test_runs,
    }
    dump(site, "l1-target-tests.json", EVIDENCE["l1_target_tests"])
    note(f"L1 目标测试：{l1.status.value}（exit={l1.exit_code}，回读 {len(l1.tests)} 条）")

    # --- 5. 生产 RegressionRunner：L2 受影响 + L3 全量 779 条授权 NodeID
    state = RepairState(
        task_id=INSTANCE,
        run_id=run_id,
        repository_id=workspace.repository_id,
        base_commit=inference.base_commit,
        issue=inference.problem_statement,
        started_at=utcnow(),
        budget=RepairBudget(max_test_runs=12),
        baseline=baseline,
        execution=l1,
        current_patch=canonical.text,
        impact=ImpactAnalysis(related_tests=related),
        status=RunStatus.RUNNING,
    )
    result = RegressionRunner().run(state, deps)
    l3 = result.executions.get("L3")
    readback = sorted(t.node_id for t in (l3.tests if l3 else []))
    duplicates = sorted({node for node in readback if readback.count(node) > 1})
    l3_outcomes = {t.node_id: t.outcome.value for t in (l3.tests if l3 else [])}
    EVIDENCE["regression"] = {
        "complete": result.complete,
        "status": result.status,
        "mode": result.mode.value,
        "failure_reason": result.failure_reason,
        "levels_run": [level.value for level in result.levels_run],
        "baseline_hash": result.baseline_hash,
        "executed": len(result.executed_tests),
        "unexecuted": len(result.unexecuted_tests),
        "unexecuted_list": result.unexecuted_tests,
        "new_failures": result.new_failures,
        "existing_failures": result.existing_failures,
        "flaky": sorted(result.flaky),
        "status_changes": result.status_changes,
        "l2_scope": len(related),
        "l2_status": result.executions["L2"].status.value if "L2" in result.executions else None,
        "l2_readback": len(result.executions["L2"].tests) if "L2" in result.executions else 0,
        "l3_requested": len(derived),
        "l3_readback": len(readback),
        "l3_readback_sha256": sha256_text(json.dumps(readback)),
        "readback_equals_authorized": readback == derived,
        "duplicates": duplicates,
        "l3_status": l3.status.value if l3 else None,
        "l3_exit_code": l3.exit_code if l3 else None,
        "l3_duration_seconds": l3.duration_seconds if l3 else None,
        "l3_junit": _read_junit(artifacts, l3.artifact_dir) if l3 else {},
        "l3_execution_backend": l3.diagnostics.get("execution_backend") if l3 else None,
        "l3_outcome_mix": _mix(list(l3_outcomes.values())),
        "test_runs_total": deps.test_runs,
    }
    dump(site, "regression-result.json", EVIDENCE["regression"])
    dump(
        site,
        "per-nodeid-results.json",
        {
            "note": "baseline = 重新生成的真实基线判读；observed = L3 全量执行经 JUnit 回读后的判读",
            "results": {
                node: {
                    "baseline": baseline.outcomes[node].value,
                    "observed": l3_outcomes.get(node, "MISSING"),
                }
                for node in derived
            },
        },
    )
    note(
        f"L3 全量回读：{EVIDENCE['regression']['l3_readback']}/{EXPECTED_AUTHORIZED}，"
        f"缺失 {len(result.unexecuted_tests)}，重复 {len(duplicates)}，"
        f"new_failures={len(result.new_failures)}，complete={result.complete}"
    )

    # --- 6. 超时与清理语义（真实镜像，一小批特殊字符 ID + timeout_seconds=1）
    if not args.skip_timeout_probe:
        probe = _timeout_probe(args, site, workspace.path, inference, artifacts)
        EVIDENCE["timeout_probe"] = probe
        dump(site, "timeout-probe.json", probe)
        note(
            f"超时探针：{probe['status']}（exit={probe['exit_code']}，回读 {probe['cases_parsed']} 条）"
        )

    after = docker_residue()
    EVIDENCE["docker_after"] = after
    EVIDENCE["residue"] = {
        "owned_containers_before": len(before["owned_containers"]),
        "owned_containers_after": len(after["owned_containers"]),
        "owned_volumes_before": len(before["owned_volumes"]),
        "owned_volumes_after": len(after["owned_volumes"]),
        "new_owned_containers": sorted(
            set(after["owned_containers"]) - set(before["owned_containers"])
        ),
        "new_owned_volumes": sorted(set(after["owned_volumes"]) - set(before["owned_volumes"])),
    }
    note(
        f"残留核对：新增 codepilot 容器 {len(EVIDENCE['residue']['new_owned_containers'])} 个、"
        f"新增 codepilot 卷 {len(EVIDENCE['residue']['new_owned_volumes'])} 个"
    )

    EVIDENCE["model_calls"] = 0
    EVIDENCE["wall_clock_seconds"] = round(time.time() - started, 2)
    EVIDENCE["verdict"] = _verdict(EVIDENCE)
    dump(site, "SUMMARY.json", EVIDENCE)
    (site / "run.log").write_text("\n".join(LOG) + "\n", encoding="utf-8")
    ok = EVIDENCE["verdict"]["ok"]
    note(
        f"{'全部判据通过' if ok else '存在未通过判据'}，用时 {EVIDENCE['wall_clock_seconds']}s，模型调用 0 次"
    )
    for name, passed in EVIDENCE["verdict"]["criteria"].items():
        note(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    return 0 if ok else 1


def _mix(values: list[str]) -> dict[str, int]:
    """判读分布（passed/failed/skipped/…）。只统计，不引入新的判读口径。"""
    mix: dict[str, int] = {}
    for value in values:
        mix[value] = mix.get(value, 0) + 1
    return mix


def _related_to(changed_files: list[str], node: str) -> bool:
    """把「改动模块对应的测试文件」当作 L2 受影响范围。

    生产里这一步是 ImpactAnalyzer 的产物；本脚本不建索引，改用同一份**判据形状**
    （模块名 ↔ 同名测试文件）从授权清单里选。L2 的执行仍然完全走
    RegressionRunner + 生产 DockerSandbox + 真实容器。
    """
    stems = {Path(name).stem for name in changed_files} | {
        f"test_{Path(name).stem}" for name in changed_files
    }
    return Path(node.split("::", 1)[0]).stem in stems


def _read_junit(artifacts: ArtifactStore, artifact_dir: str) -> dict[str, Any]:
    if not artifact_dir:
        return {"present": False}
    root = Path(artifact_dir)
    if not root.is_absolute():
        root = artifacts.run_dir / root
    path = root / "junit.xml"
    if not path.is_file():
        return {"present": False, "expected": str(path)}
    try:
        report = parse_junit(path)
    except JUnitParseError as exc:
        return {"present": True, "parse_error": str(exc)}
    ids = [t.node_id for t in report.tests]
    return {
        "present": True,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "declared_tests": report.declared_tests,
        "cases": len(report.tests),
        "unique_cases": len(set(ids)),
        "suite_errors": report.suite_errors,
        "suite_failures": report.suite_failures,
        "suite_skipped": report.suite_skipped,
    }


def _timeout_probe(
    args: argparse.Namespace, site: Path, workspace: Path, inference: Any, artifacts: ArtifactStore
) -> dict[str, Any]:
    """真实镜像上的一次超时执行：验证超时判定与容器/卷清理，仍然 0 次模型调用。

    用**完整 779 条授权清单**打头（``timeout_seconds=1``，实测全量套件约 3 s）：这条
    路径同时压到"整条超长命令的逐项转义"与"超时后容器/卷回收"两件事 —— 生产里
    超时正是回归最常见的非绿结局之一。
    """
    authorized = json.loads((site / "authorized-nodeids.json").read_text(encoding="utf-8"))
    selected = tuple(authorized)
    run_id = f"{args.run_label}-timeout"
    before = docker_residue()
    sandbox = OfficialCachedSandbox(
        instance_id=INSTANCE,
        expected_image_id=args.expected_image_id,
        artifacts=ArtifactStore(site / "artifacts", run_id),
        allowed_workspace_root=site,
        workspace_target="/testbed",
    )
    environment = sandbox.build_env(
        repository=str(args.mirror),
        base_commit=inference.base_commit,
        workspace=workspace,
        env_spec=inference.env_spec,
    )
    spec = inference.env_spec.model_copy(update={"timeout_seconds": 1})
    execution = sandbox.run_tests(
        workspace=workspace,
        environment=environment,
        env_spec=spec,
        tests=selected,
        artifact_iteration=2001,
    )
    after = docker_residue()
    return {
        "requested": list(selected),
        "reason_for_selection": "完整 779 条授权清单 + timeout_seconds=1：超长转义命令与超时回收同时命中",
        "timeout_seconds": spec.timeout_seconds,
        "status": execution.status.value,
        "exit_code": execution.exit_code,
        "error_reason": execution.error_reason,
        "cases_parsed": len(execution.tests),
        "duration_seconds": execution.duration_seconds,
        "artifact_dir": execution.artifact_dir,
        "junit": _read_junit(artifacts, execution.artifact_dir),
        "execution_backend": execution.diagnostics.get("execution_backend"),
        "new_owned_containers": sorted(
            set(after["owned_containers"]) - set(before["owned_containers"])
        ),
        "new_owned_volumes": sorted(set(after["owned_volumes"]) - set(before["owned_volumes"])),
        "residue_readable": after["readable"] is True,
    }


def _verdict(evidence: dict[str, Any]) -> dict[str, Any]:
    regression = evidence["regression"]
    static = evidence["static_id_checks"]
    baseline = evidence["baseline_rederivation"]
    l1 = evidence["l1_target_tests"]
    criteria = {
        "baseline_is_779": baseline["outcomes"] == EXPECTED_AUTHORIZED,
        "baseline_complete": baseline["complete"] is True,
        "baseline_matches_archive_set": baseline["matches_archived_set"] is True,
        "baseline_matches_archive_outcomes": baseline["matches_archived_outcomes"] is True,
        "all_authorized_accepted_by_validator": static["rejected_by_target_validator"] == [],
        "all_authorized_accepted_by_sandbox_selection": (
            static["accepted_by_sandbox_selection"] == EXPECTED_AUTHORIZED
        ),
        "injection_negatives_all_rejected": static["hostile_all_rejected"] is True,
        "shlex_roundtrip_exact": static["shlex_roundtrip_exact"] is True,
        "l1_targets_are_authorized_scope": l1["all_from_authorized_scope"] is True,
        "l1_readback_matches_request": sorted(l1["observed"]) == sorted(l1["requested"]),
        "l1_target_tests_passed": l1["status"] == "PASS",
        "l2_and_l3_both_ran": "L2_impact" in regression["levels_run"]
        and "L3_full" in regression["levels_run"],
        "l3_scope_exact": regression["readback_equals_authorized"] is True,
        "l3_no_duplicates": regression["duplicates"] == [],
        "l3_no_missing": regression["unexecuted"] == 0,
        "l3_complete": regression["complete"] is True,
        "l3_ran_in_docker": regression["l3_execution_backend"] == "docker",
        "junit_declared_equals_cases": (
            regression["l3_junit"].get("declared_tests") == regression["l3_readback"]
            and regression["l3_junit"].get("cases") == regression["l3_readback"]
            and regression["l3_junit"].get("unique_cases") == regression["l3_readback"]
        ),
        "junit_suite_clean": (
            regression["l3_junit"].get("suite_errors") == 0
            and regression["l3_junit"].get("suite_failures") == 0
        ),
        "no_new_container_residue": evidence["residue"]["new_owned_containers"] == [],
        "no_new_volume_residue": evidence["residue"]["new_owned_volumes"] == [],
        "residue_read_from_docker": (
            evidence["docker_before"]["readable"] is True
            and evidence["docker_after"]["readable"] is True
        ),
        "zero_model_calls": evidence["model_calls"] == 0,
    }
    if "timeout_probe" in evidence:
        probe = evidence["timeout_probe"]
        criteria["timeout_probe_terminated_as_a_timeout"] = probe["status"] == "TIMEOUT"
        criteria["timeout_probe_left_no_residue"] = (
            probe["residue_readable"] is True
            and probe["new_owned_containers"] == []
            and probe["new_owned_volumes"] == []
        )
    return {"ok": all(criteria.values()), "criteria": criteria}


if __name__ == "__main__":
    sys.exit(main())
