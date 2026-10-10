"""Run the Phase 1 parser, golden, incremental and CLI contracts inside Linux Docker."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

_FILES = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "tests/conftest.py",
    "tests/unit/repo/test_parser_subprocess.py",
    "tests/integration/test_index_golden.py",
    "tests/integration/test_cli_index.py",
    "tests/integration/test_incremental.py",
    "tests/support/__init__.py",
    "tests/support/gitfixture.py",
    "tests/support/indexdump.py",
    "tests/fixtures/golden/mini_auth_index.json",
    "tests/docker/Dockerfile.phase1",
    "tests/docker/phase1_acceptance.py",
)


def _build_context(repo_root: Path, target: Path) -> None:
    """Copy only source and named contract inputs; no .env, keys, git or artifacts."""
    files = [repo_root / relative for relative in _FILES]
    files.extend((repo_root / "src").rglob("*.py"))
    files.append(repo_root / "src/codepilot/py.typed")
    fixture = repo_root / "tests/fixtures/repos/mini_auth"
    for pattern in ("*.py", "*.toml", "*.md"):
        files.extend(fixture.rglob(pattern))
    for source in files:
        destination = target / source.relative_to(repo_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _docker(*args: str, timeout: int) -> str:
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(
            f"Docker exceeded {timeout}s: {args!r}\n"
            f"stdout={error.stdout!r}\nstderr={error.stderr!r}"
        )
    print(result.stdout, end="")
    print(result.stderr, end="")
    assert result.returncode == 0, (
        f"docker {' '.join(args)} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    return result.stdout


@pytest.mark.docker
@pytest.mark.timeout(1200)
def test_phase1_contracts_in_linux_container(tmp_path: Path, repo_root: Path) -> None:
    context = tmp_path / "context"
    _build_context(repo_root, context)
    identity = uuid4().hex
    image = f"codepilot-phase1-test:{identity}"
    container = f"codepilot-phase1-test-{identity}"
    try:
        _docker(
            "build",
            "--progress=plain",
            "--tag",
            image,
            "--file",
            str(context / "tests/docker/Dockerfile.phase1"),
            str(context),
            timeout=600,
        )
        output = _docker(
            "run",
            "--name",
            container,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=2g",
            "--cpus=2",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            image,
            timeout=240,
        )
        assert "PHASE1_DOCKER_ACCEPTANCE_OK" in output, output
    finally:
        # Resource names are unique to this test; never prune shared Docker state.
        for args in (("rm", "--force", container), ("image", "rm", "--force", image)):
            try:
                result = subprocess.run(
                    ["docker", *args], capture_output=True, text=True, timeout=30, check=False
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"Docker cleanup: {args!r}: {error}")
                continue
            if result.returncode:
                print(f"Docker cleanup: {args!r}: {result.stderr.strip()}")
