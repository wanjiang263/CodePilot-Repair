"""官方 SWE-bench 4.1.0 环境规格、镜像身份、缓存与 Docker 适配（E-14 / 契约 §5）。

为什么单独一个模块、为什么只读冻结快照：

* ``swebench==4.1.0`` 在原生 Windows 上不可 import：``swebench.harness.prepare_images``
  顶层 ``import resource``（Unix-only）。生产代码因此**绝不**顶层 import swebench，
  只读 ``configs/swebench/env-specs-4.1.0.json``（``MAP_REPO_VERSION_TO_SPECS`` 的
  冻结副本）。
* 官方 image key 由 ``TestSpec`` 决定。instance 镜像名可离线精确计算：
  ``sweb.eval.x86_64.<instance_id.lower()>:latest``；base 镜像名在 Python 仓库上为
  ``sweb.base.py.x86_64:latest``（规格带 ``docker_specs`` 时追加 10 位 hash）。
  env 镜像名里的 22 位 hash 来自 ``make_env_script_list``，对 requirements.txt /
  environment.yml 规格会联网抓取仓库文件，离线无法复算，所以本模块给 env 镜像一个
  **确定性候选键**，并在 ``DEFERRED_CAPABILITIES`` 里明确标注"官方 env/base 镜像
  构建与复用未实现"。任何缓存/预检查都不把该候选键当作镜像存在性依据。

错误种类（所有对外异常都是 :class:`SWEBenchEnvironmentError`，``kind`` 固定四值）：

* ``ENV_ERROR``：配置/身份/平台/权限等确定性错误（含原生 Windows 执行官方 harness）。
* ``RESOURCE_BLOCKED``：外部资源不可用或不足（Docker daemon、Linux 容器模式、磁盘
  余量、镜像缺失且 ``allow_build=False``、WSL 不可用）。
* ``UNSUPPORTED``：官方规格不存在 / 非 Python 仓库 / EnvSpec 后端不支持
  （如官方 python 3.9、conda environment.yml、非 pytest 的 test_cmd）。
* ``BUILD_FAILED``：``allow_build=True`` 后拉取/构建失败，或产出的镜像校验不通过。

本模块刻意没有 "删除镜像" / "prune" 能力：SWE-bench 适配只允许复用镜像、只清理
自己打了 ``codepilot=1`` + ``codepilot.role=swebench-env`` 标签的临时容器，绝不删除
其他项目的资源。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from docker.errors import DockerException, ImageNotFound, NotFound
from pydantic import BaseModel, ConfigDict, ValidationError

import docker
from codepilot.observability.redaction import redact_text
from codepilot.sandbox.base import EnvironmentBuildResult, Sandbox
from codepilot.sandbox.env_spec import EnvSpec, EnvSpecError, from_swebench_spec

__all__ = [
    "ARCH",
    "CACHE_FILENAME",
    "DEFAULT_SPEC_SNAPSHOT",
    "DEFERRED_CAPABILITIES",
    "HARNESS_PACKAGE",
    "HARNESS_VERSION",
    "OFFICIAL_NAMESPACE",
    "ContainerInfo",
    "DockerBackend",
    "DockerBackendError",
    "DockerDaemonUnavailableError",
    "DockerPermissionDeniedError",
    "DockerResourceExhaustedError",
    "DockerSdkBackend",
    "EnvironmentHandle",
    "ImageInfo",
    "InstanceImageOverride",
    "PlatformInfo",
    "PreflightReport",
    "ResolvedInstanceImage",
    "SWEBenchEnvironmentAdapter",
    "SWEBenchEnvironmentError",
    "SWEBenchEnvironmentIdentity",
    "SWEBenchEnvironmentKind",
    "SWEBenchEnvironmentSpec",
    "SWEBenchInstanceImageResolver",
    "SWEBenchSandboxFactory",
    "SWEBenchSandboxPlan",
    "SWEBenchTaskLike",
    "instance_image_key",
]

HARNESS_PACKAGE = "swebench"
HARNESS_VERSION = "4.1.0"
ARCH = "x86_64"
IMAGE_TAG = "latest"
#: 官方 4.1.0 CLI 的 `-n/--namespace` 默认值（`harness/run_evaluation.py`）。非空
#: namespace 意味着实例镜像是**远端镜像**（官方在 Docker Hub 上预构建），镜像名会带
#: `<namespace>/` 前缀、且实例 id 里的 `__` 被替换成 `_1776_`；`-n none` 才是本地构建
#: 名。preflight 查的必须是官方真正会用的那个名字，否则"镜像存在/缺失"的结论与官方
#: 执行不一致。
OFFICIAL_NAMESPACE = "swebench"
#: 官方 `instance_image_key` 对 `__` 的替换标记（`test_spec.py::instance_image_key`）。
_NAMESPACE_SEPARATOR = "_1776_"

DEFAULT_SPEC_SNAPSHOT = (
    Path(__file__).resolve().parents[3] / "configs" / "swebench" / "env-specs-4.1.0.json"
)

CACHE_SCHEMA_VERSION = 1
CACHE_FILENAME = "swebench-env-cache.json"

#: 只清理本适配器角色标注的容器；镜像永不删除。
_LABEL_OWNER = "codepilot"
_LABEL_ROLE = "codepilot.role"
_SWEEBENCH_ROLE = "swebench-env"

_COMMIT_HEX = re.compile(r"^[0-9a-f]{40}$")
_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: Phase 11 明确没做的能力；调用方必须把这些当作能力缺口，而不是"已经支持"。
DEFERRED_CAPABILITIES: tuple[str, ...] = (
    "official_env_image_build",
    "official_env_image_reuse",
    "official_instance_image_build",
    "official_harness_execution_on_windows",
)

_IMAGE_OS = "linux"
_IMAGE_ARCHITECTURES = frozenset({"amd64", "x86_64"})
_PYTHON_SUPPORTED_BY_ENV_SPEC = frozenset({"3.10", "3.11", "3.12", "3.13"})


class SWEBenchEnvironmentKind(StrEnum):
    """契约 §5 的四种环境错误归因。"""

    ENV_ERROR = "ENV_ERROR"
    RESOURCE_BLOCKED = "RESOURCE_BLOCKED"
    UNSUPPORTED = "UNSUPPORTED"
    BUILD_FAILED = "BUILD_FAILED"


class SWEBenchEnvironmentError(RuntimeError):
    """带归因种类的环境适配错误；``kind`` 供调用方做分支与统计。"""

    kind: str

    def __init__(self, message: str, *, kind: SWEBenchEnvironmentKind | str) -> None:
        normalized = kind.value if isinstance(kind, SWEBenchEnvironmentKind) else str(kind)
        if normalized not in {item.value for item in SWEBenchEnvironmentKind}:
            raise ValueError(f"非法 SWEBenchEnvironmentError.kind: {normalized!r}")
        super().__init__(message)
        self.kind = normalized


class DockerBackendError(RuntimeError):
    """Docker 后端不可用或操作失败；由适配器映射成契约错误种类。"""


class DockerDaemonUnavailableError(DockerBackendError):
    """daemon 没有运行 / 连不上：资源不可用。"""


class DockerPermissionDeniedError(DockerBackendError):
    """权限不足（socket/npipe 拒绝访问）：确定性环境错误。"""


class DockerResourceExhaustedError(DockerBackendError):
    """磁盘/资源耗尽：资源不足。"""


class PlatformInfo(BaseModel):
    """执行平台事实；注入后测试无需依赖真实宿主机。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    machine: str
    wsl_available: bool = False

    @classmethod
    def detect(cls) -> PlatformInfo:
        return cls(
            system=platform_module.system(),
            machine=platform_module.machine(),
            wsl_available=shutil.which("wsl") is not None,
        )

    @property
    def windows_native(self) -> bool:
        return self.system.strip().lower() == "windows"

    @property
    def harness_runnable(self) -> bool:
        """官方 harness 依赖 Unix 的 ``resource`` 模块；原生 Windows 不可执行。"""
        return not self.windows_native


@dataclass(frozen=True)
class ImageInfo:
    """Docker 镜像的最小事实集合（无 daemon 测试用 Fake 构造）。"""

    ref: str
    image_id: str
    os: str
    architecture: str
    repo_tags: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContainerInfo:
    """Docker 容器的最小事实集合（清理逻辑只依赖 id 与标签）。"""

    container_id: str
    labels: Mapping[str, str] = field(default_factory=dict)


class DockerBackend(ABC):
    """Docker 后端抽象。

    刻意不提供 ``prune`` / ``remove_image``：适配器只允许复用镜像、清理自己标注的
    临时容器，绝不删除镜像（尤其是其他项目的镜像）。
    """

    @abstractmethod
    def daemon_ok(self) -> bool:
        """daemon 是否可响应；连不上时抛 :class:`DockerDaemonUnavailableError`。"""

    @abstractmethod
    def ostype(self) -> str:
        """容器模式（``linux`` / ``windows``）。"""

    @abstractmethod
    def list_images(self, *, labels: Mapping[str, str] | None = None) -> tuple[ImageInfo, ...]:
        """列出镜像；只读，不做任何清理。"""

    @abstractmethod
    def inspect_image(self, ref: str) -> ImageInfo | None:
        """读取镜像事实；镜像不存在返回 ``None``。"""

    @abstractmethod
    def build(
        self,
        *,
        dockerfile: str,
        tag: str,
        context: Path,
        labels: Mapping[str, str],
    ) -> str:
        """构建镜像并返回 image id；Phase 11 未调用（官方构建 deferred）。"""

    @abstractmethod
    def pull(self, ref: str) -> ImageInfo:
        """拉取镜像；Phase 11 仅 Fake 验收，不做真实拉取。"""

    @abstractmethod
    def disk_free_gb(self, path: Path) -> float | None:
        """``path`` 所在卷的剩余空间（GiB）；无法确认返回 ``None``。"""

    @abstractmethod
    def list_containers(self, *, labels: Mapping[str, str]) -> tuple[ContainerInfo, ...]:
        """按标签列出容器（清理前查询）。"""

    @abstractmethod
    def remove_container(self, identifier: str) -> bool:
        """删除单个容器；容器已不存在返回 ``False``。"""


def _map_docker_error(exc: BaseException) -> DockerBackendError:
    """把 docker SDK / OS 异常映射成可归因的后端错误。

    只看顶层异常字符串不够：Windows 上 ``docker.from_env()`` 把 npipe 的
    ``pywintypes.error(winerror=5, '拒绝访问')`` 包成 ``DockerException``，中文
    Windows 的错误文本是"拒绝访问"，所以还要沿 ``__cause__``/``__context__``
    链检查 winerror 5。
    """
    message = redact_text(str(exc)) or exc.__class__.__name__
    lowered = message.lower()
    chain = _exception_chain(exc)
    if any(
        isinstance(item, PermissionError) or getattr(item, "winerror", None) == 5 for item in chain
    ) or any(
        marker in lowered
        for marker in (
            "permission denied",
            "access is denied",
            "access denied",
            "forbidden",
            "拒绝访问",
        )
    ):
        return DockerPermissionDeniedError(message)
    if any(
        marker in lowered
        for marker in ("no space left", "disk full", "not enough space", "insufficient storage")
    ):
        return DockerResourceExhaustedError(message)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(
        exc, PermissionError
    ):
        return DockerDaemonUnavailableError(message)
    if any(
        marker in lowered
        for marker in (
            "cannot connect",
            "is the docker daemon running",
            "error while fetching server api version",
            "connection aborted",
            "connection refused",
            "docker_engine",
            "npipe",
        )
    ):
        return DockerDaemonUnavailableError(message)
    return DockerBackendError(message)


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            break
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _disk_free_gb(path: Path) -> float | None:
    """返回 ``path``（或最近的已存在父目录）所在卷的剩余 GiB。"""
    for candidate in (Path(path), *Path(path).parents):
        if not candidate.exists():
            continue
        try:
            return round(shutil.disk_usage(candidate).free / (1024**3), 3)
        except OSError:
            return None
    return None


def _image_info(image: Any, *, fallback_ref: str) -> ImageInfo:
    """从 docker SDK Image 或 Fake 对象提取 :class:`ImageInfo`。"""
    attrs_raw = getattr(image, "attrs", None)
    attrs: Mapping[str, Any] = attrs_raw if isinstance(attrs_raw, Mapping) else {}
    config_raw = attrs.get("Config")
    config: Mapping[str, Any] = config_raw if isinstance(config_raw, Mapping) else {}
    labels_raw = config.get("Labels")
    labels = (
        {str(key): str(value) for key, value in labels_raw.items()}
        if isinstance(labels_raw, Mapping)
        else {}
    )
    tags_raw = attrs.get("RepoTags")
    if not isinstance(tags_raw, Sequence) or isinstance(tags_raw, (str, bytes)):
        tags_fallback = getattr(image, "tags", None)
        tags_raw = tags_fallback if isinstance(tags_fallback, Sequence) else ()
    repo_tags = tuple(str(tag) for tag in tags_raw)
    image_id = attrs.get("Id") or getattr(image, "id", "")
    return ImageInfo(
        ref=fallback_ref,
        image_id=str(image_id),
        os=str(attrs.get("Os") or ""),
        architecture=str(attrs.get("Architecture") or ""),
        repo_tags=repo_tags,
        labels=labels,
    )


class DockerSdkBackend(DockerBackend):
    """docker SDK 真实实现；``client`` 可注入以便无 daemon 单测。"""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _sdk(self) -> Any:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except (DockerException, OSError, PermissionError) as exc:
                raise _map_docker_error(exc) from exc
        return self._client

    def daemon_ok(self) -> bool:
        try:
            return bool(self._sdk().ping())
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc

    def ostype(self) -> str:
        try:
            info = self._sdk().info()
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        ostype = info.get("OSType") if isinstance(info, Mapping) else None
        return str(ostype or "")

    def list_images(self, *, labels: Mapping[str, str] | None = None) -> tuple[ImageInfo, ...]:
        filters = _label_filters(labels) if labels else None
        try:
            images = self._sdk().images.list(filters=filters)
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        return tuple(
            _image_info(image, fallback_ref=label) for image, label in _iter_images(images)
        )

    def inspect_image(self, ref: str) -> ImageInfo | None:
        try:
            image = self._sdk().images.get(ref)
        except ImageNotFound:
            return None
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        return _image_info(image, fallback_ref=ref)

    def build(
        self,
        *,
        dockerfile: str,
        tag: str,
        context: Path,
        labels: Mapping[str, str],
    ) -> str:
        try:
            image, _logs = self._sdk().images.build(
                path=str(context),
                dockerfile=dockerfile,
                tag=tag,
                labels=dict(labels),
            )
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        return str(getattr(image, "id", tag))

    def pull(self, ref: str) -> ImageInfo:
        try:
            image = self._sdk().images.pull(ref)
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        return _image_info(image, fallback_ref=ref)

    def disk_free_gb(self, path: Path) -> float | None:
        return _disk_free_gb(path)

    def list_containers(self, *, labels: Mapping[str, str]) -> tuple[ContainerInfo, ...]:
        try:
            containers = self._sdk().containers.list(all=True, filters=_label_filters(labels))
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        result: list[ContainerInfo] = []
        for container in containers:
            attrs_raw = getattr(container, "attrs", None)
            attrs: Mapping[str, Any] = attrs_raw if isinstance(attrs_raw, Mapping) else {}
            config_raw = attrs.get("Config")
            config: Mapping[str, Any] = config_raw if isinstance(config_raw, Mapping) else {}
            labels_raw = config.get("Labels")
            parsed = (
                {str(key): str(value) for key, value in labels_raw.items()}
                if isinstance(labels_raw, Mapping)
                else {}
            )
            result.append(
                ContainerInfo(
                    container_id=str(getattr(container, "id", attrs.get("Id", ""))),
                    labels=parsed,
                )
            )
        return tuple(result)

    def remove_container(self, identifier: str) -> bool:
        try:
            container = self._sdk().containers.get(identifier)
        except NotFound:
            return False
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        try:
            container.remove(force=True)
        except NotFound:
            return False
        except (DockerException, OSError, PermissionError) as exc:
            raise _map_docker_error(exc) from exc
        return True


def _label_filters(labels: Mapping[str, str]) -> dict[str, list[str]]:
    return {"label": [f"{key}={value}" for key, value in sorted(labels.items())]}


def _iter_images(images: Any) -> tuple[tuple[Any, str], ...]:
    """给 SDK 镜像补一个 fallback ref（SDK 没给 ref 时用 id 占位）。"""
    if not isinstance(images, Sequence):
        return ()
    result: list[tuple[Any, str]] = []
    for image in images:
        tags_raw = getattr(image, "tags", None)
        ref = str(tags_raw[0]) if isinstance(tags_raw, Sequence) and tags_raw else ""
        if not ref:
            attrs_raw = getattr(image, "attrs", None)
            attrs: Mapping[str, Any] = attrs_raw if isinstance(attrs_raw, Mapping) else {}
            tags = attrs.get("RepoTags")
            ref = (
                str(tags[0])
                if isinstance(tags, Sequence) and tags
                else str(getattr(image, "id", ""))
            )
        result.append((image, ref))
    return tuple(result)


class SWEBenchTaskLike(Protocol):
    """``SWEBenchInferenceTask`` 的结构约束。

    刻意不 import ``codepilot.eval.swebench_task``：Phase 11 各 owner 并行开发，
    环境适配只需要身份字段，避免模块未就绪/循环依赖。
    """

    instance_id: str
    repo: str
    base_commit: str
    version: str
    environment_setup_commit: str

    @property
    def dataset(self) -> Any: ...


class InstanceImageOverride(BaseModel):
    """官方实例镜像的显式替代镜像；必须绑定父镜像与完整镜像 ID。

    替代只在 ``instance_id`` 精确命中时生效。缺失或 ID 不匹配一律 fail closed，
    绝不回退到官方 ``:latest``——那会把一个已知坏环境伪装成可用环境。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    image_ref: str
    image_id: str
    parent_image_ref: str
    parent_image_id: str
    reason: str
    evidence: str | None = None
    validated_at: str | None = None


class ResolvedInstanceImage(BaseModel):
    """一次解析的最终执行镜像；``image_id`` 是必须命中的完整 digest。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str
    image_ref: str
    image_id: str | None = None
    override: bool = False
    parent_image_ref: str | None = None
    parent_image_id: str | None = None
    selection_reason: str | None = None


_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?$")
_OVERRIDE_SCHEMA_VERSION = 1
#: 加入 execution_image_* 后提升；旧身份/缓存按 fail closed 处理，不静默复用。
_IDENTITY_SCHEMA_VERSION = 2


class SWEBenchInstanceImageResolver:
    """实例 → 执行镜像的显式解析器。

    无覆盖项时返回官方 ``instance_image_key``；有覆盖项时返回经审计的替代镜像，
    并把父镜像与完整 ID 一起带出，供执行记录和对账使用。
    """

    def __init__(self, overrides: Mapping[str, InstanceImageOverride] | None = None) -> None:
        self._overrides: dict[str, InstanceImageOverride] = {}
        for key, item in dict(overrides or {}).items():
            if key != item.instance_id:
                raise SWEBenchEnvironmentError(
                    f"镜像覆盖键与 instance_id 不一致：{key!r} != {item.instance_id!r}",
                    kind=SWEBenchEnvironmentKind.ENV_ERROR,
                )
            self._validate_override(item)
            self._overrides[key] = item

    @staticmethod
    def _validate_override(item: InstanceImageOverride) -> None:
        if _INSTANCE_ID.fullmatch(item.instance_id) is None:
            raise SWEBenchEnvironmentError(
                f"镜像覆盖含非法 instance_id: {item.instance_id!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        for name in ("image_ref", "parent_image_ref"):
            value = getattr(item, name)
            if _IMAGE_REF.fullmatch(value) is None:
                raise SWEBenchEnvironmentError(
                    f"镜像覆盖 {item.instance_id} 的 {name} 非法：{value!r}",
                    kind=SWEBenchEnvironmentKind.ENV_ERROR,
                )
        for name in ("image_id", "parent_image_id"):
            value = getattr(item, name)
            if _IMAGE_ID.fullmatch(value) is None:
                raise SWEBenchEnvironmentError(
                    f"镜像覆盖 {item.instance_id} 的 {name} 必须是完整 sha256 digest：{value!r}",
                    kind=SWEBenchEnvironmentKind.ENV_ERROR,
                )
        if item.parent_image_ref != instance_image_key(item.instance_id):
            raise SWEBenchEnvironmentError(
                f"镜像覆盖 {item.instance_id} 的父镜像不是该实例的官方镜像："
                f"{item.parent_image_ref!r} != {instance_image_key(item.instance_id)!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        if not item.reason.strip():
            raise SWEBenchEnvironmentError(
                f"镜像覆盖 {item.instance_id} 缺少原因说明",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )

    @classmethod
    def load(cls, path: Path | None) -> SWEBenchInstanceImageResolver:
        """加载覆盖文件；``None`` 表示只用官方镜像。格式错误一律 fail closed。"""
        if path is None:
            return cls()
        snapshot = Path(path)
        try:
            text = snapshot.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SWEBenchEnvironmentError(
                f"无法读取实例镜像覆盖文件: {snapshot}（{exc}）",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            ) from exc
        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SWEBenchEnvironmentError(
                f"实例镜像覆盖文件不是合法 JSON: {snapshot}（{exc}）",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            ) from exc
        if not isinstance(payload, Mapping):
            raise SWEBenchEnvironmentError(
                f"实例镜像覆盖文件顶层必须是对象: {snapshot}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        if payload.get("schema_version") != _OVERRIDE_SCHEMA_VERSION:
            raise SWEBenchEnvironmentError(
                f"实例镜像覆盖 schema 版本不匹配: {payload.get('schema_version')!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        raw_overrides = payload.get("overrides")
        if not isinstance(raw_overrides, list):
            raise SWEBenchEnvironmentError(
                f"实例镜像覆盖文件缺少 overrides 列表: {snapshot}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        overrides: dict[str, InstanceImageOverride] = {}
        for raw in raw_overrides:
            try:
                item = InstanceImageOverride.model_validate(raw)
            except ValidationError as exc:
                raise SWEBenchEnvironmentError(
                    f"实例镜像覆盖条目无效: {exc}",
                    kind=SWEBenchEnvironmentKind.ENV_ERROR,
                ) from exc
            if item.instance_id in overrides:
                raise SWEBenchEnvironmentError(
                    f"实例镜像覆盖重复: {item.instance_id}",
                    kind=SWEBenchEnvironmentKind.ENV_ERROR,
                )
            overrides[item.instance_id] = item
        return cls(overrides)

    @property
    def overrides(self) -> Mapping[str, InstanceImageOverride]:
        return MappingProxyType(self._overrides)

    def resolve(self, instance_id: str) -> ResolvedInstanceImage:
        official = instance_image_key(instance_id)
        item = self._overrides.get(instance_id)
        if item is None:
            return ResolvedInstanceImage(instance_id=instance_id, image_ref=official)
        return ResolvedInstanceImage(
            instance_id=instance_id,
            image_ref=item.image_ref,
            image_id=item.image_id,
            override=True,
            parent_image_ref=item.parent_image_ref,
            parent_image_id=item.parent_image_id,
            selection_reason=item.reason,
        )


class SWEBenchEnvironmentIdentity(BaseModel):
    """单个任务的官方环境身份；environment_hash 是全部身份字段的 sha256。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 旧身份缺少新字段时保持默认值 1，``ensure`` 会显式拒绝而不是复用旧缓存。
    identity_schema_version: int = 1
    instance_id: str
    repo: str
    base_commit: str
    version: str
    environment_setup_commit: str
    dataset_revision: str
    harness_version: str
    python_version: str
    env_image_key: str
    instance_image_key: str
    base_image_key: str
    execution_image_key: str = ""
    execution_image_id: str | None = None
    execution_image_override: bool = False
    execution_image_selection_reason: str | None = None
    spec_sha256: str
    environment_hash: str


class PreflightReport(BaseModel):
    """环境预检查结果；``problems`` 是环境就绪阻断，``warnings`` 是能力提示。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    harness_version: str
    harness_version_ok: bool
    daemon_ok: bool
    container_os: str | None
    linux_containers: bool
    disk_free_gb: float | None
    disk_limit_gb: float | None
    disk_ok: bool
    platform: str
    windows_native: bool
    wsl_available: bool
    harness_runnable: bool
    required_instance: str | None = None
    image_key: str | None = None
    image_present: bool | None = None
    image_expected_id: str | None = None
    image_override: bool = False
    image_selection_reason: str | None = None
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    deferred: tuple[str, ...] = DEFERRED_CAPABILITIES


class EnvironmentHandle(BaseModel):
    """已就绪的官方实例镜像句柄；``cache_state`` 记录复用来源便于审计。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: SWEBenchEnvironmentIdentity
    image_key: str
    image_id: str
    from_cache: bool
    cache_state: Literal["hit", "revalidated", "miss", "pulled"]
    image_override: bool = False
    expected_image_id: str | None = None
    parent_image_ref: str | None = None
    parent_image_id: str | None = None
    selection_reason: str | None = None
    warnings: tuple[str, ...] = ()


class _CacheEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    image_key: str
    image_id: str
    spec_sha256: str
    validated_at: str


class _EnvironmentCache:
    """``cache_dir/swebench-env-cache.json`` 的原子读写。

    缓存只是加速与审计，**不是**环境正确性的前提：损坏/无权限时忽略并继续向
    Docker 校验，绝不因为缓存问题把可复用的镜像判成失败。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> tuple[dict[str, _CacheEntry], tuple[str, ...]]:
        if not self.path.is_file():
            return {}, ()
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            return {}, (f"环境缓存读取失败，已忽略：{exc}",)
        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError:
            return {}, ("环境缓存文件损坏（JSON 解析失败），已忽略并重新校验镜像",)
        if not isinstance(payload, Mapping):
            return {}, ("环境缓存文件结构无效，已忽略并重新校验镜像",)
        if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            return {}, ("环境缓存 schema 版本不匹配，已忽略并重新校验镜像",)
        if payload.get("harness_version") != HARNESS_VERSION:
            return {}, ("环境缓存 harness 版本不匹配，已忽略并重新校验镜像",)
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, Mapping):
            return {}, ("环境缓存 entries 结构无效，已忽略并重新校验镜像",)
        entries: dict[str, _CacheEntry] = {}
        for identity_hash, raw in raw_entries.items():
            if not isinstance(identity_hash, str):
                continue
            try:
                entries[identity_hash] = _CacheEntry.model_validate(raw)
            except ValidationError:
                continue
        return entries, ()

    def save(self, entries: Mapping[str, _CacheEntry]) -> tuple[str, ...]:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "harness_version": HARNESS_VERSION,
            "entries": {
                identity_hash: entry.model_dump(mode="json")
                for identity_hash, entry in sorted(entries.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            # newline="\n" 让缓存内容在 Windows 上也是 LF，避免 CRLF 改变文件 hash。
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except OSError as exc:
            return (f"环境缓存写入失败（不影响本次环境复用）：{exc}",)
        return ()


class SWEBenchEnvironmentSpec:
    """官方 4.1.0 环境规格的只读解析器；不 import swebench。"""

    def __init__(self, payload: Mapping[str, Any], *, path: Path) -> None:
        self.path = Path(path)
        metadata = _as_str_keyed_mapping(payload, what="规格快照顶层")
        harness_version = metadata.get("harness_version")
        if not isinstance(harness_version, str) or not harness_version.strip():
            raise SWEBenchEnvironmentError(
                f"官方环境规格快照缺少 harness_version: {self.path}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        self.harness_version = harness_version
        harness_package = metadata.get("harness_package")
        if harness_package is not None and harness_package != HARNESS_PACKAGE:
            raise SWEBenchEnvironmentError(
                f"官方环境规格快照的 harness_package 不是 {HARNESS_PACKAGE!r}：{harness_package!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        source = metadata.get("source")
        self.source = source if isinstance(source, str) else ""
        self._specs = _parse_spec_table(metadata.get("specs"), path=self.path)
        self.repo_names = tuple(sorted(self._specs))

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_SPEC_SNAPSHOT,
        *,
        harness_version: str = HARNESS_VERSION,
    ) -> SWEBenchEnvironmentSpec:
        snapshot = Path(path)
        try:
            raw_bytes = snapshot.read_bytes()
        except OSError as exc:
            raise SWEBenchEnvironmentError(
                f"无法读取官方环境规格快照: {snapshot}（{exc}）",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            ) from exc
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SWEBenchEnvironmentError(
                f"官方环境规格快照不是合法 UTF-8: {snapshot}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            ) from exc
        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SWEBenchEnvironmentError(
                f"官方环境规格快照不是合法 JSON: {snapshot}（{exc}）",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            ) from exc
        if not isinstance(payload, Mapping):
            raise SWEBenchEnvironmentError(
                f"官方环境规格快照顶层必须是对象: {snapshot}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        loaded = cls(payload, path=snapshot)
        if loaded.harness_version != harness_version:
            raise SWEBenchEnvironmentError(
                "官方环境规格快照版本与锁定 harness 不一致："
                f"快照={loaded.harness_version!r}，期望={harness_version!r}（{snapshot}）",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        return loaded

    def _raw_spec(self, repo: str, version: str) -> dict[str, Any]:
        if not isinstance(repo, str) or not repo.strip():
            raise SWEBenchEnvironmentError(
                f"非法 repo: {repo!r}", kind=SWEBenchEnvironmentKind.UNSUPPORTED
            )
        if not isinstance(version, str) or not version.strip():
            raise SWEBenchEnvironmentError(
                f"非法 version: {version!r}", kind=SWEBenchEnvironmentKind.UNSUPPORTED
            )
        versions = self._specs.get(repo)
        if versions is None:
            raise SWEBenchEnvironmentError(
                f"冻结快照中没有仓库 {repo!r} 的官方环境规格（4.1.0 共 {len(self._specs)} 个仓库）",
                kind=SWEBenchEnvironmentKind.UNSUPPORTED,
            )
        spec = versions.get(version)
        if spec is None:
            raise SWEBenchEnvironmentError(
                f"冻结快照中没有 {repo}@{version} 的官方环境规格",
                kind=SWEBenchEnvironmentKind.UNSUPPORTED,
            )
        return spec

    def spec_for(self, repo: str, version: str) -> Mapping[str, Any]:
        """返回只读的官方规格映射；repo/version 不存在时 UNSUPPORTED。"""
        return _readonly(self._raw_spec(repo, version))

    def env_spec_for(self, repo: str, version: str) -> EnvSpec:
        """把官方规格转换成 CodePilot :class:`EnvSpec`。

        4.1.0 的 ``pip_packages`` 会并入 ``install``（在仓库安装之前）。官方规格可能
        使用 EnvSpec 后端不支持的 python（3.9 等）或 conda ``environment.yml``，
        这些情况明确 UNSUPPORTED，而不是静默造一个"看起来成功、实则错误"的环境。
        """
        raw = self._raw_spec(repo, version)
        try:
            env_spec = from_swebench_spec(raw)
        except EnvSpecError as exc:
            raise SWEBenchEnvironmentError(
                _env_spec_unsupported_message(repo, version, raw, exc),
                kind=SWEBenchEnvironmentKind.UNSUPPORTED,
            ) from exc
        except ValidationError as exc:
            raise SWEBenchEnvironmentError(
                _env_spec_unsupported_message(repo, version, raw, exc),
                kind=SWEBenchEnvironmentKind.UNSUPPORTED,
            ) from exc
        if not _is_pytest_command(env_spec.test_command):
            raise SWEBenchEnvironmentError(
                f"官方规格 {repo}@{version} 的 test_cmd={raw.get('test_cmd')!r} 不是 pytest；"
                "DockerSandbox 只能从 JUnit XML 读取结果，官方判定必须走官方 harness（deferred）",
                kind=SWEBenchEnvironmentKind.UNSUPPORTED,
            )
        return env_spec

    def requirements(self, repo: str, version: str) -> tuple[str, ...]:
        """返回该 repo/version 需要额外安装的 pip 依赖清单。

        优先 ``pip_packages``；没有时回退 ``packages``：

        * ``requirements*.txt`` → ``("-r requirements.txt",)``；
        * ``environment.yml`` → ``()``（conda 依赖，pip 无法表达，``env_spec_for`` 会 UNSUPPORTED）；
        * 普通包字符串 → 原样返回（官方规格里可能是空格分隔的多个包）。
        """
        raw = self._raw_spec(repo, version)
        pip_packages = raw.get("pip_packages")
        if pip_packages is not None:
            return _string_items(pip_packages, what=f"{repo}@{version} 的 pip_packages")
        packages = raw.get("packages")
        if packages is None:
            return ()
        if isinstance(packages, str):
            stripped = packages.strip()
            if not stripped:
                return ()
            if stripped in {"requirements.txt", "requirements-dev.txt"}:
                return (f"-r {stripped}",)
            if stripped.endswith(("environment.yml", "environment.yaml")):
                return ()
            return (stripped,)
        return _string_items(packages, what=f"{repo}@{version} 的 packages")


class _Blocker(BaseModel):
    """内部阻断项：kind + 中文归因消息。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    message: str


@dataclass(frozen=True)
class _RuntimeProbe:
    daemon_ok: bool
    container_os: str | None
    linux_containers: bool
    disk_free_gb: float | None
    blockers: tuple[_Blocker, ...]


class SWEBenchEnvironmentAdapter:
    """官方镜像身份 + 预检查 + 缓存复用 + 清理。

    真实镜像操作全部经过 :class:`DockerBackend`；本类不直接建容器，也不删除镜像。
    """

    def __init__(
        self,
        *,
        docker: DockerBackend | None,
        spec_snapshot: Path,
        cache_dir: Path,
        allow_build: bool = False,
        disk_limit_gb: float | None = None,
        platform: PlatformInfo | None = None,
        images: SWEBenchInstanceImageResolver | None = None,
    ) -> None:
        if disk_limit_gb is not None and disk_limit_gb < 0:
            raise ValueError("disk_limit_gb 不能为负数")
        self._backend = docker
        self._spec = SWEBenchEnvironmentSpec.load(spec_snapshot)
        self.cache_dir = Path(cache_dir)
        self._cache = _EnvironmentCache(self.cache_dir / CACHE_FILENAME)
        self.allow_build = allow_build
        self.disk_limit_gb = disk_limit_gb
        self._platform = platform if platform is not None else PlatformInfo.detect()
        self._images = images if images is not None else SWEBenchInstanceImageResolver()

    @property
    def spec(self) -> SWEBenchEnvironmentSpec:
        return self._spec

    @property
    def images(self) -> SWEBenchInstanceImageResolver:
        return self._images

    @property
    def platform_info(self) -> PlatformInfo:
        return self._platform

    def identity(self, task: SWEBenchTaskLike) -> SWEBenchEnvironmentIdentity:
        """由推理任务生成官方环境身份；缺字段/非法提交 fail closed。"""
        instance_id = _task_text(task, "instance_id")
        repo = _task_text(task, "repo")
        base_commit = _task_text(task, "base_commit")
        version = _task_text(task, "version")
        environment_setup_commit = _task_text(task, "environment_setup_commit")
        dataset = getattr(task, "dataset", None)
        dataset_revision = getattr(dataset, "revision", None)
        if not isinstance(dataset_revision, str) or not dataset_revision.strip():
            raise SWEBenchEnvironmentError(
                "任务缺少 dataset.revision，无法生成可复现的环境身份",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        if _REPO_NAME.fullmatch(repo) is None or ".." in repo:
            raise SWEBenchEnvironmentError(
                f"非法 repo（必须 owner/name）: {repo!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        for name, value in (
            ("base_commit", base_commit),
            ("environment_setup_commit", environment_setup_commit),
        ):
            if _COMMIT_HEX.fullmatch(value) is None:
                raise SWEBenchEnvironmentError(
                    f"非法 {name}（需要 40 位小写 hex）: {value!r}",
                    kind=SWEBenchEnvironmentKind.ENV_ERROR,
                )
        raw = self._spec._raw_spec(repo, version)
        python_version = raw.get("python")
        if not isinstance(python_version, str) or not python_version.strip():
            raise SWEBenchEnvironmentError(
                f"官方规格 {repo}@{version} 没有 python 字段；Phase 11 只支持 SWE-bench Lite "
                "的 Python 仓库（多语言镜像 deferred）",
                kind=SWEBenchEnvironmentKind.UNSUPPORTED,
            )
        spec_sha256 = _sha256(_canonical_json(raw))
        base_image_key = _base_image_key(raw)
        env_image_key = _candidate_env_image_key(repo, version, raw)
        instance_key = instance_image_key(instance_id)
        resolved_image = self._images.resolve(instance_id)
        payload = _identity_payload(
            identity_schema_version=_IDENTITY_SCHEMA_VERSION,
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            version=version,
            environment_setup_commit=environment_setup_commit,
            dataset_revision=dataset_revision,
            harness_version=self._spec.harness_version,
            python_version=python_version,
            env_image_key=env_image_key,
            instance_image_key=instance_key,
            base_image_key=base_image_key,
            execution_image_key=resolved_image.image_ref,
            execution_image_id=resolved_image.image_id,
            spec_sha256=spec_sha256,
        )
        return SWEBenchEnvironmentIdentity(
            **{**payload, "execution_image_id": resolved_image.image_id},
            execution_image_override=resolved_image.override,
            execution_image_selection_reason=resolved_image.selection_reason,
            environment_hash=_sha256(_canonical_json(payload)),
        )

    def preflight(self, *, required_instance: str | None = None) -> PreflightReport:
        """只读预检查：daemon、Linux 容器模式、磁盘、镜像、平台/WSL、harness 版本。"""
        probe = self._probe_runtime()
        problems = [blocker.message for blocker in probe.blockers]
        warnings: list[str] = []
        windows_native = self._platform.windows_native
        harness_runnable = self._platform.harness_runnable
        if windows_native:
            executable = "WSL" if self._platform.wsl_available else "WSL2/Linux"
            warnings.append(
                "原生 Windows 无法执行官方 harness（swebench 4.1.0 的 prepare_images 顶层 "
                f"import resource）；可经 {executable} 执行（deferred）"
            )
        image_key: str | None = None
        image_present: bool | None = None
        image_expected_id: str | None = None
        image_override = False
        image_selection_reason: str | None = None
        if required_instance is not None:
            resolved = self._images.resolve(required_instance)
            image_key = resolved.image_ref
            image_expected_id = resolved.image_id
            image_override = resolved.override
            image_selection_reason = resolved.selection_reason
            if self._backend is None or not probe.daemon_ok:
                warnings.append(f"未检查镜像 {image_key}：Docker daemon 不可用")
            else:
                try:
                    info = self._backend.inspect_image(image_key)
                except DockerBackendError as exc:
                    problems.append(
                        f"镜像预检查失败（{_kind_for_docker_error(exc)}）：{redact_text(str(exc))}"
                    )
                else:
                    if info is None:
                        image_present = False
                        problems.append(f"镜像不存在：{image_key}")
                    elif not _image_is_valid(info, image_key):
                        image_present = False
                        problems.append(
                            f"镜像存在但校验失败（OS/架构/tag 不匹配，已按失效处理）：{image_key}"
                        )
                    elif image_expected_id is not None and info.image_id != image_expected_id:
                        image_present = False
                        problems.append(
                            "镜像身份不匹配（override 期望 "
                            f"{image_expected_id}，实际 {info.image_id}）：{image_key}"
                        )
                    else:
                        image_present = True
        harness_version_ok = self._spec.harness_version == HARNESS_VERSION
        if not harness_version_ok:
            problems.append(
                f"harness 版本不匹配：快照={self._spec.harness_version!r}，锁定={HARNESS_VERSION!r}"
            )
        disk_ok = self.disk_limit_gb is None or (
            probe.disk_free_gb is not None and probe.disk_free_gb >= self.disk_limit_gb
        )
        return PreflightReport(
            ok=not problems,
            harness_version=self._spec.harness_version,
            harness_version_ok=harness_version_ok,
            daemon_ok=probe.daemon_ok,
            container_os=probe.container_os,
            linux_containers=probe.linux_containers,
            disk_free_gb=probe.disk_free_gb,
            disk_limit_gb=self.disk_limit_gb,
            disk_ok=disk_ok,
            platform=f"{self._platform.system} {self._platform.machine}".strip(),
            windows_native=windows_native,
            wsl_available=self._platform.wsl_available,
            harness_runnable=harness_runnable,
            required_instance=required_instance,
            image_key=image_key,
            image_present=image_present,
            image_expected_id=image_expected_id,
            image_override=image_override,
            image_selection_reason=image_selection_reason,
            problems=tuple(problems),
            warnings=tuple(warnings),
        )

    def ensure(self, identity: SWEBenchEnvironmentIdentity) -> EnvironmentHandle:
        """确保官方实例镜像可用：命中→校验复用；缺失且 ``allow_build=False``→阻断。"""
        self._validate_identity(identity)
        probe = self._probe_runtime()
        if probe.blockers:
            raise SWEBenchEnvironmentError(
                "；".join(blocker.message for blocker in probe.blockers),
                kind=probe.blockers[0].kind,
            )
        if self._backend is None:  # pragma: no cover - blockers 已覆盖
            raise SWEBenchEnvironmentError(
                "未配置 DockerBackend", kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED
            )
        resolved = self._images.resolve(identity.instance_id)
        key = resolved.image_ref
        entries, warnings = self._cache.load()
        warnings_list = list(warnings)
        entry = entries.pop(identity.environment_hash, None)
        cache_state: Literal["hit", "revalidated", "miss", "pulled"] = "miss"
        info: ImageInfo | None = None
        if entry is not None:
            if entry.spec_sha256 != identity.spec_sha256 or entry.image_key != key:
                warnings_list.append("环境缓存与当前身份不一致，已失效并重新校验镜像")
            else:
                info = self._inspect_or_raise(key)
                if (
                    info is not None
                    and _image_is_valid(info, key)
                    and info.image_id == entry.image_id
                    and (resolved.image_id is None or info.image_id == resolved.image_id)
                ):
                    return EnvironmentHandle(
                        identity=identity,
                        image_key=key,
                        image_id=entry.image_id,
                        from_cache=True,
                        cache_state="hit",
                        image_override=resolved.override,
                        expected_image_id=resolved.image_id,
                        parent_image_ref=resolved.parent_image_ref,
                        parent_image_id=resolved.parent_image_id,
                        selection_reason=resolved.selection_reason,
                        warnings=tuple(warnings_list),
                    )
                info = None
                warnings_list.append("环境缓存镜像已失效（不存在/ID/OS/架构/tag 变化），重新校验")
        if info is None:
            info = self._inspect_or_raise(key)
        if info is not None and _image_is_valid(info, key):
            if resolved.image_id is not None and info.image_id != resolved.image_id:
                raise SWEBenchEnvironmentError(
                    "实例镜像身份不匹配，已停止该实例（禁止回退到官方镜像）："
                    f"{identity.instance_id} 期望 {resolved.image_id}，实际 {info.image_id} "
                    f"（{key}）",
                    kind=SWEBenchEnvironmentKind.ENV_ERROR,
                )
            cache_state = "revalidated" if entry is not None else "miss"
            entries[identity.environment_hash] = _cache_entry(identity, info, image_key=key)
            warnings_list.extend(self._cache.save(entries))
            return EnvironmentHandle(
                identity=identity,
                image_key=key,
                image_id=info.image_id,
                from_cache=True,
                cache_state=cache_state,
                image_override=resolved.override,
                expected_image_id=resolved.image_id,
                parent_image_ref=resolved.parent_image_ref,
                parent_image_id=resolved.parent_image_id,
                selection_reason=resolved.selection_reason,
                warnings=tuple(warnings_list),
            )
        reason = "镜像不存在" if info is None else "镜像校验失败（OS/架构/tag 不匹配）"
        if resolved.override:
            raise SWEBenchEnvironmentError(
                f"实例 {identity.instance_id} 的显式镜像不可用：{key}（{reason}）；"
                "已停止该实例，禁止静默回退到官方镜像",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        if not self.allow_build:
            warnings_list.extend(self._cache.save(entries))
            raise SWEBenchEnvironmentError(
                f"官方实例镜像不可用：{key}（{reason}）；allow_build=False，"
                "不会联网拉取/构建镜像（RESOURCE_BLOCKED）。启用 allow_build 只允许拉取"
                "官方发布镜像；官方 env/base 镜像构建与复用 deferred",
                kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED,
            )
        try:
            pulled = self._backend.pull(key)
        except DockerBackendError as exc:
            kind = (
                SWEBenchEnvironmentKind.RESOURCE_BLOCKED
                if isinstance(exc, DockerResourceExhaustedError)
                else SWEBenchEnvironmentKind.BUILD_FAILED
            )
            raise SWEBenchEnvironmentError(
                f"官方实例镜像准备失败（{kind}）：{key}；{redact_text(str(exc))}",
                kind=kind,
            ) from exc
        refreshed = self._inspect_or_raise(key) or pulled
        if not _image_is_valid(refreshed, key):
            raise SWEBenchEnvironmentError(
                f"拉取后镜像校验失败（OS/架构/tag 不匹配）：{key}",
                kind=SWEBenchEnvironmentKind.BUILD_FAILED,
            )
        if resolved.image_id is not None and refreshed.image_id != resolved.image_id:
            raise SWEBenchEnvironmentError(
                "拉取后镜像身份与 override 不符，已停止该实例："
                f"期望 {resolved.image_id}，实际 {refreshed.image_id}（{key}）",
                kind=SWEBenchEnvironmentKind.BUILD_FAILED,
            )
        entries[identity.environment_hash] = _cache_entry(identity, refreshed, image_key=key)
        warnings_list.extend(self._cache.save(entries))
        return EnvironmentHandle(
            identity=identity,
            image_key=key,
            image_id=refreshed.image_id,
            from_cache=False,
            cache_state="pulled",
            image_override=resolved.override,
            expected_image_id=resolved.image_id,
            parent_image_ref=resolved.parent_image_ref,
            parent_image_id=resolved.parent_image_id,
            selection_reason=resolved.selection_reason,
            warnings=tuple(warnings_list),
        )

    def cleanup_owned(self) -> tuple[str, ...]:
        """只清理本适配器标注的 ``swebench-env`` 临时容器；镜像与其他容器不动。"""
        backend = self._backend
        if backend is None:
            raise SWEBenchEnvironmentError(
                "未配置 DockerBackend，无法清理容器",
                kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED,
            )
        try:
            daemon_ok = backend.daemon_ok()
        except DockerBackendError as exc:
            raise SWEBenchEnvironmentError(
                f"Docker daemon 不可用，无法清理（{_kind_for_docker_error(exc)}）："
                f"{redact_text(str(exc))}",
                kind=_kind_for_docker_error(exc),
            ) from exc
        if not daemon_ok:
            raise SWEBenchEnvironmentError(
                "Docker daemon 不可用（daemon_ok=False），无法清理",
                kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED,
            )
        labels = {_LABEL_OWNER: "1", _LABEL_ROLE: _SWEEBENCH_ROLE}
        try:
            containers = backend.list_containers(labels=labels)
        except DockerBackendError as exc:
            raise SWEBenchEnvironmentError(
                f"列出自有容器失败（{_kind_for_docker_error(exc)}）：{redact_text(str(exc))}",
                kind=_kind_for_docker_error(exc),
            ) from exc
        removed: list[str] = []
        failures: list[str] = []
        for container in containers:
            # 即使后端过滤器失灵，也按标签二次确认，绝不误删别的项目容器。
            if (
                container.labels.get(_LABEL_OWNER) != "1"
                or container.labels.get(_LABEL_ROLE) != _SWEEBENCH_ROLE
            ):
                continue
            try:
                deleted = backend.remove_container(container.container_id)
            except DockerBackendError as exc:
                failures.append(f"{container.container_id}: {redact_text(str(exc))}")
                continue
            if deleted:
                removed.append(container.container_id)
            else:
                failures.append(f"{container.container_id}: 容器已不存在（可能被并发清理）")
        if failures:
            raise SWEBenchEnvironmentError(
                "清理自有 SWE-bench 临时容器失败（未触碰其他项目资源）：" + "；".join(failures),
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        return tuple(sorted(removed))

    def _probe_runtime(self) -> _RuntimeProbe:
        blockers: list[_Blocker] = []
        daemon_ok = False
        container_os: str | None = None
        linux_containers = False
        backend = self._backend
        if backend is None:
            blockers.append(
                _Blocker(
                    kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED.value,
                    message="未配置 DockerBackend（RESOURCE_BLOCKED）：无法检查/复用 Docker 镜像",
                )
            )
        else:
            try:
                daemon_ok = backend.daemon_ok()
            except DockerBackendError as exc:
                blockers.append(_docker_blocker("Docker daemon 预检查", exc))
            else:
                if not daemon_ok:
                    blockers.append(
                        _Blocker(
                            kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED.value,
                            message=(
                                "Docker daemon 不可用（ping 失败，RESOURCE_BLOCKED）："
                                "请启动 Docker Desktop 或以 Linux 容器模式运行"
                            ),
                        )
                    )
                else:
                    try:
                        container_os = backend.ostype()
                    except DockerBackendError as exc:
                        blockers.append(_docker_blocker("Docker 容器模式预检查", exc))
                    else:
                        linux_containers = container_os == "linux"
                        if not linux_containers:
                            blockers.append(
                                _Blocker(
                                    kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED.value,
                                    message=(
                                        "Docker 必须运行 Linux 容器（当前 OSType="
                                        f"{container_os!r}，RESOURCE_BLOCKED）"
                                    ),
                                )
                            )
        disk_free_gb: float | None = None
        try:
            disk_free_gb = (
                backend.disk_free_gb(self.cache_dir)
                if backend is not None
                else _disk_free_gb(self.cache_dir)
            )
        except (DockerBackendError, OSError) as exc:
            disk_free_gb = None
            if self.disk_limit_gb is not None:
                blockers.append(
                    _Blocker(
                        kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED.value,
                        message=f"无法确认磁盘余量（RESOURCE_BLOCKED）：{redact_text(str(exc))}",
                    )
                )
        if self.disk_limit_gb is not None and (
            disk_free_gb is None or disk_free_gb < self.disk_limit_gb
        ):
            blockers.append(
                _Blocker(
                    kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED.value,
                    message=(
                        f"磁盘余量不足（RESOURCE_BLOCKED）：free={disk_free_gb}GiB，"
                        f"limit={self.disk_limit_gb}GiB；镜像需要数 GiB 空间"
                    ),
                )
            )
        return _RuntimeProbe(
            daemon_ok=daemon_ok,
            container_os=container_os,
            linux_containers=linux_containers,
            disk_free_gb=disk_free_gb,
            blockers=tuple(blockers),
        )

    def _inspect_or_raise(self, key: str) -> ImageInfo | None:
        if self._backend is None:  # pragma: no cover - 调用前已检查
            raise SWEBenchEnvironmentError(
                "未配置 DockerBackend", kind=SWEBenchEnvironmentKind.RESOURCE_BLOCKED
            )
        try:
            return self._backend.inspect_image(key)
        except DockerBackendError as exc:
            raise SWEBenchEnvironmentError(
                f"镜像检查失败（{_kind_for_docker_error(exc)}）：{key}；{redact_text(str(exc))}",
                kind=_kind_for_docker_error(exc),
            ) from exc

    def _validate_identity(self, identity: SWEBenchEnvironmentIdentity) -> None:
        if identity.identity_schema_version != _IDENTITY_SCHEMA_VERSION:
            raise SWEBenchEnvironmentError(
                "环境身份 schema 版本已过期，旧缓存不得复用："
                f"{identity.identity_schema_version!r} != {_IDENTITY_SCHEMA_VERSION!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        if identity.harness_version != self._spec.harness_version:
            raise SWEBenchEnvironmentError(
                "身份 harness 版本与冻结快照不一致："
                f"{identity.harness_version!r} != {self._spec.harness_version!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        if identity.instance_image_key != instance_image_key(identity.instance_id):
            raise SWEBenchEnvironmentError(
                f"身份 instance_image_key 与 instance_id 不一致：{identity.instance_image_key!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        resolved = self._images.resolve(identity.instance_id)
        if identity.execution_image_key != resolved.image_ref:
            raise SWEBenchEnvironmentError(
                "身份 execution_image_key 与当前镜像解析结果不一致："
                f"{identity.execution_image_key!r} != {resolved.image_ref!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        if identity.execution_image_id != resolved.image_id:
            raise SWEBenchEnvironmentError(
                "身份 execution_image_id 与当前 override 不一致："
                f"{identity.execution_image_id!r} != {resolved.image_id!r}",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        payload = _identity_payload_from_identity(identity)
        if _sha256(_canonical_json(payload)) != identity.environment_hash:
            raise SWEBenchEnvironmentError(
                "身份 environment_hash 校验失败（身份字段被篡改或算法版本不一致）",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )


class SWEBenchSandboxPlan(BaseModel):
    """把官方环境交给现有 DockerSandbox 的装配计划。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: SWEBenchEnvironmentIdentity
    env_spec: EnvSpec
    deferred: tuple[str, ...] = ()


class SWEBenchSandboxFactory:
    """只把 EnvSpec 交给现有 ``DockerSandbox``，不自行创建容器/镜像。

    不在本类里放宽任何隔离参数：workspace 校验、``--network none``、只读根文件系统、
    cap_drop 等全部由 ``DockerSandbox`` 自己决定。官方镜像复用未实现，明确进
    ``deferred``；官方判定语义（日志解析而非 JUnit）也未实现。
    """

    _FACTORY_DEFERRED: tuple[str, ...] = (
        "official_image_reuse",
        "official_junit_semantics",
    )

    def __init__(
        self,
        *,
        sandbox: Sandbox | None = None,
        spec: SWEBenchEnvironmentSpec | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._spec = spec

    @property
    def deferred(self) -> tuple[str, ...]:
        return self._FACTORY_DEFERRED

    def _environment_spec(self) -> SWEBenchEnvironmentSpec:
        if self._spec is None:
            self._spec = SWEBenchEnvironmentSpec.load()
        return self._spec

    def plan(self, identity: SWEBenchEnvironmentIdentity) -> SWEBenchSandboxPlan:
        env_spec = self._environment_spec().env_spec_for(identity.repo, identity.version)
        return SWEBenchSandboxPlan(
            identity=identity,
            env_spec=env_spec,
            deferred=self._FACTORY_DEFERRED,
        )

    def build_env(
        self,
        *,
        identity: SWEBenchEnvironmentIdentity,
        workspace: Path,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult:
        if self._sandbox is None:
            raise SWEBenchEnvironmentError(
                "SWEBenchSandboxFactory 未注入 DockerSandbox；工厂不会自行创建容器/镜像"
                "（避免绕过隔离规则）",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        plan = self.plan(identity)
        # 所有隔离参数由 DockerSandbox 决定，这里只传契约规定的字段。
        return self._sandbox.build_env(
            repository=identity.repo,
            base_commit=identity.base_commit,
            workspace=workspace,
            env_spec=plan.env_spec,
            allow_network=allow_network,
        )


def instance_image_key(
    instance_id: str,
    *,
    arch: str = ARCH,
    tag: str = IMAGE_TAG,
    namespace: str | None = OFFICIAL_NAMESPACE,
) -> str:
    """官方 4.1.0 实例镜像名，逐规则对齐 ``TestSpec.instance_image_key``。

    ``namespace`` 默认取官方 CLI 默认值 ``swebench``（远端预构建镜像）；传 ``None``
    得到本地构建名。官方在加前缀后会把整个名字里的 ``__`` 换成 ``_1776_``，所以
    ``swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`` 才是默认形态。
    """
    if not isinstance(instance_id, str) or _INSTANCE_ID.fullmatch(instance_id.strip()) is None:
        raise SWEBenchEnvironmentError(
            f"非法 SWE-bench instance_id: {instance_id!r}",
            kind=SWEBenchEnvironmentKind.ENV_ERROR,
        )
    key = f"sweb.eval.{arch}.{instance_id.strip().lower()}:{tag}"
    if namespace:
        key = f"{namespace}/{key}".replace("__", _NAMESPACE_SEPARATOR)
    return key


def _base_image_key(raw: Mapping[str, Any]) -> str:
    """官方 base 镜像名；与 ``TestSpec.base_image_key`` 的规则一致。"""
    docker_specs_raw = raw.get("docker_specs")
    if docker_specs_raw is None:
        docker_specs: dict[str, Any] = {}
    else:
        docker_specs = _as_str_keyed_mapping(docker_specs_raw, what="官方规格的 docker_specs")
    suffix = ""
    if docker_specs:
        digest = _sha256(str(docker_specs))[:10]
        suffix = f".{digest}"
    return f"sweb.base.py.{ARCH}{suffix}:{IMAGE_TAG}"


def _candidate_env_image_key(repo: str, version: str, raw: Mapping[str, Any]) -> str:
    """env 镜像候选键。

    官方 22 位 hash 来自 ``make_env_script_list``（对 requirements.txt/environment.yml
    会联网取仓库文件），离线不可复算；这里用冻结规格的 canonical JSON 生成确定性
    候选键，形状与官方一致，但**不**宣称等于官方镜像 tag（见 deferred）。
    """
    payload = {
        "harness_version": HARNESS_VERSION,
        "repo": repo,
        "spec": _jsonable(raw),
        "version": version,
    }
    digest = _sha256(_canonical_json(payload))[:22]
    return f"sweb.env.py.{ARCH}.{digest}:{IMAGE_TAG}"


def _identity_payload(
    *,
    identity_schema_version: int,
    instance_id: str,
    repo: str,
    base_commit: str,
    version: str,
    environment_setup_commit: str,
    dataset_revision: str,
    harness_version: str,
    python_version: str,
    env_image_key: str,
    instance_image_key: str,
    base_image_key: str,
    execution_image_key: str,
    execution_image_id: str | None,
    spec_sha256: str,
) -> dict[str, str]:
    return {
        "identity_schema_version": str(identity_schema_version),
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "version": version,
        "environment_setup_commit": environment_setup_commit,
        "dataset_revision": dataset_revision,
        "harness_version": harness_version,
        "python_version": python_version,
        "env_image_key": env_image_key,
        "instance_image_key": instance_image_key,
        "base_image_key": base_image_key,
        "execution_image_key": execution_image_key,
        "execution_image_id": execution_image_id or "",
        "spec_sha256": spec_sha256,
    }


def _identity_payload_from_identity(identity: SWEBenchEnvironmentIdentity) -> dict[str, str]:
    return _identity_payload(
        identity_schema_version=identity.identity_schema_version,
        instance_id=identity.instance_id,
        repo=identity.repo,
        base_commit=identity.base_commit,
        version=identity.version,
        environment_setup_commit=identity.environment_setup_commit,
        dataset_revision=identity.dataset_revision,
        harness_version=identity.harness_version,
        python_version=identity.python_version,
        env_image_key=identity.env_image_key,
        instance_image_key=identity.instance_image_key,
        base_image_key=identity.base_image_key,
        execution_image_key=identity.execution_image_key,
        execution_image_id=identity.execution_image_id,
        spec_sha256=identity.spec_sha256,
    )


def _cache_entry(
    identity: SWEBenchEnvironmentIdentity, info: ImageInfo, *, image_key: str
) -> _CacheEntry:
    return _CacheEntry(
        image_key=image_key,
        image_id=info.image_id,
        spec_sha256=identity.spec_sha256,
        validated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _image_is_valid(info: ImageInfo, key: str) -> bool:
    """镜像校验：Linux / amd64(x86_64) / RepoTags 必须包含目标 key。"""
    return (
        info.os == _IMAGE_OS and info.architecture in _IMAGE_ARCHITECTURES and key in info.repo_tags
    )


def _kind_for_docker_error(exc: DockerBackendError) -> str:
    if isinstance(exc, DockerPermissionDeniedError):
        return SWEBenchEnvironmentKind.ENV_ERROR.value
    if isinstance(exc, (DockerDaemonUnavailableError, DockerResourceExhaustedError)):
        return SWEBenchEnvironmentKind.RESOURCE_BLOCKED.value
    return SWEBenchEnvironmentKind.ENV_ERROR.value


def _docker_blocker(what: str, exc: DockerBackendError) -> _Blocker:
    kind = _kind_for_docker_error(exc)
    return _Blocker(
        kind=kind,
        message=f"{what}失败（{kind}）：{redact_text(str(exc))}",
    )


def _task_text(task: object, field_name: str) -> str:
    value = getattr(task, field_name, None)
    if not isinstance(value, str) or not value.strip():
        raise SWEBenchEnvironmentError(
            f"任务缺少合法字段 {field_name}",
            kind=SWEBenchEnvironmentKind.ENV_ERROR,
        )
    return value.strip()


def _as_str_keyed_mapping(value: object, *, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SWEBenchEnvironmentError(f"{what} 必须是对象", kind=SWEBenchEnvironmentKind.ENV_ERROR)
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise SWEBenchEnvironmentError(
                f"{what} 含非字符串键: {key!r}", kind=SWEBenchEnvironmentKind.ENV_ERROR
            )
        result[key] = item
    return result


def _parse_spec_table(value: object, *, path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    raw = _as_str_keyed_mapping(value, what=f"{path} 的 specs")
    if not raw:
        raise SWEBenchEnvironmentError(
            f"官方环境规格快照 specs 为空: {path}",
            kind=SWEBenchEnvironmentKind.ENV_ERROR,
        )
    specs: dict[str, dict[str, dict[str, Any]]] = {}
    for repo, versions_raw in raw.items():
        versions = _as_str_keyed_mapping(versions_raw, what=f"specs[{repo!r}]")
        if not versions:
            raise SWEBenchEnvironmentError(
                f"官方规格仓库 {repo!r} 没有任何版本",
                kind=SWEBenchEnvironmentKind.ENV_ERROR,
            )
        parsed: dict[str, dict[str, Any]] = {}
        for version, spec_raw in versions.items():
            parsed[version] = _as_str_keyed_mapping(spec_raw, what=f"specs[{repo!r}][{version!r}]")
        specs[repo] = parsed
    return specs


def _string_items(value: object, *, what: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise SWEBenchEnvironmentError(
            f"{what} 必须是字符串数组", kind=SWEBenchEnvironmentKind.ENV_ERROR
        )
    if not all(isinstance(item, str) for item in value):
        raise SWEBenchEnvironmentError(
            f"{what} 只能包含字符串", kind=SWEBenchEnvironmentKind.ENV_ERROR
        )
    return tuple(item.strip() for item in value if item.strip())


def _readonly(spec: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(spec)


def _is_pytest_command(command: str) -> bool:
    return any(
        token == "pytest" or token.endswith("/pytest") or token.endswith("\\pytest")
        for token in command.split()
    )


def _env_spec_unsupported_message(
    repo: str,
    version: str,
    raw: Mapping[str, Any],
    exc: Exception,
) -> str:
    parts = [f"官方规格 {repo}@{version} 无法转换为 CodePilot EnvSpec：{exc}"]
    python_version = raw.get("python")
    if isinstance(python_version, str) and python_version not in _PYTHON_SUPPORTED_BY_ENV_SPEC:
        parts.append(
            f"官方 python={python_version}，EnvSpec/DockerSandbox 后端只支持 3.10/3.11/3.12/3.13"
        )
    packages = raw.get("packages")
    if isinstance(packages, str) and packages.endswith(("environment.yml", "environment.yaml")):
        parts.append("该规格依赖 conda environment.yml")
    parts.append("该任务必须走官方镜像（Phase 11 未实现官方镜像复用/构建，见 deferred）")
    return "；".join(parts)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    """稳定 JSON：排序键、紧凑分隔符；换行/编码差异不会改变 hash。"""
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
