"""Cross-check `configs/swebench/env-specs-4.1.0.json` against the installed official
`swebench==4.1.0` (`MAP_REPO_VERSION_TO_SPECS`), and record the official image-naming
rules that the frozen snapshot cannot express.

Run inside the harness venv (it is the only environment where swebench imports)::

    /root/swebench-venv/bin/python scripts/check_env_specs_against_official.py \
        --repo-root /root/cp-run/CodePilot-Repair --json-out /tmp/spec-check.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    from importlib.metadata import version as pkg_version

    import swebench  # noqa: F401
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

    snapshot_path = args.repo_root / "configs" / "swebench" / "env-specs-4.1.0.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    official = {repo: dict(versions) for repo, versions in MAP_REPO_VERSION_TO_SPECS.items()}

    frozen_specs = snapshot.get("specs", snapshot)
    if not isinstance(frozen_specs, dict):
        raise SystemExit(f"unexpected snapshot shape: {type(frozen_specs)}")

    repos_frozen = set(frozen_specs)
    repos_official = set(official)
    only_frozen = sorted(repos_frozen - repos_official)
    only_official = sorted(repos_official - repos_frozen)
    mismatched: list[str] = []
    for repo in sorted(repos_frozen & repos_official):
        if _canonical(frozen_specs[repo]) != _canonical(official[repo]):
            versions_frozen = set(frozen_specs[repo])
            versions_official = set(official[repo])
            if versions_frozen != versions_official:
                mismatched.append(f"{repo}: versions {sorted(versions_official - versions_frozen)}")
            else:
                differing = sorted(
                    version
                    for version in versions_frozen
                    if _canonical(frozen_specs[repo][version])
                    != _canonical(official[repo][version])
                )
                mismatched.append(f"{repo}: {differing}")

    payload = {
        "harness_installed_version": pkg_version("swebench"),
        "snapshot_path": str(snapshot_path),
        "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "repos_frozen": len(repos_frozen),
        "repos_official": len(repos_official),
        "only_in_frozen_snapshot": only_frozen,
        "missing_from_frozen_snapshot": only_official,
        "content_mismatches": mismatched,
        "identical": not (only_frozen or only_official or mismatched),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
