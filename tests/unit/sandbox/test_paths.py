from __future__ import annotations

import os
from pathlib import Path

import pytest

from codepilot.sandbox.paths import (
    PathBoundaryError,
    container_workspace_path,
    docker_bind_source,
    validate_workspace_path,
    windows_path_to_container_hint,
)


def test_windows_docker_sdk_path_stays_native() -> None:
    assert docker_bind_source(r"C:\项目\repo", host_platform="win32") == r"C:\项目\repo"
    assert windows_path_to_container_hint(r"C:\项目\repo") == "/c/项目/repo"


def test_unc_is_rejected_by_default() -> None:
    with pytest.raises(PathBoundaryError, match="UNC"):
        docker_bind_source(r"\\server\share\repo", host_platform="win32")
    with pytest.raises(PathBoundaryError, match="UNC"):
        windows_path_to_container_hint(r"\\server\share\repo")


@pytest.mark.parametrize("value", ["../outside", "/absolute", r"..\outside"])
def test_container_paths_reject_escape(value: str) -> None:
    with pytest.raises(PathBoundaryError):
        container_workspace_path(value)


def test_container_path_normalizes_backslashes_and_unicode() -> None:
    assert container_workspace_path(r"tests\中文\test_x.py") == "/workspace/tests/中文/test_x.py"


def test_workspace_must_be_inside_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "runs"
    inside = allowed / "run" / "workspace"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    assert validate_workspace_path(inside, allowed_root=allowed) == inside.resolve()
    with pytest.raises(PathBoundaryError, match="允许根目录"):
        validate_workspace_path(outside, allowed_root=allowed)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    allowed = tmp_path / "runs"
    workspace = allowed / "run" / "workspace"
    outside = tmp_path / "secret"
    workspace.mkdir(parents=True)
    outside.mkdir()
    try:
        (workspace / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前系统不允许创建符号链接")
    with pytest.raises(PathBoundaryError, match="链接逃逸"):
        validate_workspace_path(workspace, allowed_root=allowed)


def test_relative_native_bind_path_resolves_on_current_platform(tmp_path: Path) -> None:
    if os.name == "nt":
        assert ":" in docker_bind_source(tmp_path, host_platform="win32")
    else:
        assert docker_bind_source(tmp_path).startswith("/")
