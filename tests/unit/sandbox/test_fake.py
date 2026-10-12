from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.sandbox.base import Sandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox
from codepilot.schemas.enums import ExecutionStatus


@pytest.mark.parametrize("status", list(ExecutionStatus))
def test_fake_sandbox_scripts_all_six_statuses(status: ExecutionStatus, tmp_path: Path) -> None:
    sandbox = FakeSandbox.from_statuses([status])
    assert isinstance(sandbox, Sandbox)
    environment = sandbox.build_env(
        repository="repo",
        base_commit="a" * 40,
        workspace=tmp_path,
        env_spec=EnvSpec(),
    )
    result = sandbox.run_tests(
        workspace=tmp_path,
        environment=environment,
        env_spec=EnvSpec(),
        tests=("tests/test_x.py::test_x",),
    )
    assert result.status is status
    assert result.diagnostics == {"execution_backend": "fake", "container_verified": False}
    assert result.error_reason == "SCRIPTED_FAKE_RESULT"


def test_fake_sandbox_is_deterministic_and_exhaustion_is_loud(tmp_path: Path) -> None:
    sandbox = FakeSandbox.from_statuses([ExecutionStatus.PASS])
    environment = sandbox.build_env(
        repository="repo", base_commit="base", workspace=tmp_path, env_spec=EnvSpec()
    )
    kwargs = {
        "workspace": tmp_path,
        "environment": environment,
        "env_spec": EnvSpec(),
        "artifact_iteration": 7,
    }
    assert sandbox.run_tests(**kwargs).status is ExecutionStatus.PASS
    assert sandbox.run_calls == [(str(tmp_path), (), 7)]
    with pytest.raises(RuntimeError, match="耗尽"):
        sandbox.run_tests(**kwargs)
