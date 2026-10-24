"""Persistent intent/result journal for Phase 9 external operations.

An uncertain intent is never silently replayed. SQLite commits the redacted result
and its digest together; this narrows, but cannot remove, external uncertainty.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from codepilot.graph.deps import Deps
from codepilot.observability.redaction import redact
from codepilot.patch.diff import generate_git_diff
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage


class OperationUncertainError(RuntimeError):
    """An operation has intent but no verifiable committed outcome."""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def execute_once[T: BaseModel](
    state: RepairState, deps: Deps, key: str, schema: type[T], action: Callable[[], T]
) -> T:
    identity = operation_identity(state, deps)
    # Identity (patch + baseline + environment + spec) rather than the attempt
    # number keys the evidence.  A repair round can re-produce the exact same
    # candidate patch; repeating L1/L2 for it spent three test runs in the
    # Sphinx Phase 20 trajectory and then let flaky results contradict the
    # earlier evidence.  A changed patch gets a different identity and can
    # never reuse the old result.
    operation_id = f"{identity[:16]}:{key}"
    with sqlite3.connect(deps.artifacts.run_dir / "operations.sqlite") as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, identity TEXT, payload TEXT, hash TEXT)"
        )
        row = db.execute(
            "SELECT identity,payload,hash FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if row:
            if row[0] != identity or row[1] is None:
                raise OperationUncertainError(
                    "identity mismatch or uncertain in-flight operation; usage may be unknown"
                )
            if digest(row[1]) != row[2]:
                raise OperationUncertainError("operation evidence corrupted")
            payload = json.loads(row[1])
            for name, expected in payload.get("artifacts", {}).items():
                path = Path(name)
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise OperationUncertainError(
                        "external execution artifact missing or corrupted"
                    )
            result = schema.model_validate(payload["result"])
            deps.accounting.seed(max(deps.test_runs, payload["test_runs"]))
            usage = Usage.model_validate(payload["usage"])
            if usage.llm_calls > deps.usage.llm_calls:
                deps.restore_usage(usage)
            return result
        db.execute("INSERT INTO operations VALUES (?,?,NULL,NULL)", (operation_id, identity))
        db.commit()
        result = action()
        references = {}
        directory = getattr(result, "artifact_dir", "")
        if directory:
            root = Path(directory)
            if not root.is_absolute():
                root = getattr(deps.accounting.inner, "artifacts", deps.artifacts).run_dir / root
            for path in [root] if root.is_file() else root.rglob("*"):
                if path.is_file():
                    references[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
        if key == "review":
            path = deps.artifacts.run_dir / "review" / f"{state.iteration}-model.json"
            if path.is_file():
                references[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = json.dumps(
            redact(
                {
                    "artifacts": references,
                    "result": result.model_dump(mode="json"),
                    "test_runs": deps.test_runs,
                    "usage": deps.usage.model_dump(mode="json"),
                }
            ),
            sort_keys=True,
        )
        db.execute(
            "UPDATE operations SET payload=?,hash=? WHERE id=?",
            (payload, digest(payload), operation_id),
        )
        db.commit()
        return schema.model_validate(json.loads(payload)["result"])


def operation_identity(state: RepairState, deps: Deps) -> str:
    patch = generate_git_diff(deps.workspace, state.base_commit, policy=deps.validator.policy)
    if patch.text != state.current_patch:
        raise OperationUncertainError("workspace/patch identity mismatch")
    return digest(
        {
            "run": state.run_id,
            "task": state.task_id,
            "base": state.base_commit,
            "workspace": str(deps.workspace.resolve()),
            "patch": patch.text,
            "baseline": (
                {**state.baseline.model_dump(mode="json"), "flaky": sorted(state.baseline.flaky)}
                if state.baseline
                else None
            ),
            "environment": {
                "cache_key": deps.environment.cache_key,
                "image_id": deps.environment.image_id,
                "image_ref": deps.environment.image_ref,
            },
            "spec": deps.env_spec.model_dump(mode="json"),
        }
    )


def verify_journal(state: RepairState, deps: Deps) -> None:
    """Verify completed evidence even when LangGraph will skip its node."""
    path = deps.artifacts.run_dir / "operations.sqlite"
    if not path.exists():
        if state.regression is not None or state.review is not None:
            raise OperationUncertainError("missing Phase 9 operation journal")
        return
    identity = operation_identity(state, deps)
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT identity,payload,hash FROM operations WHERE id LIKE ?",
            (f"{identity[:16]}:%",),
        ).fetchall()
    for recorded, raw, checksum in rows:
        if recorded != identity or raw is None or digest(raw) != checksum:
            raise OperationUncertainError("incomplete, mismatched or corrupt operation journal")
        payload = json.loads(raw)
        for name, expected in payload.get("artifacts", {}).items():
            artifact = Path(name)
            if (
                not artifact.is_file()
                or hashlib.sha256(artifact.read_bytes()).hexdigest() != expected
            ):
                raise OperationUncertainError("operation artifact missing or corrupted")
