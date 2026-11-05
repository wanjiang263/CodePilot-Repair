"""Phase26 final corrections: authored, NOT EXECUTED (static-only delivery)."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot.graph.deps import AccountingSandbox
from codepilot.graph.validation_time import estimate_timeout, timing_identity
from codepilot.llm.retry import ConcurrencyLimiter
from codepilot.observability.dispatch import (
    Deadline,
    DispatchScope,
    DispatchStoppedError,
    dispatch_scope,
)
from codepilot.observability.lifecycle import snapshot_hash
from codepilot.observability.resources import ResourceCancelledError, resource_slot
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.execution import ExecutionResult
from codepilot.tools.base import PathAccessError, WorkspacePathPolicy
from codepilot.tools.query_cache import QueryCache
from codepilot.tools.read_tools import _search_root, read_evidence_tool, search_code_tool
from codepilot.tools.registry import ToolRegistry


def test_directory_prefix_and_fallback(tmp_path):
    directory = tmp_path / "src/module"
    directory.mkdir(parents=True)
    policy = WorkspacePathPolicy(tmp_path)
    assert _search_root(policy, "src/module/**") == directory
    assert policy.resolve_directory(".") == tmp_path
    for glob in (None, "*.py", "missing/**", "../escape/**"):
        assert _search_root(policy, glob) == tmp_path
    with pytest.raises(PathAccessError):
        policy.resolve_directory("../escape")


def test_scope_registry_cache_invalidation(tmp_path, monkeypatch):
    import codepilot.tools.query_cache as cache_module

    monkeypatch.setattr(cache_module.time, "monotonic", lambda: 0.0)
    directory = tmp_path / "src/module"
    directory.mkdir(parents=True)
    policy = WorkspacePathPolicy(tmp_path)
    registry = ToolRegistry([search_code_tool(policy)])
    registry.query_cache = QueryCache(policy)
    args = {"pattern": "needle", "path_glob": "src/module/**"}
    assert registry.execute("search_code", args).data["matches"] == []
    assert registry.execute("search_code", args).data["cache"]["hit"]
    source = directory / "a.py"
    source.write_text("needle = 1")
    assert registry.execute("search_code", args).data["matches"]
    source.write_text("absent = 1")
    assert not registry.execute("search_code", args).data["cache"]["hit"]
    (tmp_path / ".ignore").write_text("src/module/a.py")
    assert not registry.execute("search_code", args).data["cache"]["hit"]


def test_directory_symlink_rejected(tmp_path):
    (tmp_path / "real").mkdir()
    link = tmp_path / "alias"
    try:
        link.symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(PathAccessError):
        WorkspacePathPolicy(tmp_path).resolve_directory("alias")


def evidence(root, payload):
    ref = "tool-results/" + snapshot_hash(payload) + ".json"
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return ref


@pytest.mark.parametrize("deleted", [False, True])
def test_historical_existing_and_deleted_permissions(tmp_path, deleted):
    source = tmp_path / "a.py"
    source.write_text("old")
    archive = tmp_path / "task"
    ref = evidence(
        archive, {"tool": "read_file", "result": {"data": {"path": "a.py", "content": "old"}}}
    )
    if deleted:
        source.unlink()
    policy = WorkspacePathPolicy(tmp_path)
    tool = read_evidence_tool(archive, policy)
    result = tool.execute({"reference": ref, "length": 10})
    assert result.ok and result.data["historical"] and result.data["next_offset"] == 10
    policy.deny_files = frozenset({"a.py"})
    assert not tool.execute({"reference": ref}).ok


def test_evidence_tamper_and_other_task(tmp_path):
    archive = tmp_path / "task"
    ref = evidence(archive, {"tool": "search_code", "result": {"data": {"matches": []}}})
    tool = read_evidence_tool(archive, WorkspacePathPolicy(tmp_path))
    (archive / ref).write_text("{}")
    assert not tool.execute({"reference": ref}).ok
    assert not tool.execute({"reference": "../other/" + ref}).ok
    assert (
        not read_evidence_tool(tmp_path / "other", WorkspacePathPolicy(tmp_path))
        .execute({"reference": ref})
        .ok
    )


def test_wait_reclamps_actual_runner_timeout(tmp_path, monkeypatch):
    import codepilot.observability.resources as resources

    clock = [0.0]

    @contextmanager
    def wait(*args, **kwargs):
        clock[0] += 30
        yield

    monkeypatch.setattr(resources, "resource_slot", wait)
    received = []

    def run(**kwargs):
        received.append(kwargs["env_spec"].timeout_seconds)
        return ExecutionResult(status=ExecutionStatus.TIMEOUT)

    sandbox = AccountingSandbox(SimpleNamespace(run_tests=run))
    with dispatch_scope(DispatchScope(Deadline(100, lambda: clock[0]), reserve=10)):
        result = sandbox.run_tests(
            workspace=tmp_path,
            environment=EnvironmentBuildResult(status=ExecutionStatus.PASS, cache_key="env"),
            env_spec=EnvSpec(timeout_seconds=300),
        )
    assert received == [30] and sandbox.runs == 1
    assert result.diagnostics["time_allocation"]["final_timeout_seconds"] == 30
    assert not result.diagnostics["timing_fact"]["complete"]


def test_expired_or_cancelled_does_not_dispatch(tmp_path):
    sandbox = AccountingSandbox(SimpleNamespace(run_tests=lambda **kw: pytest.fail("dispatched")))
    for scope, exception in [
        (DispatchScope(Deadline(0)), DispatchStoppedError),
        (
            DispatchScope(Deadline(time.monotonic() + 20), cancel=lambda: True),
            ResourceCancelledError,
        ),
    ]:
        with dispatch_scope(scope), pytest.raises(exception):
            sandbox.run_tests(
                workspace=tmp_path,
                environment=EnvironmentBuildResult(status=ExecutionStatus.PASS, cache_key="env"),
                env_spec=EnvSpec(),
            )
    assert sandbox.runs == 0


def test_provider_limit_allows_two_and_cancel_releases():
    limiter = ConcurrencyLimiter(2)
    with limiter:
        with limiter:
            assert limiter.available == 0
        assert limiter.available == 1
    with dispatch_scope(DispatchScope(Deadline(0))), pytest.raises(DispatchStoppedError), limiter:
        pytest.fail("expired")
    assert limiter.available == 2


def test_resource_reentry_and_exception_release(tmp_path):
    with (
        pytest.raises(ValueError),
        resource_slot("workspace", workspace=tmp_path),
        resource_slot("workspace", workspace=tmp_path),
    ):
        raise ValueError("release")
    with (
        resource_slot("workspace", workspace=tmp_path),
        resource_slot("workspace", workspace=tmp_path / "other"),
    ):
        pass


def test_duration_exact_identity_lower_bounds_and_invalid():
    facts = [
        {"identity": "same", "seconds": 20, "complete": True, "status": "pass"},
        {"identity": "same", "seconds": 80, "status": "timeout"},
        {"identity": "same", "seconds": float("nan"), "complete": True, "status": "pass"},
    ]
    estimate = estimate_timeout("same", facts, 300)
    assert estimate["suggested_seconds"] >= 80
    assert estimate["complete_samples"] == 1 and estimate["timeout_samples"] == 1
    assert estimate_timeout("other", facts, 300)["suggested_seconds"] == 300
    assert (
        estimate_timeout("same", [{"identity": "same", "seconds": 999, "complete": False}], 300)[
            "source"
        ]
        == "conservative_default"
    )


def test_environment_order_and_runner_identity():
    base = timing_identity(
        "repo",
        "base",
        {"image_id": "one"},
        {"test_command": "pytest", "timeout_seconds": 300},
        ["a", "b"],
    )
    assert base == timing_identity(
        "repo",
        "base",
        {"image_id": "one"},
        {"test_command": "pytest", "timeout_seconds": 10},
        ["a", "b"],
    )
    assert base != timing_identity(
        "repo", "base", {"image_id": "two"}, {"test_command": "pytest"}, ["a", "b"]
    )
    assert base != timing_identity(
        "repo", "base", {"image_id": "one"}, {"test_command": "pytest"}, ["b", "a"]
    )


# Concurrency and process-release tests are authored only; never executed here.
def _hold_workspace_until_exit(path, ready, release):
    import os

    with resource_slot("workspace", workspace=Path(path)):
        ready.set()
        release.wait(10)
        os._exit(0)


def test_process_exit_releases_workspace(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(
        target=_hold_workspace_until_exit, args=(str(tmp_path), ready, release)
    )
    process.start()
    try:
        assert ready.wait(60)
        release.set()
        process.join(60)
        assert process.exitcode == 0
        with (
            dispatch_scope(DispatchScope(Deadline(time.monotonic() + 2))),
            resource_slot("workspace", workspace=tmp_path),
        ):
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)


def test_wait_cancel_and_distinct_workspaces(tmp_path):
    import threading

    held, release, cancelled = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with resource_slot("workspace", workspace=tmp_path):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(2)
        with resource_slot("workspace", workspace=tmp_path / "independent"):
            pass
        threading.Timer(0.02, cancelled.set).start()
        with (
            pytest.raises(ResourceCancelledError),
            resource_slot("workspace", workspace=tmp_path, cancel=cancelled.is_set),
        ):
            pytest.fail("same workspace entered")
    finally:
        release.set()
        thread.join(5)
    with resource_slot("workspace", workspace=tmp_path):
        pass


def test_two_independent_model_slots():
    import concurrent.futures
    import threading

    limiter = ConcurrencyLimiter(2)
    rendezvous = threading.Barrier(2)

    def request():
        with limiter:
            rendezvous.wait(timeout=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)
    assert limiter.available == 2


def test_budget_rejection_is_not_provider_unknown(tmp_path):
    from codepilot.llm.dispatch_budget import DispatchBudget, DispatchBudgetStoppedError
    from codepilot.llm.tracking import UsageTrackingProvider
    from codepilot.llm.types import LLMRequest
    from codepilot.schemas.usage import Usage

    ledger = DispatchBudget(1, tmp_path / "ledger.json")

    def complete(request):
        return ledger.send(SimpleNamespace(price=None), {}, lambda: pytest.fail("HTTP dispatched"))

    usage = Usage()
    provider = UsageTrackingProvider(SimpleNamespace(complete=complete), usage=usage)
    with pytest.raises(DispatchBudgetStoppedError):
        provider.complete(LLMRequest(messages=[]))
    assert usage.llm_calls == 0 and usage.cost_unknown_calls == 0
    assert ledger.records == [] and ledger.total == 0


def test_validation_old_slow_samples_do_not_block_changed_candidate(tmp_path):
    seen = []

    def run_tests(**kwargs):
        seen.append(kwargs["env_spec"].timeout_seconds)
        return ExecutionResult(status=ExecutionStatus.TIMEOUT)

    sandbox = AccountingSandbox(SimpleNamespace(run_tests=run_tests))
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS, image_id="image", cache_key="env"
    )
    spec = EnvSpec(timeout_seconds=300)
    identity = timing_identity(
        "",
        "",
        {**environment.model_dump(mode="json"), "runner_identity": "types.SimpleNamespace:pytest"},
        spec.model_dump(mode="json"),
        ["test.py::test_one"],
    )
    sandbox.timing_facts = [
        {"identity": identity, "seconds": seconds, "complete": True, "status": "pass"}
        for seconds in (100, 110, 120)
    ]
    with dispatch_scope(DispatchScope(Deadline(time.monotonic() + 30), reserve=10)):
        result = sandbox.run_tests(
            workspace=tmp_path, environment=environment, env_spec=spec, tests=["test.py::test_one"]
        )
    assert sandbox.runs == 1
    assert 1 <= seen[0] <= 20
    assert result.status == ExecutionStatus.TIMEOUT
    assert result.diagnostics["time_allocation"]["margin_insufficient"]


def test_validation_margin_above_runner_cap_still_dispatches(tmp_path):
    seen = []

    def run_tests(**kwargs):
        seen.append(kwargs["env_spec"].timeout_seconds)
        return ExecutionResult(status=ExecutionStatus.PASS)

    sandbox = AccountingSandbox(SimpleNamespace(run_tests=run_tests))
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS, image_id="image", cache_key="env"
    )
    spec = EnvSpec(timeout_seconds=300)
    identity = timing_identity(
        "",
        "",
        {**environment.model_dump(mode="json"), "runner_identity": "types.SimpleNamespace:pytest"},
        spec.model_dump(mode="json"),
        ["test.py::test_one"],
    )
    sandbox.timing_facts = [
        {"identity": identity, "seconds": 250, "complete": True, "status": "pass"},
        {"identity": identity, "seconds": 290, "status": "timeout"},
    ]
    result = sandbox.run_tests(
        workspace=tmp_path, environment=environment, env_spec=spec, tests=["test.py::test_one"]
    )
    assert seen == [300]
    assert result.diagnostics["time_allocation"]["margin_insufficient"] is True


def test_extreme_duration_does_not_overflow_or_lower_default():
    estimate = estimate_timeout(
        "id", [{"identity": "id", "seconds": 1.7e308, "status": "timeout"}], 300
    )
    assert estimate["suggested_seconds"] > 300
    assert (
        estimate_timeout(
            "id", [{"identity": "id", "seconds": -1, "complete": True, "status": "pass"}], 300
        )["source"]
        == "conservative_default"
    )


def test_new_module_resolves_previously_missing_import(stores, tmp_path):
    from codepilot.repo.index import Indexer, RepositoryIndex
    from codepilot.repo.workspace import Workspace, normalize_source
    from codepilot.storage.db import create_engine_from_url, create_session_factory, init_db
    from codepilot.storage.repository import Stores
    from support.indexdump import dump_index

    (tmp_path / "a.py").write_text("from missing import target\ndef use(): return target()\n")
    workspace = Workspace(
        path=tmp_path,
        source=normalize_source(str(tmp_path)),
        base_commit="0" * 40,
        repository_id="new-module",
    )
    indexer = Indexer()
    indexer.index(stores, workspace)
    (tmp_path / "missing.py").write_text("def target(): return 1\n")
    indexer.index(stores, workspace)
    engine = create_engine_from_url("sqlite://")
    init_db(engine)
    try:
        with create_session_factory(engine)() as session:
            fresh = Stores(session)
            Indexer().index(fresh, workspace)
            assert dump_index(stores, workspace.repository_id) == dump_index(
                fresh, workspace.repository_id
            )
            assert any(
                edge.target_id
                for edge in RepositoryIndex(stores, workspace.repository_id).edges(kind="IMPORT")
            )
    finally:
        engine.dispose()
