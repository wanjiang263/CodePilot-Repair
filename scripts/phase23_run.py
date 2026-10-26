"""One audited, typed recovery launcher for every fixed Phase23 instance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/phase23"


def read(p):
    return json.loads(p.read_text(encoding="utf-8-sig"))


def sha(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )


def phase_cost():
    rows = []
    for p in OUT.glob("instances/*/*/dispatch-ledger.json"):
        ledger = read(p)
        value = sum(
            r["cost_cny"] if r["status"] == "settled" else r["reserved_cny"]
            for r in ledger["requests"]
        )
        rows.append((p, p.parent.parent.name, value, ledger["frozen"]))
    return rows


async def export_checkpoint(source, destination):
    from codepilot.graph.checkpoint import checkpoint_state, open_checkpointer

    copied = destination.parent / "source-checkpoint.sqlite"
    if not copied.exists():
        shutil.copy2(source / "checkpoints.sqlite", copied)
    async with await open_checkpointer(copied, source.name) as handle:
        state = await checkpoint_state(handle.saver, source.name)
        if state is None:
            raise RuntimeError("no usable checkpoint state")
        write(destination, state.model_dump(mode="json"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--segment", type=int, default=1)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--preflight-scope", nargs="+")
    parser.add_argument("--entry")
    parser.add_argument("--source-run", type=Path)
    parser.add_argument("--reason")
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT or ROOT.drive.casefold() != "d:":
        raise RuntimeError("D project root required")
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    from codepilot.artifacts import ArtifactStore
    from codepilot.eval.assembly import InferenceAssembly, ProviderBundle
    from codepilot.eval.swebench import SWEBenchLoader
    from codepilot.llm.config import load_llm_config
    from codepilot.llm.dispatch_budget import DispatchBudget, configure_dispatch_budget
    from codepilot.llm.registry import transport_provider
    from codepilot.repo.workspace import WorkspaceManager
    from codepilot.sandbox.official import OfficialInstanceSandbox
    from codepilot.sandbox.runners import RunnerOverrides
    from codepilot.sandbox.swebench_env import SWEBenchInstanceImageResolver
    from codepilot.schemas.budget import RepairBudget
    from codepilot.schemas.run_config import IndexSnapshot, InferenceRunConfig, StageRecovery

    audit = read(OUT / "opening-audit.json")
    row = next(x for x in audit["instances"] if x["instance_id"] == args.instance)
    if row.get("official_reused"):
        raise RuntimeError("sklearn already resolved; no dispatch")
    original = ROOT / row["source_run"]
    previous = (args.source_run or original).resolve()
    if not previous.is_relative_to(ROOT) or (args.source_run and not args.reason):
        raise ValueError("recovery source must be inside D project, with a new-information reason")
    instance = OUT / "instances" / args.instance
    segment = instance / (
        f"preflight-{args.segment}" if args.preflight else f"segment-{args.segment}"
    )
    segment.mkdir(parents=True, exist_ok=False)
    process = {
        "pid": os.getpid(),
        "status": "running",
        "code_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    write(segment / "process.json", process)
    started = time.monotonic()
    paid_lock = None
    if not args.preflight:
        paid_lock = OUT / "paid.lock"
        descriptor = os.open(paid_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, json.dumps({"pid": os.getpid(), "instance": args.instance}).encode())
        os.close(descriptor)
    try:
        catalog = read(ROOT / "configs/phase22/catalog.json")
        loader = SWEBenchLoader(
            ROOT / "cache/swebench",
            revision=catalog["dataset_revision"],
            env_spec_snapshot=ROOT / "configs/swebench/env-specs-4.1.0.json",
        )
        task = loader.get_inference_task("test", args.instance)
        manager = WorkspaceManager(
            cache_root=original.parent.parent / "repo-cache", default_mode="copy"
        )
        images = SWEBenchInstanceImageResolver.load(
            ROOT / "configs/phase23/instance-image-overrides.json"
        )
        runners = RunnerOverrides.load(ROOT / "configs/phase21/instance-runner-overrides.json")

        def sandbox(artifacts):
            return OfficialInstanceSandbox(
                artifacts=artifacts,
                allowed_workspace_root=segment,
                instance_id=args.instance,
                images=images,
                runners=runners,
                bind_workspace=True,
            )

        run_options = InferenceRunConfig()
        llm = load_llm_config(ROOT / "configs/phase22/llm.yaml").for_model(
            run_options.model_alias, require_temperature=True
        )
        providers = ProviderBundle.live(llm, budget_currency="CNY", record_llm=True)
        transport = []
        for role, chain in providers.roles.items():
            actual = transport_provider(chain)
            if (
                actual.spec.alias != run_options.model_alias
                or actual.spec.model != "glm-5.3-flash"
                or actual.provider.name != "bigmodel"
                or actual.policy.max_attempts != 1
                or not actual.spec.supports_temperature
            ):
                raise RuntimeError(
                    "actual batch provider is not frozen BigModel GLM with zero retries"
                )
            config = llm.providers[llm.resolve_role(role).provider]
            expected = {
                "connect": config.connect_timeout_seconds or config.timeout_seconds,
                "read": config.timeout_seconds,
                "write": config.write_timeout_seconds or config.timeout_seconds,
                "pool": config.pool_timeout_seconds or config.timeout_seconds,
            }
            if (
                actual._client.timeout.as_dict() != expected
                or actual._client.trust_env != config.trust_env
            ):
                raise RuntimeError("actual batch client transport mismatch before HTTP")
            transport.append(
                {
                    "role": role,
                    "model": llm.resolve_role(role).model,
                    "alias": actual.spec.alias,
                    "temperature": run_options.temperature,
                    "supports_temperature": actual.spec.supports_temperature,
                    "timeout": actual._client.timeout.as_dict(),
                    "trust_env": actual._client.trust_env,
                    "retries": config.max_retries,
                }
            )
        write(segment / "actual-client.json", transport)
        archive = previous / "llm_calls.jsonl"
        if archive.is_file():
            from codepilot.llm.settled_cache import SettledResponseCache

            source_ledgers = list(previous.parent.parent.glob("dispatch-ledger*.json"))
            if len(source_ledgers) != 1 or any(
                r["status"] != "settled" for r in read(source_ledgers[0])["requests"]
            ):
                raise RuntimeError("only fully settled response archives can be reused")
            from dataclasses import replace

            cached_roles = {
                role: SettledResponseCache(
                    chain,
                    archive=archive,
                    alias=llm.resolve_role(role).alias,
                    evidence=segment / "settled-cache-hits.jsonl",
                )
                for role, chain in providers.roles.items()
            }
            providers = replace(providers, roles=cached_roles)

        if args.preflight:
            artifacts = ArtifactStore(segment, "light")
            workspace = manager.create(
                source=str(task.repository),
                base_commit=task.base_commit,
                target=artifacts.run_dir / "workspace",
                mode="copy",
            )
            backend = sandbox(artifacts)
            env = backend.build_env(
                repository=str(task.repository),
                base_commit=task.base_commit,
                workspace=workspace.path,
                env_spec=task.env_spec,
            )
            scope = (
                args.preflight_scope
                or read(ROOT / "configs/phase21/zero-model-scopes.json")["instances"][
                    args.instance
                ]["target_tests"]
            )
            result = backend.run_tests(
                workspace=workspace.path,
                environment=env,
                env_spec=task.env_spec,
                tests=scope,
                artifact_iteration=1,
            )
            write(
                segment / "environment.json",
                {
                    "environment": env.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                    "client_verified_before_http": True,
                },
            )
            candidates = list((previous / "reproduction").glob("*-candidate.py"))
            if candidates:
                import ast

                from codepilot.agents.reproducer import (
                    BaseWorkspaceValidator,
                    WorkspaceManagerBaseProvider,
                )

                candidate_file = candidates[0]
                content = candidate_file.read_text(encoding="utf8")
                names = [
                    node.name
                    for node in ast.parse(content).body
                    if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
                ]
                validator = BaseWorkspaceValidator(
                    sandbox=backend,
                    env_spec=task.env_spec,
                    environment=env,
                    provider=WorkspaceManagerBaseProvider(
                        manager,
                        source=str(task.repository),
                        base_commit=task.base_commit,
                        root=segment / "real-reproducer-base",
                        mode="copy",
                    ),
                )
                checked = validator.check(
                    filename="test_recovered_probe.py", content=content, attempt=1, test_names=names
                )
                write(
                    segment / "real-reproducer.json",
                    {
                        "source_candidate": str(candidate_file),
                        "source_sha256": sha(candidate_file),
                        "kind": checked.kind.value,
                        "valid": checked.valid,
                        "detail": checked.detail,
                        "execution": checked.execution.model_dump(mode="json")
                        if checked.execution
                        else None,
                    },
                )
                if checked.execution is None or checked.execution.is_environment_problem:
                    raise RuntimeError("real Reproducer workspace entry failed")
            providers.close()
            if result.status.value != "PASS" or not result.tests:
                raise RuntimeError("real light test failed")
            print(args.instance, "preflight PASS", len(result.tests), flush=True)
            return
        costs = phase_cost()
        prior_new = sum(x[2] for x in costs)
        instance_new = sum(x[2] for x in costs if x[1] == args.instance)
        remaining = min(
            24 - prior_new, 30 - audit["original30_used_upper_cny"] - prior_new, 4 - instance_new
        )
        if remaining < 0.82:
            raise RuntimeError("insufficient balance for full request reserve")
        history = {str(ROOT / p): h for p, h in audit["prior_authorization_sources"].items()}
        history.update({str(p): sha(p) for p, _, _, _ in costs})
        budget = DispatchBudget(
            remaining,
            segment / "dispatch-ledger.json",
            opening_source={
                "kind": "phase23_explicit_segment",
                "historical_sources": history,
                "original30_used_upper_before_segment": audit["original30_used_upper_cny"]
                + prior_new,
                "instance_phase23_used_upper": instance_new,
                "reason": "Purposeful recovery under current user authorization; all prior unknown included",
            },
        )
        configure_dispatch_budget(budget)
        state_file = previous / "state_final.json"
        if not state_file.exists():
            state_file = OUT / "imports" / args.instance / "state.json"
            if not state_file.exists():
                state_file.parent.mkdir(parents=True, exist_ok=True)
                asyncio.run(export_checkpoint(previous, state_file))
        raw = read(state_file)
        entry = (
            args.entry
            or {
                "sympy__sympy-22714": "review",
                "matplotlib__matplotlib-18869": "validate_patch",
                "sphinx-doc__sphinx-8273": "analyze_impact"
                if raw.get("localization")
                else "localize",
                "psf__requests-3362": "reproduce",
                "django__django-11422": "plan",
            }[args.instance]
        )
        candidate = (
            raw.get("current_patch", "")
            if entry in {"review", "validate_patch", "run_regression"}
            else ""
        )
        recovery = StageRecovery(
            source_state=state_file,
            source_artifacts=previous,
            source_state_sha256=sha(state_file),
            source_run_id=raw["run_id"],
            entry=entry,
            candidate_sha256=hashlib.sha256(candidate.encode()).hexdigest() if candidate else None,
            historical_ledger_hashes=history,
            reason=args.reason
            or {
                "review": "Re-review complete unchanged legal candidate; old review omitted patch",
                "validate_patch": "Recover legal candidate and discover relevant tests on base",
                "plan": "Replace test-edit loop with source-only typed tool policy and new diagnosis",
                "reproduce": "Packed objects/base-copy repaired; recover reproduction after real preflight",
                "analyze_impact": "Reuse settled localization checkpoint and verify production transport",
                "localize": "Recover verified issue analysis/retrieval after transport factory fix",
            }[entry],
            previous_runtime_seconds=row.get("runtime_seconds", 0),
        )
        index = previous / "index.sqlite3"
        runtime = InferenceRunConfig(
            budget=RepairBudget(
                max_iterations=5,
                max_test_runs=18,
                max_review_calls=2,
                max_runtime_seconds=1800,
                max_total_tokens=4_000_000,
                max_cost=remaining,
            ),
            index_snapshot=IndexSnapshot(path=index, sha256=sha(index)),
            recovery=recovery,
            deny_existing_test_edits=True,
        )
        write(segment / "config.json", runtime.model_dump(mode="json"))
        run_id = f"phase23-{args.instance}-s{args.segment}"
        assembly = InferenceAssembly(
            task=task,
            workspace_manager=manager,
            work_root=segment,
            sandbox_factory=sandbox,
            provider_bundle=providers,
            runtime_config=runtime,
            expected_budget=runtime.budget,
            record_llm=True,
        )
        outcome = assembly.run(run_id=run_id, seed=0)
        write(
            segment / "outcome.json",
            outcome.__dict__
            if hasattr(outcome, "__dict__")
            else {
                "status": outcome.status,
                "stop_reason": outcome.stop_reason,
                "run_id": run_id,
                "patch_sha256": outcome.final_patch_sha256,
            },
        )
        from codepilot.eval.candidate import prepare_candidate

        run = segment / "runs" / run_id
        receipt = prepare_candidate(
            project=ROOT,
            run=run,
            instance=args.instance,
            base=task.base_commit,
            source_ledger=segment / "dispatch-ledger.json",
            revision=catalog["dataset_revision"],
        )
        print(
            json.dumps(
                {
                    "instance": args.instance,
                    "internal": outcome.status,
                    "receipt": str(receipt) if receipt else None,
                    "ledger_frozen": budget.frozen,
                    "requests": len(budget.records),
                }
            ),
            flush=True,
        )
    except BaseException as exc:
        process.update(status="error", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        if process["status"] == "running":
            process["status"] = "exited"
        process["elapsed_seconds"] = time.monotonic() - started
        write(segment / "process.json", process)
        if paid_lock is not None:
            paid_lock.unlink()


if __name__ == "__main__":
    main()
