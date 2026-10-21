"""基于 docker SDK for Python 的两阶段测试沙箱。

这些限制用于降低研发评测中意外副作用的风险，不构成对抗恶意代码的完整安全
边界。特别敏感的工作负载仍应运行在独立 VM/一次性主机中。
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from docker.errors import DockerException, ImageNotFound
from requests import Timeout as RequestsTimeout
from requests.exceptions import ConnectionError as RequestsConnectionError

import docker
from codepilot.artifacts import JUNIT_XML, ArtifactStore
from codepilot.observability.redaction import redact_text
from codepilot.sandbox.artifacts import persist_logs
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.junit import JUnitParseError, JUnitReport, parse_junit
from codepilot.sandbox.paths import validate_workspace_path
from codepilot.sandbox.status import classify_execution
from codepilot.sandbox.transfer import (
    DEFAULT_WORKSPACE_TARGET,
    OUTPUT_TARGET,
    TransferError,
    collect_output,
    new_volume_token,
    output_volume_name,
    stage_into_volumes,
    upload_archive,
    workspace_volume_name,
)
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult

__all__ = ["DockerSandbox", "environment_cache_key", "execution_container_config"]

_LABEL_OWNER = "codepilot"
_LABEL_CREATED = "codepilot.created_at"
_LABEL_KIND = "codepilot.kind"
_KIND_EXECUTION = "execution"
_KIND_EXECUTION_STAGE = "execution-stage"
_ENV_REPOSITORY = "codepilot-env"
#: 只读根文件系统下 daemon 拒绝 ``put_archive``（实测），所以上传必须由一个
#: 临时的可写 staging 容器代做，测试容器本身保持与旧契约完全一致。
_STAGE_MEMORY = "1g"

#: daemon 报的 ISO 时间可能是纳秒精度，``fromisoformat`` 只接受到微秒。
_FRACTION = re.compile(r"\.(\d+)")


def environment_cache_key(repository: str, base_commit: str, env_spec: EnvSpec) -> str:
    """稳定镜像身份；repo、commit、规范化 EnvSpec 任一变化都会失效。"""
    payload = "\x00".join((repository, base_commit, env_spec.canonical_json()))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def execution_container_config(
    *,
    image_ref: str,
    workspace_volume: str,
    output_volume: str,
    command: str,
    environment: Mapping[str, str],
    workspace_target: str = DEFAULT_WORKSPACE_TARGET,
    created_at: float | None = None,
) -> dict[str, Any]:
    """02 §13.3 的逐字段配置；保持为纯函数方便无 daemon 审计。

    两个卷都是 **daemon 侧的命名卷**（key 是卷名，不是宿主路径）。宿主 bind 曾经
    写在这里，而卷的宿主路径解析发生在 daemon 那一侧：一旦 daemon 看不见调用方的
    文件系统（WSL2 连 Docker Desktop 就是这种拓扑），容器拿到的就是空目录，而且
    失败点在 ``containers.create`` 之后，症状是"测试莫名全挂"。
    """
    labels = {_LABEL_OWNER: "1", _LABEL_CREATED: str(created_at or time.time())}
    return {
        "image": image_ref,
        "command": ["/bin/sh", "-lc", command],
        "working_dir": workspace_target,
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
        "detach": True,
        "labels": labels,
        "environment": dict(environment),
        "volumes": {
            workspace_volume: {"bind": workspace_target, "mode": "rw"},
            output_volume: {"bind": OUTPUT_TARGET, "mode": "rw"},
        },
    }


class DockerSandbox:
    """真实 Docker 执行器；客户端与时钟可注入以进行确定性单元测试。"""

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        allowed_workspace_root: Path,
        client: Any | None = None,
        base_dockerfile: Path | None = None,
        log_tail_bytes: int = 16_384,
        clock: Any = time.time,
        workspace_target: str = DEFAULT_WORKSPACE_TARGET,
    ) -> None:
        if log_tail_bytes < 0:
            raise ValueError("log_tail_bytes 不能为负数")
        if (
            not workspace_target.startswith("/")
            or ".." in PurePosixPath(workspace_target).parts
            or workspace_target in {"/", OUTPUT_TARGET}
        ):
            raise ValueError(f"非法容器工作区路径: {workspace_target!r}")
        self.artifacts = artifacts
        self.allowed_workspace_root = Path(allowed_workspace_root)
        self.client = client if client is not None else docker.from_env()
        self.base_dockerfile = base_dockerfile or (
            Path(__file__).resolve().parents[3] / "sandbox" / "Dockerfile.base"
        )
        self.log_tail_bytes = log_tail_bytes
        self._clock = clock
        self._startup_cleanup_done = False
        self.workspace_target = workspace_target

    def build_env(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: EnvSpec,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult:
        started = self._clock()
        key = environment_cache_key(repository, base_commit, env_spec)
        image_ref = f"{_ENV_REPOSITORY}:{key}"
        try:
            checked_workspace = validate_workspace_path(
                workspace, allowed_root=self.allowed_workspace_root
            )
            self._startup_cleanup()
            cached = self._get_valid_cached_image(image_ref, key, base_commit, env_spec)
            if cached is not None:
                return EnvironmentBuildResult(
                    status=ExecutionStatus.PASS,
                    image_ref=image_ref,
                    image_id=str(getattr(cached, "id", image_ref)),
                    cache_key=key,
                    from_cache=True,
                    duration_seconds=max(self._clock() - started, 0.0),
                )
            if not allow_network:
                return EnvironmentBuildResult(
                    status=ExecutionStatus.ENV_ERROR,
                    cache_key=key,
                    duration_seconds=max(self._clock() - started, 0.0),
                    error=(
                        "环境镜像缓存未命中；首次依赖安装可能联网，必须在受控环境中"
                        "显式允许 allow_network"
                    ),
                )
            return self._build_uncached(
                repository=repository,
                base_commit=base_commit,
                workspace=checked_workspace,
                env_spec=env_spec,
                key=key,
                image_ref=image_ref,
                started=started,
            )
        except DockerException as exc:
            return EnvironmentBuildResult(
                status=ExecutionStatus.SANDBOX_ERROR,
                cache_key=key,
                duration_seconds=max(self._clock() - started, 0.0),
                error=f"Docker 不可用或容器操作失败: {redact_text(str(exc))}",
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return EnvironmentBuildResult(
                status=ExecutionStatus.SANDBOX_ERROR,
                cache_key=key,
                duration_seconds=max(self._clock() - started, 0.0),
                error=redact_text(str(exc)),
            )

    def _build_uncached(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: EnvSpec,
        key: str,
        image_ref: str,
        started: float,
    ) -> EnvironmentBuildResult:
        base_image = self._ensure_base_image(env_spec.python_version)
        container: Any | None = None
        result: EnvironmentBuildResult | None = None
        cleanup_error: str | None = None
        try:
            container = self.client.containers.create(
                image=base_image,
                command=["/bin/sh", "-lc", "while :; do sleep 3600; done"],
                working_dir="/workspace",
                network_mode="bridge",
                user="0:0",
                read_only=False,
                privileged=False,
                detach=True,
                labels={_LABEL_OWNER: "1", _LABEL_CREATED: str(self._clock())},
            )
            container.start()
            archive = _workspace_archive(workspace)
            if not container.put_archive("/", archive):
                raise RuntimeError("无法把固定 base commit workspace 复制进构建容器")
            script = _build_script(env_spec)
            execution = container.exec_run(
                ["/bin/sh", "-lc", script],
                workdir="/workspace",
                environment=dict(env_spec.env),
                demux=True,
            )
            exit_code, output = _exec_result(execution)
            if exit_code != 0:
                combined = redact_text(_decode_exec_output(output))
                result = EnvironmentBuildResult(
                    status=ExecutionStatus.ENV_ERROR,
                    cache_key=key,
                    duration_seconds=max(self._clock() - started, 0.0),
                    error=f"依赖安装失败（exit={exit_code}）: {combined[-4000:]}",
                )
            else:
                changes = [
                    'LABEL codepilot="1"',
                    f'LABEL codepilot.cache_key="{key}"',
                    f'LABEL codepilot.base_commit="{base_commit}"',
                    f'LABEL codepilot.env_sha256="{_env_digest(env_spec)}"',
                    f'LABEL codepilot.repository_sha256="{_text_digest(repository)}"',
                ]
                image = container.commit(repository=_ENV_REPOSITORY, tag=key, changes=changes)
                result = EnvironmentBuildResult(
                    status=ExecutionStatus.PASS,
                    image_ref=image_ref,
                    image_id=str(getattr(image, "id", image_ref)),
                    cache_key=key,
                    from_cache=False,
                    duration_seconds=max(self._clock() - started, 0.0),
                )
        except DockerException as exc:
            result = EnvironmentBuildResult(
                status=ExecutionStatus.SANDBOX_ERROR,
                cache_key=key,
                duration_seconds=max(self._clock() - started, 0.0),
                error=f"Docker 环境构建失败: {redact_text(str(exc))}",
            )
        except (OSError, RuntimeError) as exc:
            result = EnvironmentBuildResult(
                status=ExecutionStatus.ENV_ERROR,
                cache_key=key,
                duration_seconds=max(self._clock() - started, 0.0),
                error=f"环境构建失败: {redact_text(str(exc))}",
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException as exc:
                    cleanup_error = redact_text(str(exc))
        if cleanup_error:
            return EnvironmentBuildResult(
                status=ExecutionStatus.SANDBOX_ERROR,
                cache_key=key,
                duration_seconds=max(self._clock() - started, 0.0),
                error=f"环境构建容器清理失败: {cleanup_error}",
            )
        if result is None:  # pragma: no cover - 防止未来修改漏掉分支
            raise RuntimeError("环境构建未生成结果")
        return result

    def _ensure_base_image(self, python_version: str) -> str:
        tag = f"codepilot-sandbox-base:py{python_version}"
        try:
            self.client.images.get(tag)
            return tag
        except ImageNotFound:
            pass
        if not self.base_dockerfile.is_file():
            raise RuntimeError(f"基础镜像 Dockerfile 不存在: {self.base_dockerfile}")
        context = self.base_dockerfile.parent.parent
        relative = self.base_dockerfile.relative_to(context).as_posix()
        self.client.images.build(
            path=str(context),
            dockerfile=relative,
            tag=tag,
            buildargs={"PYTHON_VERSION": python_version},
            rm=True,
            forcerm=True,
            labels={_LABEL_OWNER: "1", "codepilot.kind": "base"},
        )
        return tag

    def _get_valid_cached_image(
        self,
        image_ref: str,
        key: str,
        base_commit: str,
        env_spec: EnvSpec,
    ) -> Any | None:
        try:
            image = self.client.images.get(image_ref)
        except ImageNotFound:
            return None
        labels = getattr(image, "attrs", {}).get("Config", {}).get("Labels", {}) or {}
        expected = {
            _LABEL_OWNER: "1",
            "codepilot.cache_key": key,
            "codepilot.base_commit": base_commit,
            "codepilot.env_sha256": _env_digest(env_spec),
        }
        return image if all(labels.get(name) == value for name, value in expected.items()) else None

    def run_tests(
        self,
        *,
        workspace: Path,
        environment: EnvironmentBuildResult,
        env_spec: EnvSpec,
        tests: Sequence[str] = (),
        artifact_iteration: int = 1,
    ) -> ExecutionResult:
        started = self._clock()
        if not environment.succeeded:
            decision = classify_execution(
                exit_code=None,
                junit=None,
                sandbox_error=environment.error
                if environment.status is ExecutionStatus.SANDBOX_ERROR
                else None,
                environment_error=environment.error
                if environment.status is ExecutionStatus.ENV_ERROR
                else None,
            )
            return ExecutionResult(
                status=decision.status,
                duration_seconds=max(self._clock() - started, 0.0),
                error_reason=decision.error_reason,
                diagnostics=decision.diagnostics,
            )

        # Container output is staged outside ArtifactStore.  Raw repository output may
        # contain secrets; only the redacted copy below is allowed into the run record.
        with tempfile.TemporaryDirectory(prefix="codepilot-out-") as temporary_output:
            return self._run_tests_in_container(
                workspace=workspace,
                environment=environment,
                env_spec=env_spec,
                tests=tests,
                artifact_iteration=artifact_iteration,
                started=started,
                output_dir=Path(temporary_output),
            )

    def _run_tests_in_container(
        self,
        *,
        workspace: Path,
        environment: EnvironmentBuildResult,
        env_spec: EnvSpec,
        tests: Sequence[str],
        artifact_iteration: int,
        started: float,
        output_dir: Path,
    ) -> ExecutionResult:

        container: Any | None = None
        exit_code: int | None = None
        timed_out = False
        oom_killed = False
        sandbox_error: str | None = None
        #: 清理失败可以有多条（kill、容器删除、卷删除、staging 删除），全部记账，
        #: 后面的失败不允许把前面的证据顶掉。
        cleanup_notes: list[str] = []
        token = new_volume_token()
        volumes = (workspace_volume_name(token), output_volume_name(token))
        created: list[str] = []
        try:
            workspace = validate_workspace_path(workspace, allowed_root=self.allowed_workspace_root)
            selected = _validate_test_selection(tests, workspace=workspace)
            command = env_spec.test_command
            if selected:
                command += " " + " ".join(shlex.quote(item) for item in selected)
            command += f" > {OUTPUT_TARGET}/stdout.log 2> {OUTPUT_TARGET}/stderr.log"
            environment_values = {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                **env_spec.env,
            }
            labels = {
                _LABEL_OWNER: "1",
                _LABEL_CREATED: str(self._clock()),
                "codepilot.run_id": self.artifacts.run_id,
                "codepilot.transfer": token,
            }
            for name in volumes:
                self.client.api.create_volume(name=name, labels={**labels, _LABEL_KIND: "volume"})
                created.append(name)
            config = execution_container_config(
                image_ref=environment.image_ref or "",
                workspace_volume=volumes[0],
                output_volume=volumes[1],
                command=command,
                environment=environment_values,
                workspace_target=self.workspace_target,
                created_at=self._clock(),
            )
            container = self.client.containers.create(**config)
            # 上传只能由另一个容器代做：daemon 对只读 rootfs 的容器直接拒绝
            # ``put_archive``（实测 400 "container rootfs is marked read-only"），
            # 而把测试容器改成可写就等于放弃本轮要守住的只读根契约。
            staged = stage_into_volumes(
                self.client,
                image_ref=environment.image_ref or "",
                workspace=workspace,
                workspace_volume=volumes[0],
                output_volume=volumes[1],
                labels={**labels, _LABEL_KIND: _KIND_EXECUTION_STAGE},
            )
            if staged.cleanup_error:
                # staging 容器删不掉要留证据（它可能还占着卷），但不改写主判定。
                cleanup_notes.append(staged.cleanup_error)
            container.start()
            try:
                wait_result = container.wait(timeout=env_spec.timeout_seconds)
                exit_code = _status_code(wait_result)
            except (RequestsTimeout, RequestsConnectionError, TimeoutError) as exc:
                if not _is_wait_timeout(exc):
                    raise
                timed_out = True
                try:
                    container.kill()
                except DockerException as kill_error:
                    cleanup_notes.append(f"超时后 kill 失败: {redact_text(str(kill_error))}")
            # 回传必须发生在 remove 之前：卷随容器删除后就没有读取入口了。
            # 超时被 kill 的场合同样先回传，留下的部分日志是归因证据；回传失败
            # 会被记成 sandbox_error，但 classify 仍按 TIMEOUT 优先归因。
            try:
                collect_output(self.client, str(container.id), destination=output_dir)
            except (DockerException, OSError, ValueError, RuntimeError) as exc:
                if sandbox_error is None:
                    sandbox_error = (
                        f"结果归档回传失败: {type(exc).__name__}: {redact_text(str(exc))}"
                    )
        except TransferError as exc:
            sandbox_error = f"工作区传输失败: {redact_text(str(exc))}"
        except DockerException as exc:
            sandbox_error = redact_text(str(exc))
        except (OSError, ValueError, RuntimeError) as exc:
            sandbox_error = redact_text(str(exc))
        finally:
            if container is not None:
                try:
                    container.reload()
                    oom_killed = bool(
                        getattr(container, "attrs", {}).get("State", {}).get("OOMKilled", False)
                    )
                except DockerException as exc:
                    if sandbox_error is None:
                        sandbox_error = f"读取容器状态失败: {redact_text(str(exc))}"
                try:
                    container.remove(force=True)
                except DockerException as exc:
                    cleanup_notes.append(f"容器强制删除失败: {redact_text(str(exc))}")
            cleanup_notes.extend(self._remove_volumes(created))

        cleanup_error = "; ".join(cleanup_notes) or None

        stdout = _read_text(output_dir / "stdout.log")
        stderr = _read_text(output_dir / "stderr.log")
        junit: JUnitReport | None = None
        junit_error: str | None = None
        try:
            junit = parse_junit(output_dir / JUNIT_XML)
        except JUnitParseError as exc:
            junit_error = str(exc)

        diagnostic = "\n".join(
            [
                stdout,
                stderr,
                *(
                    (test.traceback or "") + "\n" + (test.message or "")
                    for test in (junit.tests if junit else [])
                ),
            ]
        )
        if cleanup_error and not timed_out:
            sandbox_error = cleanup_error
        decision = classify_execution(
            exit_code=exit_code,
            junit=junit,
            timed_out=timed_out,
            sandbox_error=sandbox_error,
            oom_killed=oom_killed,
            workspace=workspace,
            diagnostic_text=diagnostic,
        )
        try:
            logs = persist_logs(
                self.artifacts,
                iteration=artifact_iteration,
                stdout=stdout,
                stderr=stderr,
                tail_bytes=self.log_tail_bytes,
            )
            junit_path = output_dir / JUNIT_XML
            if junit_path.is_file():
                self.artifacts.write_text(
                    self.artifacts.attempt_relpath(artifact_iteration, JUNIT_XML),
                    redact_text(_read_text(junit_path)),
                )
        except OSError as exc:
            return ExecutionResult(
                status=ExecutionStatus.SANDBOX_ERROR,
                exit_code=exit_code,
                duration_seconds=max(self._clock() - started, 0.0),
                error_reason="ARTIFACT_WRITE_FAILED",
                diagnostics={"detail": redact_text(str(exc))},
                artifact_dir="",
            )
        cases = [_redacted_case(case) for case in (junit.tests if junit else [])]
        diagnostics = dict(decision.diagnostics)
        diagnostics.update(
            {
                "execution_backend": "docker",
                "container_verified": True,
                "network_mode": "none",
                "oom_killed": oom_killed,
                "cleanup_error": cleanup_error,
                "junit_error": junit_error,
            }
        )
        return ExecutionResult(
            status=decision.status,
            exit_code=exit_code,
            duration_seconds=max(self._clock() - started, 0.0),
            tests=cases,
            tests_passed=sum(case.outcome is TestOutcome.PASSED for case in cases),
            tests_failed=sum(
                case.outcome in {TestOutcome.FAILED, TestOutcome.ERROR} for case in cases
            ),
            stdout_tail=logs.stdout_tail,
            stderr_tail=logs.stderr_tail,
            artifact_dir=logs.artifact_dir,
            error_reason=decision.error_reason,
            diagnostics=diagnostics,
        )

    def _remove_volumes(self, names: Sequence[str]) -> list[str]:
        """删除本轮创建的卷；失败回报卷名，绝不静默把磁盘当临时堆放区。"""
        failures: list[str] = []
        for name in names:
            try:
                self.client.api.remove_volume(name, force=True)
            except DockerException as exc:
                failures.append(f"卷 {name} 删除失败: {redact_text(str(exc))}")
            except Exception as exc:  # pragma: no cover - SDK 传输层的意外形态
                failures.append(f"卷 {name} 删除失败: {type(exc).__name__}")
        return failures

    def _cleanup_stale_volumes(self, *, cutoff: float) -> tuple[list[str], list[str]]:
        """扫掉崩溃残留的 codepilot 卷。

        卷没有 ``created_at`` 标签（创建时间来自 daemon 的 ``CreatedAt``），所以这里
        认不出时间的卷一律**不删**——宁可留一个孤儿卷等人来看，也不误删别人正在
        用的卷。名字前缀与 ``codepilot=1`` 标签是双重归属确认。
        """
        removed: list[str] = []
        failures: list[str] = []
        listing = self.client.api.volumes(filters={"label": f"{_LABEL_OWNER}=1"}) or {}
        for row in listing.get("Volumes") or []:
            name = str(row.get("Name") or "")
            if not name.startswith("codepilot-"):
                continue
            labels = row.get("Labels") or {}
            if labels.get(_LABEL_OWNER) != "1":
                continue
            created = _parse_created_at(row.get("CreatedAt"))
            if created is None or created > cutoff:
                continue
            try:
                self.client.api.remove_volume(name, force=True)
                removed.append(name)
            except DockerException as exc:
                failures.append(f"{name}: {redact_text(str(exc))}")
        return removed, failures

    def cleanup(self, *, older_than_seconds: int = 3600) -> list[str]:
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds 不能为负数")
        cutoff = self._clock() - older_than_seconds
        removed: list[str] = []
        failures: list[str] = []
        containers = self.client.containers.list(all=True, filters={"label": "codepilot=1"})
        for container in containers:
            labels = getattr(container, "labels", None) or getattr(container, "attrs", {}).get(
                "Config", {}
            ).get("Labels", {})
            if labels.get(_LABEL_OWNER) != "1":
                continue
            try:
                created = float(labels.get(_LABEL_CREATED, "inf"))
            except (TypeError, ValueError):
                continue
            if created > cutoff:
                continue
            identifier = str(getattr(container, "id", "<unknown>"))
            try:
                container.remove(force=True)
                removed.append(identifier)
            except DockerException as exc:
                failures.append(f"{identifier}: {redact_text(str(exc))}")
        volume_removed, volume_failures = self._cleanup_stale_volumes(cutoff=cutoff)
        removed.extend(volume_removed)
        failures.extend(volume_failures)
        if failures:
            raise RuntimeError("残留容器或卷清理失败: " + "; ".join(failures))
        return removed

    def _startup_cleanup(self) -> None:
        if self._startup_cleanup_done:
            return
        self.cleanup(older_than_seconds=3600)
        self._startup_cleanup_done = True


def _build_script(env_spec: EnvSpec) -> str:
    commands = ["set -eu", *env_spec.pre_install, *env_spec.install]
    if len(commands) == 1:
        commands.append("python --version")
    return "\n".join(commands)


def _workspace_archive(workspace: Path) -> bytes:
    """环境构建路径的归档：条目以 ``workspace/`` 为根，配合 ``put_archive("/")``。

    归属统一成容器内 uid、排除项与符号链接边界检查都在 :mod:`codepilot.sandbox.transfer`
    里，构建容器与测试执行共用同一套规则。
    """
    return upload_archive(workspace, arcname="workspace")


def _parse_created_at(value: Any) -> float | None:
    """把 daemon 的 ``CreatedAt``（可能带纳秒）转成 epoch 秒；认不出就返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    normalized = _FRACTION.sub(lambda match: f".{match.group(1)[:6]}", normalized, count=1)
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _exec_result(execution: Any) -> tuple[int, Any]:
    if hasattr(execution, "exit_code"):
        return int(execution.exit_code), execution.output
    return int(execution[0]), execution[1]


def _decode_exec_output(output: Any) -> str:
    if isinstance(output, tuple):
        return "\n".join(
            item.decode("utf-8", errors="replace") for item in output if isinstance(item, bytes)
        )
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output or "")


def _is_wait_timeout(exc: BaseException) -> bool:
    """判定 ``container.wait()`` 的异常是否就是超时。

    Docker Desktop 的 npipe 传输不会抛出 ``requests.exceptions.ReadTimeout``：
    urllib3 的读超时被包成 ``requests.exceptions.ConnectionError``，真正的
    ``TimeoutError`` 只出现在 ``__context__`` 链里（S-18 实测）。只按顶层类型
    判定的话，一次真实超时会被误报成 SANDBOX_ERROR，Phase 5 的修复循环就再也
    拿不到 TIMEOUT 这条路由。
    """
    current: BaseException | None = exc
    for _ in range(8):
        if current is None:
            return False
        if isinstance(current, (RequestsTimeout, TimeoutError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _status_code(wait_result: Any) -> int | None:
    if isinstance(wait_result, Mapping):
        value = wait_result.get("StatusCode")
        return int(value) if value is not None else None
    return int(wait_result) if wait_result is not None else None


def _redacted_case(case: TestCaseResult) -> TestCaseResult:
    """JUnit 的失败文本由被测仓库自己产生，进 ExecutionResult 之前必须脱敏。

    ``stdout``/``stderr``/JUnit 文件三份 artifact 早已在落盘前脱敏，但
    ``ExecutionResult.tests`` 走的是内存里的解析结果：Phase 5 会把这段文本喂给
    模型并写进运行记录，实测（真实 CLI run-tests）一个失败用例就能把
    ``sk-`` 令牌照原样带进 CLI 输出的 JSON。
    """
    return case.model_copy(
        update={
            "message": None if case.message is None else redact_text(case.message),
            "traceback": None if case.traceback is None else redact_text(case.traceback),
        }
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _validate_test_selection(tests: Sequence[str], *, workspace: Path) -> tuple[str, ...]:
    selected: list[str] = []
    for raw in tests:
        value = raw.strip()
        if not value or value.startswith("-") or "\x00" in value or "\n" in value:
            raise ValueError(f"非法测试选择: {raw!r}")
        file_part = value.split("::", 1)[0]
        relative = Path(file_part)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"测试路径越界: {raw!r}")
        resolved = (workspace / relative).resolve(strict=False)
        if not resolved.is_relative_to(workspace):
            raise ValueError(f"测试路径越界: {raw!r}")
        if not resolved.exists():
            raise ValueError(f"测试路径不存在: {raw!r}")
        selected.append(value)
    return tuple(selected)


def _env_digest(env_spec: EnvSpec) -> str:
    return hashlib.sha256(env_spec.canonical_json().encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
