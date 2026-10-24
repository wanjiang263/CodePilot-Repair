"""Per-instance environment and runner preflight for the M3 batch.

Phase 20 treated every instance as pytest+junit.  That is true for Sphinx,
Matplotlib and (with a compatibility plugin) Requests, but not for Django's
``tests/runtests.py`` or SymPy's ``bin/test``.  This script resolves the frozen
runner decision, runs a bounded, issue-derived smoke scope, parses the native
output through the production adapters, and records staged timings so a 900s
collection timeout can be attributed to a phase instead of the whole run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
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
    _copy_testdeps,
    _pytest_command,
    instance_testdeps_root,
)
from codepilot.sandbox.runners import (  # noqa: E402
    PYTEST,
    RunnerAdapter,
    RunnerOverrides,
)
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver  # noqa: E402

_COLLECTED = re.compile(r"^(?P<count>\d+) (?:tests?|items?) collected", re.MULTILINE)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


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


def _seed_cache(manager: WorkspaceManager, repository: str, seed: Path | None) -> Path:
    """Seed a base-only bare cache from a previous artifact tree.

    A local ``git clone``/``fetch`` from a Python child process can fail before
    the repository is opened (MSYS ``NtCreateDirectoryObject``).  Copying the
    bare object database is deterministic and leaves the cache base-only; the
    production WorkspaceManager already has a fetch fallback for its own local
    transport, so only the cache needs seeding.
    """
    cache = manager.cache_path(repository)
    if cache.exists():
        return cache
    if seed is None or not seed.is_dir():
        raise RuntimeError(f"no repo cache and no seed cache for {repository}")
    children = [path for path in seed.iterdir() if path.is_dir()]
    if len(children) != 1:
        raise RuntimeError(f"seed cache must contain exactly one bare repo: {seed}")
    shutil.copytree(children[0], cache, symlinks=True)
    return cache


def _pytest_collected(stdout: str) -> int:
    matches = _COLLECTED.findall(_ANSI.sub("", stdout))
    return int(matches[-1]) if matches else 0


def _collect(
    instance: str,
    image: str,
    workspace: Path,
    output: Path,
    spec: EnvSpec,
    *,
    runner: RunnerAdapter | None,
    scope: tuple[str, ...],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run the preflight command and return a typed, timed result.

    Native runners have no collect-only mode, so their preflight is a bounded
    smoke *execution* of the issue-derived scope.  That is recorded explicitly
    instead of pretending it was a collection.
    """
    output.mkdir(parents=True, exist_ok=True)
    native = runner is not None and runner.native
    kind = runner.kind if runner is not None else PYTEST
    if native:
        command = runner.build_command(spec.test_command, scope, workspace=workspace)
    else:
        command = _PYTEST_PREFIX.sub(
            "/opt/miniconda3/envs/testbed/bin/python -m pytest", spec.test_command
        )
        command = _pytest_command(instance, command)
        if runner is not None:
            command = runner.build_command(command, (), workspace=workspace)
        command = command.replace("-m pytest", "-m pytest -p no:cacheprovider", 1)
        if scope:
            command += " " + " ".join(shlex.quote(item) for item in scope)
        command += " --collect-only -q"
    stage_started = time.perf_counter()
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
            "PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--env",
            "HOME=/tmp",
            "--label",
            "codepilot=1",
            "--label",
            f"codepilot.phase21=preflight-{instance}",
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
    timed_out = False
    exit_code: int | None = None
    timed_out_at: str | None = None
    try:
        _required(_docker("start", create), "start collection container")
        try:
            waited = _docker("wait", create, timeout=timeout_seconds)
            exit_code = int(waited.stdout.strip())
        except subprocess.TimeoutExpired:
            timed_out = True
            timed_out_at = f"collection exceeded {timeout_seconds}s"
            subprocess.run(
                ["docker", "kill", create],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "MSYS_NO_PATHCONV": "1"},
            )
        logs = subprocess.run(
            ["docker", "logs", create],
            capture_output=True,
            check=False,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        (output / "stdout.log").write_bytes(logs.stdout)
        (output / "stderr.log").write_bytes(logs.stderr)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", create],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
    duration = time.perf_counter() - stage_started
    stdout = (output / "stdout.log").read_text(encoding="utf-8", errors="replace")
    stderr = (output / "stderr.log").read_text(encoding="utf-8", errors="replace")
    parsed: dict[str, Any] = {}
    ok = False
    parse_status: str | None = None
    if native:
        assert runner is not None
        outcome = runner.parse(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            workspace=workspace,
            # The preflight scope is a runner selector (module/file), not a
            # canonical NodeID list; filtering by it would report every parsed
            # node as missing.
            requested=(),
            timed_out=timed_out,
        )
        assert outcome is not None
        parse_status = outcome.status.value
        parsed = {
            "tests": len(outcome.tests),
            "passed": sum(test.outcome.value == "passed" for test in outcome.tests),
            "failed": sum(test.outcome.value in {"failed", "error"} for test in outcome.tests),
            "missing": list(outcome.missing),
            "diagnostics": outcome.diagnostics,
        }
        ok = (
            not timed_out
            and len(outcome.tests) > 0
            and outcome.status.value in {"PASS", "FAIL"}
            and not outcome.missing
        )
    else:
        collected = _pytest_collected(stdout)
        junit = output / "junit.xml"
        parsed = {"tests_collected": collected, "junit_present": junit.is_file()}
        parse_status = "COLLECTED" if collected else "NO_TESTS_COLLECTED"
        ok = not timed_out and exit_code == 0 and collected > 0
    return {
        "kind": kind,
        "collection_mode": "execute" if native else "collect_only",
        "command": command,
        "scope": list(scope),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timed_out_at": timed_out_at,
        "duration_seconds": round(duration, 3),
        "parse_status": parse_status,
        "parsed": parsed,
        "stdout": str(output / "stdout.log"),
        "stderr": str(output / "stderr.log"),
        "ok": ok,
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
    parser.add_argument("--runner-overrides", type=Path, default=None)
    parser.add_argument("--cache-seeds", type=Path, default=None)
    parser.add_argument("--source-root", type=Path, default=None)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    artifact = args.artifact_root if args.artifact_root.is_absolute() else root / args.artifact_root
    catalog_path = args.catalog if args.catalog.is_absolute() else root / args.catalog
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    runner_path = (
        args.runner_overrides if args.runner_overrides is None else Path(args.runner_overrides)
    )
    if runner_path is not None and not runner_path.is_absolute():
        runner_path = root / runner_path
    runners = RunnerOverrides.load(runner_path)
    cache_seeds_path = (
        args.cache_seeds
        if args.cache_seeds is None
        else (args.cache_seeds if args.cache_seeds.is_absolute() else root / args.cache_seeds)
    )
    cache_seeds = (
        json.loads(cache_seeds_path.read_text(encoding="utf-8")).get("seeds", {})
        if cache_seeds_path is not None and cache_seeds_path.is_file()
        else {}
    )
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
        "schema_version": 2,
        "batch_id": catalog["batch_id"],
        "generated_at": _now(),
        "repo_root": str(root),
        "runner_overrides": str(runner_path) if runner_path is not None else None,
        "runner_overrides_sha256": runners.sha256 if runners is not None else None,
        "runner_kinds": runners.kinds() if runners is not None else {},
        "docker_engine": _required(
            _docker("version", "--format", "{{.Server.Version}}"), "Docker Engine"
        ).stdout.strip(),
        "credential_reference_present": _credential_present(root),
        "instances": [],
    }
    for item in catalog["instances"]:
        instance = item["instance_id"]
        started = _now()
        timings: dict[str, float] = {}
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
        runner_config = runners.for_instance(instance) if runners is not None else None
        runner = RunnerAdapter(runner_config) if runner_config is not None else None
        try:
            resolution = resolver.resolve(instance)
            inspect = _docker("image", "inspect", resolution.image_ref)
            if inspect.returncode != 0:
                if (
                    "No such image" not in inspect.stderr
                    and "not found" not in inspect.stderr.lower()
                ):
                    raise RuntimeError(f"image inspect failed: {inspect.stderr.strip()}")
                _required(
                    _docker("pull", resolution.image_ref, timeout=3600),
                    "pull missing official image",
                )
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
            task = loader.get_inference_task("test", instance)
            manager = WorkspaceManager(cache_root=instance_root / "repo-cache")
            workspace = preflight_root / "workspace"
            stage = time.perf_counter()
            seed_value = cache_seeds.get(instance)
            seed = (root / str(seed_value)).resolve() if seed_value else None
            cache = _seed_cache(manager, str(task.repository), seed)
            if _git(cache, "rev-list", "--all", "--count").stdout.strip() != "1":
                raise RuntimeError(f"seed cache is not base-only: {cache}")
            if _git(cache, "rev-parse", "HEAD").stdout.strip() != item["base_commit"]:
                raise RuntimeError("seed cache HEAD does not match the catalog base")
            if not workspace.exists():
                manager.create(
                    source=str(task.repository),
                    base_commit=item["base_commit"],
                    target=workspace,
                    mode="copy",
                )
            timings["workspace_prepare"] = round(time.perf_counter() - stage, 3)
            if _git(workspace, "rev-parse", "HEAD").stdout.strip() != item["base_commit"]:
                raise RuntimeError("workspace base identity mismatch")
            if _git(workspace, "rev-list", "--all", "--count").stdout.strip() != "1":
                raise RuntimeError("workspace contains history beyond the shallow base")
            clean = _git(workspace, "status", "--porcelain").stdout.strip()
            if clean:
                raise RuntimeError(f"workspace is not clean: {clean[:500]}")
            stage = time.perf_counter()
            sandbox = OfficialInstanceSandbox(
                artifacts=ArtifactStore(preflight_root, "official-image"),
                allowed_workspace_root=instance_root,
                instance_id=instance,
                images=resolver,
                runners=runners,
            )
            environment = sandbox.build_env(
                repository=item["repo"],
                base_commit=item["base_commit"],
                workspace=workspace,
                env_spec=task.env_spec,
            )
            timings["environment"] = round(time.perf_counter() - stage, 3)
            result["environment"] = environment.model_dump(mode="json")
            result["cache"] = {
                "path": str(cache),
                "base_commit": item["base_commit"],
                "commits": 1,
            }
            per_instance_testdeps = instance_testdeps_root(instance)
            if per_instance_testdeps.is_dir():
                result["testdeps_files"] = _copy_testdeps(per_instance_testdeps, workspace)
            scope = tuple(runner_config.preflight_scope) if runner_config is not None else ()
            timeout_seconds = int(
                runner_config.timeout_seconds
                if runner_config and runner_config.timeout_seconds
                else 900
            )
            stage = time.perf_counter()
            result["collection"] = _collect(
                instance,
                resolution.image_ref,
                workspace,
                preflight_root / "collection",
                task.env_spec,
                runner=runner,
                scope=scope,
                timeout_seconds=timeout_seconds,
            )
            timings["collection"] = round(time.perf_counter() - stage, 3)
            result["ok"] = bool(result["collection"]["ok"])
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["timings"] = timings
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
