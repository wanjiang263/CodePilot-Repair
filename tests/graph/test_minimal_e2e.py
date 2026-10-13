from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codepilot.agents.coder import CodingAgent
from codepilot.agents.impact_analyzer import ImpactAnalyzer
from codepilot.agents.issue_analyzer import IssueAnalyzer
from codepilot.agents.localizer import Localizer
from codepilot.agents.planner import Planner
from codepilot.artifacts import ArtifactStore, RetrievalSnapshotStore
from codepilot.graph.minimal import MinimalDependencies, run_minimal_graph
from codepilot.llm.fake import FakeLLM
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.patch.validator import PatchValidator
from codepilot.repo.graph import RepoGraph
from codepilot.repo.index import Indexer, RepositoryIndex
from codepilot.repo.workspace import Workspace, normalize_source
from codepilot.retrieval.service import retrieve_symbols
from codepilot.sandbox.base import Sandbox
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus, RunStatus
from codepilot.schemas.retrieval import FusionMethod
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage
from codepilot.storage.repository import Stores
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.registry import build_tool_registry
from codepilot.tools.write_tools import TestRunBudget as _TestRunBudget


def _base_repo(tmp_path: Path) -> tuple[Path, str]:
    from codepilot.repo.gitcmd import run_git

    original = tmp_path / "original"
    (original / "src/pkg").mkdir(parents=True)
    (original / "tests").mkdir()
    (original / "src/pkg/core.py").write_text(
        "def normalize(value: int) -> int:\n    return value - 1\n", encoding="utf-8"
    )
    (original / "tests/test_core.py").write_text(
        "from pkg.core import normalize\n\ndef test_normalize():\n    assert normalize(3) == 3\n",
        encoding="utf-8",
    )
    (original / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0.0.1'\n", encoding="utf-8"
    )
    run_git(["init", "-q"], cwd=original)
    run_git(["config", "user.email", "tests@example.invalid"], cwd=original)
    run_git(["config", "user.name", "Tests"], cwd=original)
    run_git(["add", "."], cwd=original)
    run_git(["commit", "-qm", "base"], cwd=original)
    return original, run_git(["rev-parse", "HEAD"], cwd=original).stdout.strip()


def _run(
    stores: Stores,
    tmp_path: Path,
    *,
    sandbox_status: ExecutionStatus = ExecutionStatus.PASS,
    sandbox_build_status: ExecutionStatus = ExecutionStatus.PASS,
    coder_replies: list[object] | None = None,
    max_tool_calls: int = 30,
    real_docker: bool = False,
) -> tuple[RepairState, Path, Path]:
    original, base = _base_repo(tmp_path)
    workspace_root = tmp_path / "workspace"
    shutil.copytree(original, workspace_root)
    workspace = Workspace(
        path=workspace_root,
        source=normalize_source(str(original)),
        base_commit=base,
        repository_id="phase5_e2e_repo",
    )
    Indexer().index(stores, workspace)
    index = RepositoryIndex(stores, workspace.repository_id)
    graph = RepoGraph(index)
    symbols = index.symbol_objects()
    target = next(item for item in symbols if item.qualified_name == "pkg.core.normalize")
    indexed_file = next(item for item in index.files() if item.path == "src/pkg/core.py")
    replies: list[object] = [
        {"files": [{"candidate_id": indexed_file.id, "reason": "BM25 top file"}]},
        {
            "symbols": [
                {
                    "candidate_id": target.id,
                    "start_line": target.start_line,
                    "end_line": target.end_line,
                    "reason": "exact normalize symbol",
                }
            ]
        },
        {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}},
        {
            "root_cause_hypothesis": "normalize subtracts one unexpectedly",
            "evidence": ["src/pkg/core.py returns value - 1"],
            "changes": [
                {
                    "file": "src/pkg/core.py",
                    "symbol": "pkg.core.normalize",
                    "action": "modify",
                    "description": "return the input without changing the signature",
                }
            ],
            "tests_to_run": ["tests/test_core.py"],
            "risk": "low",
            "alternatives": ["adjust only the arithmetic expression"],
        },
    ]
    replies.extend(
        coder_replies
        or [
            {
                "tool": "edit_file",
                "arguments": {
                    "path": "src/pkg/core.py",
                    "search": "    return value - 1",
                    "replace": "    return value",
                },
            },
            {"tool": "finish", "arguments": {"summary": "patched normalize"}},
        ]
    )
    raw_llm = FakeLLM(replies)
    usage = Usage()
    llm = UsageTrackingProvider(raw_llm, usage=usage)
    artifacts = ArtifactStore(tmp_path / "artifacts", "run-001")
    snapshots = RetrievalSnapshotStore(artifacts)
    sandbox: Sandbox = (
        DockerSandbox(artifacts=artifacts, allowed_workspace_root=tmp_path)
        if real_docker
        else FakeSandbox([scripted_result(sandbox_status)], build_status=sandbox_build_status)
    )
    env_spec = EnvSpec(
        install=("python -m pip install pytest",),
        env={"PYTHONPATH": "/workspace/src"},
    )
    environment = sandbox.build_env(
        repository=str(original),
        base_commit=base,
        workspace=workspace_root,
        env_spec=env_spec,
        allow_network=real_docker,
    )
    policy = WorkspacePathPolicy(workspace_root)
    test_budget = _TestRunBudget(12)
    registry = build_tool_registry(
        policy=policy,
        index=index,
        graph=graph,
        base_commit=base,
        sandbox=sandbox,
        environment=environment,
        env_spec=env_spec,
        test_budget=test_budget,
    )
    planner = Planner(llm, registry, policy, index)
    coder = CodingAgent(llm, registry, max_tool_calls=max_tool_calls)
    localizer = Localizer(llm, index, workspace_root, snapshots)

    def retrieve(issue: str):
        return retrieve_symbols(
            workspace_root,
            symbols,
            issue,
            channel="bm25",
            fusion_method=FusionMethod.WEIGHTED,
            channel_weights={"bm25": 1.0},
            graph=graph,
        )

    deps = MinimalDependencies(
        repository=str(original),
        workspace=workspace_root,
        index=index,
        graph=graph,
        issue_analyzer=IssueAnalyzer(),
        retriever=retrieve,
        localizer=localizer,
        impact_analyzer=ImpactAnalyzer(index, graph),
        planner=planner,
        coder=coder,
        validator=PatchValidator(policy, base, index=index),
        sandbox=sandbox,
        environment=environment,
        env_spec=env_spec,
        artifacts=artifacts,
        usage=usage,
        simulated_llm=True,
    )
    state = RepairState(
        task_id="fixture/normalize",
        run_id="run-001",
        repository_id=workspace.repository_id,
        base_commit=base,
        issue="normalize returns one too small; normalize(3) should return 3",
        started_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    return run_minimal_graph(state, deps), original, workspace_root


def test_fake_llm_fake_sandbox_runs_real_index_retrieval_and_patch(
    stores: Stores, tmp_path: Path
) -> None:
    state, original, workspace = _run(stores, tmp_path)
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json(indent=2)
    assert (
        (workspace / "src/pkg/core.py").read_text(encoding="utf-8").endswith("    return value\n")
    )
    assert "return value - 1" in (original / "src/pkg/core.py").read_text(encoding="utf-8")
    from codepilot.repo.gitcmd import run_git

    assert run_git(["status", "--porcelain"], cwd=original).stdout == ""
    assert state.current_attempt is not None and state.current_attempt.validation.passed
    assert state.execution is not None
    assert state.execution.diagnostics["execution_backend"] == "fake"
    assert state.usage.llm_calls == 6
    assert state.report_artifacts
    report = (tmp_path / "artifacts/runs/run-001/report.json").read_text(encoding="utf-8")
    assert '"simulated_llm": true' in report
    assert "return value" in report


def test_minimal_graph_records_tests_failed(stores: Stores, tmp_path: Path) -> None:
    state, _, _ = _run(stores, tmp_path, sandbox_status=ExecutionStatus.FAIL)
    assert state.status is RunStatus.TESTS_FAILED
    assert state.execution is not None and state.execution.status is ExecutionStatus.FAIL


def test_minimal_graph_records_patch_rejection(stores: Stores, tmp_path: Path) -> None:
    state, _, workspace = _run(
        stores,
        tmp_path,
        coder_replies=[
            {
                "tool": "edit_file",
                "arguments": {
                    "path": "tests/test_core.py",
                    "search": "assert normalize(3) == 3",
                    "replace": "assert True",
                },
            },
            {"tool": "finish", "arguments": {"summary": "weakened test"}},
        ],
    )
    assert state.status is RunStatus.PATCH_REJECTED
    assert state.execution is None
    assert "assert True" in (workspace / "tests/test_core.py").read_text(encoding="utf-8")


def test_minimal_graph_records_tool_failure(stores: Stores, tmp_path: Path) -> None:
    state, _, _ = _run(
        stores,
        tmp_path,
        coder_replies=[
            {
                "tool": "edit_file",
                "arguments": {
                    "path": "src/pkg/core.py",
                    "search": "missing block",
                    "replace": "x",
                },
            },
            {"tool": "finish", "arguments": {"summary": "could not edit"}},
        ],
    )
    assert state.status is RunStatus.TOOL_ERROR


def test_minimal_graph_records_environment_error(stores: Stores, tmp_path: Path) -> None:
    state, _, _ = _run(stores, tmp_path, sandbox_status=ExecutionStatus.ENV_ERROR)
    assert state.status is RunStatus.SANDBOX_ERROR


def test_minimal_graph_stops_before_analysis_when_environment_build_fails(
    stores: Stores, tmp_path: Path
) -> None:
    state, _, _ = _run(stores, tmp_path, sandbox_build_status=ExecutionStatus.ENV_ERROR)
    assert state.status is RunStatus.SANDBOX_ERROR
    assert state.issue_analysis is None
    assert state.execution is None


def test_minimal_graph_records_coding_budget_exhaustion(stores: Stores, tmp_path: Path) -> None:
    state, _, _ = _run(stores, tmp_path, max_tool_calls=1)
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert len(state.tool_calls) == 1
    assert state.execution is None


def test_fake_pipeline_is_deterministic_excluding_run_specific_evidence(
    stores: Stores, tmp_path: Path
) -> None:
    first, _, _ = _run(stores, tmp_path / "first")
    second, _, _ = _run(stores, tmp_path / "second")
    assert first.status == second.status
    assert first.current_patch == second.current_patch
    assert first.plan == second.plan
    assert first.impact == second.impact
    assert first.current_attempt is not None and second.current_attempt is not None
    assert first.current_attempt.validation == second.current_attempt.validation
    assert [(item["name"], item["ok"]) for item in first.tool_calls] == [
        (item["name"], item["ok"]) for item in second.tool_calls
    ]


def test_solve_cli_runs_the_real_graph_with_explicit_fake_modes(
    stores: Stores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from typer.testing import CliRunner

    from codepilot.cli import app
    from codepilot.config import reset_settings_cache
    from codepilot.repo.gitcmd import run_git
    from codepilot.repo.workspace import WorkspaceManager

    original, base = _base_repo(tmp_path)
    manager = WorkspaceManager(cache_root=tmp_path / "probe-cache")
    probe = manager.create(source=str(original), base_commit=base, target=tmp_path / "probe")
    Indexer().index(stores, probe)
    probe_index = RepositoryIndex(stores, probe.repository_id)
    file_id = next(item.id for item in probe_index.files() if item.path == "src/pkg/core.py")
    symbol = next(
        item for item in probe_index.symbol_objects() if item.qualified_name == "pkg.core.normalize"
    )
    manager.remove(probe)
    script = tmp_path / "fake-llm.json"
    script.write_text(
        json.dumps(
            [
                {"files": [{"candidate_id": file_id, "reason": "top file"}]},
                {
                    "symbols": [
                        {
                            "candidate_id": symbol.id,
                            "start_line": symbol.start_line,
                            "end_line": symbol.end_line,
                            "reason": "target symbol",
                        }
                    ]
                },
                {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}},
                {
                    "root_cause_hypothesis": "off by one",
                    "evidence": ["return value - 1"],
                    "changes": [
                        {
                            "file": "src/pkg/core.py",
                            "symbol": "pkg.core.normalize",
                            "action": "modify",
                            "description": "return input",
                        }
                    ],
                    "tests_to_run": ["tests/test_core.py"],
                    "risk": "low",
                    "alternatives": ["change arithmetic only"],
                },
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": "    return value - 1",
                        "replace": "    return value",
                    },
                },
                {"tool": "finish", "arguments": {"summary": "done"}},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEPILOT_ARTIFACTS_DIR", str(tmp_path / "cli-artifacts"))
    monkeypatch.setenv("CODEPILOT_CACHE_DIR", str(tmp_path / "cli-cache"))
    reset_settings_cache()
    try:
        result = CliRunner().invoke(
            app,
            [
                "solve",
                "--repo",
                str(original),
                "--issue",
                "normalize(3) should return 3",
                "--base-commit",
                base,
                "--fake-llm-script",
                str(script),
                "--fake-sandbox",
            ],
        )
    finally:
        reset_settings_cache()
    assert result.exit_code == 0, result.stdout
    assert '"status": "provisionally_verified"' in result.stdout
    assert '"simulated_llm": true' in result.stdout
    assert run_git(["status", "--porcelain"], cwd=original).stdout == ""


@pytest.mark.docker
def test_fake_llm_with_real_docker_executes_controlled_fixture(
    stores: Stores, tmp_path: Path
) -> None:
    from codepilot.repo.gitcmd import run_git

    state, original, _ = _run(stores, tmp_path, real_docker=True)
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json(indent=2)
    assert state.execution is not None
    assert state.execution.diagnostics["execution_backend"] == "docker"
    assert state.execution.diagnostics["container_verified"] is True
    assert run_git(["status", "--porcelain"], cwd=original).stdout == ""
