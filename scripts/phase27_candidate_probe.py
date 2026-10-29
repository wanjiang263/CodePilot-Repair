"""Real bounded Docker recovery and identity-checked official result recovery."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from codepilot.artifacts import ArtifactStore
from codepilot.eval.candidate import verify_candidate
from codepilot.eval.swebench_harness import HarnessCacheError, HarnessConfig, OfficialHarnessRunner
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.official import OfficialInstanceSandbox
from codepilot.sandbox.runners import RunnerOverrides
from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "artifacts/phase27/candidate"
    out.mkdir(exist_ok=True)
    old = root / "artifacts/phase25/django14999-recovery/runs/phase25-django14999-pytest-recovery"
    receipt = json.loads((old / "official-candidate.json").read_text(encoding="utf8"))
    prediction, patch_sha = verify_candidate(root, receipt)
    if not (out / "workspace").exists():
        workspace = WorkspaceManager(cache_root=out / "cache").create(
            source=str(root / "artifacts/phase25/seeds/django__django-14999/repo"),
            base_commit=receipt["base_commit"],
            target=out / "workspace",
        )
        subprocess.run(
            ["git", "-C", str(workspace.path), "apply", str(root / receipt["patch"])], check=True
        )
    else:
        from types import SimpleNamespace

        workspace = SimpleNamespace(path=out / "workspace")
    before = subprocess.check_output(["git", "-C", str(workspace.path), "diff", "--binary", "HEAD"])
    from codepilot.graph.stop_rules import patch_hash

    assert patch_hash(before.decode()) == receipt["canonical_patch_sha256"]
    report = json.loads((old / "report.json").read_text(encoding="utf8"))
    tests = list(report["baseline"]["outcomes"])[:3]
    backend = OfficialInstanceSandbox(
        artifacts=ArtifactStore(out, "docker-recovery"),
        allowed_workspace_root=out,
        instance_id=receipt["instance_id"],
        images=SWEBenchInstanceImageResolver.load(
            root / "artifacts/phase25/django14999-image-overrides.json"
        ),
        runners=RunnerOverrides.load(root / "artifacts/phase25/segment2/runners.json"),
        bind_workspace=True,
    )
    env = EnvSpec(timeout_seconds=1)
    built = backend.build_env(
        repository="", base_commit=receipt["base_commit"], workspace=workspace.path, env_spec=env
    )
    results_path = out / "docker-results.json"
    if results_path.exists():
        runs = json.loads(results_path.read_text(encoding="utf8"))
    else:
        runs = []
        for iteration, seconds in enumerate((1, 40), start=1):
            result = backend.run_tests(
                workspace=workspace.path,
                environment=built,
                env_spec=env.model_copy(update={"timeout_seconds": seconds}),
                tests=tests,
                artifact_iteration=iteration,
            )
            runs.append({"configured_timeout": seconds, "result": result.model_dump(mode="json")})
            results_path.write_text(json.dumps(runs, indent=2) + "\n", encoding="utf8")
    after = subprocess.check_output(["git", "-C", str(workspace.path), "diff", "--binary", "HEAD"])
    assert before == after
    assert runs[0]["result"]["status"] == "TIMEOUT"
    assert runs[1]["result"]["exit_code"] == 0 and len(runs[1]["result"]["tests"]) == len(tests)
    assert not runs[1]["result"]["tests_failed"]
    # Reuse the trusted official report; never re-evaluate just to create a row.
    # A missing identity sidecar is the controlled recovery failure.
    report_dir = out / "official-report"
    report_dir.mkdir()
    name = "glm-5.3-flash-main.phase25-django14999-pytest-recovery.json"
    shutil.copy2(old / "official-report" / name, report_dir / name)
    config = HarnessConfig(
        predictions_path=prediction,
        run_id=receipt["run_id"],
        report_dir=report_dir,
        max_workers=1,
        timeout_seconds=1800,
        instance_ids=(receipt["instance_id"],),
        harness_python=Path(sys.executable),
        harness_version="4.1.0",
        model_name_or_path="glm-5.3-flash-main",
        dataset_revision=receipt["dataset_revision"],
        dataset_snapshot=root
        / f"cache/swebench/princeton-nlp__SWE-bench_Lite__{receipt['dataset_revision']}/test.jsonl",
        cache_level="instance",
    )
    runner = OfficialHarnessRunner(config, artifacts=ArtifactStore(out, "official-recovery"))
    failure = None
    try:
        runner.run()
    except HarnessCacheError as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)}
    assert failure is not None, "missing cache identity must fail closed"
    (out / "official-first-failure.json").write_text(
        json.dumps(failure, indent=2) + "\n", encoding="utf8"
    )
    shutil.copy2(
        old / "official-report" / (name + ".identity.json"), report_dir / (name + ".identity.json")
    )
    recovered = runner.run()
    assert recovered.reused
    result = {
        "receipt": str(old / "official-candidate.json"),
        "instance": receipt["instance_id"],
        "base": receipt["base_commit"],
        "patch_sha256": patch_sha,
        "prediction_sha256": hashlib.sha256(prediction.read_bytes()).hexdigest(),
        "unchanged": True,
        "model_requests": 0,
        "inference_constructed": False,
        "official_recovery": recovered.model_dump(mode="json"),
        "historical_full_regression": str(
            root / "artifacts/phase25/django14999-recovery/postrun-validation/full-regression.json"
        ),
    }
    (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")
    print("Docker timeout recovered; official", recovered.decision, flush=True)


if __name__ == "__main__":
    main()
