"""S-22：容器清理与资源泄漏验收。

覆盖 PASS / FAIL / TIMEOUT / OOM / 启动失败 / 清理异常六条分支。每个分支都用
唯一 ``codepilot.acceptance_run`` 标签在 daemon 侧追踪本轮资源：结束后必须一个
都不剩。清理只作用于 CodePilot 自己拥有的容器——其他项目的容器与镜像由会话级
守卫全程看护，任何 prune 式的全局销毁都会让守卫失败。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import pytest
from docker.errors import NotFound

from codepilot.artifacts import ArtifactStore
from codepilot.sandbox.docker import DockerSandbox
from codepilot.schemas.enums import ExecutionStatus
from conftest import (
    RecordingClient,
    assert_no_run_residue,
    spec_with,
)

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_broken():\n    assert False, '受控失败'\n"
HANGING = "import time\n\n\ndef test_never_returns():\n    while True:\n        time.sleep(0.05)\n"
HOGGING = (
    "def test_allocates_until_killed():\n    blocks = []\n"
    "    for _ in range(2048):\n        blocks.append(bytearray(1024 * 1024))\n"
    "    raise AssertionError('没有触发 OOM')\n"
)

SCENARIOS: dict[str, dict[str, Any]] = {
    "pass": {
        "files": {"test_ok.py": PASSING},
        "tests": ("test_ok.py::test_ok",),
        "overrides": {},
        "spec": {},
        "status": ExecutionStatus.PASS,
        "reason": None,
    },
    "fail": {
        "files": {"test_broken.py": FAILING},
        "tests": ("test_broken.py::test_broken",),
        "overrides": {},
        "spec": {},
        "status": ExecutionStatus.FAIL,
        "reason": None,
    },
    "timeout": {
        "files": {"test_hang.py": HANGING},
        "tests": ("test_hang.py::test_never_returns",),
        "overrides": {},
        "spec": {"timeout_seconds": 5},
        "status": ExecutionStatus.TIMEOUT,
        "reason": "EXECUTION_TIMEOUT",
    },
    "oom": {
        "files": {"test_hog.py": HOGGING},
        "tests": ("test_hog.py::test_allocates_until_killed",),
        "overrides": {"mem_limit": "256m", "memswap_limit": "256m"},
        "spec": {"timeout_seconds": 120},
        "status": ExecutionStatus.SANDBOX_ERROR,
        "reason": "OOM_KILLED",
    },
    "start-failure": {
        "files": {"test_ok.py": PASSING},
        "tests": ("test_ok.py::test_ok",),
        "overrides": {"network_mode": "codepilot-no-such-network-acceptance"},
        "spec": {},
        "status": ExecutionStatus.SANDBOX_ERROR,
        "reason": "DOCKER_OR_CONTAINER_ERROR",
    },
}


@pytest.mark.docker
@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_execution_branch_leaves_no_residue(
    scenario_id: str,
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    scenario = SCENARIOS[scenario_id]
    repo = fixture_workspace(scenario["files"])
    sandbox, client = make_sandbox(create_overrides=scenario["overrides"])

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec_with(**scenario["spec"]),
        tests=scenario["tests"],
    )
    residue = assert_no_run_residue(docker_client, client.run_id)

    record_evidence(
        f"S-22-residue-{scenario_id}",
        {
            "status": result.status.value,
            "error_reason": result.error_reason,
            "exit_code": result.exit_code,
            "created_container_ids": client.created_ids,
            "residue_after_run": residue,
            "duration_seconds": round(result.duration_seconds, 3),
        },
    )

    assert result.status is scenario["status"], result.model_dump()
    assert result.error_reason == scenario["reason"]
    # 本轮确实创建过真实容器，且结束时全部回收。
    assert client.created_ids, f"{scenario_id} 分支没有创建任何容器"
    assert residue == []
    for identifier in client.created_ids:
        with pytest.raises(NotFound):
            docker_client.api.inspect_container(identifier)


@pytest.mark.docker
def test_residue_cleanup_only_touches_codepilot_owned_containers(
    docker_client: Any,
    tmp_path: Any,
    record_evidence: Any,
) -> None:
    """过期清理按标签定向：别的项目的容器、以及本轮新建的容器都不许动。"""
    image = "codepilot-sandbox-base:py3.12"
    stale = docker_client.containers.create(
        image=image,
        command=["/bin/sh", "-lc", "sleep 1"],
        detach=True,
        labels={"codepilot": "1", "codepilot.created_at": "1"},
    )
    fresh = docker_client.containers.create(
        image=image,
        command=["/bin/sh", "-lc", "sleep 1"],
        detach=True,
        labels={"codepilot": "1", "codepilot.created_at": str(time.time())},
    )
    # 冒充"其他项目"的容器：没有 codepilot 标签，验收结束后由本测试自己回收。
    foreign = docker_client.containers.create(
        image=image,
        command=["/bin/sh", "-lc", "sleep 1"],
        detach=True,
        labels={"acceptance.foreign_project": "1"},
    )
    sandbox = DockerSandbox(
        artifacts=ArtifactStore(tmp_path / "artifacts", "cleanup-scope"),
        allowed_workspace_root=tmp_path,
        client=RecordingClient(docker_client, run_id="scope-check"),
    )
    try:
        removed = sandbox.cleanup(older_than_seconds=3600)
        still_present = {str(row["Id"]) for row in docker_client.api.containers(all=True)}
        record_evidence(
            "S-22-cleanup-scope",
            {
                "removed": removed,
                "stale_id": stale.id,
                "fresh_id": fresh.id,
                "foreign_id": foreign.id,
                "still_present_after_cleanup": sorted(still_present),
            },
        )
        assert stale.id in removed
        assert fresh.id not in removed
        assert foreign.id not in removed
        assert fresh.id in still_present, "刚创建的 CodePilot 容器被误删"
        assert foreign.id in still_present, "非 CodePilot 拥有的容器被误删"
        assert stale.id not in still_present
    finally:
        # 定向回收本测试自己创建的三个容器（stale 可能已被清理掉）。
        for container in (stale, fresh, foreign):
            with contextlib.suppress(NotFound):
                docker_client.api.remove_container(container.id, force=True)
    present = {str(row["Id"]) for row in docker_client.api.containers(all=True)}
    assert not present & {stale.id, fresh.id, foreign.id}, "本测试创建的容器没有回收干净"


@pytest.mark.docker
def test_cleanup_scopes_every_daemon_query_by_label(
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
) -> None:
    """清理与启动清理都必须带 ``codepilot=1`` 过滤，绝不全局枚举后再删。"""
    repo = fixture_workspace({"test_ok.py": PASSING})
    sandbox, client = make_sandbox()
    sandbox.build_env(
        repository=str(repo.path.resolve()),
        base_commit=repo.head,
        workspace=repo.path,
        env_spec=spec_with(),
        allow_network=False,
    )
    sandbox.cleanup(older_than_seconds=3600)

    assert client.list_calls, "没有发起过任何容器查询"
    for call in client.list_calls:
        assert call.get("filters") == {"label": "codepilot=1"}, call


@pytest.mark.docker
def test_cleanup_failure_keeps_error_information(
    docker_client: Any,
    tmp_path: Any,
    make_sandbox: Any,
    acceptance_run_id: str,
    record_evidence: Any,
) -> None:
    """删除失败必须回报容器身份与原因，并留下可按归属核对的残留。"""
    stale = docker_client.containers.create(
        image="codepilot-sandbox-base:py3.12",
        command=["/bin/sh", "-lc", "sleep 1"],
        detach=True,
        labels={"codepilot": "1", "codepilot.created_at": "1"},
    )
    sandbox, client = make_sandbox(fail_remove=True)
    try:
        with pytest.raises(RuntimeError) as raised:
            sandbox.cleanup(older_than_seconds=3600)
        record_evidence(
            "S-22-cleanup-failure",
            {
                "raised": str(raised.value),
                "stale_id": stale.id,
                "created_by_this_run": client.created_ids,
            },
        )
        assert stale.id[:12] in str(raised.value)
        assert "注入的删除失败" in str(raised.value)
        assert stale.id in {str(row["Id"]) for row in docker_client.api.containers(all=True)}
    finally:
        docker_client.api.remove_container(stale.id, force=True)
    assert assert_no_run_residue(docker_client, acceptance_run_id) == []
