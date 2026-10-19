"""宿主路径校验与 Docker Desktop 路径契约。"""

from __future__ import annotations

import ntpath
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = [
    "PathBoundaryError",
    "container_workspace_path",
    "docker_bind_source",
    "validate_workspace_path",
    "windows_path_to_container_hint",
]

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


class PathBoundaryError(ValueError):
    """路径不属于批准的 workspace 或无法安全映射。"""


def validate_workspace_path(path: Path, *, allowed_root: Path) -> Path:
    """resolve 后再次检查边界，并拒绝指向边界外的 symlink/reparse point。"""
    root = Path(allowed_root).resolve(strict=True)
    candidate = Path(path).resolve(strict=True)
    if not candidate.is_dir() or not candidate.is_relative_to(root):
        raise PathBoundaryError(f"workspace 不在允许根目录内: {candidate}")
    for entry in candidate.rglob("*"):
        try:
            # ``st_file_attributes`` 只存在于 Windows 的 stat_result；POSIX 上按
            # "没有 reparse point" 处理（getattr 让同一份代码在 Linux mypy 也成立）。
            attributes = getattr(entry.lstat(), "st_file_attributes", 0)
        except OSError as exc:
            raise PathBoundaryError(f"无法检查 workspace 路径: {entry}") from exc
        is_reparse = (
            bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name == "nt" else False
        )
        if entry.is_symlink() or is_reparse:
            resolved = entry.resolve(strict=False)
            if not resolved.is_relative_to(candidate):
                raise PathBoundaryError(f"workspace 内链接逃逸: {entry} -> {resolved}")
    return candidate


def docker_bind_source(
    path: str | Path,
    *,
    host_platform: str | None = None,
    allow_unc: bool = False,
) -> str:
    """返回 Docker SDK ``volumes`` 所需的宿主原生绝对路径。

    Docker Desktop SDK 接受 ``C:\\...``，不是 CLI 风格的 ``/c/...``。后者只由
    :func:`windows_path_to_container_hint` 提供给诊断信息，绝不冒充 bind source。
    """
    platform = host_platform or sys.platform
    raw = str(path)
    if platform == "win32" or _DRIVE.match(raw) or raw.startswith(("\\\\", "//")):
        windows = PureWindowsPath(ntpath.abspath(raw))
        if windows.anchor.startswith(("\\\\", "//")) and not allow_unc:
            raise PathBoundaryError("默认拒绝 UNC 路径；Docker Desktop 共享边界不明确")
        if not windows.drive:
            raise PathBoundaryError(f"Windows bind 路径必须带盘符: {raw!r}")
        return str(windows)
    return str(Path(raw).resolve(strict=True))


def windows_path_to_container_hint(path: str | Path) -> str:
    """生成 Docker Desktop/WSL 常见显示形式，仅用于日志与人工诊断。"""
    windows = PureWindowsPath(str(path))
    if windows.anchor.startswith(("\\\\", "//")):
        raise PathBoundaryError("UNC 路径没有稳定的 Linux 容器映射")
    if not windows.drive:
        raise PathBoundaryError("Windows 路径必须是绝对盘符路径")
    drive = windows.drive.rstrip(":").lower()
    return str(PurePosixPath("/", drive, *windows.parts[1:]))


def container_workspace_path(relative: str | Path = ".") -> str:
    """把相对路径放到固定容器根 ``/workspace``，拒绝 ``..`` 与绝对路径。"""
    raw = str(relative).replace("\\", "/")
    posix = PurePosixPath(raw)
    if posix.is_absolute() or ".." in posix.parts:
        raise PathBoundaryError(f"非法容器相对路径: {relative!r}")
    return str(PurePosixPath("/workspace", posix))
