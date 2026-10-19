#!/usr/bin/env python
"""用**真实安装的 swebench==4.1.0** 校验 CodePilot 官方 Harness 适配器（零镜像、零下载）。

回答三个只有官方源码/CLI 才能回答的问题：

1. 适配器拼出的 argv 是否被官方 4.1.0 的 argparse 接受？
   做法：把 predictions 指向不存在的文件后真实执行官方 CLI。官方代码在
   ``argparse`` 之后、``docker.from_env()`` 之前就会抛错——所以"报错点在哪"
   就证明了参数是否被接受，且不会碰 Docker。
2. 官方 ``--help`` 暴露的参数集与适配器实际使用的参数集是否一致？
   缺失的官方参数会被记下来（默认值即官方行为）。
3. 官方 ``TestSpec`` 计算出的镜像名是否与 CodePilot 的镜像身份一致？
   分别按 ``namespace="swebench"``（官方 CLI 默认）和 ``namespace=None`` 计算。

用法（在装有 ``swebench==4.1.0`` 的 venv 之外，由主 venv 调用）::

    uv run python scripts/swebench_official_probe.py \
        --harness-python /path/to/swebench-venv/bin/python \
        --split dev --json-out artifacts/swebench/official-probe.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from codepilot.eval.swebench import SWEBenchLoader  # noqa: E402
from codepilot.eval.swebench_harness import (  # noqa: E402
    HARNESS_VERSION,
    HarnessConfig,
    OfficialHarnessRunner,
    PlatformInfo,
)
from codepilot.sandbox.swebench_env import instance_image_key  # noqa: E402

#: 官方 4.1.0 ``run_evaluation.py`` 的真实默认值（源码 harness/run_evaluation.py）。
OFFICIAL_DEFAULTS: dict[str, Any] = {
    "dataset_name": "SWE-bench/SWE-bench_Lite",
    "split": "test",
    "max_workers": 4,
    "open_file_limit": 4096,
    "timeout": 1800,
    "force_rebuild": False,
    "cache_level": "env",
    "clean": False,
    "namespace": "swebench",
    "instance_image_tag": "latest",
    "env_image_tag": "latest",
    "rewrite_reports": False,
    "report_dir": ".",
    "modal": False,
}

#: 官方报告/日志目录常量（``RUN_EVALUATION_LOG_DIR = Path("logs/run_evaluation")``）。
OFFICIAL_LOG_DIR = "logs/run_evaluation"


def _official_flags(harness_python: Path) -> set[str]:
    """从真实 ``--help`` 里抽出官方 CLI 接受的参数集合。"""
    proc = subprocess.run(
        [str(harness_python), "-m", "swebench.harness.run_evaluation", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"官方 --help 执行失败 rc={proc.returncode}: {proc.stderr[:400]}")
    flags: set[str] = set()
    for match in re.finditer(r"-{1,2}[A-Za-z_][A-Za-z0-9_]*", proc.stdout):
        flags.add(match.group(0))
    return flags


def _adapter_flags(argv: tuple[str, ...]) -> set[str]:
    return {token for token in argv if token.startswith("-")}


def _run_with_missing_predictions(
    harness_python: Path, argv: list[str], workdir: Path
) -> dict[str, Any]:
    """真实执行官方 CLI，但 predictions 指向不存在的文件（不会碰 Docker）。"""
    patched = list(argv)
    patched[0] = str(harness_python)
    for index, token in enumerate(patched):
        if token == "-p":
            patched[index + 1] = str(workdir / "does-not-exist.jsonl")
    proc = subprocess.run(
        patched,
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    blob = (proc.stdout or "") + (proc.stderr or "")
    tail = blob.strip().splitlines()[-12:]
    return {
        "returncode": proc.returncode,
        "accepted_by_argparse": "unrecognized arguments" not in blob
        and "expected one argument" not in blob,
        "failed_at": tail[-1][:300] if tail else "",
        "touched_docker": "docker.from_env" in blob or "Error while fetching server API" in blob,
        "tail": tail,
    }


def _official_image_keys(harness_python: Path, snapshot: Path, instance_id: str) -> dict[str, Any]:
    """在 harness venv 里用**官方代码**算镜像名（namespace 两种取值）。"""
    snippet = f"""
import json, sys
from swebench.harness.test_spec.test_spec import make_test_spec
rows = [json.loads(l) for l in open(r"{snapshot}", encoding="utf-8").read().splitlines() if l]
inst = next(r for r in rows if r["instance_id"] == "{instance_id}")
out = {{}}
for ns in ("swebench", None):
    spec = make_test_spec(inst, namespace=ns)
    out["namespaced" if ns else "local"] = {{
        "instance_image_key": spec.instance_image_key,
        "env_image_key": spec.env_image_key,
        "base_image_key": spec.base_image_key,
        "is_remote_image": spec.is_remote_image,
        "container_name": spec.get_instance_container_name("probe-run-id"),
        "language": spec.language,
        "arch": spec.arch,
        "docker_specs": spec.docker_specs,
        "test_cmd_kind": spec.eval_script_list[:1],
    }}
print(json.dumps(out))
"""
    proc = subprocess.run(
        [str(harness_python), "-c", snippet],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if proc.returncode != 0:
        raise SystemExit(f"官方镜像名计算失败: {proc.stderr[:600]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness-python", required=True, type=Path)
    parser.add_argument("--cache-root", type=Path, default=ROOT / "cache" / "swebench")
    parser.add_argument("--split", default="dev", choices=("dev", "test"))
    parser.add_argument("--instance-id")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    # 不能 resolve()：Linux venv 的 bin/python 是指向基础解释器的符号链接，
    # 解析后会丢失 venv 上下文（site-packages 找不到 swebench）。
    harness_python = args.harness_python.absolute()
    loader = SWEBenchLoader(args.cache_root)
    loader.load_dataset(args.split)
    instance_id = args.instance_id or loader.list_instances(args.split)[0]
    snapshot = Path(loader.snapshot_dir) / f"{args.split}.jsonl"

    config = HarnessConfig(
        predictions_path=snapshot.parent / "probe-predictions.jsonl",
        run_id="probe-run-0000000000",
        report_dir=Path(tempfile.gettempdir()) / "cp-official-probe",
        dataset_name=loader.dataset_name,
        split=args.split,
        instance_ids=(instance_id,),
        harness_python=harness_python,
        model_name_or_path="codepilot-official-probe",
        dataset_revision=loader.revision,
        dataset_snapshot=snapshot,
    )
    command = OfficialHarnessRunner(config, platform=PlatformInfo.detect()).preview_command()
    argv = [str(item) for item in command.argv]

    flags_official = _official_flags(harness_python)
    flags_adapter = _adapter_flags(tuple(argv))
    with tempfile.TemporaryDirectory(prefix="cp-probe-") as tmp:
        execution = _run_with_missing_predictions(harness_python, argv, Path(tmp))
    keys = _official_image_keys(harness_python, snapshot, instance_id)

    report = {
        "harness_python": str(harness_python),
        "harness_version_expected": HARNESS_VERSION,
        "instance_id": instance_id,
        "split": args.split,
        "snapshot": str(snapshot),
        "adapter_argv": argv,
        "flags_used_by_adapter": sorted(flags_adapter),
        "flags_missing_from_adapter": sorted(
            f
            for f in ("--cache_level", "-n", "--force_rebuild", "--clean", "--open_file_limit")
            if f not in flags_adapter
        ),
        "official_default_namespace": OFFICIAL_DEFAULTS["namespace"],
        "official_default_cache_level": OFFICIAL_DEFAULTS["cache_level"],
        "official_report_relative_to_cwd": OFFICIAL_LOG_DIR,
        "official_flags_all_known": flags_adapter <= flags_official,
        "unknown_to_official": sorted(flags_adapter - flags_official),
        "argparse_execution": execution,
        "official_image_keys": keys,
        "codepilot_instance_image_key": instance_image_key(instance_id),
    }
    report["image_name_matches_official_default_namespace"] = (
        report["codepilot_instance_image_key"] == keys["namespaced"]["instance_image_key"]
    )
    report["image_name_matches_official_local_build"] = (
        report["codepilot_instance_image_key"] == keys["local"]["instance_image_key"]
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
