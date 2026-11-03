"""Phase32 T08: one minimal official SWE-bench image case (no pulls, no network).

Why: Phase32 has to show that the Docker execution path still works, using only
images already on this daemon. This script reuses the official instance image for
``psf__requests-3362`` and a copy of the Phase21 prepared workspace, runs exactly
one already-collected test, and writes the raw container outcome next to the
scratch tree.

The Phase21 workspace is copied instead of used in place: ``build_env`` writes
testdeps into the workspace, and historical evidence directories stay read-only.

Usage: ``.venv/Scripts/python.exe scripts/phase32_official_probe.py``
Output: ``.codex-pytest-tmp/phase32/official-probe.json``.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from codepilot.artifacts import ArtifactStore  # noqa: E402
from codepilot.eval.swebench import SWEBenchLoader  # noqa: E402
from codepilot.sandbox.official import OfficialInstanceSandbox  # noqa: E402
from codepilot.sandbox.runners import RunnerOverrides  # noqa: E402
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver  # noqa: E402

INSTANCE = "psf__requests-3362"
BASE_COMMIT = "36453b95b13079296776d11b09cab2567ea3e703"
TEST = "tests/test_utils.py::TestSuperLen::test_string"
SCRATCH = ROOT / ".codex-pytest-tmp" / "phase32"
SOURCE_WORKSPACE = (
    ROOT / "artifacts/phase21/m3-phase21-recovery-6-r3/instances" / INSTANCE / "preflight/workspace"
)
COPY = SCRATCH / "official-workspace"
ARTIFACTS = SCRATCH / "official-probe"
OUT = SCRATCH / "official-probe.json"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def clear_readonly(func: Any, path: str, _exc: Any) -> None:
    """git pack files are read-only on Windows; clear the bit and retry once."""
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, onerror=clear_readonly)


def prepare_workspace() -> Path:
    if COPY.exists():
        remove_tree(COPY)
    shutil.copytree(
        SOURCE_WORKSPACE,
        COPY,
        ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"),
    )
    head = git("rev-parse", "HEAD", cwd=COPY)
    if head != BASE_COMMIT:
        raise SystemExit(f"workspace HEAD {head!r} != {BASE_COMMIT!r}")
    return COPY


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    workspace = prepare_workspace()
    catalog = json.loads((ROOT / "configs/phase22/catalog.json").read_text("utf-8"))
    loader = SWEBenchLoader(
        ROOT / "cache/swebench",
        revision=catalog["dataset_revision"],
        env_spec_snapshot=ROOT / "configs/swebench/env-specs-4.1.0.json",
    )
    loader.load_dataset("test")
    task = loader.get_inference_task("test", INSTANCE)
    images = SWEBenchInstanceImageResolver.load(
        ROOT / "configs/swebench/instance-image-overrides.json"
    )
    runners = RunnerOverrides.load(ROOT / "configs/phase21/instance-runner-overrides.json")
    sandbox = OfficialInstanceSandbox(
        artifacts=ArtifactStore(ARTIFACTS, "light"),
        allowed_workspace_root=workspace.parent,
        instance_id=INSTANCE,
        images=images,
        runners=runners,
    )
    started = datetime.now(UTC)
    environment = sandbox.build_env(
        repository=task.repo,
        base_commit=BASE_COMMIT,
        workspace=workspace,
        env_spec=task.env_spec,
    )
    result = sandbox.run_tests(
        workspace=workspace,
        environment=environment,
        env_spec=task.env_spec,
        tests=[TEST],
        artifact_iteration=1,
    )
    payload: dict[str, Any] = {
        "phase": 32,
        "instance_id": INSTANCE,
        "base_commit": BASE_COMMIT,
        "test": TEST,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "image": sandbox.last_image,
        "environment": environment.model_dump(mode="json"),
        "status": result.status.value,
        "duration_seconds": result.duration_seconds,
        "error_reason": result.error_reason,
        "exit_code": getattr(result, "exit_code", None),
        "tests": [test.model_dump(mode="json") for test in result.tests],
    }
    OUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        json.dumps(
            {
                "instance_id": INSTANCE,
                "status": payload["status"],
                "tests": [
                    (test.get("node_id") or test.get("name"), test["outcome"])
                    for test in payload["tests"]
                ],
                "image_ref": (payload["image"] or {}).get("image_ref"),
                "image_id": (payload["image"] or {}).get("image_id"),
                "duration_s": payload["duration_seconds"],
            },
            indent=2,
        )
    )
    return 0 if str(payload["status"]).lower().startswith("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
