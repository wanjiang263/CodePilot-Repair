#!/usr/bin/env python
"""用**真实 Docker + 真实官方 harness** 跑最小正/负向控制（Phase 11 E-16/E-17 验收）。

这不是模型评测：不调用任何 LLM，补丁全部由评测侧构造，只为证明官方判分链路本身可用。

控制项（同一个实例镜像，四次官方调用）：

* ``gold``   —— ORACLE_CONTROL，官方 gold patch，期望 ``resolved``；不计入模型 Resolved Rate。
* ``wrong``  —— 在 gold patch 的同一个 hunk 里注入一行必然失败的语句：补丁能 apply、
  但修不好问题，期望 ``unresolved``（能 apply ≠ 已解决）。
* ``garbage`` —— 语法合法但上下文不匹配的 diff，期望 ``patch_apply_failed``。
* ``empty``  —— 空补丁，期望官方只记 ``empty_patch``，不启动评测容器。

隔离：gold 只从 ``SWEBenchEvaluationTask`` 读取；预测写到独立文件、独立 run_id、
独立 report_dir；推理侧对象完全不参与本次脚本。

用法（WSL2 Linux，需要 Docker daemon）::

    uv run python scripts/swebench_official_control.py --control gold \\
        --instance-id marshmallow-code__marshmallow-1343 \\
        --harness-python /root/swebench-venv/bin/python \\
        --artifact-root artifacts/swebench-official-control

环境要求：``DOCKER_HOST``（或默认 docker.sock）可用；原生 Windows 无法运行官方 harness。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from codepilot.eval.swebench import SWEBenchLoader  # noqa: E402
from codepilot.eval.swebench_harness import (  # noqa: E402
    OFFICIAL_CACHE_LEVEL,
    OFFICIAL_NAMESPACE,
    HarnessConfig,
    OfficialHarnessRunner,
    PlatformInfo,
    derive_run_id,
    oracle_control_run_id,
    run_oracle_control,
    write_oracle_predictions,
)
from codepilot.sandbox.swebench_env import instance_image_key  # noqa: E402

CONTROLS = ("gold", "wrong", "garbage", "empty")
NEGATIVE_MODEL = "codepilot/negative-control"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _wrong_variant(gold: str) -> str:
    """把失败语句注入 gold patch 的第一个新增行之后（保持 hunk 头与上下文不变）。

    官方以 ``git apply`` 应用补丁，所以只能改 ``+`` 侧内容；缩进沿用被复制的那一行，
    保证语法上仍在同一个 block 里。
    """
    lines = gold.splitlines()
    marker = "raise AssertionError('codepilot negative control: patch applies but does not fix')"
    for index, line in enumerate(lines):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        indent = body[: len(body) - len(body.lstrip())]
        if not body.strip() or body.strip().startswith(("#", '"', "'")):
            continue
        injected = f"+{indent}{marker}\n"
        return "\n".join(lines[: index + 1]) + injected + "\n".join(lines[index + 1 :])
    raise SystemExit("gold patch 里没有可注入的新增代码行，无法构造 wrong 控制")


def _garbage_variant(gold: str) -> str:
    """保留 diff 头，但把 hunk 的行数计数改成不可能匹配的形态。"""
    lines = gold.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("@@"):
            out.append("@@ -999999,3 +999999,3 @@")
            out.append(" context line that does not exist in the repository A")
            out.append(" context line that does not exist in the repository B")
            out.append(" context line that does not exist in the repository C")
            break
        out.append(line)
    return "\n".join(out) + "\n"


def _docker_snapshot(image_key: str) -> dict[str, Any]:
    """记录真实镜像身份（存在性 + image ID），失败不阻断评测。"""

    def call(args: list[str]) -> str:
        try:
            done = subprocess.run(args, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"UNAVAILABLE: {type(error).__name__}"
        return (done.stdout or done.stderr).strip()

    docker = os.environ.get("DOCKER_CLI", "docker")
    return {
        "image_key": image_key,
        "inspect_id": call([docker, "images", "--no-trunc", "--format", "{{.ID}}", image_key]),
        "disk_df": call(
            [docker, "system", "df", "--format", "{{.Type}}|{{.Size}}|{{.Reclaimable}}"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", required=True, choices=CONTROLS)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--cache-root", type=Path, default=ROOT / "cache" / "swebench")
    parser.add_argument("--harness-python", required=True, type=Path)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--namespace", default=OFFICIAL_NAMESPACE)
    parser.add_argument("--cache-level", default=OFFICIAL_CACHE_LEVEL)
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()

    if platform.system() != "Linux":
        raise SystemExit(
            f"官方 harness 4.1.0 只能在 Linux 执行（当前 {platform.system()}）；请用 WSL2 运行本脚本"
        )

    namespace: str | None = None if args.namespace in {"none", ""} else args.namespace
    loader = SWEBenchLoader(args.cache_root)
    loader.load_dataset(args.split)
    if args.instance_id not in set(loader.list_instances(args.split)):
        raise SystemExit(f"{args.instance_id} 不在官方 {args.split} split 里")
    task = loader.get_evaluation_task(args.split, args.instance_id)
    gold = task.gold_patch
    snapshot = Path(loader.snapshot_dir) / f"{args.split}.jsonl"
    image_key = instance_image_key(args.instance_id, namespace=namespace)

    workdir = args.artifact_root / args.control
    workdir.mkdir(parents=True, exist_ok=True)
    predictions = workdir / "predictions.jsonl"

    base = {
        "report_dir": workdir / "official",
        "dataset_name": loader.dataset_name,
        "dataset_revision": loader.revision,
        "split": args.split,
        "instance_ids": (args.instance_id,),
        "harness_python": args.harness_python.absolute(),
        "dataset_snapshot": snapshot,
        "namespace": namespace,
        "cache_level": args.cache_level,
        "max_workers": args.max_workers,
        "timeout_seconds": args.timeout_seconds,
    }

    if args.control == "gold":
        config = HarnessConfig(
            predictions_path=predictions,
            run_id=oracle_control_run_id({args.instance_id: gold}),
            model_name_or_path="codepilot-oracle-control",
            **base,
        )
        oracle_predictions = workdir / "oracle-predictions.jsonl"
        write_oracle_predictions({args.instance_id: gold}, oracle_predictions)
        assert oracle_predictions != config.predictions_path
        result = run_oracle_control(
            config,
            {args.instance_id: gold},
            oracle_predictions_path=oracle_predictions,
            image_ids={args.instance_id: image_key},
        )
    else:
        if args.control == "wrong":
            patch = _wrong_variant(gold)
        elif args.control == "garbage":
            patch = _garbage_variant(gold)
        else:
            patch = ""
        # 空补丁不参与 patch 集合摘要（官方在评测前就过滤掉、只记 empty_patch），
        # 所以 run_id 必须按"空集合"派生，否则适配器的摘要校验会拒绝它。
        patch_digests = {} if patch == "" else {args.instance_id: _sha(patch)}
        run_id = derive_run_id(patch_digests, seed="negative")
        config = HarnessConfig(
            predictions_path=predictions,
            run_id=run_id,
            model_name_or_path=NEGATIVE_MODEL,
            **base,
        )
        predictions.write_text(
            json.dumps(
                {
                    "instance_id": args.instance_id,
                    "model_name_or_path": NEGATIVE_MODEL,
                    "model_patch": patch,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        runner = OfficialHarnessRunner(config, image_ids={args.instance_id: image_key})
        result = runner.run()

    payload: dict[str, Any] = {
        "control": args.control,
        "instance_id": args.instance_id,
        "split": args.split,
        "dataset_name": loader.dataset_name,
        "dataset_revision": loader.revision,
        "gold_patch_sha256": task.gold_patch_sha256,
        "test_patch_sha256": task.test_patch_sha256,
        "fail_to_pass_count": len(task.fail_to_pass),
        "pass_to_pass_count": len(task.pass_to_pass),
        "namespace": namespace,
        "cache_level": args.cache_level,
        "instance_image_key": image_key,
        "harness_version": config.harness_version,
        "run_id": config.run_id,
        "decision": result.decision.value,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "runtime_seconds": round(result.runtime_seconds, 1),
        "control_kind": result.control_kind.value,
        "included_in_resolved_rate": result.included_in_resolved_rate,
        "error_kind": result.error_kind,
        "official_verdict": result.verdict_for(args.instance_id).value,
        "report_path": str(result.report_path) if getattr(result, "report_path", None) else None,
        "expected_report": str(config.report_dir / config.expected_report_name),
        "platform": PlatformInfo.detect().describe(),
        "docker": _docker_snapshot(image_key),
    }
    if result.report is not None:
        payload["official_report"] = json.loads(result.report.model_dump_json())
    out = workdir / "control-result.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
