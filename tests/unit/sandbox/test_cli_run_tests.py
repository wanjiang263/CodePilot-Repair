from __future__ import annotations

import json
from pathlib import Path

import pytest
from docker.errors import DockerException
from typer.testing import CliRunner

import codepilot.cli as cli
import codepilot.sandbox.docker as docker_module
from codepilot.config import Settings
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.execution import ExecutionResult
from support.gitfixture import init_repo

runner = CliRunner()


class FakeDockerSandbox:
    build_status = ExecutionStatus.PASS
    seen_tests: tuple[str, ...] = ()

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def build_env(self, **kwargs: object) -> EnvironmentBuildResult:
        return EnvironmentBuildResult(
            status=self.build_status,
            image_ref="fake-image" if self.build_status is ExecutionStatus.PASS else None,
            image_id="id" if self.build_status is ExecutionStatus.PASS else None,
            cache_key="cache-key",
            from_cache=True,
            error=None if self.build_status is ExecutionStatus.PASS else "daemon unavailable",
        )

    def run_tests(self, **kwargs: object) -> ExecutionResult:
        type(self).seen_tests = tuple(kwargs["tests"])  # type: ignore[arg-type]
        return ExecutionResult(
            status=ExecutionStatus.PASS,
            exit_code=0,
            duration_seconds=0.5,
            tests_passed=2,
            artifact_dir="artifact-dir",
            diagnostics={"execution_backend": "docker", "container_verified": True},
        )


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, status: ExecutionStatus) -> Path:
    repository = init_repo(
        tmp_path / "repo",
        {
            "pyproject.toml": "[project]\nname='demo'\nversion='0.1'\n",
            "tests/test_one.py": "def test_one(): assert True\n",
            "tests/test_two.py": "def test_two(): assert True\n",
        },
    )
    settings = Settings(
        artifacts_dir=tmp_path / "artifacts",
        cache_dir=tmp_path / "cache",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    FakeDockerSandbox.build_status = status
    FakeDockerSandbox.seen_tests = ()
    monkeypatch.setattr(docker_module, "DockerSandbox", FakeDockerSandbox)
    return repository.path


def test_cli_single_and_multiple_test_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _configure(tmp_path, monkeypatch, status=ExecutionStatus.PASS)
    result = runner.invoke(
        cli.app,
        [
            "run-tests",
            "--repo",
            str(repo),
            "--tests",
            "tests/test_one.py::test_one tests/test_two.py::test_two",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["tests_passed"] == 2
    assert payload["artifact_dir"] == "artifact-dir"
    assert FakeDockerSandbox.seen_tests == (
        "tests/test_one.py::test_one",
        "tests/test_two.py::test_two",
    )


def test_cli_omitted_tests_means_full_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _configure(tmp_path, monkeypatch, status=ExecutionStatus.PASS)
    result = runner.invoke(cli.app, ["run-tests", "--repo", str(repo)])
    assert result.exit_code == 0
    assert FakeDockerSandbox.seen_tests == ()


def test_cli_explicit_empty_selection_is_rejected() -> None:
    result = runner.invoke(cli.app, ["run-tests", "--repo", "repo", "--tests", ""])
    assert result.exit_code == 2
    assert "不能是空字符串" in result.output


def test_cli_docker_unavailable_is_structured_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _configure(tmp_path, monkeypatch, status=ExecutionStatus.SANDBOX_ERROR)
    result = runner.invoke(cli.app, ["run-tests", "--repo", str(repo)])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "SANDBOX_ERROR"
    assert payload["error_reason"] == "DOCKER_UNAVAILABLE"
    assert "daemon unavailable" in payload["diagnostics"]["detail"]


def test_cli_docker_constructor_failure_is_structured_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _configure(tmp_path, monkeypatch, status=ExecutionStatus.PASS)

    class UnavailableDockerSandbox:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            raise DockerException("named pipe unavailable")

    monkeypatch.setattr(docker_module, "DockerSandbox", UnavailableDockerSandbox)
    result = runner.invoke(cli.app, ["run-tests", "--repo", str(repo)])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "SANDBOX_ERROR"
    assert payload["error_reason"] == "DOCKER_UNAVAILABLE"
    assert "named pipe unavailable" in payload["diagnostics"]["detail"]
