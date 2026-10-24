from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codepilot.graph.stop_rules import patch_hash
from codepilot.sandbox.junit import parse_junit
from codepilot.sandbox.official import (
    _pytest_command,
    _reclassify_pytest_fixture_dependency,
)
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.execution import ExecutionResult

INSTANCE = "pytest-dev__pytest-11148"
BASE_COMMIT = "2f7415cfbc4b6ca62f9013f1abd27136f46b9653"
IMAGE_REF = "swebench/sweb.eval.x86_64.pytest-dev_1776_pytest-11148:latest"
IMAGE_ID = "sha256:b8eef9af40c714067a5455e8d88620439b536a5455fed96c5646b4ed37028442"
A21_RUN = "swebench-smoke-l2cal10e-a21-pytest-dev__pytest-11148-s0-6f621582"
A18_RUN = "swebench-smoke-l2cal10e-a18-pytest-dev__pytest-11148-s0-31ccfbc9"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _docker(*args: str, timeout: int = 660) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result


def _historical(root: Path, label: str, run_id: str) -> tuple[Path, dict[str, Any]]:
    run_dir = root / "artifacts/phase18/l2-cal10e" / f"{INSTANCE}-{label}" / "runs" / run_id
    return run_dir, json.loads((run_dir / "report.json").read_text(encoding="utf-8"))


def _extract_base(root: Path) -> Path:
    base = root / "base-testbed"
    if base.exists():
        raise FileExistsError(base)
    container = _docker("create", "--label", "codepilot=1", IMAGE_REF, "/bin/true").stdout.strip()
    try:
        _docker("cp", f"{container}:/testbed", str(base))
    finally:
        _docker("rm", container)
    _run_git(base, "config", "core.filemode", "false")
    _run_git(base, "config", "core.autocrlf", "false")
    _run_git(base, "checkout", "--detach", BASE_COMMIT)
    head = _run_git(base, "rev-parse", "HEAD").stdout.strip()
    if head != BASE_COMMIT:
        raise RuntimeError(f"base identity mismatch: {head} != {BASE_COMMIT}")
    return base


def _workspace(base: Path, patch: Path, testdeps: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(target)
    shutil.copytree(base, target, symlinks=True)
    shutil.copytree(testdeps, target / "testdeps", dirs_exist_ok=True)
    result = subprocess.run(
        ["git", "-C", str(target), "apply", "--whitespace=nowarn", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"historical patch did not apply: {result.stderr.strip()}")
    _run_git(target, "diff", "--check")
    delivered = _run_git(target, "diff").stdout
    if not delivered.strip():
        raise RuntimeError("historical patch produced an empty diff")
    if patch_hash(delivered) != patch_hash(patch.read_text(encoding="utf-8")):
        raise RuntimeError("applied diff identity differs from the historical patch")


def _execute(
    *,
    root: Path,
    base: Path,
    patch: Path,
    testdeps: Path,
    group: str,
    nodes: list[str],
    image: dict[str, Any],
) -> dict[str, Any]:
    group_root = root / group
    workspace = group_root / "workspace"
    output = group_root / "out"
    output.mkdir(parents=True)
    _workspace(base, patch, testdeps, workspace)
    _write_json(group_root / "requested-order.json", nodes)
    command = _pytest_command(
        INSTANCE,
        shlex.join(
            [
                "/opt/miniconda3/envs/testbed/bin/python",
                "-m",
                "pytest",
                "-q",
                "--junitxml=/out/results.xml",
                *nodes,
            ]
        ),
    )
    container = _docker(
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
        f"codepilot.phase20={group}",
        "--mount",
        f"type=bind,source={workspace.resolve()},target=/testbed",
        "--mount",
        f"type=bind,source={output.resolve()},target=/out",
        IMAGE_REF,
        "/bin/sh",
        "-lc",
        command,
    ).stdout.strip()
    started = datetime.now(UTC)
    try:
        _docker("start", container)
        exit_code = int(_docker("wait", container, timeout=600).stdout.strip())
        logs = subprocess.run(
            ["docker", "logs", container],
            capture_output=True,
            check=False,
            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        )
        if logs.returncode != 0:
            raise RuntimeError(f"docker logs failed: {logs.stderr.decode(errors='replace')}")
        (group_root / "stdout.log").write_bytes(logs.stdout)
        (group_root / "stderr.log").write_bytes(logs.stderr)
    finally:
        _docker("rm", container)
    junit_path = output / "results.xml"
    junit = parse_junit(junit_path)
    outcomes = Counter(test.outcome.value for test in junit.tests)
    actual_nodes = [test.node_id for test in junit.tests]
    failed = [test.node_id for test in junit.tests if test.outcome.value in {"failed", "error"}]
    complete = exit_code == 0 and actual_nodes == nodes and not failed
    payload = {
        "schema_version": 1,
        "group": group,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "instance_id": INSTANCE,
        "base_commit": BASE_COMMIT,
        "patch_sha256": _sha256(patch),
        "image_ref": IMAGE_REF,
        "image_id": image["Id"],
        "image_os": image.get("Os"),
        "image_architecture": image.get("Architecture"),
        "workspace": str(workspace),
        "workspace_mount_source": str(workspace.resolve()),
        "exit_code": exit_code,
        "status": "PASS" if complete else "FAIL",
        "requested_count": len(nodes),
        "junit_count": len(actual_nodes),
        "outcomes": dict(sorted(outcomes.items())),
        "suite_errors": junit.suite_errors,
        "failed_nodes": failed,
        "complete_requested_scope": complete,
        "junit": str(junit_path),
        "stdout": str(group_root / "stdout.log"),
        "stderr": str(group_root / "stderr.log"),
    }
    _write_json(group_root / "result.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    if output.exists():
        raise FileExistsError(f"refusing to overwrite replay root: {output}")
    output.mkdir(parents=True)
    image = json.loads(_docker("image", "inspect", IMAGE_REF).stdout)[0]
    if image["Id"] != IMAGE_ID:
        raise RuntimeError(f"image identity mismatch: {image['Id']} != {IMAGE_ID}")
    a21_dir, a21 = _historical(root, "a21", A21_RUN)
    a18_dir, a18 = _historical(root, "a18", A18_RUN)
    a21_patch = a21_dir / "patch_final.diff"
    a18_patch = a18_dir / "patch_final.diff"
    target = list(a21["closure"]["target_tests"])
    collection_order = [node for node in a21["baseline"]["run_outcomes"][0] if node in set(target)]
    if len(collection_order) != len(target):
        raise RuntimeError("a21 baseline does not cover every historical target")
    failed_nodes = [
        test["node_id"]
        for test in a21["test_execution"]["tests"]
        if test["outcome"] in {"failed", "error"}
    ]
    baseline_failed = {node: a21["baseline"]["outcomes"].get(node) for node in failed_nodes}
    if len(failed_nodes) != 8 or set(baseline_failed.values()) != {"passed"}:
        raise RuntimeError(f"unexpected a21 historical failure shape: {baseline_failed}")
    authorized = list(a18["baseline"]["run_outcomes"][0])
    historical_l2 = json.loads((a18_dir / "regression/1/L2.json").read_text(encoding="utf-8"))
    l2_nodes = [test["node_id"] for test in historical_l2["tests"]]
    if authorized != l2_nodes:
        raise RuntimeError("a18 historical L2 does not match the authorized baseline order")
    historical_result = ExecutionResult.model_validate(historical_l2)
    replay_input = historical_result.model_copy(
        update={
            "status": ExecutionStatus.ENV_ERROR,
            "error_reason": "MISSING_THIRD_PARTY_DEPENDENCY",
        }
    )
    reclassified = _reclassify_pytest_fixture_dependency(INSTANCE, replay_input)
    parsing = {
        "historical_source": str(a18_dir / "regression/1/L2.json"),
        "historical_sha256": _sha256(a18_dir / "regression/1/L2.json"),
        "historical_status": historical_result.status.value,
        "historical_exit_code": historical_result.exit_code,
        "historical_passed": historical_result.tests_passed,
        "historical_failed": historical_result.tests_failed,
        "historical_skipped": sum(
            test.outcome.value == "skipped" for test in historical_result.tests
        ),
        "replay_input_status": replay_input.status.value,
        "reclassified_status": reclassified.status.value,
        "reclassified_error_reason": reclassified.error_reason,
    }
    _write_json(output / "a18-parsing-replay.json", parsing)
    if historical_result.exit_code != 0 or historical_result.tests_failed != 0:
        raise RuntimeError("a18 historical evidence is not the documented exit-zero shape")
    if historical_result.tests_passed != 368 or reclassified.status is not ExecutionStatus.PASS:
        raise RuntimeError("a18 parsing replay did not reproduce the documented PASS shape")
    base = _extract_base(output)
    testdeps = root / "artifacts/phase18/l2-cal10e/testdeps"
    results = {
        "a21_lexicographic": _execute(
            root=output,
            base=base,
            patch=a21_patch,
            testdeps=testdeps,
            group="a21-lexicographic",
            nodes=sorted(target),
            image=image,
        ),
        "a21_collection": _execute(
            root=output,
            base=base,
            patch=a21_patch,
            testdeps=testdeps,
            group="a21-collection",
            nodes=collection_order,
            image=image,
        ),
        "a18_L2": _execute(
            root=output,
            base=base,
            patch=a18_patch,
            testdeps=testdeps,
            group="a18-L2",
            nodes=authorized,
            image=image,
        ),
        "a18_L3": _execute(
            root=output,
            base=base,
            patch=a18_patch,
            testdeps=testdeps,
            group="a18-L3",
            nodes=authorized,
            image=image,
        ),
    }
    eight = {}
    for node in failed_nodes:
        eight[node] = {
            key: next(
                (
                    test.outcome.value
                    for test in parse_junit(output / group / "out/results.xml").tests
                    if test.node_id == node
                ),
                "unexecuted",
            )
            for key, group in (
                ("lexicographic", "a21-lexicographic"),
                ("collection", "a21-collection"),
            )
        }
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_calls": 0,
        "official_harness_runs": 0,
        "image": {
            "ref": IMAGE_REF,
            "id": image["Id"],
            "architecture": image.get("Architecture"),
        },
        "a18_parsing_replay": parsing,
        "a18_docker": {"L2": results["a18_L2"], "L3": results["a18_L3"]},
        "a21_baseline_for_historical_failures": baseline_failed,
        "a21_eight_nodes": eight,
        "a21_order_explains_historical_failures": all(
            item["lexicographic"] in {"failed", "error"} and item["collection"] == "passed"
            for item in eight.values()
        ),
        "results": results,
        "historical_pytest_agent_p2": "NOT VERIFIED",
    }
    _write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
