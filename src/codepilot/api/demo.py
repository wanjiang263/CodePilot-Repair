"""Server-side demo task: a real M2 graph run with scripted, clearly-fake models.

The Web demo must not invent a second "web agent". It assembles the same
:class:`~codepilot.eval.assembly.InferenceAssembly` used by evaluation, with a
scripted FakeLLM and a deterministic workspace-aware fake sandbox. Everything is
marked simulated in the run report and in the UI.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from codepilot.eval.assembly import ROLES, ProviderBundle
from codepilot.llm.fake import FakeLLM
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.llm.types import LLMRequest
from codepilot.repo.gitcmd import run_git
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec, resolve_env_spec
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.usage import Usage

__all__ = [
    "DEMO_NAME",
    "DEMO_TEST_IDS",
    "DeterministicSandbox",
    "build_demo_providers",
    "demo_env_spec",
    "materialize_demo_repo",
]

DEMO_NAME = "mini_calc"
DEMO_TEST_IDS = ("tests/test_calc.py::test_add", "tests/test_calc.py::test_subtract")
_BUG = "    return left + right - 1"
_FIXED = "    return left + right"
_TARGET_FILE = "src/demo_calc/calc.py"
_TARGET_SYMBOL = "demo_calc.calc.add"
_HEADER = re.compile(r"\]\s*(file|symbol):([A-Za-z0-9_-]+)")

#: Stable commit identity for the materialized demo repository. A timestamped
#: commit would make every checkout a different base commit and break run replay.
_GIT_DATE = "2026-01-01T00:00:00+00:00"


def _content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or "__pycache__" in relative:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def materialize_demo_repo(source: Path, cache_root: Path) -> tuple[Path, str]:
    """Copy a demo template into a cached git repository and return (path, HEAD)."""
    source = Path(source).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"demo repository template is missing: {source}")
    key = f"{source.name}-{_content_digest(source)}"
    target = (Path(cache_root) / "demos" / key).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / ".git").is_dir():
        head = run_git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
        return target, head

    staging = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    _remove(staging)
    shutil.copytree(
        source,
        staging,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    environment = {
        "GIT_AUTHOR_DATE": _GIT_DATE,
        "GIT_COMMITTER_DATE": _GIT_DATE,
    }
    try:
        run_git(["init", "-q"], cwd=staging)
        run_git(["config", "user.email", "demo@codepilot.invalid"], cwd=staging)
        run_git(["config", "user.name", "CodePilot Demo"], cwd=staging)
        run_git(["add", "."], cwd=staging)
        run_git(
            ["-c", "commit.gpgsign=false", "commit", "-qm", "demo base"],
            cwd=staging,
            env=environment,
        )
        head = run_git(["rev-parse", "HEAD"], cwd=staging).stdout.strip()
        if target.exists():
            _remove(staging)
        else:
            staging.replace(target)
    except BaseException:
        _remove(staging)
        raise
    if not (target / ".git").is_dir():
        raise RuntimeError("demo repository materialization failed")
    return target, head


def demo_env_spec(workspace: Path) -> EnvSpec:
    try:
        return resolve_env_spec(workspace)
    except Exception:
        # The fake sandbox ignores installs; keep the run deterministic when a
        # demo template has no package metadata.
        return EnvSpec(install=(), env={"PYTHONPATH": "/workspace/src"})


class DeterministicSandbox:
    """A fake sandbox whose per-test outcome depends on the workspace content.

    It does not execute pytest. It reports the two demo test IDs and marks every
    result as a fake backend so the report and UI cannot mistake this for a real
    container verification.
    """

    def __init__(self) -> None:
        self.build_calls = 0
        self.run_calls: list[tuple[tuple[str, ...], int]] = []

    def build_env(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: EnvSpec,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult:
        del repository, base_commit, workspace, env_spec, allow_network
        self.build_calls += 1
        return EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="fake://demo-environment",
            image_id="fake-demo-image",
            cache_key="fake-demo-cache",
            error=None,
        )

    def run_tests(
        self,
        *,
        workspace: Path,
        environment: EnvironmentBuildResult,
        env_spec: EnvSpec,
        tests: Sequence[str] = (),
        artifact_iteration: int = 1,
    ) -> ExecutionResult:
        del environment, env_spec
        scope = tuple(tests) if tests else DEMO_TEST_IDS
        self.run_calls.append((scope, artifact_iteration))
        try:
            source = (Path(workspace) / _TARGET_FILE).read_text(encoding="utf-8")
        except OSError:
            source = ""
        results = [_demo_case(node, source) for node in scope]
        failed = sum(item.outcome is not TestOutcome.PASSED for item in results)
        return ExecutionResult(
            status=ExecutionStatus.FAIL if failed else ExecutionStatus.PASS,
            exit_code=1 if failed else 0,
            duration_seconds=0.01,
            tests=results,
            tests_passed=len(results) - failed,
            tests_failed=failed,
            artifact_dir=f"attempts/{artifact_iteration:02d}",
            diagnostics={
                "execution_backend": "deterministic_fake",
                "container_verified": False,
                "note": "Workspace-aware fake result; pytest was not executed.",
            },
        )

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:
        del older_than_seconds
        return []


def _demo_case(node_id: str, source: str) -> TestCaseResult:
    if node_id.endswith("test_add") and _BUG in source:
        return TestCaseResult(
            node_id=node_id,
            outcome=TestOutcome.FAILED,
            duration=0.01,
            failure_type="AssertionError",
            message="assert 4 == 5",
            traceback=(
                f"{_TARGET_FILE}:6: AssertionError: assert add(2, 3) == 5\nE       assert 4 == 5"
            ),
        )
    return TestCaseResult(node_id=node_id, outcome=TestOutcome.PASSED, duration=0.01)


class _DemoScript:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.llm = FakeLLM(default=self._reply, model_alias="fake-demo")

    def _reply(self, request: LLMRequest) -> Any:
        self.calls[request.purpose] += 1
        attempt = self.calls[request.purpose]
        prompt = "\n".join(message.content or "" for message in request.messages)
        if request.purpose == "localizer_files":
            candidate = _find_part(prompt, kind="file", marker='"path": "src/demo_calc/calc.py"')
            return {
                "files": [{"candidate_id": candidate, "reason": "issue names the add function"}]
            }
        if request.purpose == "localizer_symbols":
            candidate, start, end = _find_symbol(prompt)
            return {
                "symbols": [
                    {
                        "candidate_id": candidate,
                        "start_line": start,
                        "end_line": end,
                        "reason": "the buggy arithmetic lives here",
                    }
                ]
            }
        if request.purpose == "planner":
            if attempt == 1:
                return {"tool": "read_file", "arguments": {"path": _TARGET_FILE}}
            return {
                "root_cause_hypothesis": "add() subtracts one from the sum",
                "evidence": ["calc.py returns left + right - 1"],
                "changes": [
                    {
                        "file": _TARGET_FILE,
                        "symbol": _TARGET_SYMBOL,
                        "action": "modify",
                        "description": "return the exact sum",
                    }
                ],
                "tests_to_run": ["tests/test_calc.py::test_add"],
                "planned_new_tests": [],
                "risk": "low",
                "alternatives": ["guard the subtraction with a conditional"],
            }
        if request.purpose == "coder":
            if attempt == 1:
                return {
                    "tool": "edit_file",
                    "arguments": {
                        "path": _TARGET_FILE,
                        "search": _BUG,
                        "replace": _FIXED,
                    },
                }
            return {"tool": "finish", "arguments": {"summary": "add() now returns the exact sum"}}
        if request.purpose == "reviewer":
            return {
                "approved": True,
                "risk": "low",
                "issues": [],
                "summary": "The change is minimal and the target test covers the regression.",
            }
        raise AssertionError(f"demo scripted provider has no reply for purpose={request.purpose!r}")


def _find_part(prompt: str, *, kind: str, marker: str) -> str:
    fallback = ""
    for block in prompt.split("[/]"):
        matched = _HEADER.search(block)
        if matched is None or matched.group(1) != kind:
            continue
        if not fallback:
            fallback = matched.group(2)
        if marker in block:
            return matched.group(2)
    if fallback:
        return fallback
    raise AssertionError(f"no {kind} candidate in localizer prompt")


def _find_symbol(prompt: str) -> tuple[str, int, int]:
    fallback: tuple[str, int, int] | None = None
    for block in prompt.split("[/]"):
        matched = _HEADER.search(block)
        if matched is None or matched.group(1) != "symbol":
            continue
        start_match = re.search(r'"start_line":\s*(\d+)', block)
        end_match = re.search(r'"end_line":\s*(\d+)', block)
        if start_match is None or end_match is None:
            continue
        candidate = (matched.group(2), int(start_match.group(1)), int(end_match.group(1)))
        if fallback is None:
            fallback = candidate
        if f'"qualified_name": "{_TARGET_SYMBOL}"' in block:
            return candidate
    if fallback is not None:
        return fallback
    raise AssertionError("no symbol candidate in localizer prompt")


def build_demo_providers() -> tuple[ProviderBundle, FakeLLM]:
    script = _DemoScript()
    usage = Usage()
    provider = UsageTrackingProvider(script.llm, usage=usage)
    bundle = ProviderBundle(
        roles=dict.fromkeys(ROLES, provider),
        usage=usage,
        simulated=True,
        model_identity="FakeLLM/demo-mini_calc",
    )
    return bundle, script.llm
