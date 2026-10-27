from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import urllib3.exceptions
from docker.errors import APIError, DockerException, ImageNotFound
from requests.exceptions import ConnectionError as RequestsConnectionError

from codepilot.artifacts import ArtifactStore
from codepilot.observability.redaction import REDACTED
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


def _returned_tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        root = tarfile.TarInfo("out")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, payload in files.items():
            info = tarfile.TarInfo(f"out/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class FakeApi:
    """daemon 侧的卷与归档接口。卷内容按卷名存，不碰宿主路径。"""

    def __init__(self, owner: FakeClient) -> None:
        self.owner = owner

    def create_volume(self, *, name: str, labels: dict[str, str] | None = None) -> None:
        if name in self.owner.volume_files:
            raise RuntimeError(f"volume {name} 已存在")
        self.owner.volume_files[name] = {}
        self.owner.volume_labels[name] = dict(labels or {})
        self.owner.volume_created_at[name] = self.owner.volume_clock()
        self.owner.volumes_created.append(name)

    def remove_volume(self, name: str, *, force: bool = False) -> None:
        assert force is True
        if self.owner.remove_volume_error:
            raise DockerException(f"volume {name} 删除失败")
        self.owner.volume_files.pop(name, None)
        self.owner.volume_labels.pop(name, None)
        self.owner.volumes_removed.append(name)

    def volumes(self, filters: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "Volumes": [
                {
                    "Name": name,
                    "Labels": self.owner.volume_labels.get(name, {}),
                    "CreatedAt": self.owner.volume_created_at.get(name, ""),
                }
                for name in sorted(self.owner.volume_files)
            ]
        }

    def get_archive(
        self, container: str, path: str, **kwargs: object
    ) -> tuple[Any, dict[str, int]]:
        assert path == "/out", path
        target = self.owner.test_container
        assert target is not None, "没有测试容器就没有可回传的输出卷"
        files = target.output_files
        payload = _returned_tar(files)
        return (
            (payload[offset : offset + 4096] for offset in range(0, len(payload), 4096)),
            {"size": len(payload)},
        )


class FakeContainer:
    def __init__(
        self, owner: FakeClient, create_kwargs: dict[str, object], **extra: object
    ) -> None:
        self.owner = owner
        self.create_kwargs = create_kwargs
        self.id = str(extra.get("id") or "container-id")
        self.labels = dict(extra.get("labels") or {})
        self.attrs: dict[str, object] = {"State": {"OOMKilled": owner.oom}}
        self.removed = False
        self.killed = False
        self.started = False
        #: 卷名 → 卷内容；从 create_kwargs["volumes"] 解析，与 daemon 的挂载表同形。
        self.mounts: dict[str, str] = {}
        volumes = create_kwargs.get("volumes")
        if isinstance(volumes, dict):
            for name, spec in volumes.items():
                bind = str(spec.get("bind") if isinstance(spec, dict) else spec)
                self.mounts[bind] = str(name)
        self.output_files: dict[str, bytes] = {}

    @property
    def is_test_container(self) -> bool:
        return bool(self.create_kwargs.get("read_only"))

    @property
    def is_staging_container(self) -> bool:
        return self.labels.get("codepilot.kind") == "execution-stage"

    def start(self) -> None:
        self.started = True
        if not self.is_test_container:
            return
        # 真实容器里这一步是 pytest 自己写 /out；这里按被测结果直接产出卷内容。
        self.output_files = {"stdout.log": self.owner.stdout, "stderr.log": b""}
        if self.owner.emit_junit:
            self.output_files["junit.xml"] = (self.owner.junit_xml or DEFAULT_JUNIT).encode("utf-8")

    def put_archive(self, path: str, data: bytes) -> bool:
        self.owner.put_archive_calls.append((self.id, path, len(data)))
        if self.create_kwargs.get("read_only"):
            # 实测：daemon 对只读 rootfs 的容器一律拒绝上传，与目标路径是不是
            # tmpfs/卷无关。产品代码因此必须走 staging 容器。
            raise APIError("container rootfs is marked read-only")
        volume = self.mounts.get(path, f"rootfs:{path}")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    handle = archive.extractfile(member)
                    payload = handle.read() if handle else b""
                    self.owner.volume_files[volume][member.name] = payload
                    # 卷会在收尾时被删掉，上传内容单独留一份供断言。
                    self.owner.uploaded.setdefault(volume, {})[member.name] = payload
        return self.owner.put_archive_result

    def exec_run(self, *args: object, **kwargs: object) -> tuple[int, tuple[bytes, bytes]]:
        command = str(args[0] if args else "")
        if "chown" in command:
            self.owner.chown_runs += 1
            return self.owner.chown_exit_code, (b"drwxr-xr-x", b"")
        if "wipe" in command:
            self.owner.wipe_runs += 1
            return self.owner.wipe_exit_code, (b"", b"")
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
        if self.owner.wait_error is not None:
            raise self.owner.wait_error
        if self.owner.timeout:
            raise TimeoutError("deadline")
        return {"StatusCode": self.owner.exit_code}

    def kill(self) -> None:
        self.killed = True

    def reload(self) -> None:
        self.attrs = {"State": {"OOMKilled": self.owner.oom}}

    def remove(self, *, force: bool) -> None:
        assert force is True
        # 只让"非 staging"容器的删除失败：这是 S-22「清理失败不得假绿」要演的分支，
        # staging 容器用完即删的失败由 test_transfer.py 单独覆盖。
        if self.owner.remove_error and not self.is_staging_container:
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
        if container.is_test_container:
            self.owner.test_container = container
        return container

    def list(self, **kwargs: object) -> list[FakeContainer]:
        self.owner.list_calls.append(kwargs)
        return self.listed


class FakeClient:
    def __init__(self) -> None:
        self.images = FakeImages()
        self.containers = FakeContainers(self)
        self.api = FakeApi(self)
        self.install_runs = 0
        self.install_exit_code = 0
        self.chown_runs = 0
        self.chown_exit_code = 0
        self.wipe_runs = 0
        self.wipe_exit_code = 0
        self.list_calls: list[dict[str, object]] = []
        self.exit_code = 0
        self.timeout = False
        self.wait_error: BaseException | None = None
        self.oom = False
        self.remove_error = False
        self.junit_xml: str | None = None
        self.emit_junit = True
        self.stdout = "hello🙂".encode()
        self.put_archive_result = True
        self.put_archive_calls: list[tuple[str, str, int]] = []
        self.volume_files: dict[str, dict[str, bytes]] = {}
        self.uploaded: dict[str, dict[str, bytes]] = {}
        self.volume_labels: dict[str, dict[str, str]] = {}
        self.volume_created_at: dict[str, str] = {}
        self.volume_clock = lambda: "2024-01-01T00:00:00.000000000Z"
        self.volumes_created: list[str] = []
        self.volumes_removed: list[str] = []
        self.remove_volume_error = False
        self.test_container: FakeContainer | None = None

    @property
    def execution_volumes(self) -> list[str]:
        """本轮测试容器挂载的两个卷名。"""
        volumes = self.test_container.create_kwargs.get("volumes") if self.test_container else None
        return sorted(str(name) for name in (volumes or {}))


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
    config = execution_container_config(
        image_ref="image",
        workspace_volume="codepilot-token-ws",
        output_volume="codepilot-token-out",
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
        "working_dir": "/workspace",
    }
    for key, value in expected.items():
        assert config[key] == value
    # 卷的 key 必须是 **卷名**，不是宿主路径：daemon 看不见调用方的文件系统时，
    # 宿主路径只会让容器安静地拿到一个空目录。
    assert sorted(config["volumes"]) == ["codepilot-token-out", "codepilot-token-ws"]
    assert {value["bind"] for value in config["volumes"].values()} == {"/workspace", "/out"}
    assert all(not Path(name).is_absolute() for name in config["volumes"])
    assert all("docker.sock" not in name.lower() for name in config["volumes"])


def test_bind_workspace_mounts_current_project_paths_without_named_volumes(
    tmp_path: Path,
) -> None:
    client = FakeClient()
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", "utf-8")
    artifacts = ArtifactStore(tmp_path / "artifacts", "run")
    engine = DockerSandbox(
        artifacts=artifacts,
        allowed_workspace_root=tmp_path,
        client=client,
        bind_workspace=True,
    )
    result = engine.run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="env",
            image_id="id",
            cache_key="key",
        ),
        env_spec=EnvSpec(),
        tests=("tests/test_x.py::test_x",),
    )
    container = client.test_container
    assert container is not None
    volumes = container.create_kwargs["volumes"]
    assert isinstance(volumes, dict)
    assert volumes[str(workspace.resolve())] == {"bind": "/workspace", "mode": "rw"}
    output_mount = next(name for name, spec in volumes.items() if spec["bind"] == "/out")
    assert Path(output_mount).resolve().is_relative_to(artifacts.run_dir.resolve())
    assert client.volumes_created == []
    assert all(not item.is_staging_container for item in client.containers.created)
    assert result.status is ExecutionStatus.PASS


def test_execution_container_config_honours_the_workspace_target() -> None:
    """官方实例镜像的 checkout 在 /testbed，工作区必须覆盖到同一个路径。"""
    config = execution_container_config(
        image_ref="image",
        workspace_volume="ws",
        output_volume="out",
        command="pytest",
        environment={},
        workspace_target="/testbed",
    )
    assert config["working_dir"] == "/testbed"
    assert config["volumes"]["ws"] == {"bind": "/testbed", "mode": "rw"}


@pytest.mark.parametrize("target", ["workspace", "/out", "/", "/workspace/../etc", ".."])
def test_rejects_unusable_workspace_targets(tmp_path: Path, target: str) -> None:
    with pytest.raises(ValueError, match="容器工作区路径"):
        DockerSandbox(
            artifacts=ArtifactStore(tmp_path / "artifacts", "run"),
            allowed_workspace_root=tmp_path,
            client=FakeClient(),
            workspace_target=target,
        )


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
    (workspace / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", "utf-8")
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
    container = client.test_container
    assert container is not None
    assert result.status is ExecutionStatus.PASS
    assert result.tests_passed == 1 and result.tests_failed == 0
    assert result.stdout_tail.endswith("🙂")
    assert container.create_kwargs["network_mode"] == "none"
    assert container.removed
    volumes = container.create_kwargs["volumes"]
    assert isinstance(volumes, dict)
    # 传输全部发生在 daemon 侧：卷名不是宿主路径，产物则来自 get_archive，
    # 落点仍在本任务的临时输出目录（不在 run artifact 里）。
    assert all(not Path(name).is_absolute() and "/" not in name for name in volumes)
    assert {value["bind"] for value in volumes.values()} == {"/workspace", "/out"}
    assert (Path(result.artifact_dir) / "junit.xml").is_file()
    assert len(client.volumes_created) == 2
    assert client.volumes_removed == client.volumes_created, "本轮的卷必须原样回收"
    assert client.volume_files == {}


def test_workspace_reaches_the_container_through_staging_not_the_test_container(
    tmp_path: Path,
) -> None:
    """只读 rootfs 的容器收不下上传（实测 400），所以必须由 staging 代做。"""
    client = FakeClient()
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_x.py").write_text("", encoding="utf-8")
    sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(),
    )
    assert len(client.containers.created) == 2, "一个测试容器 + 一个 staging 容器"
    test = client.test_container
    assert test is not None
    staging = next(item for item in client.containers.created if item is not test)
    assert staging.create_kwargs["read_only"] is False
    assert test.create_kwargs["read_only"] is True
    assert [call[1] for call in client.put_archive_calls] == ["/stage-ws"]
    assert client.wipe_runs == 1 and client.chown_runs == 1, "先清空预填、再上传、再移交所有权"
    workspace_volume = next(
        name for name, spec in test.create_kwargs["volumes"].items() if spec["bind"] == "/workspace"
    )
    assert client.uploaded[workspace_volume]["tests/test_x.py"] == b"", "上传的正是这份工作区"


def test_upload_refusal_is_a_transfer_failure_not_a_test_failure(tmp_path: Path) -> None:
    client = FakeClient()
    client.put_archive_result = False
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
    assert result.exit_code is None, "没有跑成的执行不能编造退出码"
    assert "工作区传输失败" in str(result.diagnostics["detail"])
    assert result.tests == []
    assert client.volumes_created == client.volumes_removed


def test_prefilled_volume_that_cannot_be_cleared_stops_the_run(tmp_path: Path) -> None:
    """镜像预填内容清不掉 → 归因传输失败，且绝不上传、不跑测试、不回收成"通过"。"""
    client = FakeClient()
    client.wipe_exit_code = 17
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
    assert result.exit_code is None
    assert "工作区传输失败" in str(result.diagnostics["detail"])
    assert client.put_archive_calls == []
    assert client.test_container is not None
    assert not client.test_container.started, "卷没清干净就绝不启动测试容器"
    assert client.test_container.removed
    assert client.volumes_created == client.volumes_removed


def test_missing_junit_is_not_reported_as_pass_even_with_exit_zero(tmp_path: Path) -> None:
    client = FakeClient()
    client.emit_junit = False
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
    assert result.error_reason == "MISSING_OR_INVALID_JUNIT"


def test_get_archive_failure_is_attributed_to_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def refuse(*args: object, **kwargs: object) -> None:
        raise DockerException("404 no such volume content")

    monkeypatch.setattr(client.api, "get_archive", refuse)
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(),
    )
    assert result.status is ExecutionStatus.SANDBOX_ERROR
    assert "结果归档回传失败" in str(result.diagnostics["detail"])
    assert client.volumes_created == client.volumes_removed


def test_timeout_still_collects_and_reclaims_everything(tmp_path: Path) -> None:
    client = FakeClient()
    client.timeout = True
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(timeout_seconds=1),
    )
    container = client.test_container
    assert result.status is ExecutionStatus.TIMEOUT
    assert container is not None and container.killed and container.removed
    assert client.volumes_created == client.volumes_removed
    assert client.volume_files == {}


def test_cleanup_removes_stale_labeled_volumes_only(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    client = FakeClient()
    engine = sandbox(tmp_path, client, clock=lambda: now)

    def iso(seconds: float) -> str:
        # daemon 的 CreatedAt 带纳秒，正好一起覆盖 _parse_created_at 的截断。
        text = datetime.fromtimestamp(seconds, tz=UTC).isoformat()
        return text.replace("+00:00", f"{'.000000001'}+00:00")

    stale = "codepilot-stale-ws"
    recent = "codepilot-recent-ws"
    foreign = "some-other-project-volume"
    undated = "codepilot-no-timestamp-ws"
    client.volume_created_at.update(
        {
            stale: iso(now - 7200),
            recent: iso(now - 60),
            foreign: iso(now - 7200),
            undated: "",
        }
    )
    for name in (stale, recent, foreign, undated):
        client.volume_files.setdefault(name, {})
        client.volume_labels.setdefault(name, {"codepilot": "1"})
    client.volume_labels[foreign] = {}
    removed = engine.cleanup(older_than_seconds=3600)
    assert removed == [stale], "只回收有 codepilot 标签、名字前缀匹配且时间戳可解析的旧卷"
    assert recent in client.volume_files
    assert foreign in client.volume_files
    assert undated in client.volume_files, "认不出创建时间的卷一律不删"


def test_timeout_outranks_a_failed_result_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时优先归因；回传失败作为附注留下，而不是把 TIMEOUT 改写成传输故障。"""
    client = FakeClient()
    client.timeout = True

    def refuse(*args: object, **kwargs: object) -> None:
        raise DockerException("archive endpoint gone with the container")

    monkeypatch.setattr(client.api, "get_archive", refuse)
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
    assert result.error_reason == "EXECUTION_TIMEOUT"
    assert "结果归档回传失败" in str(result.diagnostics["post_timeout_error"])


def test_volume_removal_failure_leaves_visible_residue(tmp_path: Path) -> None:
    client = FakeClient()
    client.remove_volume_error = True
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
    assert "删除失败" in str(result.diagnostics["cleanup_error"])
    assert client.volume_files, "删不掉的卷必须留在现场可核对，不能记成已回收"
    assert client.volumes_removed == []


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


def test_junit_failure_text_is_redacted_inside_execution_result(tmp_path: Path) -> None:
    """``ExecutionResult.tests`` 走的是内存解析结果，这条路此前原样带出密钥。

    落盘的三份 artifact 一直有脱敏，但 Phase 5 把 ``tests`` 里的失败文本直接喂给
    模型并写进运行记录；真实 CLI run-tests 实测到一个 FAIL 用例把 ``sk-`` token
    原样打进了 CLI 输出的 JSON。
    """
    client = FakeClient()
    client.exit_code = 1
    client.junit_xml = (
        '<testsuite tests="2">'
        '<testcase classname="tests.test_x" name="ok" time="0.1"/>'
        '<testcase classname="tests.test_x" name="leak" time="0.2">'
        '<failure message="token sk-leakedsecret123">'
        "password=verysecretvalue\n  assert login('demo', 'sk-leakedsecret123')\n"
        "</failure></testcase></testsuite>"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(),
    )
    assert result.status is ExecutionStatus.FAIL
    assert result.tests_passed == 1 and result.tests_failed == 1
    leaked = result.tests[1]
    assert "leak" in leaked.node_id
    assert "sk-leakedsecret123" not in (leaked.message or "")
    assert "sk-leakedsecret123" not in (leaked.traceback or "")
    assert "verysecretvalue" not in (leaked.traceback or "")
    assert REDACTED in (leaked.message or "")
    assert REDACTED in (leaked.traceback or "")
    assert "sk-leakedsecret123" not in result.model_dump_json()


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
    container = client.test_container
    assert container is not None
    assert result.status is ExecutionStatus.TIMEOUT
    assert container.killed and container.removed


def _npipe_read_timeout() -> RequestsConnectionError:
    """复刻 Docker Desktop npipe 传输实测到的异常链（S-18）。

    该传输不把读超时报告成 ``requests.exceptions.ReadTimeout``，而是
    ``ConnectionError``，超时本体只存在于 ``__context__`` 链里。
    """
    deadline = TimeoutError("Read timed out.")
    read_timeout = urllib3.exceptions.ReadTimeoutError(None, None, "Read timed out.")
    read_timeout.__cause__ = deadline
    connection_error = RequestsConnectionError(
        "NpipeHTTPConnectionPool(host='localhost'): Read timed out."
    )
    connection_error.__context__ = read_timeout
    return connection_error


def test_npipe_wrapped_read_timeout_is_still_a_timeout(tmp_path: Path) -> None:
    client = FakeClient()
    client.wait_error = _npipe_read_timeout()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(timeout_seconds=1),
    )
    container = client.test_container
    assert container is not None
    assert result.status is ExecutionStatus.TIMEOUT
    assert result.error_reason == "EXECUTION_TIMEOUT"
    assert container.killed and container.removed


def test_connection_failure_without_timeout_stays_sandbox_error(tmp_path: Path) -> None:
    client = FakeClient()
    client.wait_error = RequestsConnectionError("daemon closed the connection")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = sandbox(tmp_path, client).run_tests(
        workspace=workspace,
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="env", image_id="id", cache_key="key"
        ),
        env_spec=EnvSpec(timeout_seconds=1),
    )
    assert result.status is ExecutionStatus.SANDBOX_ERROR
    assert client.test_container is not None
    assert not client.test_container.killed


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


def test_desktop_proxy_maps_wsl_drive_binds_without_changing_other_sources(monkeypatch):
    from codepilot.sandbox.docker import daemon_bind_source

    monkeypatch.setenv(
        "DOCKER_HOST",
        "unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock",
    )
    assert (
        daemon_bind_source("/mnt/d/project/workspace")
        == "/run/desktop/mnt/host/d/project/workspace"
    )
    assert daemon_bind_source("/mnt/d/project/out") == "/run/desktop/mnt/host/d/project/out"
    assert daemon_bind_source("/home/user/project") == "/home/user/project"
    assert daemon_bind_source("D:/project") == "D:/project"
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    assert daemon_bind_source("/mnt/d/project") == "/mnt/d/project"
