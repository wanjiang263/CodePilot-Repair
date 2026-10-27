"""Create base-only seeds; verify first five through the production bound sandbox."""

from __future__ import annotations

import io
import json
import shlex
import sys
import tarfile
import time
import traceback
from pathlib import Path

import docker
from codepilot.artifacts import ArtifactStore
from codepilot.eval.swebench import SWEBenchLoader
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.official import OfficialInstanceSandbox
from codepilot.sandbox.runners import RunnerOverrides
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/phase25"
SCOPES = {
    "sphinx-doc__sphinx-8506": ["tests/test_domain_std.py"],
    "django__django-14999": ["migrations.test_operations.OperationTests.test_rename_model"],
    "astropy__astropy-14995": ["astropy/table/tests/test_table.py"],
    "astropy__astropy-6938": ["astropy/io/fits/tests/test_table.py"],
    "django__django-16816": ["modeladmin.test_checks.ListDisplayTests"],
}


def main() -> None:
    client = docker.from_env(timeout=600)
    catalog = json.loads((OUT / "catalog.json").read_text())
    loader = SWEBenchLoader(ROOT / "cache/swebench", revision=catalog["revision"])
    runners = RunnerOverrides.load(OUT / "runners.json")
    images = SWEBenchInstanceImageResolver.load(
        ROOT / "configs/swebench/instance-image-overrides.json"
    )
    for index, row in enumerate(catalog["instances"]):
        if "--first-five" in sys.argv and index >= 5:
            break
        iid = row["instance_id"]
        result_path = OUT / "environments" / f"{iid}.json"
        if result_path.exists() and json.loads(result_path.read_text()).get("ok"):
            continue
        started = time.monotonic()
        result = {"instance_id": iid, "base_commit": row["base_commit"], "ok": False}
        try:
            ref = f"swebench/sweb.eval.x86_64.{iid.replace('__', '_1776_')}:latest"
            try:
                image = client.images.get(ref)
            except docker.errors.ImageNotFound:
                import shutil

                if shutil.disk_usage(ROOT).free < 15 * 1024**3:
                    raise RuntimeError("less than 15GiB free on D drive") from None
                print(iid, "pull", flush=True)
                image = client.images.pull(ref)
            result["image_id"] = image.id
            seed = OUT / "seeds" / iid
            seed.mkdir(parents=True, exist_ok=True)
            if not (seed / "repo" / "HEAD").exists():
                base = shlex.quote(row["base_commit"])
                command = f"git init --bare /tmp/seed/repo && git --git-dir=/tmp/seed/repo fetch --depth=1 /testbed {base} && git --git-dir=/tmp/seed/repo update-ref refs/heads/base {base} && git --git-dir=/tmp/seed/repo symbolic-ref HEAD refs/heads/base && test $(git --git-dir=/tmp/seed/repo rev-list --all --count) = 1"
                container = client.containers.run(
                    ref,
                    ["bash", "-lc", command],
                    detach=True,
                    working_dir="/testbed",
                    network_mode="none",
                    mem_limit="2g",
                )
                try:
                    status = container.wait(timeout=180)
                    if status["StatusCode"]:
                        raise RuntimeError(container.logs().decode(errors="replace"))
                    archive, _ = container.get_archive("/tmp/seed/repo")
                    with tarfile.open(fileobj=io.BytesIO(b"".join(archive))) as tar:
                        tar.extractall(seed, filter="data")
                finally:
                    container.remove(force=True)
            if index < 5:
                task = loader.get_inference_task("test", iid)
                artifacts = ArtifactStore(OUT / "binding" / iid, "probe")
                manager = WorkspaceManager(
                    cache_root=OUT / "binding-cache" / iid, default_mode="copy"
                )
                import shutil

                from m3_batch import canonical_repo_source

                target = manager.cache_path(canonical_repo_source(row["repo"]))
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(seed / "repo", target)
                from l2cal10e_internal import _ReusingWorkspaceManager

                workspace = _ReusingWorkspaceManager(
                    manager, artifacts.run_dir / "workspace"
                ).create(
                    source=str(task.repository),
                    base_commit=task.base_commit,
                    target=artifacts.run_dir / "workspace",
                    mode="copy",
                )
                backend = OfficialInstanceSandbox(
                    artifacts=artifacts,
                    allowed_workspace_root=OUT / "binding" / iid,
                    instance_id=iid,
                    images=images,
                    runners=runners,
                    bind_workspace=True,
                )
                env = backend.build_env(
                    repository=str(task.repository),
                    base_commit=task.base_commit,
                    workspace=workspace.path,
                    env_spec=task.env_spec,
                )
                tested = backend.run_tests(
                    workspace=workspace.path,
                    environment=env,
                    env_spec=task.env_spec,
                    tests=SCOPES[iid],
                    artifact_iteration=1,
                )
                result["environment"] = env.model_dump(mode="json")
                result["test_result"] = tested.model_dump(mode="json")
                if not tested.tests:
                    raise RuntimeError("bound sandbox returned zero parsed tests")
            result["ok"] = True
        except (RuntimeError, OSError, ValueError, docker.errors.DockerException) as exc:
            result["error"] = str(exc)
            result["traceback"] = traceback.format_exc()
        result["elapsed_seconds"] = time.monotonic() - started
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if result_path.exists():
            history = result_path.with_name(f"{iid}-{time.time_ns()}.json")
            result_path.rename(history)
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        print(iid, result["ok"], result.get("error", ""), flush=True)


if __name__ == "__main__":
    main()
