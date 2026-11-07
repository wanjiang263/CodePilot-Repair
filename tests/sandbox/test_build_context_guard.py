"""Deferred SDK builds must reject before reading or uploading a workspace."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from codepilot.sandbox.swebench_env import (
    DockerSdkBackend,
    SWEBenchEnvironmentError,
    SWEBenchEnvironmentKind,
)


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "../Dockerfile", "FROM python:3.12"])
def test_directory_build_never_reaches_sdk(tmp_path: Path, dockerfile: str) -> None:
    (tmp_path / ".env").write_text("FIXTURE_ONLY=must-not-be-uploaded\n", encoding="utf-8")
    client = Mock()
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        DockerSdkBackend(client).build(
            dockerfile=dockerfile, context=tmp_path, tag="fixture", labels={}
        )
    assert caught.value.kind == SWEBenchEnvironmentKind.UNSUPPORTED
    assert client.mock_calls == []


def test_rejection_does_not_connect_to_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = Mock(side_effect=AssertionError("must reject before connecting"))
    monkeypatch.setattr("codepilot.sandbox.swebench_env.docker.from_env", connect)
    with pytest.raises(SWEBenchEnvironmentError):
        DockerSdkBackend().build(
            dockerfile="Dockerfile", context=Path("missing-context"), tag="fixture", labels={}
        )
    connect.assert_not_called()
