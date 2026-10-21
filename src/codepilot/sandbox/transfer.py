"""Engine-local file transfer for the test sandbox: no host paths, no bind mounts.

Why this module exists (Phase 11R, measured on this machine): a container's bind
mount source is resolved by the *daemon's* filesystem. When a WSL2 distro talks to
Docker Desktop's engine over the guest socket, the host paths it sees (``/root/...``,
``/tmp/...``, even ``/mnt/c/...``) belong to a different filesystem than the one the
daemon mounts, so the container silently gets an empty directory -- and a
Windows-form source is rejected outright. Repository code that ran tests through
bind volumes therefore failed with ``bind source path does not exist`` on the very
first real experiment.

The replacement keeps every runtime security property of the old contract:

* the test container still runs with ``read_only`` rootfs, ``user 1000:1000``,
  ``network_mode=none``, the same CPU/memory/PID caps and the same cleanup;
* the workspace reaches the engine through a *named volume* filled by a short-lived
  staging container (upload needs ``put_archive``, which the daemon refuses on a
  read-only rootfs -- measured), so the test container itself never becomes writable;
* results come back through ``get_archive`` on the output volume, which -- unlike a
  tmpfs -- still holds the files after the test container exits.

Nothing here depends on the daemon being able to see a host path.
"""

from __future__ import annotations

import io
import posixpath
import tarfile
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

__all__ = [
    "CONTAINER_UID",
    "DEFAULT_WORKSPACE_TARGET",
    "OUTPUT_TARGET",
    "StageOutcome",
    "TransferError",
    "collect_output",
    "output_volume_name",
    "stage_into_volumes",
    "upload_archive",
    "workspace_volume_name",
]

#: 上传归档里的归属统一成容器内的运行身份；宿主 uid 不进容器。
CONTAINER_UID = 1000

#: 容器内的工作区路径。失败解析器按这个前缀把 traceback 帧认成仓库内帧。
DEFAULT_WORKSPACE_TARGET = "/workspace"

#: 测试产物路径，与 :class:`~codepilot.sandbox.env_spec.EnvSpec` 的
#: ``--junitxml=/out/junit.xml`` 契约同源。
OUTPUT_TARGET = "/out"

#: staging 容器的挂载点；只在这一次上传里存在，与测试容器的路径无关。
STAGE_WORKSPACE = "/stage-ws"
STAGE_OUTPUT = "/stage-out"
_STAGE_MEMORY = "1g"

#: 回传归档的体量上限。bind 时代由宿主目录自然受限，volume 必须显式设限，
#: 否则一个失败测试刷爆日志就能把宿主磁盘当免费空间用。
MAX_RETURNED_BYTES = 128 * 1024 * 1024
MAX_RETURNED_FILE_BYTES = 32 * 1024 * 1024

#: 永不离开宿主、也绝不进容器的评测/凭据材料。``eval`` 与
#: ``codepilot.eval.synthetic.EVALUATION_DIRECTORY`` 同一含义，这里按字面量声明
#: 以避免 sandbox 反向依赖 eval 层。
_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".env",
        ".env.local",
        ".git-credentials",
        ".aws",
        ".codex",
        ".npmrc",
        "eval",
        "artifacts",
    }
)


class TransferError(RuntimeError):
    """归档构造、上传或回传失败。绝不降级成宿主挂载。"""


def workspace_volume_name(token: str) -> str:
    return f"codepilot-{token}-ws"


def output_volume_name(token: str) -> str:
    return f"codepilot-{token}-out"


def new_volume_token() -> str:
    return uuid.uuid4().hex[:16]


def upload_archive(workspace: Path, *, arcname: str = ".") -> bytes:
    """把工作区打成 tar 字节流：目录/常规文件/工作区内的相对符号链接。

    ``arcname="."`` 产生以 volume 根为基准的条目（配合 :func:`stage_into_volumes`）；
    ``arcname="workspace"`` 保留环境构建路径的旧形状（配合 ``put_archive("/")``）。
    """
    root = Path(workspace).resolve(strict=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        if arcname != ".":
            _add_directory(archive, PurePosixPath(arcname), mode=_dir_mode(root))
        for relative, entry in _walk_workspace(root):
            if entry.is_symlink():
                _add_symlink(archive, root=root, relative=relative, entry=entry, arcname=arcname)
            elif entry.is_dir():
                _add_directory(archive, _arc(relative, arcname=arcname), mode=_dir_mode(entry))
            elif entry.is_file():
                _add_file(archive, relative=relative, entry=entry, arcname=arcname)
            else:
                raise TransferError(f"工作区含无法安全传输的 special file: {relative}")
    return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """一次 staging 上传的结果。

    ``cleanup_error`` 只记账、不改写主判定：staging 容器删不掉确实要留下证据
    （卷可能被它占着），但那不是"这次测试没跑成"，把 TIMEOUT/OOM 改写成传输故障
    会把基础设施噪声冒充成执行结论。
    """

    uploaded_bytes: int
    cleanup_error: str | None = None


def stage_into_volumes(
    client: Any,
    *,
    image_ref: str,
    workspace: Path,
    workspace_volume: str,
    output_volume: str,
    labels: Mapping[str, str],
    timeout_seconds: int = 300,
) -> StageOutcome:
    """用一次性 staging 容器把归档灌进两个 volume，并交出可写所有权。

    staging 容器是唯一可写 rootfs 的那个，它只执行我们自己的固定命令：不跑仓库代码、
    没有网络、只有 CHOWN/FOWNER/DAC_OVERRIDE 三项额外能力。测试容器因此可以继续保持只读根。
    """
    payload = upload_archive(workspace)
    staging = client.containers.create(
        image=image_ref,
        command=["/bin/sh", "-lc", f"sleep {max(int(timeout_seconds), 1)}"],
        network_mode="none",
        user="0:0",
        read_only=False,
        tmpfs={"/tmp": "size=64m"},
        mem_limit=_STAGE_MEMORY,
        memswap_limit=_STAGE_MEMORY,
        pids_limit=128,
        cap_drop=["ALL"],
        # CHOWN/FOWNER 用来把卷根移交给了 uid 1000；DAC_OVERRIDE 是清空卷内预填
        # 内容所必需的：那些文件的属主是镜像里的构建用户，不一定是 root。
        cap_add=["CHOWN", "FOWNER", "DAC_OVERRIDE"],
        security_opt=["no-new-privileges"],
        privileged=False,
        detach=True,
        labels=dict(labels),
        volumes={
            workspace_volume: {"bind": STAGE_WORKSPACE, "mode": "rw"},
            output_volume: {"bind": STAGE_OUTPUT, "mode": "rw"},
        },
    )
    try:
        staging.start()
        _wipe_volumes(staging)
        if not staging.put_archive(STAGE_WORKSPACE, payload):
            raise TransferError("工作区归档上传失败（put_archive 返回 False）")
        probe = staging.exec_run(
            [
                "/bin/sh",
                "-lc",
                f"chown {CONTAINER_UID}:{CONTAINER_UID} {STAGE_WORKSPACE} {STAGE_OUTPUT}",
            ]
        )
        exit_code, output = _exec_result(probe)
        if exit_code != 0:
            raise TransferError(
                f"volume 所有权移交失败（exit={exit_code}）: "
                f"{output.decode('utf-8', errors='replace')[:400]!r}"
            )
    finally:
        # 只在正常流程里回报清理失败：在 finally 里 return 会吞掉上传异常，
        # 那会把"没上传成功"伪装成"上传完成"，测试随即跑在一个空工作区上。
        cleanup_error = _remove_quietly(staging)
    return StageOutcome(uploaded_bytes=len(payload), cleanup_error=cleanup_error)


def _exec_result(result: Any) -> tuple[int | None, bytes]:
    """``exec_run`` 的返回形态不止一种：新 SDK 给 ``ExecResult``，旧路径给元组。

    只按其中一种读会把另一种当成"没返回码"，进而把失败当成成功。
    """
    if hasattr(result, "exit_code"):
        output = getattr(result, "output", b"") or b""
        return int(result.exit_code), output if isinstance(output, bytes) else bytes(
            str(output), "utf-8"
        )
    if isinstance(result, tuple) and result:
        code, output = result[0], (result[1] if len(result) > 1 else b"")
        if isinstance(output, tuple):
            output = b"".join(item for item in output if isinstance(item, bytes))
        return int(code), output if isinstance(output, bytes) else b""
    return None, b""


def _wipe_volumes(staging: Any) -> None:
    """清空两个卷的预填内容，再让工作区归档成为唯一内容。

    Docker 在容器启动时会把**镜像在该路径下的内容复制进新卷**（volume population）。
    实测：把新卷挂到官方实例镜像的 ``/testbed`` 上，卷里直接出现镜像自带的
    ``.git``，HEAD 是 base commit **之后一个**提交（``rev-list base..HEAD`` = 1）。
    不清空就等于把可能含修复提交的 Git 对象库连同旧 checkout 一起交给被测容器，
    这是 Gold 隔离的破口，也是"莫名成功率偏高"的经典来源。
    """
    script = (
        "set -eu\n"
        "wipe() {\n"
        '  if command -v find >/dev/null 2>&1; then find "$1" -mindepth 1 -delete\n'
        '  else rm -rf "$1"/* "$1"/.[!.]* "$1"/..?* 2>/dev/null || true\n'
        "  fi\n"
        '  if [ -n "$(ls -A "$1" 2>/dev/null)" ]; then echo "not-empty:$1" >&2; exit 17; fi\n'
        "}\n"
        f"wipe {STAGE_WORKSPACE}\n"
        f"wipe {STAGE_OUTPUT}\n"
    )
    result = staging.exec_run(["/bin/sh", "-lc", script])
    exit_code, output = _exec_result(result)
    if exit_code != 0:
        raise TransferError(
            f"卷预填内容未能清空（exit={exit_code}）: "
            f"{output.decode('utf-8', errors='replace')[-300:]!r}；"
            "拒绝在残留镜像内容上跑测试"
        )


def _remove_quietly(container: Any) -> str | None:
    """删掉 staging 容器；失败只记账，不改写主判定。"""
    try:
        container.remove(force=True)
    except Exception as exc:  # Docker 传输层的失败形态不止一种，一律记账
        return f"staging 容器清理失败: {type(exc).__name__}: {str(exc)[:200]}"
    return None


def collect_output(
    client: Any,
    container_id: str,
    *,
    destination: Path,
    max_bytes: int = MAX_RETURNED_BYTES,
) -> list[str]:
    """取回 ``/out`` 并安全解包到任务专属目录。

    回传归档是容器（即被测代码）产出的，因此按不可信输入处理：绝对路径、``..``、
    任何链接或 special file 一律拒绝，落点必须留在 ``destination`` 之内。
    """
    stream = io.BytesIO()
    bits, _stat = client.api.get_archive(container_id, OUTPUT_TARGET)
    written = 0
    for chunk in bits:
        written += len(chunk)
        if written > max_bytes:
            raise TransferError(f"回传归档超过上限 {max_bytes} 字节（已读 {written}），拒绝解包")
        stream.write(chunk)
    stream.seek(0)
    return _extract_returned_archive(stream, destination=destination, max_bytes=max_bytes)


def _extract_returned_archive(
    source: io.BytesIO, *, destination: Path, max_bytes: int
) -> list[str]:
    destination = Path(destination).resolve(strict=True)
    extracted: list[str] = []
    total = 0
    with tarfile.open(fileobj=source, mode="r:*") as archive:
        for member in archive.getmembers():
            relative = _returned_relative(member.name)
            if relative is None:
                continue
            if not (member.isfile() or member.isdir()):
                raise TransferError(f"回传归档含非预期条目类型: {member.name!r}")
            target = (destination / relative).resolve(strict=False)
            if not target.is_relative_to(destination):
                raise TransferError(f"回传归档路径逃逸任务目录: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.size > MAX_RETURNED_FILE_BYTES:
                raise TransferError(
                    f"回传文件超过单文件上限: {member.name!r}（{member.size} 字节）"
                )
            total += member.size
            if total > max_bytes:
                raise TransferError(f"回传内容超过上限 {max_bytes} 字节，拒绝继续解包")
            handle = archive.extractfile(member)
            if handle is None:  # pragma: no cover - isfile 条目总能读出
                raise TransferError(f"无法读取回传文件: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())
            extracted.append(relative.as_posix())
    return extracted


def _returned_relative(name: str) -> PurePosixPath | None:
    """``get_archive`` 以请求目录名为根返回条目，剥掉它并拒绝越界写法。"""
    posix = PurePosixPath(name)
    if posix.is_absolute() or posix.drive:
        raise TransferError(f"回传归档使用绝对路径: {name!r}")
    parts = [part for part in posix.parts if part not in ("", ".")]
    if not parts:
        return None
    if parts[0] == PurePosixPath(OUTPUT_TARGET).name:
        parts = parts[1:]
    if ".." in parts:
        raise TransferError(f"回传归档包含路径穿越: {name!r}")
    if not parts:
        return None
    return PurePosixPath(*parts)


def _walk_workspace(root: Path) -> Iterator[tuple[PurePosixPath, Path]]:
    """按稳定顺序产出工作区条目，跳过排除目录，并当场做符号链接边界检查。"""
    stack: list[Path] = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise TransferError(f"无法读取工作区目录 {directory}: {exc}") from exc
        for entry in entries:
            relative = _relative(root, entry)
            if relative.name in _EXCLUDED_NAMES:
                continue
            if entry.is_symlink():
                _assert_link_inside(root, relative, entry)
            if entry.is_dir() and not entry.is_symlink():
                yield relative, entry
                stack.append(entry)
            else:
                yield relative, entry


def _relative(root: Path, entry: Path) -> PurePosixPath:
    return PurePosixPath(entry.relative_to(root).as_posix())


def _link_target(entry: Path) -> Path:
    """链接指向的原始目标（未规范化）；绝对目标会直接生效。"""
    return entry.parent / entry.readlink()


def _assert_link_inside(root: Path, relative: PurePosixPath, entry: Path) -> None:
    try:
        resolved = _link_target(entry).resolve(strict=False)
    except OSError as exc:
        raise TransferError(f"无法读取符号链接 {relative}: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise TransferError(f"工作区符号链接逃逸: {relative} -> {entry.readlink()}")


def _add_symlink(
    archive: tarfile.TarFile,
    *,
    root: Path,
    relative: PurePosixPath,
    entry: Path,
    arcname: str,
) -> None:
    target = _link_target(entry).resolve(strict=False).relative_to(root)
    info = archive.gettarinfo(str(entry))
    info.name = _arc(relative, arcname=arcname).as_posix()
    info.type = tarfile.SYMTYPE
    # 绝对链接指向的是宿主路径，在容器里没有意义；改写成同一棵树内的相对链接。
    info.linkname = posixpath.relpath(target.as_posix(), relative.parent)
    info.uid, info.gid = CONTAINER_UID, CONTAINER_UID
    archive.addfile(info)


def _add_file(
    archive: tarfile.TarFile,
    *,
    relative: PurePosixPath,
    entry: Path,
    arcname: str,
) -> None:
    info = archive.gettarinfo(str(entry))
    info.name = _arc(relative, arcname=arcname).as_posix()
    info.type = tarfile.REGTYPE
    info.uid, info.gid = CONTAINER_UID, CONTAINER_UID
    with entry.open("rb") as handle:
        archive.addfile(info, handle)


def _add_directory(archive: tarfile.TarFile, path: PurePosixPath, *, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(f"{path.as_posix()}/")
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.uid, info.gid = CONTAINER_UID, CONTAINER_UID
    archive.addfile(info)


def _arc(relative: PurePosixPath, *, arcname: str) -> PurePosixPath:
    return relative if arcname == "." else PurePosixPath(arcname, relative)


def _dir_mode(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o7777
    except OSError:
        return 0o755
