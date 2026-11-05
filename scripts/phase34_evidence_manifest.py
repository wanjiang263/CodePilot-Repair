"""Write and verify the in-repo Phase34 evidence manifest (SHA256SUMS).

The Phase34 runtime evidence is only reproducible if the files it reads ship
with the repository. This tool owns the manifest for
``docs/notes/phase34-evidence``: ``--write`` hashes every file in the pack
(except the manifest itself) and ``--check`` proves the pack still matches.
The runtime generator imports :func:`verify_manifest` so a missing or tampered
evidence file is reported as ``evidence_missing``/``evidence_manifest_mismatch``
instead of looking like a runtime payload mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "docs" / "notes" / "phase34-evidence"
MANIFEST_NAME = "SHA256SUMS"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse(folder: Path) -> tuple[dict[str, str], list[str]]:
    manifest = folder / MANIFEST_NAME
    if not manifest.is_file():
        return {}, [f"missing {MANIFEST_NAME}"]
    entries: dict[str, str] = {}
    errors: list[str] = []
    for lineno, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            errors.append(f"line {lineno}: expected '<sha256>  <path>'")
            continue
        digest, name = parts
        name = name.strip()
        if name.startswith("*"):
            name = name[1:]
        name = name.replace("\\", "/")
        if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
            errors.append(f"line {lineno}: invalid sha256 {digest!r}")
            continue
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            errors.append(f"line {lineno}: unsafe path {name!r}")
            continue
        if name in entries:
            errors.append(f"line {lineno}: duplicate path {name!r}")
            continue
        entries[name] = digest.lower()
    if not entries and not errors:
        errors.append(f"{MANIFEST_NAME} lists no files")
    return entries, errors


def verify_manifest(folder: Path) -> dict[str, Any]:
    """Return a JSON-ready integrity report for an evidence folder."""
    report: dict[str, Any] = {"folder": str(folder), "manifest": MANIFEST_NAME}
    if not folder.is_dir():
        report.update({"ok": False, "reason": "evidence_missing", "missing": [MANIFEST_NAME]})
        return report
    manifest = folder / MANIFEST_NAME
    if not manifest.is_file():
        report.update(
            {
                "ok": False,
                "reason": "evidence_missing",
                "entries": 0,
                "paths": [],
                "missing": [MANIFEST_NAME],
                "mismatched": [],
                "errors": [],
            }
        )
        return report
    entries, errors = _parse(folder)
    missing = sorted(name for name in entries if not (folder / name).is_file())
    mismatched = sorted(
        name
        for name, digest in entries.items()
        if (folder / name).is_file() and sha256(folder / name) != digest
    )
    if missing:
        reason = "evidence_missing"
    elif mismatched:
        reason = "evidence_manifest_mismatch"
    elif errors:
        reason = "evidence_manifest_invalid"
    else:
        reason = "ok"
    report.update(
        {
            "ok": reason == "ok",
            "reason": reason,
            "entries": len(entries),
            "paths": sorted(entries),
            "missing": missing,
            "mismatched": mismatched,
            "errors": errors,
        }
    )
    return report


def write_manifest(folder: Path) -> dict[str, Any]:
    if not folder.is_dir():
        return {"ok": False, "reason": "evidence_missing", "folder": str(folder)}
    names = sorted(
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    )
    if not names:
        return {"ok": False, "reason": "evidence_empty", "folder": str(folder)}
    lines = [f"{sha256(folder / name)}  {name}" for name in names]
    (folder / MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"ok": True, "manifest": str(folder / MANIFEST_NAME), "entries": len(names)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default=str(DEFAULT_DIR))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite SHA256SUMS")
    mode.add_argument("--check", action="store_true", help="verify SHA256SUMS")
    args = parser.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = ROOT / folder
    result = write_manifest(folder) if args.write else verify_manifest(folder)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
