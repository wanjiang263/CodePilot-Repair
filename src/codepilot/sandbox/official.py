"""Official SWE-bench instance image execution.

The image key is resolved per instance (official tag or an audited override).
The resolved full image ID is verified before every execution; a missing or
mismatched image stops the instance instead of falling back to another tag.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from docker.errors import DockerException, ImageNotFound

from codepilot.repo.gitcmd import run_git
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.docker import DockerSandbox
from codepilot.sandbox.swebench_env import (
    SWEBenchInstanceImageResolver,
    _image_is_valid,
)
from codepilot.schemas.enums import ExecutionStatus
from codepilot.schemas.execution import ExecutionResult

__all__ = ["OfficialImageError", "OfficialInstanceSandbox"]

_PYTEST_PREFIX = re.compile(r"^(?:python(?:3)? -m )?pytest\b")
#: pytest 的 ``_version.py`` 是 setuptools_scm 生成的、被 gitignore 的文件。官方镜像
#: 在构建时生成过它，但内层 workspace 卷挂到 ``/testbed`` 后会把它遮住；直接跑
#: ``python -m pytest`` 会在 import 阶段报 ``No module named '_pytest._version'``。
#: 这里只补生成物，不改测试、不改断言。
_PYTEST_VERSION_BOOTSTRAP = (
    "if [ ! -f /testbed/src/_pytest/_version.py ]; then "
    "(cd /testbed && python setup.py --version >/dev/null 2>&1) || true; "
    "if [ ! -f /testbed/src/_pytest/_version.py ]; then "
    "printf '%s\\n' \"__version__ = version = '8.0.0'\" "
    '"__version_tuple__ = version_tuple = (8, 0, 0)" '
    "> /testbed/src/_pytest/_version.py; fi; fi; "
)


def _pytest_command(instance_id: str, command: str) -> str:
    """Prefix pytest test commands with the generated-version bootstrap."""
    if not instance_id.startswith("pytest-dev__pytest-"):
        return command
    return _PYTEST_VERSION_BOOTSTRAP + command


def _archive_members(archive: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")


def _extract_suffix(archive: bytes, workspace: Path, *, prefix: str, suffix: str) -> int:
    """Copy matching files from a ``get_archive`` tar into the workspace."""
    extracted = 0
    workspace_root = workspace.resolve()
    with _archive_members(archive) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.lstrip("./")
            root = prefix.rstrip("/")
            if not name.startswith(root + "/") or not name.endswith(suffix):
                continue
            relative = name[len(root) + 1 :]
            # Tar member names come from an image, not from this repository.
            # Never let ``..`` or an absolute member name write outside the
            # workspace.
            relative_path = PurePosixPath(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                continue
            target = workspace / root / relative
            # Also catch Windows drive/UNC components and symlinked parents,
            # not just POSIX ``..`` segments.
            if not target.resolve().is_relative_to(workspace_root):
                continue
            if target.exists():
                continue
            data = tar.extractfile(member)
            if data is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.read())
            extracted += 1
    return extracted


def _extract_single_file(archive: bytes, target: Path) -> int:
    if target.exists():
        return 0
    with _archive_members(archive) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            data = tar.extractfile(member)
            if data is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.read())
            return 1
    return 0


def _reclassify_pytest_fixture_dependency(
    instance_id: str, result: ExecutionResult
) -> ExecutionResult:
    """pytest's own suite intentionally imports missing modules in fixtures.

    A run that produced tests is not an environment failure; the intentional
    ``ModuleNotFoundError`` cases are test content. A run with no tests still
    keeps the environment-error attribution.
    """
    if (
        instance_id.startswith("pytest-dev__pytest-")
        and result.status is ExecutionStatus.ENV_ERROR
        and result.error_reason == "MISSING_THIRD_PARTY_DEPENDENCY"
        and result.tests
    ):
        # A missing optional module can be an intentional skip in pytest's own
        # suite.  ``EnvironmentError`` is also produced by the diagnostic text,
        # so an all-skipped + exit-0 run must stay PASS; turning it into FAIL
        # made L2 reject its own "368 passed, 0 failed" result.
        status = (
            ExecutionStatus.PASS
            if result.exit_code == 0 and not result.failed_tests
            else ExecutionStatus.FAIL
        )
        diagnostics = {
            **result.diagnostics,
            "reclassified_from": "ENV_ERROR",
            "reclassify_reason": "pytest fixture imports a missing optional module",
        }
        return result.model_copy(
            update={"status": status, "error_reason": None, "diagnostics": diagnostics}
        )
    return result


def _git_exclude_path(workspace: Path) -> Path | None:
    candidate = workspace / ".git" / "info" / "exclude"
    if candidate.is_file():
        return candidate
    try:
        raw = run_git(["rev-parse", "--git-path", "info/exclude"], cwd=workspace).stdout.strip()
    except Exception:
        return None
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else workspace / path


def _copy_testdeps(source: str | Path | None, workspace: Path) -> int:
    """Copy an optional, pre-resolved test dependency directory into the workspace.

    The directory is excluded through the repository's local git exclude so
    dependency files never become part of the model's delivered patch.
    """
    if source is None:
        return 0
    src = Path(source)
    if not src.is_dir():
        return 0
    target = workspace / "testdeps"
    if target.exists():
        return 0
    exclude = _git_exclude_path(workspace)
    if exclude is None:
        raise OfficialImageError("cannot locate git exclude for the testdeps directory")
    if not exclude.resolve().is_relative_to(workspace.resolve()):
        raise OfficialImageError(
            "git exclude resolved outside the workspace; refusing to write it"
        )
    shutil.copytree(src, target, symlinks=True)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
    if "testdeps/" not in {line.strip() for line in current.splitlines()}:
        exclude.write_text(current.rstrip("\n") + "\ntestdeps/\n", encoding="utf-8")
    return sum(1 for path in target.rglob("*") if path.is_file())


class OfficialImageError(RuntimeError):
    """The instance execution image is missing or does not match its identity."""


class OfficialInstanceSandbox(DockerSandbox):
    """DockerSandbox bound to the instance's official image and ``/testbed``."""

    def __init__(
        self,
        *,
        artifacts: Any,
        allowed_workspace_root: Path,
        instance_id: str,
        images: SWEBenchInstanceImageResolver,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            artifacts=artifacts,
            allowed_workspace_root=allowed_workspace_root,
            client=client,
            workspace_target="/testbed",
        )
        self.instance_id = instance_id
        self.images = images
        self.last_image: dict[str, Any] | None = None

    def build_env(
        self,
        *,
        repository: str,
        base_commit: str,
        workspace: Path,
        env_spec: Any,
        allow_network: bool = False,
    ) -> EnvironmentBuildResult:
        # 执行镜像只由实例解析器决定；这些参数属于 DockerSandbox 的构建契约，
        # 在这个"绝不构建、绝不回退"的子类里刻意不使用。
        del repository, env_spec, allow_network
        head = run_git(["rev-parse", "HEAD"], cwd=workspace).stdout.strip()
        if head != base_commit:
            raise OfficialImageError(
                f"workspace base commit mismatch: HEAD={head!r} expected={base_commit!r}"
            )
        resolution = self.images.resolve(self.instance_id)
        try:
            image = self.client.images.get(resolution.image_ref)
        except ImageNotFound as exc:
            raise OfficialImageError(
                f"instance image not available: {resolution.image_ref}; "
                "no fallback to another image is allowed"
            ) from exc
        actual_id = str(getattr(image, "id", ""))
        tags = (getattr(image, "attrs", {}) or {}).get("RepoTags") or []
        info = _image_view(image, resolution.image_ref, actual_id, tags)
        if not _image_is_valid(info, resolution.image_ref):
            raise OfficialImageError(
                f"instance image failed OS/arch/tag validation: {resolution.image_ref}"
            )
        if resolution.image_id is not None and actual_id != resolution.image_id:
            raise OfficialImageError(
                "instance image identity mismatch: "
                f"expected {resolution.image_id}, actual {actual_id} ({resolution.image_ref})"
            )
        self.last_image = {
            "instance_id": self.instance_id,
            "image_ref": resolution.image_ref,
            "image_id": actual_id,
            "expected_image_id": resolution.image_id,
            "override": resolution.override,
            "parent_image_ref": resolution.parent_image_ref,
            "parent_image_id": resolution.parent_image_id,
            "selection_reason": resolution.selection_reason,
            "workspace_target": self.workspace_target,
            "verified_head": head,
        }
        self._overlay_build_artifacts(workspace)
        testdeps = 0
        if self.instance_id.startswith("pytest-dev__pytest-"):
            testdeps = _copy_testdeps(os.environ.get("CODEPILOT_TESTDEPS_DIR"), workspace)
        if self.last_image is not None:
            self.last_image["overlaid_testdeps_files"] = testdeps
        return EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref=resolution.image_ref,
            image_id=actual_id,
            cache_key=f"official:{actual_id}",
            from_cache=True,
        )

    def _overlay_build_artifacts(self, workspace: Path) -> None:
        """Restore image-built artifacts hidden by the workspace volume mount.

        The official images build generated files and compiled extensions inside
        ``/testbed``; mounting the host workspace over that path hides them. Tests
        that import those artifacts then fail before the model ever gets a patch.
        Extract only the missing build outputs; source files and the git diff stay
        untouched. A missing required extension fails the environment build.
        """
        image_ref = str((self.last_image or {}).get("image_ref") or "")
        if not image_ref:
            return
        if self.instance_id.startswith("scikit-learn__scikit-learn-"):
            archive_path = "/testbed/sklearn"
            prefix = "sklearn"
            suffix = ".so"
            required = True
        elif self.instance_id.startswith("pytest-dev__pytest-"):
            archive_path = "/testbed/src/_pytest/_version.py"
            prefix = ""
            suffix = ""
            required = False
        else:
            return
        container: Any | None = None
        try:
            container = self.client.containers.create(image_ref, command=["/bin/true"])
            stream, _ = container.get_archive(archive_path)
            archive = b"".join(stream)
            if prefix:
                count = _extract_suffix(archive, workspace, prefix=prefix, suffix=suffix)
                if required and not any((workspace / prefix).rglob(f"*{suffix}")):
                    raise OfficialImageError(
                        "official image contains no compiled extensions to restore"
                    )
            else:
                count = _extract_single_file(archive, workspace / "src/_pytest/_version.py")
            if self.last_image is not None:
                self.last_image["overlaid_build_artifacts"] = count
        except (DockerException, OSError, tarfile.TarError) as exc:
            if self.last_image is not None:
                self.last_image["overlay_error"] = f"{type(exc).__name__}: {exc}"
            if required:
                raise OfficialImageError(f"无法从官方镜像恢复构建产物: {exc}") from exc
        finally:
            if container is not None:
                with contextlib.suppress(DockerException):
                    container.remove(force=True)

    def run_tests(self, **kwargs: Any) -> Any:
        spec = kwargs["env_spec"]
        command = _PYTEST_PREFIX.sub(
            "/opt/miniconda3/envs/testbed/bin/python -m pytest", spec.test_command
        )
        env = {**spec.env, "PYTHONPATH": "/testbed/src:/testbed:/testbed/testdeps"}
        command = _pytest_command(self.instance_id, command)
        kwargs["env_spec"] = spec.model_copy(update={"test_command": command, "env": env})
        result = super().run_tests(**kwargs)
        return _reclassify_pytest_fixture_dependency(self.instance_id, result)


def _image_view(image: Any, ref: str, image_id: str, tags: list[str]) -> Any:
    """Minimal ImageInfo-compatible view for validation without importing backend types."""
    del ref

    class _View:
        os = (getattr(image, "attrs", {}) or {}).get("Os")
        architecture = (getattr(image, "attrs", {}) or {}).get("Architecture")
        repo_tags = tuple(tags)

    _View.id = image_id  # type: ignore[attr-defined]
    _View.short_id = image_id  # type: ignore[attr-defined]
    _View.tags = tuple(tags)  # type: ignore[attr-defined]
    return _View()
