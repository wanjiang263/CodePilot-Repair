"""S-18：真实 Docker 超时终止验收。

一个死循环测试被投进真实容器执行；`DockerSandbox` 必须在 ``EnvSpec.timeout_seconds``
到点后 kill 并 remove 容器，并把结果判定为 ``TIMEOUT``——而不是被清理异常改写成
FAIL 或 PASS。所有断言都读自真实 daemon 的容器列表与 inspect 输出。
"""

from __future__ import annotations

from typing import Any

import pytest
from docker.errors import NotFound

from codepilot.schemas.enums import ExecutionStatus
from conftest import (
    ContainerObserver,
    assert_no_run_residue,
    spec_with,
)

HANGING_TEST = '''"""受控死循环夹具：只睡眠，不吃 CPU。"""

import time


def test_never_returns() -> None:
    while True:
        time.sleep(0.05)
'''

TIMEOUT_SECONDS = 15


@pytest.mark.docker
def test_infinite_loop_is_killed_and_reported_as_timeout(
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    observer: ContainerObserver,
    acceptance_run_id: str,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace({"test_hang.py": HANGING_TEST})
    sandbox, _client = make_sandbox()
    spec = spec_with(timeout_seconds=TIMEOUT_SECONDS)

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec,
        tests=("test_hang.py::test_never_returns",),
    )

    observed = observer.ids_for_run(acceptance_run_id)
    residue = assert_no_run_residue(docker_client, acceptance_run_id)
    inspect_snapshot = observer.inspect_of(observed[0]) if observed else {}
    states = observer.states.get(observed[0], []) if observed else []

    record_evidence(
        "S-18-timeout",
        {
            "container_ids": observed,
            "container_names": [
                (observer.inspect_of(item).get("Name") or "").lstrip("/") for item in observed
            ],
            "observed_states": states,
            "status": result.status.value,
            "error_reason": result.error_reason,
            "exit_code": result.exit_code,
            "duration_seconds": round(result.duration_seconds, 3),
            "configured_timeout_seconds": spec.timeout_seconds,
            "residue_after_run": residue,
            "network_mode": inspect_snapshot.get("HostConfig", {}).get("NetworkMode"),
        },
    )

    # 1) 状态必须是 TIMEOUT，且带明确的超时原因码。
    assert result.status is ExecutionStatus.TIMEOUT, result.model_dump()
    assert result.error_reason == "EXECUTION_TIMEOUT"

    # 2) 容器确实真实存在并运行过（不是 Mock 出来的等待）。
    assert observed, "没有观察到任何真实容器，验收不成立"
    assert "running" in states, f"容器从未进入 running 状态: {states}"

    # 3) 等待时长受配置约束：到点就终止，不会无限等。
    # 下界留 1 秒容差：duration 取的是宿主墙钟，WSL2 的 Hyper-V 时间同步会把时钟
    # 往回步进，实测出现过 14.94s 对 15s 上界的边界假失败（同一用例复跑两次均 > 15s）。
    # 上界保持严格，所以"没到点就提前收工"依然会被抓住。
    assert result.duration_seconds >= TIMEOUT_SECONDS - 1.0, result.duration_seconds
    assert result.duration_seconds < TIMEOUT_SECONDS + 45, result.duration_seconds

    # 4) 超时后容器被 kill + remove：daemon 侧再也查不到它。
    assert residue == []
    for identifier in observed:
        with pytest.raises(NotFound):
            docker_client.api.inspect_container(identifier)

    # 5) 六态契约没有被污染：既不是 PASS 也不是 FAIL，也没有伪造退出码。
    assert result.exit_code is None
    assert result.tests_passed == 0 and result.tests_failed == 0
    assert result.diagnostics["execution_backend"] == "docker"
    assert result.diagnostics["oom_killed"] is False

    # 6) 隔离契约在超时路径上同样成立。
    host_config = inspect_snapshot.get("HostConfig", {})
    assert host_config.get("NetworkMode") == "none"
    assert host_config.get("Privileged") is False


@pytest.mark.docker
def test_timeout_survives_a_cleanup_failure(
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    acceptance_run_id: str,
    record_evidence: Any,
) -> None:
    """清理失败必须留下证据，但不能把 TIMEOUT 改写掉。"""
    repo = fixture_workspace({"test_hang.py": HANGING_TEST})
    sandbox, _client = make_sandbox(fail_remove=True)
    spec = spec_with(timeout_seconds=5)

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec,
        tests=("test_hang.py::test_never_returns",),
    )
    leaked = assert_no_run_residue(docker_client, acceptance_run_id)

    record_evidence(
        "S-18-timeout-with-cleanup-failure",
        {
            "status": result.status.value,
            "error_reason": result.error_reason,
            "cleanup_error": result.diagnostics.get("cleanup_error"),
            "leaked_container_ids": leaked,
            "configured_timeout_seconds": spec.timeout_seconds,
        },
    )

    # 原始判定保持 TIMEOUT，既没被降级成 FAIL 也没被伪造成 PASS。
    assert result.status is ExecutionStatus.TIMEOUT, result.model_dump()
    assert result.error_reason == "EXECUTION_TIMEOUT"
    cleanup_error = result.diagnostics.get("cleanup_error")
    assert isinstance(cleanup_error, str) and "注入的删除失败" in cleanup_error

    # 故障注入结束后，按已验证归属（codepilot=1 + 本 run 标签 + 明确 id）定向回收。
    assert leaked, "注入的删除失败本应留下待清理容器"
    for identifier in leaked:
        labels = (
            docker_client.api.inspect_container(identifier).get("Config", {}).get("Labels")
        ) or {}
        assert labels.get("codepilot") == "1", f"拒绝回收非 CodePilot 拥有的容器: {identifier}"
        docker_client.api.remove_container(identifier, force=True)
    assert assert_no_run_residue(docker_client, acceptance_run_id) == []

    # 注入期间被容器占住的命名卷删不掉（这正是 cleanup_error 要报的内容）；容器回收后
    # 必须能被产品自己的过期扫描拿回来，不能靠人工或 docker volume prune。
    leftover = [
        str(row.get("Name"))
        for row in (
            docker_client.api.volumes(filters={"label": "codepilot=1"}).get("Volumes") or []
        )
        if str(row.get("Name", "")).startswith("codepilot-")
        and not str(row.get("Name", "")).startswith("codepilot-inspect-")
    ]
    reclaimed = sandbox.cleanup(older_than_seconds=0)
    assert set(leftover) <= set(reclaimed), (leftover, reclaimed)
    assert not [
        str(row.get("Name"))
        for row in (
            docker_client.api.volumes(filters={"label": "codepilot=1"}).get("Volumes") or []
        )
        if str(row.get("Name", "")).startswith("codepilot-")
    ], "传输用卷必须被产品自身的扫描回收，不留引擎侧垃圾"
