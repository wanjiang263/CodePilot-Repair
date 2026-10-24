from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from codepilot.artifacts import ArtifactStore  # noqa: E402
from codepilot.eval.swebench import SWEBenchLoader  # noqa: E402
from codepilot.repo.workspace import WorkspaceManager  # noqa: E402
from codepilot.sandbox.env_spec import EnvSpec  # noqa: E402
from codepilot.sandbox.official import (  # noqa: E402
    _PYTEST_PREFIX,
    OfficialInstanceSandbox,
    _pytest_command,
)
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _docker(*args: str, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )


def _required(
    result: subprocess.CompletedProcess[str], action: str
) -> subprocess.CompletedProcess[str]:
    if result.returncode != 0:
        raise RuntimeError(f"{action}: {result.stderr.strip()}")
    return result


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return _required(result, f"git {' '.join(args)}")


def _extract_image_source(image: str, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    container = _required(
        _docker("create", "--label", "codepilot=1", image, "/bin/true"), "create source container"
    ).stdout.strip()
    try:
        _required(_docker("cp", f"{container}:/testbed", str(target)), "extract /testbed")
    finally:
        _required(_docker("rm", container), "remove source container")


def _prepare_cache(source: Path, base: str, manager: WorkspaceManager) -> Path:
    cache = manager.cache_path(str(source))
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        _required(
            subprocess.run(
                ["git", "init", "--bare", "--quiet", str(cache)],
                capture_output=True,
                text=True,
                check=False,
            ),
            "init shallow cache",
        )
        _git(cache, "fetch", "--quiet", "--depth=1", str(source), base)
        _git(cache, "update-ref", "refs/heads/main", base)
        _git(cache, "symbolic-ref", "HEAD", "refs/heads/main")
    if _git(cache, "rev-list", "--all", "--count").stdout.strip() != "1":
        raise RuntimeError(f"cache is not base-only: {cache}")
    if _git(cache, "rev-parse", "HEAD").stdout.strip() != base:
        raise RuntimeError(f"cache HEAD mismatch: {cache}")
    return cache


def _collect(
    instance: str, image: str, workspace: Path, output: Path, spec: EnvSpec
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    command = _PYTEST_PREFIX.sub(
        "/opt/miniconda3/envs/testbed/bin/python -m pytest", spec.test_command
    )
    command = _pytest_command(instance, f"{command} --collect-only -q")
    create = _required(
        _docker(
            "create",
            "--network",
            "none",
            "--user",
            "1000:1000",
            "--read-only",
            "--tmpfs",
            "/tmp:size=512m",
            "--memory",
            "4g",
            "--memory-swap",
            "4g",
            "--cpus",
            "2",
            "--pids-limit",
            "512",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--workdir",
            "/testbed",
            "--env",
            "PYTHONPATH=/testbed/src:/testbed:/testbed/testdeps",
            "--env",
            "HOME=/tmp",
            "--label",
            "codepilot=1",
            "--label",
            f"codepilot.phase20=preflight-{instance}",
            "--mount",
            f"type=bind,source={workspace.resolve()},target=/testbed",
            "--mount",
            f"type=bind,source={output.resolve()},target=/out",
            image,
            "/bin/sh",
            "-lc",
            command,
        ),
        "create collection container",
    ).stdout.strip()
    try:
        _required(_docker("start", create), "start collection container")
        waited = _required(_docker("wait", create, timeout=900), "wait for collection")
        exit_code = int(waited.stdout.strip())
        logs = subprocess.run(
            ["docker", "logs", create],
            capture_output=True,
            check=False,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        (output / "stdout.log").write_bytes(logs.stdout)
        (output / "stderr.log").write_bytes(logs.stderr)
    finally:
        _required(_docker("rm", create), "remove collection container")
    return {
        "exit_code": exit_code,
        "command": command,
        "stdout": str(output / "stdout.log"),
        "stderr": str(output / "stderr.log"),
        "ok": exit_code == 0,
    }


def _credential_present(root: Path) -> bool:
    path = root / ".env"
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "GLM_API_KEY" and value.strip():
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--image-overrides", type=Path, default=None)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    artifact = args.artifact_root if args.artifact_root.is_absolute() else root / args.artifact_root
    catalog_path = args.catalog if args.catalog.is_absolute() else root / args.catalog
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    resolver = SWEBenchInstanceImageResolver.load(args.image_overrides)
    loader = SWEBenchLoader(
        root / "cache/swebench",
        revision=catalog["dataset_revision"],
        env_spec_snapshot=root / "configs/swebench/env-specs-4.1.0.json",
    )
    loader.load_dataset("test")
    os.environ.setdefault(
        "CODEPILOT_TESTDEPS_DIR", str(root / "artifacts/phase18/l2-cal10e/testdeps")
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "batch_id": catalog["batch_id"],
        "generated_at": _now(),
        "repo_root": str(root),
        "docker_engine": _required(
            _docker("version", "--format", "{{.Server.Version}}"), "Docker Engine"
        ).stdout.strip(),
        "credential_reference_present": _credential_present(root),
        "instances": [],
    }
    for item in catalog["instances"]:
        instance = item["instance_id"]
        started = _now()
        instance_root = artifact / "instances" / instance
        preflight_root = instance_root / "preflight"
        preflight_root.mkdir(parents=True, exist_ok=True)
        result: dict[str, Any] = {
            "instance_id": instance,
            "repo": item["repo"],
            "base_commit": item["base_commit"],
            "started_at": started,
            "ok": False,
        }
        try:
            resolution = resolver.resolve(instance)
            inspect = _docker("image", "inspect", resolution.image_ref)
            if inspect.returncode != 0:
                if (
                    "No such image" not in inspect.stderr
                    and "not found" not in inspect.stderr.lower()
                ):
                    raise RuntimeError(f"image inspect failed: {inspect.stderr.strip()}")
                _required(_docker("pull", resolution.image_ref), "pull missing official image")
                inspect = _docker("image", "inspect", resolution.image_ref)
            image = json.loads(_required(inspect, "inspect image").stdout)[0]
            if resolution.image_id is not None and image["Id"] != resolution.image_id:
                raise RuntimeError(
                    f"image identity mismatch: {image['Id']} != {resolution.image_id}"
                )
            result["image"] = {
                "ref": resolution.image_ref,
                "id": image["Id"],
                "os": image.get("Os"),
                "architecture": image.get("Architecture"),
                "override": resolution.override,
                "parent_ref": resolution.parent_image_ref,
                "parent_id": resolution.parent_image_id,
            }
            source = preflight_root / "source-testbed"
            _extract_image_source(resolution.image_ref, source)
            _git(source, "config", "core.filemode", "false")
            _git(source, "config", "core.autocrlf", "false")
            _git(source, "checkout", "--detach", item["base_commit"])
            if _git(source, "rev-parse", "HEAD").stdout.strip() != item["base_commit"]:
                raise RuntimeError("source base identity mismatch")
            manager = WorkspaceManager(cache_root=instance_root / "repo-cache")
            cache = _prepare_cache(source, item["base_commit"], manager)
            workspace = preflight_root / "workspace"
            if not workspace.exists():
                manager.create(
                    source=str(source), base_commit=item["base_commit"], target=workspace
                )
            if _git(workspace, "rev-parse", "HEAD").stdout.strip() != item["base_commit"]:
                raise RuntimeError("workspace base identity mismatch")
            if _git(workspace, "rev-list", "--all", "--count").stdout.strip() != "1":
                raise RuntimeError("workspace contains history beyond the shallow base")
            clean = _git(workspace, "status", "--porcelain").stdout.strip()
            if clean:
                raise RuntimeError(f"workspace is not clean: {clean[:500]}")
            task = loader.get_inference_task("test", instance)
            sandbox = OfficialInstanceSandbox(
                artifacts=ArtifactStore(preflight_root, "official-image"),
                allowed_workspace_root=instance_root,
                instance_id=instance,
                images=resolver,
            )
            environment = sandbox.build_env(
                repository=item["repo"],
                base_commit=item["base_commit"],
                workspace=workspace,
                env_spec=task.env_spec,
            )
            result["environment"] = environment.model_dump(mode="json")
            result["cache"] = {
                "path": str(cache),
                "base_commit": item["base_commit"],
                "commits": 1,
            }
            result["collection"] = _collect(
                instance,
                resolution.image_ref,
                workspace,
                preflight_root / "collection",
                task.env_spec,
            )
            result["ok"] = result["collection"]["ok"]
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = _now()
        _write_json(preflight_root / "result.json", result)
        report["instances"].append(result)
    report["ok"] = report["credential_reference_present"] and all(
        item["ok"] for item in report["instances"]
    )
    report["available_instances"] = sum(item["ok"] for item in report["instances"])
    _write_json(artifact / "preflight" / "preflight.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
