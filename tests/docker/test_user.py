"""S-20：真实 Docker 非 root 与容器限制验收。

配置侧从 daemon 的 ``inspect`` 读取，行为侧在容器内部执行——两侧都来自真实
容器。这里的资源上限是**测试隔离措施**，不构成对抗恶意代码的完整安全边界。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codepilot.sandbox.docker import execution_container_config
from codepilot.schemas.enums import ExecutionStatus
from conftest import assert_no_run_residue, spec_with

SECURITY_PROBE = '''"""S-20 受控探针：在容器内部验证运行身份、挂载与权限。"""

from __future__ import annotations

import os


def _mounts() -> dict[str, tuple[str, str]]:
    table: dict[str, tuple[str, str]] = {}
    with open("/proc/mounts", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) >= 4:
                table[fields[1]] = (fields[2], fields[3])
    return table


def _status_value(prefix: str) -> str:
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith(prefix):
                return line.split()[1]
    raise AssertionError(f"/proc/self/status 缺少 {prefix}")


def test_runs_as_non_root_uid_1000() -> None:
    assert os.getuid() == 1000
    assert os.getgid() == 1000


def test_all_capabilities_are_dropped() -> None:
    assert _status_value("CapEff:") == "0000000000000000"


def test_no_new_privileges_is_active() -> None:
    assert _status_value("NoNewPrivs:") == "1"


def test_cannot_gain_root_privileges_at_runtime() -> None:
    try:
        os.setuid(0)
    except OSError:
        return
    raise AssertionError("容器内的进程可以切换到 uid 0")


def test_root_filesystem_is_mounted_read_only() -> None:
    fstype, options = _mounts()["/"]
    assert options.split(",")[0] == "ro", (fstype, options)


def test_writing_to_the_root_filesystem_fails() -> None:
    try:
        with open("/etc/codepilot-acceptance-probe", "w", encoding="utf-8") as handle:
            handle.write("x")
    except OSError:
        return
    raise AssertionError("只读根文件系统被普通测试写入")


def test_tmp_is_tmpfs_and_writable() -> None:
    assert _mounts()["/tmp"][0] == "tmpfs"
    probe = "/tmp/codepilot-acceptance-probe"
    with open(probe, "w", encoding="utf-8") as handle:
        handle.write("ok")
    assert open(probe, encoding="utf-8").read() == "ok"
    os.remove(probe)


def test_workspace_is_writable() -> None:
    probe = "/workspace/codepilot-acceptance-probe.txt"
    with open(probe, "w", encoding="utf-8") as handle:
        handle.write("ok")
    assert os.path.exists(probe)
    os.remove(probe)


def test_out_accepts_artifacts() -> None:
    probe = "/out/codepilot-acceptance-probe.txt"
    with open(probe, "w", encoding="utf-8") as handle:
        handle.write("collected")
    assert os.path.getsize(probe) > 0
'''

#: 契约常量（02 §13.3）。
CONTRACT_MEMORY_BYTES = 4 * 1024**3
CONTRACT_NANO_CPUS = 2_000_000_000
CONTRACT_PIDS = 512
PROBE_CASES = 9


@pytest.mark.docker
def test_in_container_identity_filesystem_and_privilege_contract(
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace({"test_security_probe.py": SECURITY_PROBE})
    sandbox, _client = make_sandbox()

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec_with(timeout_seconds=180),
        tests=("test_security_probe.py",),
    )

    record_evidence(
        "S-20-in-container",
        {
            "status": result.status.value,
            "tests_total": len(result.tests),
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
            "failed_nodes": [case.node_id for case in result.failed_tests],
            "exit_code": result.exit_code,
        },
    )

    # 任何一条探针失败（例如容器其实能 setuid）都会让这里失败。
    failures = [(case.node_id, case.message, case.traceback) for case in result.failed_tests]
    assert result.status is ExecutionStatus.PASS, failures
    assert result.tests_passed == PROBE_CASES, [case.node_id for case in result.tests]
    assert result.tests_failed == 0


@pytest.mark.docker
def test_daemon_inspect_matches_security_and_resource_contract(
    docker_client: Any,
    sandbox_environment: Any,
    tmp_path: Path,
    acceptance_run_id: str,
    record_evidence: Any,
) -> None:
    """直接按生产配置建一个真实容器，从 daemon inspect 核对逐字段契约。"""
    workspace = tmp_path / "inspect-workspace"
    output = tmp_path / "inspect-out"
    workspace.mkdir()
    output.mkdir()
    config = execution_container_config(
        image_ref=sandbox_environment.image_ref or "",
        workspace=workspace,
        output_dir=output,
        command="sleep 25",
        environment={},
        created_at=1.0,
    )
    labels = {**config["labels"], "codepilot.acceptance_run": acceptance_run_id}
    container = docker_client.containers.create(**{**config, "labels": labels})
    try:
        container.start()
        attrs = docker_client.api.inspect_container(container.id)
        host = attrs.get("HostConfig", {})
        state = attrs.get("State", {})
        mounts = attrs.get("Mounts") or []

        record_evidence(
            "S-20-daemon-inspect",
            {
                "container_id": container.id,
                "state": state.get("Status"),
                "user": attrs.get("Config", {}).get("User"),
                "memory": host.get("Memory"),
                "memory_swap": host.get("MemorySwap"),
                "nano_cpus": host.get("NanoCpus"),
                "pids_limit": host.get("PidsLimit"),
                "read_only_rootfs": host.get("ReadonlyRootfs"),
                "tmpfs": host.get("Tmpfs"),
                "cap_drop": host.get("CapDrop"),
                "security_opt": host.get("SecurityOpt"),
                "privileged": host.get("Privileged"),
                "network_mode": host.get("NetworkMode"),
                "mounts": [
                    (item.get("Type"), item.get("Source"), item.get("Destination"), item.get("RW"))
                    for item in mounts
                ],
            },
        )

        assert state.get("Status") == "running", "容器必须真实运行起来"
        assert attrs.get("Config", {}).get("User") == "1000:1000"
        assert host.get("Memory") == CONTRACT_MEMORY_BYTES
        assert host.get("MemorySwap") == CONTRACT_MEMORY_BYTES  # 4 GiB 且无额外 swap
        assert host.get("NanoCpus") == CONTRACT_NANO_CPUS
        assert host.get("PidsLimit") == CONTRACT_PIDS
        assert host.get("ReadonlyRootfs") is True
        assert host.get("Tmpfs") == {"/tmp": "size=512m"}
        assert host.get("CapDrop") == ["ALL"]
        assert "no-new-privileges" in (host.get("SecurityOpt") or [])
        assert host.get("Privileged") is False
        assert host.get("NetworkMode") == "none"

        # 只有 workspace 与 out 两个可写 bind 挂载；没有 Docker Socket、没有额外卷。
        assert {item.get("Destination") for item in mounts} == {"/workspace", "/out"}
        assert {item.get("Destination"): item.get("RW") for item in mounts} == {
            "/workspace": True,
            "/out": True,
        }
        assert all(item.get("Type") == "bind" for item in mounts)
        assert not [item for item in mounts if "docker.sock" in str(item.get("Source", ""))]
    finally:
        docker_client.api.remove_container(container.id, force=True)

    assert assert_no_run_residue(docker_client, acceptance_run_id) == []
