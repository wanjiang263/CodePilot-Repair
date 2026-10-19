"""E-14 官方 SWE-bench 环境适配验收（全部 Fake，不拉镜像、不建容器）。

覆盖任务书 §8.7：规格解析、身份生成、缓存命中/失效、Docker 不可用、镜像不存在、
构建/拉取失败、Windows 路径与 LF/CRLF、权限不足、资源不足、清理只动自家资源、
幂等、错误归因。唯一真实 Docker 的用例标 ``docker`` 且只读预检查（conftest 在
daemon 不可用时自动 skip 并给出原因）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import DockerException, ImageNotFound, NotFound

from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec, EnvSpecError, from_swebench_spec
from codepilot.sandbox.swebench_env import (
    ARCH,
    CACHE_FILENAME,
    DEFAULT_SPEC_SNAPSHOT,
    HARNESS_VERSION,
    ContainerInfo,
    DockerBackend,
    DockerBackendError,
    DockerDaemonUnavailableError,
    DockerPermissionDeniedError,
    DockerResourceExhaustedError,
    DockerSdkBackend,
    ImageInfo,
    PlatformInfo,
    SWEBenchEnvironmentAdapter,
    SWEBenchEnvironmentError,
    SWEBenchEnvironmentSpec,
    SWEBenchSandboxFactory,
    instance_image_key,
)
from codepilot.schemas.enums import ExecutionStatus

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "configs" / "swebench" / "env-specs-4.1.0.json"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
BASE_COMMIT = "a" * 40
ENV_SETUP_COMMIT = "b" * 40
SPEC_SHA256_LENGTH = 64

_SNAPSHOT_PAYLOAD: dict[str, Any] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
_ASTROPY_SPEC: dict[str, Any] = _SNAPSHOT_PAYLOAD["specs"]["astropy/astropy"]["5.1"]
_FLASK_SPEC: dict[str, Any] = _SNAPSHOT_PAYLOAD["specs"]["pallets/flask"]["2.3"]

LINUX = PlatformInfo(system="Linux", machine="x86_64")
WINDOWS_NO_WSL = PlatformInfo(system="Windows", machine="AMD64", wsl_available=False)
WINDOWS_WSL = PlatformInfo(system="Windows", machine="AMD64", wsl_available=True)


# ---------------------------------------------------------------------------
# Fake Docker 后端 / Fake Sandbox
# ---------------------------------------------------------------------------


class FakeDockerBackend(DockerBackend):
    """可编程 Docker 后端：记录每一个会改变状态的操作，拒绝隐式副作用。"""

    def __init__(self) -> None:
        self.daemon_result = True
        self.daemon_error: Exception | None = None
        self.ostype_result: str | None = "linux"
        self.ostype_error: Exception | None = None
        self.images: dict[str, ImageInfo] = {}
        self.pull_calls: list[str] = []
        self.pull_error: Exception | None = None
        self.build_calls: list[dict[str, Any]] = []
        self.build_error: Exception | None = None
        self.disk_free: float | None = 120.0
        self.disk_error: Exception | None = None
        self.containers: dict[str, ContainerInfo] = {}
        self.remove_calls: list[str] = []
        self.fail_remove: set[str] = set()

    def add_image(
        self,
        key: str,
        *,
        image_id: str = "sha256:aaaa",
        os: str = "linux",
        architecture: str = "amd64",
        repo_tags: tuple[str, ...] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.images[key] = ImageInfo(
            ref=key,
            image_id=image_id,
            os=os,
            architecture=architecture,
            repo_tags=repo_tags if repo_tags is not None else (key,),
            labels=dict(labels or {}),
        )

    def daemon_ok(self) -> bool:
        if self.daemon_error is not None:
            raise self.daemon_error
        return self.daemon_result

    def ostype(self) -> str:
        if self.ostype_error is not None:
            raise self.ostype_error
        return self.ostype_result or ""

    def list_images(self, *, labels: Mapping[str, str] | None = None) -> tuple[ImageInfo, ...]:
        return tuple(self.images.values())

    def inspect_image(self, ref: str) -> ImageInfo | None:
        return self.images.get(ref)

    def build(
        self,
        *,
        dockerfile: str,
        tag: str,
        context: Path,
        labels: Mapping[str, str],
    ) -> str:
        self.build_calls.append(
            {"dockerfile": dockerfile, "tag": tag, "context": context, "labels": dict(labels)}
        )
        if self.build_error is not None:
            raise self.build_error
        self.add_image(tag, image_id="sha256:built")
        return "sha256:built"

    def pull(self, ref: str) -> ImageInfo:
        self.pull_calls.append(ref)
        if self.pull_error is not None:
            raise self.pull_error
        self.add_image(ref, image_id=f"sha256:pulled{len(self.pull_calls)}")
        return self.images[ref]

    def disk_free_gb(self, path: Path) -> float | None:
        if self.disk_error is not None:
            raise self.disk_error
        return self.disk_free

    def list_containers(self, *, labels: Mapping[str, str]) -> tuple[ContainerInfo, ...]:
        return tuple(
            container
            for container in self.containers.values()
            if all(container.labels.get(key) == value for key, value in labels.items())
        )

    def remove_container(self, identifier: str) -> bool:
        self.remove_calls.append(identifier)
        if identifier in self.fail_remove:
            raise DockerBackendError("注入的删除失败")
        return self.containers.pop(identifier, None) is not None


class FakeSandbox:
    """只实现 ``Sandbox`` 协议中的 ``build_env``，用来确认工厂没有绕过隔离。"""

    def __init__(self) -> None:
        self.build_calls: list[dict[str, Any]] = []
        self.result = EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="codepilot-env:test",
            image_id="sha256:env",
            cache_key="test-cache-key",
            from_cache=False,
        )

    def build_env(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: EnvSpec,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult:
        self.build_calls.append(
            {
                "repository": repository,
                "base_commit": base_commit,
                "workspace": workspace,
                "env_spec": env_spec,
                "allow_network": allow_network,
            }
        )
        return self.result

    def run_tests(self, **kwargs: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("SWEBenchSandboxFactory 不应调用 run_tests")

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:  # pragma: no cover
        raise AssertionError("SWEBenchSandboxFactory 不应调用 cleanup")


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def make_task(
    *,
    instance_id: str = "astropy__astropy-12907",
    repo: str = "astropy/astropy",
    version: str = "5.1",
    base_commit: str = BASE_COMMIT,
    environment_setup_commit: str = ENV_SETUP_COMMIT,
    dataset_revision: str = DATASET_REVISION,
) -> SimpleNamespace:
    return SimpleNamespace(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        version=version,
        environment_setup_commit=environment_setup_commit,
        dataset=SimpleNamespace(revision=dataset_revision),
    )


def make_flask_task() -> SimpleNamespace:
    return make_task(
        instance_id="pallets__flask-4992",
        repo="pallets/flask",
        version="2.3",
    )


def make_adapter(
    tmp_path: Path,
    backend: DockerBackend | None,
    *,
    allow_build: bool = False,
    disk_limit_gb: float | None = None,
    platform: PlatformInfo = LINUX,
    snapshot: Path = SNAPSHOT,
) -> SWEBenchEnvironmentAdapter:
    return SWEBenchEnvironmentAdapter(
        docker=backend,
        spec_snapshot=snapshot,
        cache_dir=tmp_path / "swebench-env-cache",
        allow_build=allow_build,
        disk_limit_gb=disk_limit_gb,
        platform=platform,
    )


def make_identity(
    tmp_path: Path,
    backend: DockerBackend | None = None,
    *,
    task: SimpleNamespace | None = None,
    snapshot: Path = SNAPSHOT,
) -> Any:
    adapter = make_adapter(tmp_path, backend, snapshot=snapshot)
    return adapter.identity(task or make_task())


def write_snapshot(
    tmp_path: Path,
    specs: Mapping[str, Any],
    *,
    harness_version: str = HARNESS_VERSION,
    name: str = "snapshot.json",
) -> Path:
    path = tmp_path / name
    payload = {
        "harness_package": "swebench",
        "harness_version": harness_version,
        "note": "test fixture",
        "python_requires": ">=3.9",
        "source": "swebench.harness.constants.MAP_REPO_VERSION_TO_SPECS",
        "specs": specs,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), "utf-8")
    return path


def _cache_path(tmp_path: Path) -> Path:
    return tmp_path / "swebench-env-cache" / CACHE_FILENAME


def _cache_payload(tmp_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_cache_path(tmp_path).read_text(encoding="utf-8"))
    return payload


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ===========================================================================
# A. 规格解析（冻结快照、不 import swebench）
# ===========================================================================


def test_load_real_snapshot_exposes_frozen_metadata() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    assert spec.harness_version == "4.1.0"
    assert spec.source == "swebench.harness.constants.MAP_REPO_VERSION_TO_SPECS"
    assert spec.path == SNAPSHOT
    assert len(spec.repo_names) == 66
    assert "astropy/astropy" in spec.repo_names
    assert DEFAULT_SPEC_SNAPSHOT == SNAPSHOT


def test_spec_for_astropy_5_1_returns_read_only_official_fields() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    raw = spec.spec_for("astropy/astropy", "5.1")
    assert raw["python"] == "3.9"
    assert raw["install"] == "python -m pip install -e .[test] --verbose"
    assert raw["test_cmd"] == "pytest -rA"
    assert raw["pip_packages"][0] == "attrs==23.1.0"
    assert raw["pip_packages"][-1] == "tomli==2.0.1"
    assert raw["pre_install"] == [
        'sed -i \'s/requires = \\["setuptools",/requires = \\["setuptools==68.0.0",/\' pyproject.toml'
    ]
    assert isinstance(raw, Mapping)
    with pytest.raises(TypeError):
        raw["python"] = "3.12"  # type: ignore[index]


def test_requirements_prefers_pip_packages_then_normalizes_packages() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    assert spec.requirements("astropy/astropy", "5.1") == tuple(_ASTROPY_SPEC["pip_packages"])
    assert spec.requirements("pallets/flask", "2.3") == tuple(_FLASK_SPEC["pip_packages"])
    # django 5.0 用 requirements.txt；没有 pip_packages 时回退成 pip 可执行形式。
    assert spec.requirements("django/django", "5.0") == ("-r requirements.txt",)
    # matplotlib 3.5 的 environment.yml 不是 pip 依赖，但 pip_packages 仍然可用。
    assert spec.requirements("matplotlib/matplotlib", "3.5") == tuple(
        _SNAPSHOT_PAYLOAD["specs"]["matplotlib/matplotlib"]["3.5"]["pip_packages"]
    )
    assert spec.requirements("psf/requests", "2.10") == ("pytest",)


def test_env_spec_for_flask_merges_pip_packages_before_repo_install() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    env_spec = spec.env_spec_for("pallets/flask", "2.3")
    assert env_spec.python_version == "3.11"
    assert env_spec.install[0] == "python -m pip install -r requirements.txt"
    assert env_spec.install[1].startswith("python -m pip install setuptools==70.0.0")
    assert "click==8.1.3" in env_spec.install[1]
    assert env_spec.install[-1] == "python -m pip install -e ."
    assert env_spec.test_command == "pytest -rA --junitxml=/out/junit.xml"


def test_env_spec_for_unsupported_python_is_attributed_unsupported() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        spec.env_spec_for("astropy/astropy", "5.1")
    assert caught.value.kind == "UNSUPPORTED"
    message = str(caught.value)
    assert "python=3.9" in message
    assert "官方镜像" in message
    assert "deferred" in message


def test_env_spec_for_conda_spec_is_attributed_unsupported() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        spec.env_spec_for("matplotlib/matplotlib", "3.5")
    assert caught.value.kind == "UNSUPPORTED"
    assert "conda" in str(caught.value)


def test_env_spec_for_non_pytest_command_is_attributed_unsupported() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        spec.env_spec_for("django/django", "5.0")
    assert caught.value.kind == "UNSUPPORTED"
    assert "不是 pytest" in str(caught.value)


def test_unknown_repo_or_version_is_attributed_unsupported() -> None:
    spec = SWEBenchEnvironmentSpec.load()
    for repo, version in (("no/such-repo", "1.0"), ("astropy/astropy", "9.9")):
        with pytest.raises(SWEBenchEnvironmentError) as caught:
            spec.spec_for(repo, version)
        assert caught.value.kind == "UNSUPPORTED"


def test_snapshot_missing_corrupt_or_wrong_version_is_env_error(tmp_path: Path) -> None:
    with pytest.raises(SWEBenchEnvironmentError) as missing:
        SWEBenchEnvironmentSpec.load(tmp_path / "does-not-exist.json")
    assert missing.value.kind == "ENV_ERROR"

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        SWEBenchEnvironmentSpec.load(corrupt)
    assert caught.value.kind == "ENV_ERROR"

    wrong_version = write_snapshot(
        tmp_path,
        {"astropy/astropy": {"5.1": _ASTROPY_SPEC}},
        harness_version="4.0.0",
        name="wrong-version.json",
    )
    with pytest.raises(SWEBenchEnvironmentError) as mismatch:
        SWEBenchEnvironmentSpec.load(wrong_version)
    assert mismatch.value.kind == "ENV_ERROR"
    assert "版本" in str(mismatch.value)

    empty = write_snapshot(tmp_path, {}, name="empty.json")
    with pytest.raises(SWEBenchEnvironmentError) as empty_error:
        SWEBenchEnvironmentSpec.load(empty)
    assert empty_error.value.kind == "ENV_ERROR"


def test_from_swebench_spec_merges_pip_packages_and_keeps_backward_compatibility() -> None:
    env_spec = from_swebench_spec(
        {
            "python": "3.11",
            "packages": "requirements.txt",
            "pip_packages": ["pytest==8.0.0", "numpy==1.25.2"],
            "pre_install": ["apt-get update"],
            "install": "python -m pip install -e .",
            "test_cmd": "python -m pytest -q",
        }
    )
    assert env_spec.install == (
        "python -m pip install -r requirements.txt",
        "python -m pip install pytest==8.0.0 numpy==1.25.2",
        "python -m pip install -e .",
    )
    # 空 pip_packages 不产生空命令，也不改变既有 synthetic fixture 行为。
    assert from_swebench_spec({"python": "3.12", "pip_packages": []}).install == ()
    with pytest.raises(EnvSpecError):
        from_swebench_spec({"python": "3.12", "pip_packages": "pytest"})
    with pytest.raises(EnvSpecError):
        from_swebench_spec({"python": "3.12", "pip_packages": ["pytest", 1]})


def test_crlf_snapshot_bytes_produce_identical_spec_digest_and_identity(
    tmp_path: Path,
) -> None:
    crlf = tmp_path / "snapshot-crlf.json"
    crlf.write_bytes(SNAPSHOT.read_bytes().replace(b"\n", b"\r\n"))
    crlf_spec = SWEBenchEnvironmentSpec.load(crlf)
    assert crlf_spec.spec_for("astropy/astropy", "5.1") == SWEBenchEnvironmentSpec.load().spec_for(
        "astropy/astropy", "5.1"
    )
    real_identity = make_identity(tmp_path, FakeDockerBackend(), snapshot=SNAPSHOT)
    crlf_identity = make_identity(tmp_path, FakeDockerBackend(), snapshot=crlf)
    assert crlf_identity.spec_sha256 == real_identity.spec_sha256
    assert crlf_identity.environment_hash == real_identity.environment_hash


# ===========================================================================
# B. 身份生成
# ===========================================================================


def test_identity_generates_official_image_keys_and_hashes(tmp_path: Path) -> None:
    identity = make_identity(tmp_path, FakeDockerBackend())
    assert identity.instance_id == "astropy__astropy-12907"
    assert (
        identity.instance_image_key == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    )
    assert identity.base_image_key == "sweb.base.py.x86_64:latest"
    assert identity.env_image_key.startswith(f"sweb.env.py.{ARCH}.")
    assert identity.env_image_key.endswith(":latest")
    assert identity.harness_version == HARNESS_VERSION
    assert identity.dataset_revision == DATASET_REVISION
    assert identity.python_version == "3.9"
    assert len(identity.spec_sha256) == SPEC_SHA256_LENGTH
    assert len(identity.environment_hash) == SPEC_SHA256_LENGTH
    int(identity.spec_sha256, 16)
    int(identity.environment_hash, 16)


def test_identity_spec_sha256_is_canonical_json_digest(tmp_path: Path) -> None:
    identity = make_identity(tmp_path, FakeDockerBackend())
    assert identity.spec_sha256 == _sha256(_canonical_json(_ASTROPY_SPEC))


def test_identity_environment_hash_is_stable_and_sensitive_to_commit_fields(
    tmp_path: Path,
) -> None:
    first = make_identity(tmp_path, FakeDockerBackend())
    second = make_identity(tmp_path, FakeDockerBackend())
    assert first.environment_hash == second.environment_hash
    moved = make_identity(
        tmp_path,
        FakeDockerBackend(),
        task=make_task(base_commit="c" * 40),
    )
    assert moved.environment_hash != first.environment_hash
    assert moved.spec_sha256 == first.spec_sha256
    moved_setup = make_identity(
        tmp_path,
        FakeDockerBackend(),
        task=make_task(environment_setup_commit="d" * 40),
    )
    assert moved_setup.environment_hash != first.environment_hash


def test_identity_base_image_key_reproduces_official_docker_specs_hash(
    tmp_path: Path,
) -> None:
    spec_with_docker_specs = dict(_ASTROPY_SPEC)
    # 快照写出时 sort_keys=True，所以这里也按字典序构造，保证官方 hash 复算一致。
    spec_with_docker_specs["docker_specs"] = {"cpus": 4, "memory": "8g"}
    snapshot = write_snapshot(
        tmp_path,
        {"astropy/astropy": {"5.1": spec_with_docker_specs}},
        name="docker-specs.json",
    )
    identity = make_identity(tmp_path, FakeDockerBackend(), snapshot=snapshot)
    expected_hash = _sha256(str(spec_with_docker_specs["docker_specs"]))[:10]
    assert identity.base_image_key == f"sweb.base.py.x86_64.{expected_hash}:latest"
    assert identity.env_image_key.startswith(f"sweb.env.py.{ARCH}.")
    assert identity.spec_sha256 == _sha256(_canonical_json(spec_with_docker_specs))


@pytest.mark.parametrize(
    ("task", "message_part"),
    [
        (make_task(repo="astropy"), "repo"),
        (make_task(base_commit="abc"), "base_commit"),
        (make_task(environment_setup_commit="z" * 40), "environment_setup_commit"),
        (make_task(instance_id="not-an-instance"), "instance_id"),
    ],
)
def test_identity_rejects_malformed_task_fields(
    tmp_path: Path, task: SimpleNamespace, message_part: str
) -> None:
    adapter = make_adapter(tmp_path, FakeDockerBackend())
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        adapter.identity(task)
    assert caught.value.kind == "ENV_ERROR"
    assert message_part in str(caught.value)


def test_identity_requires_dataset_revision(tmp_path: Path) -> None:
    task = make_task()
    task.dataset = SimpleNamespace(revision="")
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, FakeDockerBackend()).identity(task)
    assert caught.value.kind == "ENV_ERROR"
    assert "dataset.revision" in str(caught.value)


def test_identity_rejects_non_python_official_spec(tmp_path: Path) -> None:
    non_python = next(
        (repo, version)
        for repo, versions in _SNAPSHOT_PAYLOAD["specs"].items()
        for version, raw in versions.items()
        if "python" not in raw
    )
    repo, version = non_python
    adapter = make_adapter(tmp_path, FakeDockerBackend())
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        adapter.identity(make_task(repo=repo, version=version))
    assert caught.value.kind == "UNSUPPORTED"
    assert "python" in str(caught.value)


def test_identity_works_even_when_env_spec_backend_is_unsupported(tmp_path: Path) -> None:
    # astropy 5.1 的官方 python 是 3.9，EnvSpec 后端不支持；但身份仍可生成。
    identity = make_identity(tmp_path, FakeDockerBackend())
    assert identity.python_version == "3.9"
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        SWEBenchEnvironmentSpec.load().env_spec_for("astropy/astropy", "5.1")
    assert caught.value.kind == "UNSUPPORTED"


# ===========================================================================
# C. 缓存命中 / 失效 / 损坏 / 权限
# ===========================================================================


def test_ensure_hits_cache_on_second_call_and_writes_lf_cache(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key, image_id="sha256:abc")
    adapter = make_adapter(tmp_path, backend)

    first = adapter.ensure(identity)
    assert first.from_cache is True
    assert first.cache_state == "miss"
    assert first.warnings == ()
    assert backend.pull_calls == []
    second = adapter.ensure(identity)
    assert second.cache_state == "hit"
    assert second.image_id == "sha256:abc"

    raw = _cache_path(tmp_path).read_bytes()
    assert b"\r\n" not in raw  # Windows 上也必须是 LF
    assert not raw.startswith(b"\xef\xbb\xbf")  # 不写 BOM
    payload = _cache_payload(tmp_path)
    assert payload["harness_version"] == HARNESS_VERSION
    assert identity.environment_hash in payload["entries"]
    assert payload["entries"][identity.environment_hash]["image_id"] == "sha256:abc"


def test_ensure_revalidates_cache_when_image_id_changes(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key, image_id="sha256:old")
    adapter = make_adapter(tmp_path, backend)
    adapter.ensure(identity)

    backend.add_image(identity.instance_image_key, image_id="sha256:new")
    refreshed = adapter.ensure(identity)
    assert refreshed.cache_state == "revalidated"
    assert refreshed.image_id == "sha256:new"
    assert backend.pull_calls == []
    entries = _cache_payload(tmp_path)["entries"]
    assert entries[identity.environment_hash]["image_id"] == "sha256:new"


def test_ensure_drops_stale_cache_when_image_disappears(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    key = identity.instance_image_key
    backend.add_image(key, image_id="sha256:gone")
    adapter = make_adapter(tmp_path, backend)
    adapter.ensure(identity)
    del backend.images[key]

    with pytest.raises(SWEBenchEnvironmentError) as caught:
        adapter.ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "镜像不存在" in str(caught.value)
    assert identity.environment_hash not in _cache_payload(tmp_path)["entries"]


def test_ensure_ignores_corrupted_cache_with_warning(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key)
    adapter = make_adapter(tmp_path, backend)
    _cache_path(tmp_path).parent.mkdir(parents=True)
    _cache_path(tmp_path).write_text("{broken", encoding="utf-8")

    handle = adapter.ensure(identity)
    assert handle.cache_state == "miss"
    assert any("损坏" in warning for warning in handle.warnings)
    # 缓存已修复成合法 JSON。
    assert identity.environment_hash in _cache_payload(tmp_path)["entries"]


def test_ensure_cache_scope_mismatch_is_ignored(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key)
    adapter = make_adapter(tmp_path, backend)
    _cache_path(tmp_path).parent.mkdir(parents=True)
    _cache_path(tmp_path).write_text(
        json.dumps({"schema_version": 1, "harness_version": "0.0.0", "entries": {}}),
        encoding="utf-8",
    )
    handle = adapter.ensure(identity)
    assert handle.cache_state == "miss"
    assert any("harness 版本不匹配" in warning for warning in handle.warnings)


def test_ensure_unwritable_cache_does_not_block_environment(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key, image_id="sha256:ok")
    cache_dir = tmp_path / "cache-is-a-file"
    cache_dir.write_text("not a directory", encoding="utf-8")
    adapter = SWEBenchEnvironmentAdapter(
        docker=backend,
        spec_snapshot=SNAPSHOT,
        cache_dir=cache_dir,
        platform=LINUX,
    )
    handle = adapter.ensure(identity)
    assert handle.image_id == "sha256:ok"
    assert any("写入失败" in warning for warning in handle.warnings)
    # 权限/路径问题不改变环境可用性，也不抛异常。
    assert handle.from_cache is True


def test_cache_directory_with_spaces_and_windows_path_works(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key)
    cache_dir = tmp_path / "deep dir" / "cache"
    adapter = SWEBenchEnvironmentAdapter(
        docker=backend,
        spec_snapshot=SNAPSHOT,
        cache_dir=cache_dir,
        platform=LINUX,
    )
    handle = adapter.ensure(identity)
    assert handle.cache_state == "miss"
    assert (cache_dir / CACHE_FILENAME).is_file()


# ===========================================================================
# D. Docker 不可用 / 镜像缺失 / 拉取失败 / 资源不足 / 权限不足
# ===========================================================================


def test_preflight_reports_docker_daemon_unavailable(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    backend.daemon_result = False
    report = make_adapter(tmp_path, backend).preflight()
    assert report.ok is False
    assert report.daemon_ok is False
    assert report.linux_containers is False
    assert any("daemon 不可用" in problem for problem in report.problems)

    no_backend = make_adapter(tmp_path, None).preflight()
    assert no_backend.ok is False
    assert any("未配置 DockerBackend" in problem for problem in no_backend.problems)


def test_ensure_without_backend_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, None).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "DockerBackend" in str(caught.value)


def test_ensure_daemon_ping_false_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.daemon_result = False
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "ping" in str(caught.value)
    assert backend.pull_calls == []


def test_ensure_daemon_connection_error_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.daemon_error = DockerDaemonUnavailableError("cannot connect to the Docker daemon")
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "cannot connect" in str(caught.value)


def test_ensure_permission_denied_is_env_error(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.daemon_error = DockerPermissionDeniedError(
        "permission denied while trying to connect to the Docker daemon socket"
    )
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).ensure(identity)
    assert caught.value.kind == "ENV_ERROR"
    assert "permission denied" in str(caught.value)
    report = make_adapter(tmp_path, backend).preflight()
    assert any("ENV_ERROR" in problem for problem in report.problems)


def test_ensure_windows_container_mode_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.ostype_result = "windows"
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "Linux 容器" in str(caught.value)


def test_ensure_unknown_container_mode_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.ostype_result = None
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"


def test_ensure_missing_image_without_allow_build_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "allow_build=False" in str(caught.value)
    assert backend.pull_calls == []
    assert backend.build_calls == []


def test_ensure_missing_image_with_allow_build_pulls_only(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    handle = make_adapter(tmp_path, backend, allow_build=True).ensure(identity)
    assert handle.cache_state == "pulled"
    assert handle.from_cache is False
    assert backend.pull_calls == [identity.instance_image_key]
    assert backend.build_calls == []  # 官方构建 deferred，绝不本地 build


def test_ensure_pull_failure_is_build_failed(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.pull_error = DockerBackendError("registry returned 500")
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend, allow_build=True).ensure(identity)
    assert caught.value.kind == "BUILD_FAILED"
    assert "500" in str(caught.value)


def test_ensure_pull_out_of_space_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.pull_error = DockerResourceExhaustedError("no space left on device")
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend, allow_build=True).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "no space left" in str(caught.value)


def test_ensure_pull_with_invalid_architecture_is_build_failed(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    original_pull = backend.pull

    def pulling_wrong_arch(ref: str) -> ImageInfo:
        original_pull(ref)
        backend.add_image(ref, image_id="sha256:wrong-arch", architecture="arm64")
        return backend.images[ref]

    backend.pull = pulling_wrong_arch  # type: ignore[method-assign]
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend, allow_build=True).ensure(identity)
    assert caught.value.kind == "BUILD_FAILED"
    assert "校验失败" in str(caught.value)


def test_ensure_invalid_existing_image_without_build_is_resource_blocked(
    tmp_path: Path,
) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key, architecture="arm64")
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "校验失败" in str(caught.value)


def test_ensure_disk_limit_blocks_before_image_operations(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key)
    backend.disk_free = 1.5
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend, disk_limit_gb=10.0).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "磁盘余量不足" in str(caught.value)
    assert backend.pull_calls == []


def test_ensure_unknown_disk_with_limit_is_resource_blocked(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key)
    backend.disk_free = None
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend, disk_limit_gb=10.0).ensure(identity)
    assert caught.value.kind == "RESOURCE_BLOCKED"
    assert "磁盘余量" in str(caught.value)


def test_ensure_rejects_tampered_identity(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key)
    adapter = make_adapter(tmp_path, backend)

    tampered_hash = identity.model_copy(update={"environment_hash": "0" * 64})
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        adapter.ensure(tampered_hash)
    assert caught.value.kind == "ENV_ERROR"
    assert "environment_hash" in str(caught.value)

    tampered_key = identity.model_copy(
        update={"instance_image_key": "sweb.eval.x86_64.other__repo-1:latest"}
    )
    with pytest.raises(SWEBenchEnvironmentError) as key_error:
        adapter.ensure(tampered_key)
    assert key_error.value.kind == "ENV_ERROR"
    assert "instance_image_key" in str(key_error.value)

    wrong_harness = identity.model_copy(update={"harness_version": "4.0.0"})
    with pytest.raises(SWEBenchEnvironmentError) as harness_error:
        adapter.ensure(wrong_harness)
    assert harness_error.value.kind == "ENV_ERROR"
    assert "harness 版本" in str(harness_error.value)


# ===========================================================================
# E. 清理：只动自家资源、幂等、不删镜像/别的项目资源
# ===========================================================================


def _container(container_id: str, labels: Mapping[str, str]) -> ContainerInfo:
    return ContainerInfo(container_id=container_id, labels=dict(labels))


def _seed_containers(backend: FakeDockerBackend) -> None:
    backend.containers = {
        "own-1": _container("own-1", {"codepilot": "1", "codepilot.role": "swebench-env"}),
        "own-2": _container("own-2", {"codepilot": "1", "codepilot.role": "swebench-env"}),
        "other-project": _container("other-project", {"project": "someone-else"}),
        "other-codepilot": _container(
            "other-codepilot", {"codepilot": "1", "codepilot.role": "phase5"}
        ),
    }


def test_cleanup_owned_only_removes_own_containers(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key, image_id="sha256:keep")
    images_before = dict(backend.images)
    _seed_containers(backend)
    adapter = make_adapter(tmp_path, backend)

    removed = adapter.cleanup_owned()
    assert removed == ("own-1", "own-2")
    assert sorted(backend.remove_calls) == ["own-1", "own-2"]
    assert set(backend.containers) == {"other-project", "other-codepilot"}
    assert backend.images == images_before  # 镜像一个都没动
    assert backend.pull_calls == []
    assert backend.build_calls == []


def test_cleanup_owned_is_idempotent(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    _seed_containers(backend)
    adapter = make_adapter(tmp_path, backend)
    assert adapter.cleanup_owned() == ("own-1", "own-2")
    assert adapter.cleanup_owned() == ()


def test_cleanup_owned_with_no_own_containers_is_noop(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    backend.containers = {
        "other-project": _container("other-project", {"project": "someone-else"}),
    }
    adapter = make_adapter(tmp_path, backend)
    assert adapter.cleanup_owned() == ()
    assert backend.remove_calls == []
    assert set(backend.containers) == {"other-project"}


def test_cleanup_owned_daemon_error_attribution(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    backend.daemon_result = False
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).cleanup_owned()
    assert caught.value.kind == "RESOURCE_BLOCKED"

    backend = FakeDockerBackend()
    backend.daemon_error = DockerPermissionDeniedError("permission denied on docker.sock")
    with pytest.raises(SWEBenchEnvironmentError) as permission:
        make_adapter(tmp_path, backend).cleanup_owned()
    assert permission.value.kind == "ENV_ERROR"


def test_cleanup_owned_failure_is_env_error_and_keeps_other_resources(
    tmp_path: Path,
) -> None:
    backend = FakeDockerBackend()
    _seed_containers(backend)
    backend.fail_remove = {"own-1"}
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        make_adapter(tmp_path, backend).cleanup_owned()
    assert caught.value.kind == "ENV_ERROR"
    assert "未触碰其他项目资源" in str(caught.value)
    assert set(backend.containers) == {"other-project", "other-codepilot", "own-1"}


def test_cleanup_never_has_prune_or_image_removal_api() -> None:
    # 设计约束：后端抽象刻意不提供 prune / remove_image，避免误删其他项目镜像。
    assert not hasattr(DockerBackend, "prune")
    assert not hasattr(DockerBackend, "remove_image")


# ===========================================================================
# F. 平台 / WSL 归因
# ===========================================================================


def test_preflight_windows_native_marks_harness_not_runnable(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    identity = make_identity(tmp_path, backend)
    backend.add_image(identity.instance_image_key)
    report = make_adapter(tmp_path, backend, platform=WINDOWS_NO_WSL).preflight(
        required_instance="astropy__astropy-12907"
    )
    assert report.windows_native is True
    assert report.harness_runnable is False
    assert report.harness_version_ok is True
    assert report.image_present is True
    assert report.ok is True  # 环境镜像本身可以在 Windows + Linux 容器下复用
    assert any("resource" in warning for warning in report.warnings)
    assert any("WSL2/Linux" in warning for warning in report.warnings)


def test_preflight_windows_with_wsl_mentions_wsl_execution(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    backend.add_image("sweb.eval.x86_64.astropy__astropy-12907:latest")
    report = make_adapter(tmp_path, backend, platform=WINDOWS_WSL).preflight(
        required_instance="astropy__astropy-12907"
    )
    assert report.wsl_available is True
    assert report.harness_runnable is False
    assert any("WSL" in warning for warning in report.warnings)
    assert not any("WSL2/Linux" in warning for warning in report.warnings)


def test_preflight_linux_platform_is_harness_runnable(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    report = make_adapter(tmp_path, backend, platform=LINUX).preflight()
    assert report.ok is True
    assert report.platform == "Linux x86_64"
    assert report.harness_runnable is True
    assert report.warnings == ()


def test_preflight_required_instance_missing_image_is_reported(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    backend.add_image(
        "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest", image_id="sha256:bad"
    )
    report = make_adapter(tmp_path, backend).preflight(required_instance="astropy__astropy-9999")
    assert report.image_present is False
    assert report.image_key == "swebench/sweb.eval.x86_64.astropy_1776_astropy-9999:latest"
    assert any("镜像不存在" in problem for problem in report.problems)
    assert report.ok is False


def test_instance_image_key_lowercases_and_rejects_bad_ids() -> None:
    # 官方默认 namespace=swebench（远端预构建镜像）；`__` 会被替换成 `_1776_`。
    assert instance_image_key("Astropy__Astropy-12907") == (
        "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    )
    # `-n none` 的本地构建名（官方 TestSpec 同一规则的无 namespace 形态）。
    assert instance_image_key("Astropy__Astropy-12907", namespace=None) == (
        "sweb.eval.x86_64.astropy__astropy-12907:latest"
    )
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        instance_image_key("../escape")
    assert caught.value.kind == "ENV_ERROR"


# ===========================================================================
# G. Sandbox 工厂：只传 EnvSpec，不绕过 DockerSandbox
# ===========================================================================


def test_sandbox_factory_delegates_env_spec_without_bypassing_sandbox(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    adapter = make_adapter(tmp_path, backend)
    identity = adapter.identity(make_flask_task())
    sandbox = FakeSandbox()
    factory = SWEBenchSandboxFactory(sandbox=sandbox, spec=adapter.spec)

    plan = factory.plan(identity)
    assert "official_image_reuse" in plan.deferred
    assert plan.env_spec.install[-1] == "python -m pip install -e ."
    assert plan.env_spec.install[1].startswith("python -m pip install setuptools==70.0.0")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = factory.build_env(identity=identity, workspace=workspace, allow_network=False)
    assert result.status is ExecutionStatus.PASS
    assert len(sandbox.build_calls) == 1
    call = sandbox.build_calls[0]
    assert set(call) == {"repository", "base_commit", "workspace", "env_spec", "allow_network"}
    assert call["repository"] == "pallets/flask"
    assert call["base_commit"] == BASE_COMMIT
    assert call["env_spec"] == plan.env_spec
    assert call["allow_network"] is False


def test_sandbox_factory_without_sandbox_is_env_error(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    adapter = make_adapter(tmp_path, backend)
    identity = adapter.identity(make_flask_task())
    factory = SWEBenchSandboxFactory(spec=adapter.spec)
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        factory.build_env(identity=identity, workspace=tmp_path)
    assert caught.value.kind == "ENV_ERROR"
    assert "未注入 DockerSandbox" in str(caught.value)


def test_sandbox_factory_unsupported_env_is_unsupported(tmp_path: Path) -> None:
    backend = FakeDockerBackend()
    adapter = make_adapter(tmp_path, backend)
    identity = adapter.identity(make_task())  # astropy 5.1 → python 3.9
    factory = SWEBenchSandboxFactory(sandbox=FakeSandbox(), spec=adapter.spec)
    with pytest.raises(SWEBenchEnvironmentError) as caught:
        factory.plan(identity)
    assert caught.value.kind == "UNSUPPORTED"


# ===========================================================================
# H. docker SDK 真实后端的离线映射（Fake SDK client，无 daemon）
# ===========================================================================


class _FakeSdkImage:
    def __init__(self, attrs: dict[str, Any]) -> None:
        self.attrs = attrs
        self.id = attrs["Id"]
        self.tags = tuple(attrs.get("RepoTags") or ())


class _FakeSdkImages:
    def __init__(
        self,
        *,
        image: _FakeSdkImage | None = None,
        get_error: Exception | None = None,
        pull_error: Exception | None = None,
    ) -> None:
        self.image = image
        self.get_error = get_error
        self.pull_error = pull_error

    def get(self, ref: str) -> _FakeSdkImage:
        if self.get_error is not None:
            raise self.get_error
        if self.image is None:
            raise ImageNotFound(ref)
        return self.image

    def pull(self, ref: str) -> _FakeSdkImage:
        if self.pull_error is not None:
            raise self.pull_error
        assert self.image is not None
        return self.image

    def list(self, *, filters: Any = None) -> list[_FakeSdkImage]:
        return [] if self.image is None else [self.image]

    def build(self, **kwargs: Any) -> tuple[_FakeSdkImage, list[str]]:
        return _FakeSdkImage({"Id": "sha256:built", "RepoTags": [kwargs["tag"]]}), []


class _FakeSdkContainer:
    def __init__(self, container_id: str, labels: Mapping[str, str]) -> None:
        self.id = container_id
        self.attrs = {"Id": container_id, "Config": {"Labels": dict(labels)}}

    def remove(self, *, force: bool = True) -> None:
        return None

    def reload(self) -> None:
        return None


class _FakeSdkContainers:
    def __init__(
        self,
        *,
        containers: list[_FakeSdkContainer] | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.containers = containers or []
        self.get_error = get_error

    def list(self, *, all: bool = True, filters: Any = None) -> list[_FakeSdkContainer]:
        return list(self.containers)

    def get(self, identifier: str) -> _FakeSdkContainer:
        if self.get_error is not None:
            raise self.get_error
        return _FakeSdkContainer(identifier, {})


class _FakeSdkClient:
    def __init__(self, *, images: _FakeSdkImages, containers: _FakeSdkContainers) -> None:
        self.images = images
        self.containers = containers
        self.ping_error: Exception | None = None
        self.info_value: dict[str, Any] = {"OSType": "linux"}

    def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def info(self) -> dict[str, Any]:
        return self.info_value


def _sdk_image_attrs() -> dict[str, Any]:
    return {
        "Id": "sha256:deadbeef",
        "Os": "linux",
        "Architecture": "amd64",
        "RepoTags": ["sweb.eval.x86_64.astropy__astropy-12907:latest"],
        "Config": {"Labels": {"org": "swebench"}},
    }


def test_sdk_backend_maps_image_facts() -> None:
    client = _FakeSdkClient(
        images=_FakeSdkImages(image=_FakeSdkImage(_sdk_image_attrs())),
        containers=_FakeSdkContainers(),
    )
    backend = DockerSdkBackend(client)
    assert backend.daemon_ok() is True
    assert backend.ostype() == "linux"
    info = backend.inspect_image("sweb.eval.x86_64.astropy__astropy-12907:latest")
    assert info is not None
    assert info.image_id == "sha256:deadbeef"
    assert info.os == "linux"
    assert info.architecture == "amd64"
    assert info.repo_tags == ("sweb.eval.x86_64.astropy__astropy-12907:latest",)
    assert info.labels == {"org": "swebench"}


def test_sdk_backend_maps_permission_denied() -> None:
    client = _FakeSdkClient(images=_FakeSdkImages(), containers=_FakeSdkContainers())
    client.ping_error = DockerException("permission denied while connecting to docker.sock")
    with pytest.raises(DockerPermissionDeniedError):
        DockerSdkBackend(client).daemon_ok()


def test_sdk_backend_maps_no_space_pull_failure() -> None:
    client = _FakeSdkClient(
        images=_FakeSdkImages(pull_error=DockerException("no space left on device")),
        containers=_FakeSdkContainers(),
    )
    with pytest.raises(DockerResourceExhaustedError):
        DockerSdkBackend(client).pull("sweb.eval.x86_64.astropy__astropy-12907:latest")


def test_sdk_backend_maps_daemon_connection_failure() -> None:
    client = _FakeSdkClient(images=_FakeSdkImages(), containers=_FakeSdkContainers())
    client.ping_error = DockerException("Cannot connect to the Docker daemon")
    with pytest.raises(DockerDaemonUnavailableError):
        DockerSdkBackend(client).daemon_ok()


def test_sdk_backend_missing_container_returns_false() -> None:
    client = _FakeSdkClient(
        images=_FakeSdkImages(),
        containers=_FakeSdkContainers(get_error=NotFound("no such container")),
    )
    assert DockerSdkBackend(client).remove_container("ghost") is False


def test_sdk_backend_maps_windows_npipe_winerror5_to_permission_denied() -> None:
    class _WinError(Exception):
        winerror = 5

    error = DockerException("Error while fetching server API version: (5, 'CreateFile', ...)")
    error.__cause__ = _WinError("(5, 'CreateFile', '拒绝访问。')")
    client = _FakeSdkClient(images=_FakeSdkImages(), containers=_FakeSdkContainers())
    client.ping_error = error
    with pytest.raises(DockerPermissionDeniedError):
        DockerSdkBackend(client).daemon_ok()


def test_sdk_backend_disk_free_falls_back_to_existing_ancestor(tmp_path: Path) -> None:
    value = DockerSdkBackend(
        _FakeSdkClient(images=_FakeSdkImages(), containers=_FakeSdkContainers())
    ).disk_free_gb(tmp_path / "missing" / "child")
    assert value is not None
    assert value > 0


# ===========================================================================
# I. 唯一真实 Docker 用例：只读预检查（daemon 不可用时 conftest 自动 skip）
# ===========================================================================


@pytest.mark.docker
def test_real_docker_daemon_preflight_is_observable(tmp_path: Path) -> None:
    backend = DockerSdkBackend()
    adapter = SWEBenchEnvironmentAdapter(
        docker=backend,
        spec_snapshot=SNAPSHOT,
        cache_dir=tmp_path / "cache",
    )
    report = adapter.preflight()
    assert report.daemon_ok is True
    assert report.container_os == "linux"
    assert report.linux_containers is True
    assert report.harness_version_ok is True
    assert report.disk_free_gb is not None and report.disk_free_gb > 0
    assert report.ok is True
    assert report.image_present is None  # 未指定 required_instance，不检查镜像
    assert "official_env_image_build" in report.deferred
    # 只读：本用例没有调用 pull/build/容器操作。
