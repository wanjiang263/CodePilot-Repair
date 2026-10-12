"""Phase 4 第二批真实 Docker 运行时验收（S-18～S-23）的共享夹具。

这里创建的每一个容器都是真实容器：断言读的是 ``docker inspect`` 与容器内部
执行的输出，不是 FakeSandbox 的固定返回值。只有两类刻意的偏离，且都不改变
容器生命周期：

* **资源上限下调**（仅内存）。生产契约是 4 GiB，开发机上真占 4 GiB 才能触发
  OOM 没有必要；内存是资源旋钮，不是安全参数，下调它不会让任何安全断言变松。
* **单次 ``remove`` 调用的故障注入**。用于走通"清理失败仍保留原始证据"这条
  分支——真实 daemon 没有可靠办法让删除一个刚建好的容器稳定失败。

两类偏离都在被测容器上打了 ``codepilot.acceptance_run=<唯一 id>`` 标签，
所以"本轮创建的资源"可以在 daemon 侧被精确追踪，清理断言只作用于本 run。
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from docker.errors import DockerException

import docker
from codepilot.artifacts import ArtifactStore
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.schemas.enums import ExecutionStatus
from support.gitfixture import FixtureRepo, init_repo

__all__ = ["ACCEPTANCE_ENV_SPEC", "FixtureRepo", "containers_for_run"]

CODEPILOT_LABEL = "codepilot=1"
RUN_LABEL_KEY = "codepilot.acceptance_run"
EVIDENCE_PATH = (
    Path(__file__).resolve().parents[2] / "artifacts" / "docker-accept" / "runtime-evidence.jsonl"
)

#: 全套验收共用的环境镜像：只需要 pytest，冷启动一次即可。
ACCEPTANCE_ENV_SPEC = EnvSpec(
    install=("python -m pip install pytest",),
    timeout_seconds=120,
)


def containers_for_run(client: Any, run_id: str) -> list[dict[str, Any]]:
    """daemon 侧查询本轮创建的容器（含已停止的）。"""
    return client.api.containers(all=True, filters={"label": f"{RUN_LABEL_KEY}={run_id}"})


class RecordingClient:
    """真实 SDK 客户端的透明转发层：合并标签、可选下调资源、可选注入删除失败。"""

    def __init__(
        self,
        real: Any,
        *,
        run_id: str,
        create_overrides: Mapping[str, Any] | None = None,
        fail_remove: bool = False,
    ) -> None:
        self.real = real
        self.run_id = run_id
        self.create_overrides = dict(create_overrides or {})
        self.fail_remove = fail_remove
        self.created_ids: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        self.containers = _ContainersView(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)

    def merged_create_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        labels = {**(kwargs.get("labels") or {}), "codepilot": "1", RUN_LABEL_KEY: self.run_id}
        return {**kwargs, **self.create_overrides, "labels": labels}


class _ContainersView:
    def __init__(self, owner: RecordingClient) -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> _ContainerHandle:
        container = self._owner.real.containers.create(**self._owner.merged_create_kwargs(kwargs))
        self._owner.created_ids.append(str(container.id))
        return _ContainerHandle(container, self._owner)

    def list(self, *args: Any, **kwargs: Any) -> list[Any]:
        self._owner.list_calls.append({"args": args, **kwargs})
        real = self._owner.real.containers.list(*args, **kwargs)
        return [_ContainerHandle(item, self._owner) for item in real]

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return _ContainerHandle(self._owner.real.containers.get(*args, **kwargs), self._owner)


class _ContainerHandle:
    """转发一切属性与方法；只在 ``remove`` 上按注入开关抛错。"""

    def __init__(self, container: Any, owner: RecordingClient) -> None:
        self._container = container
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._container, name)

    def __repr__(self) -> str:
        return f"<acceptance-container {self._container.id[:12]}>"

    @property
    def id(self) -> str:
        return str(self._container.id)

    def remove(self, **kwargs: Any) -> Any:
        if self._owner.fail_remove:
            raise DockerException(f"注入的删除失败: {self._container.id[:12]}")
        return self._container.remove(**kwargs)

    def start(self, **kwargs: Any) -> Any:
        return self._container.start(**kwargs)


class ContainerObserver:
    """独立线程轮询 daemon，留下"容器确实存在过"的运行期证据。"""

    def __init__(self, client: Any, *, interval: float = 0.1) -> None:
        self._client = client
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.records: dict[str, dict[str, Any]] = {}
        self.states: dict[str, list[str]] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._poll, name="codepilot-observer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                rows = self._client.api.containers(all=True, filters={"label": CODEPILOT_LABEL})
            except DockerException:
                rows = []
            for row in rows:
                identifier = str(row["Id"])
                state = str(row.get("State", "unknown"))
                seen = self.states.setdefault(identifier, [])
                if not seen or seen[-1] != state:
                    seen.append(state)
                if identifier not in self.records:
                    with contextlib.suppress(DockerException):
                        self.records[identifier] = self._client.api.inspect_container(identifier)
            self._stop.wait(self._interval)

    def ids_for_run(self, run_id: str) -> list[str]:
        return [
            identifier
            for identifier, attrs in self.records.items()
            if (attrs.get("Config", {}).get("Labels") or {}).get(RUN_LABEL_KEY) == run_id
        ]

    def inspect_of(self, identifier: str) -> dict[str, Any]:
        return self.records[identifier]


@pytest.fixture(scope="session")
def docker_client() -> Any:
    client = docker.from_env()
    info = client.info()
    if info.get("OSType") != "linux":
        pytest.fail(f"验收要求 Linux 容器引擎，实际 OSType={info.get('OSType')!r}")
    return client


@pytest.fixture(scope="session")
def observer_client() -> Iterator[Any]:
    """观察者用独立连接，避免与主线程共用 SDK 会话。"""
    yield docker.from_env()


@pytest.fixture(scope="session", autouse=True)
def foreign_images_are_untouched(docker_client: Any) -> Iterator[set[str]]:
    """验收绝不允许 ``docker system prune`` 之类的全局销毁：镜像必须原地不动。"""

    def foreign() -> set[str]:
        return {
            str(image["Id"])
            for image in docker_client.api.images(all=False)
            if not ((image.get("Labels") or {}).get("codepilot"))
        }

    before = foreign()
    yield before
    missing = before - foreign()
    assert not missing, f"验收过程删除了其他项目的镜像: {sorted(missing)}" @ pytest.fixture(
        scope="session", autouse=True
    )


def foreign_containers_are_untouched(docker_client: Any) -> Iterator[set[str]]:
    """会话前后对比：CodePilot 的验收绝不允许动其他项目的容器。"""

    def foreign() -> set[str]:
        return {
            str(row["Id"])
            for row in docker_client.api.containers(all=True)
            if not (row.get("Labels") or {}).get("codepilot")
        }

    before = foreign()
    yield before
    missing = before - foreign()
    assert not missing, f"验收过程误删了其他项目的容器: {sorted(missing)}"


@pytest.fixture(scope="session")
def sandbox_environment(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """整套验收共用一次真实环境镜像构建。"""
    root = tmp_path_factory.mktemp("env-image")
    workspace = root / "workspace"
    repo = init_repo(
        workspace, {"test_image_probe.py": "def test_image_probe():\n    assert True\n"}
    )
    sandbox = DockerSandbox(
        artifacts=ArtifactStore(root / "artifacts", "image-build"),
        allowed_workspace_root=root,
        log_tail_bytes=4096,
    )
    result = sandbox.build_env(
        repository=str(workspace.resolve()),
        base_commit=repo.head,
        workspace=workspace,
        env_spec=ACCEPTANCE_ENV_SPEC,
        allow_network=True,
    )
    assert result.status is ExecutionStatus.PASS, f"环境镜像构建失败: {result.error}"
    return result


@pytest.fixture
def acceptance_run_id() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture
def fixture_workspace(tmp_path: Path) -> Callable[[Mapping[str, str]], FixtureRepo]:
    def _make(files: Mapping[str, str], *, name: str = "workspace") -> FixtureRepo:
        return init_repo(tmp_path / name, dict(files))

    return _make


@pytest.fixture
def make_sandbox(
    tmp_path: Path,
    docker_client: Any,
    acceptance_run_id: str,
) -> Callable[..., tuple[DockerSandbox, RecordingClient]]:
    def _make(
        *,
        create_overrides: Mapping[str, Any] | None = None,
        fail_remove: bool = False,
        log_tail_bytes: int = 4096,
        artifact_name: str = "artifacts",
    ) -> tuple[DockerSandbox, RecordingClient]:
        client = RecordingClient(
            docker_client,
            run_id=acceptance_run_id,
            create_overrides=create_overrides,
            fail_remove=fail_remove,
        )
        sandbox = DockerSandbox(
            artifacts=ArtifactStore(tmp_path / artifact_name, f"acceptance-{acceptance_run_id}"),
            allowed_workspace_root=tmp_path,
            client=client,
            log_tail_bytes=log_tail_bytes,
        )
        return sandbox, client

    return _make


@pytest.fixture
def observer(observer_client: Any, acceptance_run_id: str) -> Iterator[ContainerObserver]:
    watched = ContainerObserver(observer_client)
    watched.start()
    try:
        yield watched
    finally:
        watched.stop()


@pytest.fixture(scope="session")
def record_evidence() -> Callable[[str, Mapping[str, Any]], None]:
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _record(scenario: str, payload: Mapping[str, Any]) -> None:
        line = {
            "scenario": scenario,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **payload,
        }
        with EVIDENCE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")

    return _record


def spec_with(**updates: Any) -> EnvSpec:
    """派生 EnvSpec；只用于超时/测试命令等执行期参数，不用于镜像构建。"""
    return ACCEPTANCE_ENV_SPEC.model_copy(update=updates)


def assert_no_run_residue(client: Any, run_id: str) -> list[str]:
    """本轮 run 标签下必须一个容器都不剩，返回残留 id 供断言消息使用。"""
    return [str(row["Id"]) for row in containers_for_run(client, run_id)]


def wait_for(
    predicate: Callable[[], bool], *, timeout: float = 20.0, interval: float = 0.2
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def selected_tests(result: Any) -> Sequence[str]:
    return [case.node_id for case in result.tests]
