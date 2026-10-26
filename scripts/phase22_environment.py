"""Revalidate existing clean base workspaces and execute one small real scope each."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from phase21_zero_model import _git, _run  # noqa: E402
from phase22_prepare import read, sha, write_new  # noqa: E402

from codepilot.artifacts import ArtifactStore  # noqa: E402
from codepilot.eval.swebench import SWEBenchLoader  # noqa: E402
from codepilot.sandbox.official import OfficialInstanceSandbox  # noqa: E402
from codepilot.sandbox.runners import RunnerOverrides  # noqa: E402
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver  # noqa: E402


def main() -> None:
    import faulthandler

    faulthandler.dump_traceback_later(45, repeat=True)
    print("environment verification starting", flush=True)
    os.environ["CODEPILOT_BIND_WORKSPACE"] = "1"
    import docker

    client = docker.from_env()
    print("Docker connected", flush=True)
    oldpath = ROOT / "artifacts/phase21/m3-phase21-recovery-6-r5/preflight/preflight.json"
    old = read(oldpath)
    catalog = read(ROOT / "configs/phase22/catalog.json")
    loader = SWEBenchLoader(
        ROOT / "cache/swebench",
        revision=catalog["dataset_revision"],
        env_spec_snapshot=ROOT / "configs/swebench/env-specs-4.1.0.json",
    )
    loader.load_dataset("test")
    print("Dataset verified", flush=True)
    runners = RunnerOverrides.load(ROOT / "configs/phase21/instance-runner-overrides.json")
    images = SWEBenchInstanceImageResolver.load(
        ROOT / "configs/swebench/instance-image-overrides.json"
    )
    scopes = read(ROOT / "configs/phase21/zero-model-scopes.json")["instances"]
    os.environ["CODEPILOT_TESTDEPS_DIR"] = str(ROOT / "artifacts/phase18/l2-cal10e/testdeps")
    report = {
        **old,
        "batch_id": catalog["batch_id"],
        "reused_preflight_sha256": sha(oldpath),
        "instances": [],
    }
    for entry in old["instances"]:
        print("checking", entry["instance_id"], flush=True)
        instance = entry["instance_id"]
        root = ROOT / "artifacts/phase22/environment-r3" / instance
        root.mkdir(parents=True, exist_ok=False)
        workspace = Path(entry["collection"]["stdout"]).parents[1] / "workspace"
        assert _git(workspace, "rev-parse", "HEAD") == entry["base_commit"]
        assert _git(workspace, "rev-list", "--all", "--count") == "1"
        assert not _git(workspace, "status", "--porcelain"), workspace
        assert client.images.get(entry["image"]["ref"]).id == entry["image"]["id"]
        task = loader.get_inference_task("test", instance)
        sandbox = OfficialInstanceSandbox(
            artifacts=ArtifactStore(root, "light"),
            allowed_workspace_root=workspace.parent,
            instance_id=instance,
            images=images,
            runners=runners,
        )
        env = sandbox.build_env(
            repository=entry["repo"],
            base_commit=entry["base_commit"],
            workspace=workspace,
            env_spec=task.env_spec,
        )
        result = _run(
            sandbox,
            workspace,
            env,
            task.env_spec,
            tests=scopes[instance]["target_tests"],
            iteration=1,
        )
        assert result.tests and any(t.outcome.value == "passed" for t in result.tests), result
        current = {
            **entry,
            "phase22_light": {
                "status": result.status.value,
                "tests": len(result.tests),
                "environment": env.model_dump(mode="json"),
                "workspace": str(workspace),
                "clean_base_only": True,
            },
        }
        report["instances"].append(current)
        write_new(root / "result.json", current)
        print(instance, result.status.value, len(result.tests), flush=True)
    report["ok"] = True
    write_new(ROOT / "artifacts/phase22/environment-summary.json", report)


if __name__ == "__main__":
    main()
