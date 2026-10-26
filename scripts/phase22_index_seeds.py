"""Validate old indexes against clean preflight bases before freezing seed hashes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from phase22_prepare import read, sha, write_new  # noqa: E402

from codepilot.repo.index_snapshot import reuse_index  # noqa: E402
from codepilot.repo.workspace import Workspace, normalize_source, repository_id  # noqa: E402


def main() -> None:
    old = read(ROOT / "artifacts/phase21/m3-phase21-recovery-6-r5/preflight/preflight.json")
    candidates = list(
        (ROOT / "artifacts/phase21/m3-phase21-recovery-6-r5").glob(
            "instances/*/runs/*/index.sqlite3"
        )
    )
    candidates += list(
        (ROOT / "artifacts/phase20/m3-20261024-formal6").glob("instances/*/runs/*/index.sqlite3")
    )
    seeds = {}
    failures = []
    for entry in old["instances"]:
        instance = entry["instance_id"]
        path = Path(entry["collection"]["stdout"]).parents[1] / "workspace"
        source = normalize_source("https://github.com/" + entry["repo"])
        ws = Workspace(
            path, source, entry["base_commit"], repository_id(source, entry["base_commit"])
        )
        for candidate in candidates:
            if candidate.parents[2].name != instance:
                continue
            dest = ROOT / "artifacts/phase22/index-seeds" / (instance + ".sqlite3")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                digest = sha(candidate)
                reuse_index(candidate, dest, ws, digest)
                seeds[instance] = {
                    "path": dest.relative_to(ROOT).as_posix(),
                    "sha256": sha(dest),
                    "source": str(candidate.relative_to(ROOT)),
                    "source_sha256": digest,
                }
                print(instance, "validated", flush=True)
                break
            except ValueError as exc:
                failures.append({"source": str(candidate.relative_to(ROOT)), "reason": str(exc)})
    configpath = ROOT / "configs/phase22/config.json"
    config = read(configpath)
    config["index_snapshots"] = seeds
    configpath.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    write_new(
        ROOT / "artifacts/phase22/index-seeds-audit.json",
        {"validated": seeds, "rejected": failures},
    )


if __name__ == "__main__":
    main()
