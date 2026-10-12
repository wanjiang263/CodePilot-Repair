"""S-21：真实 Docker OOM 处理验收。

先看 daemon 自己是否如实报告 ``OOMKilled``（光有 exit code 137 不足以证明发生
了 OOM），再验证 `DockerSandbox` 把这份真实状态映射进六态契约并回收容器。

内存上限为本测试专门下调到 256 MiB（生产契约是 4 GiB）：内存是资源旋钮而不是
安全参数，下调它只是为了不在 8 GiB 的开发机上白白占用 4 GiB。
"""

from __future__ import annotations

from typing import Any

import pytest

from codepilot.sandbox.status import execution_route
from codepilot.schemas.enums import ExecutionStatus
from conftest import assert_no_run_residue, spec_with

#: 专门的低上限，明显低于生产默认 4 GiB。
OOM_MEMORY_LIMIT = "256m"
MAX_MIB = 2048  # 有界循环：即使上限失效也不会把宿主机吃穿。

MEMORY_HOG = '''"""受控内存压力夹具：每步 1 MiB，直到被内核 OOM kill。"""

CHUNK_BYTES = 1024 * 1024
MAX_MIB = 2048


def test_allocates_until_killed() -> None:
    blocks = []
    for _ in range(MAX_MIB):
        blocks.append(bytearray(CHUNK_BYTES))
    raise AssertionError("内存压力夹具在没有 OOM 的情况下跑完了上限")
'''


def _oom_state(client: Any, container_id: str) -> dict[str, Any]:
    return client.api.inspect_container(container_id).get("State", {})


@pytest.mark.docker
def test_daemon_reports_real_oomkilled_for_memory_over_the_limit(
    docker_client: Any,
    sandbox_environment: Any,
    tmp_path: Any,
    record_evidence: Any,
) -> None:
    """独立于 Sandbox 的真实证据：这台 daemon 确实会置位 OOMKilled。"""
    workspace = tmp_path / "hog-workspace"
    output = tmp_path / "hog-out"
    workspace.mkdir()
    output.mkdir()
    (workspace / "test_memory_hog.py").write_text(MEMORY_HOG, encoding="utf-8")

    container = docker_client.containers.create(
        image=sandbox_environment.image_ref or "",
        command=[
            "/bin/sh",
            "-lc",
            "python -m pytest -p no:cacheprovider --junitxml=/out/junit.xml test_memory_hog.py"
            " > /out/stdout.log 2> /out/stderr.log",
        ],
        working_dir="/workspace",
        network_mode="none",
        user="1000:1000",
        read_only=True,
        tmpfs={"/tmp": "size=64m"},
        mem_limit=OOM_MEMORY_LIMIT,
        memswap_limit=OOM_MEMORY_LIMIT,
        pids_limit=512,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        privileged=False,
        detach=True,
        labels={"codepilot": "1", "codepilot.kind": "acceptance-oom-probe"},
        volumes={
            str(workspace.resolve()): {"bind": "/workspace", "mode": "rw"},
            str(output.resolve()): {"bind": "/out", "mode": "rw"},
        },
    )
    try:
        container.start()
        waited = container.wait(timeout=120)
        state = _oom_state(docker_client, container.id)
        record_evidence(
            "S-21-daemon-oom",
            {
                "container_id": container.id,
                "wait_status_code": waited.get("StatusCode"),
                "state_exit_code": state.get("ExitCode"),
                "oom_killed": state.get("OOMKilled"),
                "state": state.get("Status"),
                "memory_limit": OOM_MEMORY_LIMIT,
            },
        )
        assert state.get("OOMKilled") is True, "daemon 未报告 OOMKilled，OOM 验收不成立"
        assert waited.get("StatusCode") == 137
        # exit code 137 只是旁证，真正的判据是容器状态里的 OOMKilled。
        assert state.get("OOMKilled") is not None
    finally:
        docker_client.api.remove_container(container.id, force=True)


@pytest.mark.docker
def test_sandbox_maps_real_oom_to_sandbox_error_and_reclaims_container(
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace({"test_memory_hog.py": MEMORY_HOG})
    sandbox, _client = make_sandbox(
        create_overrides={"mem_limit": OOM_MEMORY_LIMIT, "memswap_limit": OOM_MEMORY_LIMIT}
    )

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec_with(timeout_seconds=120),
        tests=("test_memory_hog.py::test_allocates_until_killed",),
    )
    residue = assert_no_run_residue(docker_client, _client.run_id)
    record_evidence(
        "S-21-sandbox-oom",
        {
            "status": result.status.value,
            "error_reason": result.error_reason,
            "exit_code": result.exit_code,
            "oom_killed_diagnostic": result.diagnostics.get("oom_killed"),
            "duration_seconds": round(result.duration_seconds, 3),
            "residue_after_run": residue,
            "memory_limit": OOM_MEMORY_LIMIT,
        },
    )

    # 六态契约：OOM 归到 SANDBOX_ERROR/OOM_KILLED，绝不新造状态，也绝不当成功。
    assert result.status is ExecutionStatus.SANDBOX_ERROR, result.model_dump()
    assert result.error_reason == "OOM_KILLED"
    assert result.diagnostics["oom_killed"] is True
    assert result.exit_code == 137
    assert result.status is not ExecutionStatus.PASS
    assert result.tests_passed == 0
    assert result.is_environment_problem is True
    assert execution_route(result.status) == "finalize", "OOM 不得进入诊断修复循环"

    # OOM 之后容器必须被回收。
    assert residue == []


@pytest.mark.docker
def test_oom_evidence_survives_a_cleanup_failure(
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace({"test_memory_hog.py": MEMORY_HOG})
    sandbox, client = make_sandbox(
        create_overrides={"mem_limit": OOM_MEMORY_LIMIT, "memswap_limit": OOM_MEMORY_LIMIT},
        fail_remove=True,
    )

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec_with(timeout_seconds=120),
        tests=("test_memory_hog.py::test_allocates_until_killed",),
    )
    leaked = assert_no_run_residue(docker_client, client.run_id)
    record_evidence(
        "S-21-sandbox-oom-with-cleanup-failure",
        {
            "status": result.status.value,
            "error_reason": result.error_reason,
            "exit_code": result.exit_code,
            "oom_killed_diagnostic": result.diagnostics.get("oom_killed"),
            "cleanup_error": result.diagnostics.get("cleanup_error"),
            "leaked_container_ids": leaked,
        },
    )

    # 清理失败被记录，但没有抹掉原始 OOM 判定。
    assert result.status is ExecutionStatus.SANDBOX_ERROR
    assert result.error_reason == "OOM_KILLED"
    assert result.diagnostics["oom_killed"] is True
    assert result.exit_code == 137
    assert "注入的删除失败" in str(result.diagnostics["cleanup_error"])

    assert leaked, "注入的删除失败本应留下待清理容器"
    for identifier in leaked:
        labels = (
            docker_client.api.inspect_container(identifier).get("Config", {}).get("Labels") or {}
        )
        assert labels.get("codepilot") == "1", f"拒绝回收非 CodePilot 拥有的容器: {identifier}"
        docker_client.api.remove_container(identifier, force=True)
    assert assert_no_run_residue(docker_client, client.run_id) == []
