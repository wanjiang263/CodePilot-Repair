"""Fail-closed preflight for the two l2cal10e instances.

Read-only for images, settings and the frozen cache source. The only write is a
copy of the single-commit bare cache into the new run root when that root has no
cache yet; ``WorkspaceManager`` must never fall back to a full remote clone.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--label", default="l2cal10e")
    parser.add_argument("--root-suffix", default="")
    parser.add_argument("--cache-source", type=Path, required=True)
    parser.add_argument(
        "--instances",
        type=Path,
        default=Path("configs/swebench/l2cal10e-instances.json"),
    )
    parser.add_argument("--prepare-caches", action="store_true")
    return parser


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _cache_identity(repo_cache: Path) -> dict[str, Any]:
    candidates = (
        [path for path in repo_cache.iterdir() if path.is_dir()] if repo_cache.is_dir() else []
    )
    if len(candidates) != 1:
        return {"ok": False, "reason": f"expected exactly one cache dir, found {len(candidates)}"}
    cache = candidates[0]
    head = _docker(["git", "-C", str(cache), "rev-parse", "HEAD"])
    count = _docker(["git", "-C", str(cache), "rev-list", "--count", "HEAD"])
    bare = _docker(["git", "-C", str(cache), "rev-parse", "--is-bare-repository"])
    status = _docker(["git", "-C", str(cache), "status", "--porcelain"])
    clean = status.returncode == 0 and not status.stdout.strip()
    is_bare = bare.stdout.strip() == "true"
    return {
        "ok": bool(head.returncode == 0 and count.stdout.strip() == "1" and (is_bare or clean)),
        "path": str(cache),
        "head": head.stdout.strip(),
        "commits": count.stdout.strip(),
        "bare": is_bare,
        "worktree_clean": clean if not is_bare else None,
    }


def _settings(repo_root: Path) -> dict[str, Any]:
    db = repo_root / "artifacts/phase18/settings/settings.sqlite3"
    if not db.is_file() or db.stat().st_size == 0:
        return {"ok": False, "reason": "settings DB missing"}
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "select payload_json from settings_documents where name='settings'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "reason": f"settings DB unreadable: {exc}"}
    if row is None:
        return {"ok": False, "reason": "settings document missing"}
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        return {"ok": False, "reason": f"settings payload invalid: {exc}"}
    provider = (payload.get("providers") or {}).get("bigmodel") or {}
    credential = provider.get("credential") or {}
    model_ids = [item.get("model_id") for item in provider.get("models") or []]
    ok = bool(
        provider.get("enabled") is True
        and credential.get("kind") == "env"
        and credential.get("name")
        and "glm-5.3-flash" in model_ids
    )
    return {
        "ok": ok,
        "provider_id": provider.get("provider_id"),
        "enabled": provider.get("enabled"),
        "credential_kind": credential.get("kind"),
        "credential_ref": credential.get("name"),
        "models": model_ids,
    }


def _dotenv_key(repo_root: Path, name: str) -> dict[str, Any]:
    dotenv = repo_root / ".env"
    if not dotenv.is_file():
        return {"ok": False, "reason": ".env missing"}
    present = False
    for line in dotenv.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name and value.strip():
            present = True
            break
    return {"ok": present, "exists": True, "value": "<redacted>" if present else None}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    base = args.base
    instances = json.loads((repo_root / args.instances).read_text(encoding="utf-8"))["instances"]
    preflight = base / "preflight"
    preflight.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "label": args.label,
        "root_suffix": args.root_suffix,
        "settings": _settings(repo_root),
        "dotenv": _dotenv_key(repo_root, "GLM_API_KEY"),
        "docker": _docker(["docker", "info", "--format", "{{.ServerVersion}}"]).stdout.strip(),
        "instances": [],
    }
    failed = False
    if not report["settings"]["ok"] or not report["dotenv"]["ok"] or not report["docker"]:
        failed = True
    for instance in instances:
        instance_id = instance["instance_id"]
        image_path = preflight / f"image-inspect-{instance_id}-{args.label}.json"
        inspect = _docker(["docker", "image", "inspect", instance["image_ref"]])
        image_path.write_text(
            inspect.stdout or json.dumps({"error": inspect.stderr}),
            encoding="utf-8",
        )
        image_id = _docker(
            ["docker", "image", "inspect", "--format", "{{.Id}}", instance["image_ref"]]
        ).stdout.strip()
        source_cache = args.cache_source / instance_id / "repo-cache"
        source_identity = _cache_identity(source_cache)
        root = base / f"{instance_id}{args.root_suffix}"
        root_cache = root / "repo-cache"
        copied = False
        if args.prepare_caches and source_identity.get("ok") and not root_cache.exists():
            root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_cache, root_cache, symlinks=True)
            copied = True
        prepared_identity = _cache_identity(root_cache) if root_cache.exists() else None
        identity = prepared_identity if prepared_identity is not None else source_identity
        cache_ok = bool(
            identity.get("ok")
            and identity.get("head") == instance["base_commit"]
            and identity.get("commits") == "1"
        )
        image_ok = image_id == instance["image_id"]
        root_ok = root_cache.exists() if args.prepare_caches else source_identity.get("ok")
        ok = bool(image_ok and cache_ok and root_ok)
        if not ok:
            failed = True
        report["instances"].append(
            {
                "instance_id": instance_id,
                "image_ref": instance["image_ref"],
                "image_id_actual": image_id,
                "image_id_expected": instance["image_id"],
                "image_ok": image_ok,
                "base_commit": instance["base_commit"],
                "cache_source": source_identity,
                "cache_root": str(root_cache),
                "cache_copied": copied,
                "cache_identity": identity,
                "cache_ok": cache_ok,
                "ok": ok,
            }
        )
    report["ok"] = not failed
    output = preflight / f"preflight-{args.label}.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failed else 3


if __name__ == "__main__":
    raise SystemExit(main())
