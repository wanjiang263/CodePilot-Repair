"""受控真实 Docker smoke；daemon 不可用时由 tests/conftest.py 统一 skip。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.artifacts import ArtifactStore
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus
from support.gitfixture import init_repo


@pytest.mark.docker
def test_real_git_fixture_to_docker_junit_execution_artifact(tmp_path: Path) -> None:
    fixture = init_repo(
        tmp_path / "workspace",
        {
            "test_smoke.py": "def test_real_container():\n    assert 2 + 2 == 4\n",
        },
    )
    artifacts = ArtifactStore(tmp_path / "artifacts", "docker-smoke")
    sandbox = DockerSandbox(
        artifacts=artifacts,
        allowed_workspace_root=tmp_path,
        log_tail_bytes=4096,
    )
    spec = EnvSpec(install=("python -m pip install pytest",), timeout_seconds=120)
    environment = sandbox.build_env(
        repository=str(fixture.path.resolve()),
        base_commit=fixture.head,
        workspace=fixture.path,
        env_spec=spec,
        allow_network=True,
    )
    assert environment.status is ExecutionStatus.PASS, environment.error
    result = sandbox.run_tests(
        workspace=fixture.path,
        environment=environment,
        env_spec=spec,
        tests=("test_smoke.py::test_real_container",),
    )
    assert result.status is ExecutionStatus.PASS, result.model_dump()
    assert result.tests_passed == 1
    assert result.tests_failed == 0
    assert result.diagnostics["execution_backend"] == "docker"
    assert (Path(result.artifact_dir) / "junit.xml").is_file()
    assert (Path(result.artifact_dir) / "stdout.log").is_file()
