"""Phase 21 zero-model execution-capability verification.

This script exercises the real production path for each frozen instance:
fresh workspace from a base-only cache, official image identity, runner-specific
collection/execution, target tests, a mechanical non-test patch, regression
against the real baseline, a mock review bound to the patch, and the real
acceptance evaluator.

It deliberately never calls a model.  The synthetic acceptance report is marked
``simulated_llm=true`` so the acceptance gate reports ``real_run=false``; this
is the proof that zero-model work cannot be presented as a real Agent success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import l2cal10e_acceptance as acceptance  # noqa: E402
import m3_preflight as preflight  # noqa: E402

from codepilot.artifacts import ArtifactStore  # noqa: E402
from codepilot.eval.swebench import SWEBenchLoader  # noqa: E402
from codepilot.graph.stop_rules import patch_hash  # noqa: E402
from codepilot.patch.diff import generate_git_diff  # noqa: E402
from codepilot.repo.workspace import WorkspaceManager  # noqa: E402
from codepilot.sandbox.official import OfficialInstanceSandbox  # noqa: E402
from codepilot.sandbox.runners import RunnerOverrides  # noqa: E402
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver  # noqa: E402
from codepilot.schemas.enums import ExecutionStatus  # noqa: E402
from codepilot.schemas.review import ReviewResult  # noqa: E402
from codepilot.tools.base import WorkspacePathPolicy  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _outcomes(result: Any) -> dict[str, str]:
    return {test.node_id: test.outcome.value for test in result.tests}


def _run(
    sandbox: OfficialInstanceSandbox,
    workspace: Path,
    environment: Any,
    env_spec: Any,
    *,
    tests: list[str],
    iteration: int,
) -> Any:
    return sandbox.run_tests(
        workspace=workspace,
        environment=environment,
        env_spec=env_spec,
        tests=tests,
        artifact_iteration=iteration,
    )


def _seed_cache(instance_root: Path, repository: str, seed: Path | None) -> Path:
    manager = WorkspaceManager(cache_root=instance_root / "repo-cache")
    cache = manager.cache_path(repository)
    if cache.exists():
        return cache
    if seed is None or not seed.is_dir():
        raise RuntimeError(f"no seed cache for {repository}")
    children = [entry for entry in seed.iterdir() if entry.is_dir()]
    if len(children) != 1:
        raise RuntimeError(f"seed cache must hold exactly one bare repo: {seed}")
    shutil.copytree(children[0], cache, symlinks=True)
    return cache


def _apply_mechanical_patch(workspace: Path, base_commit: str, relative: str) -> tuple[str, str]:
    path = workspace / relative
    if not path.is_file():
        raise RuntimeError(f"mechanical patch target missing: {relative}")
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    text += "# CodePilot Phase 21 zero-model mechanical patch\n"
    path.write_text(text, encoding="utf-8")
    diff = generate_git_diff(workspace, base_commit, policy=WorkspacePathPolicy(workspace))
    if not diff.text.strip():
        raise RuntimeError("mechanical patch produced an empty diff")
    if diff.files != [relative]:
        raise RuntimeError(f"mechanical patch touched unexpected files: {diff.files}")
    return diff.text, patch_hash(diff.text)


def _synthetic_acceptance(
    instance_root: Path,
    *,
    instance: str,
    run_id: str,
    diff_text: str,
    target_tests: list[str],
    target_status: str,
) -> dict[str, Any]:
    candidate = patch_hash(diff_text)
    raw = _sha_text(diff_text)
    review = ReviewResult(
        approved=True,
        summary="Phase 21 mock review: mechanical non-test comment patch",
        reviewed_patch_hash=candidate,
        reviewed_iteration=1,
    )
    closure = {
        "patch_present": True,
        "candidate_patch_sha256": candidate,
        "validator_executed": True,
        "validator_passed": True,
        "target_tests_executed": True,
        "target_tests_status": target_status,
        "target_test_sha256": candidate,
        "target_test_source": "planner",
        "target_tests": target_tests,
        "target_test_baseline_failures": [],
        "regression_executed": True,
        "regression_complete": True,
        "regression_mode": "full",
        "regression_unexecuted_tests": 0,
        "regression_new_failures": 0,
        "review_executed": True,
        "review_approved": True,
        "review_high_issues": 0,
        "review_high_issues_known": True,
        "review_bound_to_candidate": True,
    }
    report = {
        "run_id": run_id,
        "task_id": instance,
        "git_diff": diff_text,
        "patch_hash": raw,
        "budget": {
            "max_iterations": 5,
            "max_test_runs": 18,
            "max_tool_calls_per_iteration": 30,
            "max_review_calls": 2,
            "max_runtime_seconds": 1800,
            "source": {"max_test_runs": "cli"},
        },
        "reproduction": {"status": "skipped", "kind": "zero_model_no_reproducer"},
        "attempts": [
            {
                "iteration": 1,
                "patch_hash": candidate,
                "validation": {"passed": True},
                "target_test_source": "planner",
                "target_tests": target_tests,
            }
        ],
        "review": review.model_dump(mode="json"),
        "regression": {"mode": "full", "unexecuted_tests": []},
        "closure": closure,
    }
    _write_json(instance_root / "runs" / run_id / "report.json", report)
    _write_json(
        instance_root / "reports" / f"{run_id}.json",
        {
            "run_id": run_id,
            "run_label": "phase21-zero-model",
            "status": "ZERO_MODEL_SYNTHETIC",
            "inference": {
                "simulated_llm": True,
                "sandbox_backend": "docker",
                "final_patch_sha256": raw,
                "target_test_source": "planner",
                "target_tests": target_tests,
            },
            "prediction": {"predictions_sha256": _sha_text(diff_text)},
        },
    )
    return acceptance.evaluate(
        instance_root, instance=instance, label="phase21-zero-model", run_id=run_id
    )


def verify_instance(
    instance: dict[str, Any],
    *,
    runner_config: Any,
    image: Any,
    loader: SWEBenchLoader,
    root: Path,
    seed: Path | None,
    scopes: dict[str, Any],
) -> dict[str, Any]:
    instance_id = instance["instance_id"]
    instance_root = root / instance_id
    if instance_root.exists() and any(instance_root.iterdir()):
        raise RuntimeError(f"zero-model artifact root already exists: {instance_root}")
    instance_root.mkdir(parents=True)
    timings: dict[str, float] = {}
    summary: dict[str, Any] = {
        "instance_id": instance_id,
        "zero_model": True,
        "not_agent_capability": True,
        "started_at": _now(),
        "image": {"ref": image.ref if hasattr(image, "ref") else None},
        "status": "RUNNING",
    }
    try:
        # 1. Fresh workspace from the base-only bare cache.
        task = loader.get_inference_task("test", instance_id)
        stage = time.perf_counter()
        cache = _seed_cache(instance_root, str(task.repository), seed)
        if _git(cache, "rev-list", "--all", "--count") != "1":
            raise RuntimeError("seed cache is not base-only")
        manager = WorkspaceManager(cache_root=instance_root / "repo-cache", default_mode="copy")
        workspace = manager.create(
            source=task.repository,
            base_commit=instance["base_commit"],
            target=instance_root / "workspace",
            mode="copy",
        )
        head = _git(workspace.path, "rev-parse", "HEAD")
        if head != instance["base_commit"]:
            raise RuntimeError(f"workspace HEAD {head} != base")
        if _git(workspace.path, "rev-list", "--all", "--count") != "1":
            raise RuntimeError("workspace contains history beyond base")
        if _git(workspace.path, "status", "--porcelain"):
            raise RuntimeError("fresh workspace is not clean")
        timings["fresh_workspace"] = round(time.perf_counter() - stage, 3)
        summary["workspace"] = {
            "path": str(workspace.path),
            "base_commit": head,
            "repository_id": workspace.repository_id,
            "fresh": True,
            "base_only": True,
            "clean": True,
        }

        task = loader.get_inference_task("test", instance_id)
        stage = time.perf_counter()
        sandbox = OfficialInstanceSandbox(
            artifacts=ArtifactStore(instance_root, "zero-model-sandbox"),
            allowed_workspace_root=instance_root,
            instance_id=instance_id,
            images=image.images,
            runners=image.runners,
        )
        environment = sandbox.build_env(
            repository=instance["repo"],
            base_commit=instance["base_commit"],
            workspace=workspace.path,
            env_spec=task.env_spec,
        )
        timings["environment"] = round(time.perf_counter() - stage, 3)
        summary["environment"] = environment.model_dump(mode="json")
        summary["runner"] = {
            "kind": runner_config.kind,
            "command": runner_config.test_command,
            "preflight_scope": list(runner_config.preflight_scope),
        }

        scope = list(scopes.get("baseline_scope") or [])
        targets = list(scopes.get("target_tests") or [])
        if not scope or not targets:
            raise RuntimeError("zero-model scope/targets are not configured")

        # 2. Baseline on a fresh workspace.
        stage = time.perf_counter()
        baseline = _run(
            sandbox,
            workspace.path,
            environment,
            task.env_spec,
            tests=scope,
            iteration=1,
        )
        timings["baseline"] = round(time.perf_counter() - stage, 3)
        baseline_outcomes = _outcomes(baseline)
        summary["baseline"] = {
            "status": baseline.status.value,
            "exit_code": baseline.exit_code,
            "tests": len(baseline.tests),
            "passed": sum(v == "passed" for v in baseline_outcomes.values()),
            "failed": sum(v in {"failed", "error"} for v in baseline_outcomes.values()),
            "skipped": sum(v == "skipped" for v in baseline_outcomes.values()),
            "error_reason": baseline.error_reason,
            "diagnostics": baseline.diagnostics,
        }
        if baseline.status not in {ExecutionStatus.PASS, ExecutionStatus.FAIL}:
            raise RuntimeError(f"baseline is not parseable: {baseline.status}")
        if not baseline.tests or not any(v == "passed" for v in baseline_outcomes.values()):
            raise RuntimeError("baseline has no passed tests (all-skipped is not PASS)")

        # 3. Target tests before the mechanical patch.
        stage = time.perf_counter()
        target_before = _run(
            sandbox,
            workspace.path,
            environment,
            task.env_spec,
            tests=targets,
            iteration=2,
        )
        timings["target_before"] = round(time.perf_counter() - stage, 3)
        target_outcomes = _outcomes(target_before)
        missing = [node for node in targets if node not in target_outcomes]
        summary["target_before"] = {
            "status": target_before.status.value,
            "tests": target_outcomes,
            "missing": missing,
            "error_reason": target_before.error_reason,
        }
        if missing:
            raise RuntimeError(f"target tests were not collected: {missing}")
        if not any(v == "passed" for v in target_outcomes.values()):
            raise RuntimeError("target execution produced no PASS evidence")

        # 4. Mechanical, non-test patch and re-run target + regression.
        stage = time.perf_counter()
        diff_text, canonical = _apply_mechanical_patch(
            workspace.path, instance["base_commit"], scopes["patch_file"]
        )
        timings["mechanical_patch"] = round(time.perf_counter() - stage, 3)
        summary["mechanical_patch"] = {
            "file": scopes["patch_file"],
            "canonical_sha256": canonical,
            "raw_sha256": _sha_text(diff_text),
            "files": [scopes["patch_file"]],
            "is_test_file": False,
        }
        target_after = _run(
            sandbox,
            workspace.path,
            environment,
            task.env_spec,
            tests=targets,
            iteration=3,
        )
        after_outcomes = _outcomes(target_after)
        summary["target_after"] = {
            "status": target_after.status.value,
            "tests": after_outcomes,
        }
        regression = _run(
            sandbox,
            workspace.path,
            environment,
            task.env_spec,
            tests=scope,
            iteration=4,
        )
        timings["target_after_and_regression"] = round(time.perf_counter() - stage, 3)
        regression_outcomes = _outcomes(regression)
        new_failures = sorted(
            node
            for node, outcome in regression_outcomes.items()
            if outcome in {"failed", "error"} and baseline_outcomes.get(node) == "passed"
        )
        summary["regression"] = {
            "status": regression.status.value,
            "tests": len(regression.tests),
            "new_failures": new_failures,
            "baseline_missing": sorted(set(baseline_outcomes) - set(regression_outcomes))[:20],
            "error_reason": regression.error_reason,
        }
        if target_after.status not in {ExecutionStatus.PASS, ExecutionStatus.FAIL}:
            raise RuntimeError(f"post-patch target is not parseable: {target_after.status}")
        if not all(after_outcomes.get(node) == "passed" for node in targets):
            raise RuntimeError("mechanical patch did not keep the target passing")
        if regression.status not in {ExecutionStatus.PASS, ExecutionStatus.FAIL}:
            raise RuntimeError(f"regression is not parseable: {regression.status}")
        if new_failures:
            raise RuntimeError(f"mechanical patch introduced new failures: {new_failures[:5]}")
        if summary["regression"]["baseline_missing"]:
            raise RuntimeError("regression did not report every baseline node")

        # 5. Mock review + acceptance, with the zero-model marker preserved.
        review = ReviewResult(
            approved=True,
            summary="Phase 21 mock review: mechanical non-test comment patch",
            reviewed_patch_hash=canonical,
            reviewed_iteration=1,
        )
        summary["mock_review"] = review.model_dump(mode="json")
        acceptance_result = _synthetic_acceptance(
            instance_root,
            instance=instance_id,
            run_id=f"zero-model-{instance_id}",
            diff_text=diff_text,
            target_tests=targets,
            target_status=target_after.status.value,
        )
        summary["acceptance"] = acceptance_result
        # Zero-model work must be rejected by the real-run gate; everything else
        # in the acceptance calculation is exercised.
        failed_checks = [name for name, ok in acceptance_result["checks"].items() if not ok]
        if failed_checks != ["real_run"]:
            raise RuntimeError(
                f"zero-model acceptance checks did not isolate real_run: {failed_checks}"
            )
        summary["status"] = "PASS"
        summary["zero_model_rejected_by_real_run_gate"] = True
    except Exception as exc:
        summary["status"] = "BLOCKED"
        summary["error"] = f"{type(exc).__name__}: {exc}"
    summary["timings"] = timings
    summary["finished_at"] = _now()
    _write_json(instance_root / "summary.json", summary)
    return summary


class _ImageBundle:
    def __init__(self, images: Any, runners: RunnerOverrides) -> None:
        self.images = images
        self.runners = runners


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runner-overrides", type=Path, required=True)
    parser.add_argument("--cache-seeds", type=Path, required=True)
    parser.add_argument("--scopes", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--instance", action="append", default=[])
    args = parser.parse_args()
    catalog_path = args.catalog if args.catalog.is_absolute() else ROOT / args.catalog
    config_path = args.config if args.config.is_absolute() else ROOT / args.config
    runner_path = (
        args.runner_overrides
        if args.runner_overrides.is_absolute()
        else ROOT / args.runner_overrides
    )
    seeds_path = args.cache_seeds if args.cache_seeds.is_absolute() else ROOT / args.cache_seeds
    scopes_path = args.scopes if args.scopes.is_absolute() else ROOT / args.scopes
    artifact_root = (
        args.artifact_root if args.artifact_root.is_absolute() else ROOT / args.artifact_root
    )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    seeds = json.loads(seeds_path.read_text(encoding="utf-8")).get("seeds", {})
    scopes = json.loads(scopes_path.read_text(encoding="utf-8")).get("instances", {})
    runners = RunnerOverrides.load(runner_path)
    assert runners is not None
    resolver = SWEBenchInstanceImageResolver.load(
        ROOT / "configs/swebench/instance-image-overrides.json"
        if (ROOT / "configs/swebench/instance-image-overrides.json").is_file()
        else None
    )
    loader = SWEBenchLoader(
        ROOT / "cache/swebench",
        revision=catalog["dataset_revision"],
        env_spec_snapshot=ROOT / "configs/swebench/env-specs-4.1.0.json",
    )
    loader.load_dataset("test")
    os.environ["CODEPILOT_BIND_WORKSPACE"] = "1"
    selected = set(args.instance) or {item["instance_id"] for item in catalog["instances"]}
    results = []
    for item in catalog["instances"]:
        instance_id = item["instance_id"]
        if instance_id not in selected:
            continue
        runner_config = runners.for_instance(instance_id)
        if runner_config is None:
            results.append(
                {
                    "instance_id": instance_id,
                    "status": "BLOCKED",
                    "error": "runner override is missing",
                }
            )
            continue
        resolution = resolver.resolve(instance_id)
        image_id = preflight._docker(
            "image", "inspect", "--format", "{{.Id}}", resolution.image_ref
        ).stdout.strip()
        if resolution.image_id is not None and image_id != resolution.image_id:
            results.append(
                {
                    "instance_id": instance_id,
                    "status": "BLOCKED",
                    "error": f"image identity mismatch: {image_id} != {resolution.image_id}",
                }
            )
            continue
        seed_value = seeds.get(instance_id)
        seed = (ROOT / seed_value).resolve() if seed_value else None
        results.append(
            verify_instance(
                item,
                runner_config=runner_config,
                image=_ImageBundle(resolver, runners),
                loader=loader,
                root=artifact_root,
                seed=seed,
                scopes=scopes.get(instance_id, {}),
            )
        )
    payload = {
        "schema_version": 1,
        "kind": "phase21_zero_model",
        "generated_at": _now(),
        "zero_model": True,
        "not_agent_capability": True,
        "artifact_root": str(artifact_root),
        "runners_sha256": runners.sha256,
        "config_sha256": _sha_file(config_path),
        "scopes_sha256": _sha_file(scopes_path),
        "instances": results,
        "recovered": sum(item.get("status") == "PASS" for item in results),
        "total": len(results),
    }
    _write_json(artifact_root / "summary.json", payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload["recovered"] == payload["total"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
