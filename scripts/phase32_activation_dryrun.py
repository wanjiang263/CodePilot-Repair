"""Phase32 W4: v5 activation dry-run against a throwaway archive root.

Why not the real pointer: ``configs/harness/active.json`` must stay absent (v5
is pending, no activation this phase). The functions under test take an
explicit archive root, and ``rsi.select`` stages ``active.next.json`` and then
renames it, so running them against a copied root exercises the exact staging
path while leaving the repository pointer untouched.

What this proves, in order:
 1. with no pointer the fail-closed path yields the builtin pending selection;
 2. a pending candidate is refused by ``select`` unless ``allow_pending`` is set;
 3. staging the v5 snapshot writes ``active.next.json`` then ``active.json``
    inside the temp root and ``load_selection`` reads the v5 snapshot back;
 4. the real archive root still has no pointer and the v4/v5 candidate records
    are byte-identical before and after.

Usage: ``.venv/Scripts/python.exe scripts/phase32_activation_dryrun.py``
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from codepilot import rsi  # noqa: E402

SCRATCH = ROOT / ".codex-pytest-tmp" / "phase32"
REAL_ARCHIVE = ROOT / "configs" / "harness"
VERSION = "phase31-candidate-v5"
PARENT = "phase30-candidate-v4"
OUT = SCRATCH / "activation-dryrun.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pointer_names(archive: Path) -> list[str]:
    return sorted(name for name in ("active.json", "active.next.json") if (archive / name).exists())


def candidate_digest(archive: Path, version: str) -> str:
    return sha256(archive / "candidates" / version / "candidate.json")


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    temp_archive = SCRATCH / "activation-dryrun" / "archive"
    if temp_archive.exists():
        shutil.rmtree(temp_archive)
    # Copy the whole archive root: snapshot validation can reach sibling
    # evidence (comparisons/), and the copy must contain no pointer to start
    # from the same fail-closed state as the repository.
    shutil.copytree(REAL_ARCHIVE, temp_archive)
    assert pointer_names(temp_archive) == [], "fixture archive must start pointer-free"
    record: dict[str, Any] = {
        "version": VERSION,
        "parent": PARENT,
        "real_archive": str(REAL_ARCHIVE.relative_to(ROOT)),
        "temp_archive": str(temp_archive.relative_to(ROOT)),
        "real_pointers_before": pointer_names(REAL_ARCHIVE),
        "v5_candidate_sha256_before": candidate_digest(REAL_ARCHIVE, VERSION),
        "v4_candidate_sha256_before": candidate_digest(REAL_ARCHIVE, PARENT),
    }

    builtin = rsi.load_selection(temp_archive)
    record["no_pointer_selection"] = {
        "version": builtin.get("version"),
        "status": builtin.get("status"),
    }

    refused = None
    try:
        rsi.select(temp_archive, VERSION)
    except ValueError as exc:
        refused = str(exc)
    record["pending_without_allow_pending"] = {"refused": refused is not None, "error": refused}
    record["temp_pointers_after_refusal"] = pointer_names(temp_archive)

    rsi.select(temp_archive, VERSION, allow_pending=True)
    record["temp_pointers_after_select"] = pointer_names(temp_archive)
    record["temp_active"] = json.loads((temp_archive / "active.json").read_text(encoding="utf-8"))

    selection = rsi.load_selection(temp_archive)
    record["loaded_selection"] = {
        "version": selection.get("version"),
        "status": selection.get("status"),
        "parent": selection.get("parent"),
        "prompt_roles": sorted(selection.get("prompts", {})),
        "experience_count": len(selection.get("experiences", [])),
    }

    record["real_pointers_after"] = pointer_names(REAL_ARCHIVE)
    record["v5_candidate_sha256_after"] = candidate_digest(REAL_ARCHIVE, VERSION)
    record["v4_candidate_sha256_after"] = candidate_digest(REAL_ARCHIVE, PARENT)
    record["real_pointer_absent"] = record["real_pointers_after"] == []
    record["candidates_untouched"] = (
        record["v5_candidate_sha256_before"] == record["v5_candidate_sha256_after"]
        and record["v4_candidate_sha256_before"] == record["v4_candidate_sha256_after"]
    )
    record["pending_refused_without_flag"] = refused is not None
    record["ok"] = bool(
        record["real_pointer_absent"]
        and record["candidates_untouched"]
        and record["pending_refused_without_flag"]
        and record["loaded_selection"]["version"] == VERSION
        and record["loaded_selection"]["status"] == "pending"
        # select stages active.next.json and renames it onto active.json, so the
        # only pointer left is the target itself.
        and record["temp_pointers_after_select"] == ["active.json"]
    )
    OUT.write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
