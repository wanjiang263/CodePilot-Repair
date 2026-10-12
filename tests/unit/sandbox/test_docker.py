from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from docker.errors import DockerException, ImageNotFound

from codepilot.artifacts import ArtifactStore
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.docker import (
    DockerSandbox,
    _workspace_archive,
    environment_cache_key,
    execution_container_config,
)
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus


@dataclass
class FakeImage:
    id: str
    labels: dict[str, str]

    @property
    def attrs(self) -> dict[str, object]:
        return {"Config": {"Labels": self.labels}}


class FakeImages:
    def __init__(self) -> None:
        self.items: dict[str, FakeImage] = {}
        self.build_calls: list[dict[str, object]] = []

    def get(self, reference: str) -> FakeImage:
        try:
            return self.items[reference]
        except KeyError as exc:
            raise ImageNotFound(reference) from exc

    def build(self, **kwargs: object) -> tuple[FakeImage, list[object]]:
        self.build_calls.append(kwargs)
        image = FakeImage("base-id", {"codepilot": "1"})
        self.items[str(kwargs["tag"])] = image
        return image, []


DEFAULT_JUNIT = (
    '<testsuite tests="1"><testcase classname="tests.test_x" name="test_x" time="0.1"/></testsuite>'
)


@dataclass
class FakeContainer:
    owner: FakeClient
    create_kwargs: dict[str, object]
    id: str = "container-id"
    labels: dict[str, str] = field(default_factory=dict)
    attrs: dict[str, object] = field(default_factory=lambda: {"State": {"OOMKilled": False}})
    removed: bool = False
    killed: bool = False

    def start(self) -> None:
        volumes = self.create_kwargs.get("volumes")
        if isinstance(volumes, dict) and "/out" in [item.get("bind") for item in volumes.values()]:
            output = next(Path(path) for path, item in volumes.items() if item["bind"] == "/out")
            (output / "stdout.log").write_bytes("hello🙂".encode())
            (output / "stderr.log").write_text("", encoding="utf-8")
            (output / "junit.xml").write_text(
                self.owner.junit_xml or DEFAULT_JUNIT, encoding="utf-8"
            )

    def put_archive(self, path: str, data: bytes) -> bool:
        return path == "/" and bool(data)

    def exec_run(self, *args: object, **kwargs: object) -> tuple[int, tuple[bytes, bytes]]:
        self.owner.install_runs += 1
        return self.owner.install_exit_code, (b"installed", b"install error")

    def commit(self, *, repository: str, tag: str, changes: list[str]) -> FakeImage:
        labels: dict[str, str] = {}
        for change in changes:
            _, item = change.split(" ", 1)
            key, value = item.split("=", 1)
            labels[key] = value.strip('"')
        image = FakeImage("env-id", labels)
        self.owner.images.items[f"{repository}:{tag}"] = image
        return image

    def wait(self, *, timeout: int) -> dict[str, int]:
        assert timeout > 0
        if self.owner.timeout:
            raise TimeoutError("deadline")
        return {"StatusCode": self.owner.exit_code}

    def kill(self) -> None:
        self.killed = True

    def reload(self) -> None:
        self.attrs = {"State": {"OOMKilled": self.owner.oom}}

    def remove(self, *, force: bool) -> None:
        assert force is True
        if self.owner.remove_error:
            raise DockerException("remove failed")
        self.removed = True


class FakeContainers:
    def __init__(self, owner: FakeClient) -> None:
        self.owner = owner
        self.created: list[FakeContainer] = []
        self.listed: list[FakeContainer] = []

    def create(self, **kwargs: object) -> FakeContainer:
        container = FakeContainer(self.owner, kwargs, labels=dict(kwargs.get("labels", {})))
        self.created.append(container)
        return container

    def list(self, **kwargs: object) -> list[FakeContainer]:
        self.owner.list_calls.append(kwargs)
        return self.listed


class FakeClient:
    def __init__(self) -> None:
        self.images = FakeImages()
        self.containers = FakeContainers(self)
        self.install_runs = 0
        self.install_exit_code = 0
        self.list_calls: list[dict[str, object]] = []
        self.exit_code = 0
        self.timeout = False
        self.oom = False
        self.remove_error = False
        self.junit_xml: str | None = None


def sandbox(tmp_path: Path, client: FakeClient, *, clock: Any = lambda: 10000.0) -> DockerSandbox:
    artifacts = ArtifactStore(tmp_path / "artifacts", "run")
    return DockerSandbox(
        artifacts=artifacts,
        allowed_workspace_root=tmp_path,
        client=client,
        base_dockerfile=tmp_path / "Dockerfile.base",
        log_tail_bytes=8,
        clock=clock,
    )


def test_execution_container_config_matches_security_contract(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    output = tmp_path / "out"
    workspace.mkdir()
    output.mkdir()
    config = execution_container_config(
        image_ref="image",
        workspace=workspace,
        output_dir=output,
        command="pytest",
        environment={},
        created_at=1.0,
    )
    expected = {
        "network_mode": "none",
        "user": "1000:1000",
        "read_only": True,
        "tmpfs": {"/tmp": "size=512m"},
        "mem_limit": "4g",
        "memswap_limit": "4g",
        "nano_cpus": 2_000_000_000,
        "pids_limit": 512,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "privileged": False,
    }
    for key, value in expected.items():
        assert config[key] == value
    assert {value["bind"] for value in config["volumes"].values()} == {"/workspace", "/out"}
    assert all("docker.sock" not in key.lower() for key in config["volumes"])


def test_cache_key_changes_with_commit_repo_or_envspec() -> None:
    base = environment_cache_key("repo", "a", EnvSpec())
    assert base == environment_cache_key("repo", "a", EnvSpec())
    assert base != environment_cache_key("other", "a", EnvSpec())
    assert base != environment_cache_key("repo", "b", EnvSpec())
    assert base != environment_cache_key("repo", "a", EnvSpec(timeout_seconds=1))


def test_build_archive_excludes_credentials_and_git_remote_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / "config").write_text(
        "url = https://token@example.invalid/repo.git\n", encoding="utf-8"
    )
    (workspace / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (workspace / "pyproject.toml").write_text("[project]\nname='safe'\n", encoding="utf-8")
    with tarfile.open(fileobj=io.BytesIO(_workspace_archive(workspace)), mode="r") as archive:
        names = set(archive.getnames())
    assert "workspace/pyproject.toml" in names
    assert "workspace/.git/config" not in names
    assert "workspace/.env" not in names


def test_cache_hit_skips_install_and_second_run_hits_cache(tmp_path: Path) -> None:
    client = FakeClient()
    (tmp_path / "Dockerfile.base").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = EnvSpec(install=("python -m pip install -e .",))
    engine = sandbox(tmp_path, client)
    first = engine.build_env(
        repository="repo",
        base_commit="base",
        workspace=workspace,
        env_spec=spec,
        allow_network=True,
    )
    second = engine.build_env(
        repository="repo",
        base_commit="base",
        workspace=workspace,
        env_spec=spec,
        allow_network=False,
    )
    assert first.succeeded and not first.from_cache
    assert second.succeeded and second.from_cache
    assert client.install_runs == 1
    assert client.containers.created[0].create_kwargs["network_mode"] == "bridge"


def test_cache_miss_without_explicit_network_permission_does_not_install(tmp_path: Path) -> None:
    client = FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).build_env(
        repository="repo",
        base_commit="base",
        workspace=workspace,
        env_spec=EnvSpec(),
    )
    assert result.status is ExecutionStatus.ENV_ERROR
    assert client.install_runs == 0


def test_corrupted_cache_label_is_rebuilt(tmp_path: Path) -> None:
    client = FakeClient()
    (tmp_path / "Dockerfile.base").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = EnvSpec()
    key = environment_cache_key("repo", "base", spec)
    client.images.items[f"codepilot-env:{key}"] = FakeImage("bad", {"codepilot": "1"})
    result = sandbox(tmp_path, client).build_env(
        repository="repo",
        base_commit="base",
        workspace=workspace,
        env_spec=spec,
        allow_network=True,
    )
    assert result.succeeded and not result.from_cache
    assert client.install_runs == 1


def test_failed_install_is_not_committed_as_valid_cache(tmp_path: Path) -> None:
    client = FakeClient()
    client.install_exit_code = 1
    (tmp_path / "Dockerfile.base").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = EnvSpec(install=("false",))
    key = environment_cache_key("repo", "base", spec)
    result = sandbox(tmp_path, client).build_env(
        repository="repo",
        base_commit="base",
        workspace=workspace,
        env_spec=spec,
        allow_network=True,
    )
    assert result.status is ExecutionStatus.ENV_ERROR
    assert f"codepilot-env:{key}" not in client.images.items


def test_test_execution_is_offline_parses_junit_and_removes_container(tmp_path: Path) -> None:
    client = FakeClient()
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_x.py").write_text("", encoding="utf-8")
    engine = sandbox(tmp_path, client)
    environment = EnvironmentBuildResult(
        status=ExecutionStatus.PASS,
        image_ref="env",
        image_id="id",
        cache_key="key",
    )
    result = engine.run_tests(
        workspace=workspace,
        environment=environment,
        env_spec=EnvSpec(),
        tests=("tests/test_x.py::test_x",),
    )
    container = client.containers.created[0]
    assert result.status is ExecutionStatus.PASS
    assert result.tests_passed == 1 and result.tests_failed == 0
    assert result.stdout_tail.endswith("🙂")
    assert container.create_kwargs["network_mode"] == "none"
    assert container.removed
    volumes = container.create_kwargs["volumes"]
    assert isinstance(volumes, dict)
    staging = next(Path(path) for path, mount in volumes.items() if mount["bind"] == "/out")
    assert not staging.is_relative_to(engine.artifacts.run_dir)
    assert (Path(result.artifact_dir) / "junit.xml").is_file()


def test_junit_artifact_is_redacted_before_entering_the_run_record(tmp_path: Path) -> None:
    client = FakeClient()
    client.junit_xml = (
        '<testsuite tests="1"><testcase classname="tests.test_x" name="test_x">'
        '<failure message="api_key=sk-leakedsecret123">password=verysecretvalue</failure>'
        "</testcase></testsuite>"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = sandbox(tmp_path, client)
    result = engine.run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="env",
            image_id="id",
            cache_key="key",
        ),
        env_spec=EnvSpec(),
    )
    persisted = (Path(result.artifact_dir) / "junit.xml").read_text(encoding="utf-8")
    assert "sk-leakedsecret123" not in persisted
    assert "verysecretvalue" not in persisted
    assert "<testsuite" in persisted


def test_timeout_kills_and_force_removes_with_diagnostics(tmp_path: Path) -> None:
    client = FakeClient()
    client.timeout = True
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = sandbox(tmp_path, client)
    result = engine.run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(timeout_seconds=1),
    )
    container = client.containers.created[0]
    assert result.status is ExecutionStatus.TIMEOUT
    assert container.killed and container.removed


def test_timeout_keeps_timeout_status_when_force_remove_reports_failure(tmp_path: Path) -> None:
    client = FakeClient()
    client.timeout = True
    client.remove_error = True
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(timeout_seconds=1),
    )
    assert result.status is ExecutionStatus.TIMEOUT
    assert "remove failed" in str(result.diagnostics["cleanup_error"])


def test_cleanup_failure_on_normal_run_prevents_false_pass(tmp_path: Path) -> None:
    client = FakeClient()
    client.remove_error = True
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(),
    )
    assert result.status is ExecutionStatus.SANDBOX_ERROR
    assert "remove failed" in str(result.diagnostics["detail"])


def test_artifact_write_failure_has_no_fabricated_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = sandbox(tmp_path, client)

    def fail(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(engine.artifacts, "write_text", fail)
    result = engine.run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(),
    )
    assert result.status is ExecutionStatus.SANDBOX_ERROR
    assert result.error_reason == "ARTIFACT_WRITE_FAILED"
    assert result.artifact_dir == ""


def test_oom_is_distinct_reason_without_seventh_status(tmp_path: Path) -> None:
    client = FakeClient()
    client.oom = True
    client.exit_code = 137
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(),
    )
    assert result.status is ExecutionStatus.SANDBOX_ERROR
    assert result.error_reason == "OOM_KILLED"
    assert result.diagnostics["oom_killed"] is True


def test_cleanup_only_removes_old_owned_containers(tmp_path: Path) -> None:
    client = FakeClient()
    engine = sandbox(tmp_path, client)
    old_owned = FakeContainer(
        client, {}, id="old", labels={"codepilot": "1", "codepilot.created_at": "1"}
    )
    new_owned = FakeContainer(
        client, {}, id="new", labels={"codepilot": "1", "codepilot.created_at": "9999"}
    )
    foreign = FakeContainer(
        client, {}, id="foreign", labels={"other": "1", "codepilot.created_at": "1"}
    )
    client.containers.listed = [old_owned, new_owned, foreign]
    assert engine.cleanup(older_than_seconds=3600) == ["old"]
    assert old_owned.removed
    assert not new_owned.removed and not foreign.removed
    assert client.list_calls[-1]["filters"] == {"label": "codepilot=1"}


def test_cleanup_failure_is_reported(tmp_path: Path) -> None:
    client = FakeClient()
    client.remove_error = True
    engine = sandbox(tmp_path, client)
    client.containers.listed = [
        FakeContainer(
            client,
            {},
            id="stale",
            labels={"codepilot": "1", "codepilot.created_at": "1"},
        )
    ]
    with pytest.raises(RuntimeError, match="stale"):
        engine.cleanup(older_than_seconds=3600)
