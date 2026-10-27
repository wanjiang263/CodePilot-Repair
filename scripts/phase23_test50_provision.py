"""Provision frozen test50 environments without starting any Agent/model run."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import docker

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    source = args.preflight.resolve(strict=True)
    output = args.output.resolve()
    assert source.is_relative_to(ROOT) and output.is_relative_to(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    preflight = json.loads(source.read_text(encoding="utf8"))
    assert preflight["total"] == 50
    prior = {}
    if args.resume_from:
        if not args.reason:
            raise ValueError("resume requires a new-information reason")
        progress = args.resume_from.resolve(strict=True) / "progress.jsonl"
        assert progress.is_relative_to(ROOT)
        for line in progress.read_text(encoding="utf8").splitlines():
            row = json.loads(line)
            prior[row["instance_id"]] = row
        prior_sha = hashlib.sha256(progress.read_bytes()).hexdigest()

    def provision(row):
        started = time.monotonic()
        result = {key: row[key] for key in ("instance_id", "base_commit", "image_ref", "runner")}
        log = output / (row["instance_id"] + ".jsonl")
        try:
            client = docker.from_env(timeout=60)
            old = prior.get(row["instance_id"])
            if old and old["status"] == "ENVIRONMENT_CLI_READY":
                image = client.images.get(row["image_ref"])
                assert image.id == old["image_id"] and row["base_commit"] == old["base_commit"]
                return {
                    **old,
                    "reused": True,
                    "previous_progress_sha256": prior_sha,
                    "recovery_reason": args.reason,
                }
            if shutil.disk_usage(ROOT).free < 15 * 1024**3:
                raise RuntimeError(
                    "RESOURCE_BLOCKED: D drive must retain 15 GiB for existing workspaces"
                )
            try:
                image = client.images.get(row["image_ref"])
                result["download"] = "CACHED"
            except docker.errors.ImageNotFound:
                with log.open("w", encoding="utf8") as stream:
                    for event in client.api.pull(row["image_ref"], stream=True, decode=True):
                        stream.write(json.dumps(event) + "\n")
                        stream.flush()
                        if event.get("error"):
                            raise RuntimeError(event["error"]) from None
                        if time.monotonic() - started > 1200:
                            raise TimeoutError("bounded image download exceeded 1200s") from None
                        if event.get("status", "").startswith(
                            ("Status: Downloaded", "Status: Image is up to date")
                        ):
                            break
                image = client.images.get(row["image_ref"])
                result["download"] = "DOWNLOADED"
            result["image_id"] = image.id
            result["image_size_bytes"] = image.attrs.get("Size")
            assert image.attrs.get("Os") == "linux" and image.attrs.get("Architecture") == "amd64"
            python = "/opt/miniconda3/envs/testbed/bin/python"
            if row["runner"] == "django":
                command = f"{python} tests/runtests.py --help"
            elif row["runner"] == "sympy":
                command = f"{python} bin/test --help"
            else:
                command = f"{python} -m pytest --version"
            # Images carry environment setup trees, not necessarily the issue base.
            # Reset only the isolated disposable container to the frozen public base.
            base = row["base_commit"]
            assert len(base) == 40 and all(c in "0123456789abcdef" for c in base)
            command = (
                f"git -c safe.directory=/testbed cat-file -e {base}^{{commit}} && "
                f"git -c safe.directory=/testbed reset --hard {base} >/dev/null && "
                "git -c safe.directory=/testbed rev-parse HEAD && "
                + python
                + " --version && "
                + command
            )
            container = client.containers.create(
                row["image_ref"],
                command=["/bin/bash", "-lc", command],
                working_dir="/testbed",
                network_mode="none",
                read_only=False,
                tmpfs={"/tmp": "size=256m"},
                mem_limit="4g",
                pids_limit=512,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                environment={
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": "/testbed:/testbed/src:/testbed/tests",
                    "HOME": "/tmp",
                },
                labels={"codepilot": "1", "codepilot.phase23": "test50-environment-only"},
            )
            try:
                container.start()
                outcome = container.wait(timeout=120)
                text = container.logs(stdout=True, stderr=True).decode(errors="replace")
                (output / (row["instance_id"] + "-runner.txt")).write_text(text, encoding="utf8")
                assert outcome["StatusCode"] == 0, f"runner CLI exit {outcome['StatusCode']}"
                identity_stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
                assert identity_stdout.splitlines()[0].strip() == row["base_commit"], (
                    "image base HEAD mismatch"
                )
                result["runner_cli"] = "PASS"
                result["base_identity"] = "PASS"
            finally:
                container.remove(force=True)
            result["status"] = "ENVIRONMENT_CLI_READY"
            result["remaining"] = (
                "Clean host workspace, index identity and issue-derived bounded real-test preflight before paid dispatch. CLI smoke is not a test pass."
            )
        except Exception as exc:
            result["status"] = "ERROR"
            result["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        result["elapsed_seconds"] = time.monotonic() - started
        return result

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(provision, row) for row in preflight["instances"]]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            with (output / "progress.jsonl").open("a", encoding="utf8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(row["instance_id"], row["status"], row.get("error", ""), flush=True)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "No model calls; fixed image provisioning, base and runner CLI verification",
        "model_requests": 0,
        "total": 50,
        "ready": sum(row["status"] == "ENVIRONMENT_CLI_READY" for row in rows),
        "instances": rows,
    }
    (output / "provisioning.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf8"
    )


if __name__ == "__main__":
    main()
