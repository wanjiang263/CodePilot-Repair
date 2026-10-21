"""S-24：真实 Docker 传输验收——卷内容必须恰好是本轮工作区。

Docker 在容器启动时会把**镜像在该挂载路径下的内容复制进新命名卷**
（volume population）。环境镜像本身就是"上一份工作区 + 依赖"的快照，所以如果
不清空，被测容器会同时看到本轮工作区和构建时那份残留——包括 ``.git``。
在官方实例镜像上实测更严重：预填进来的 ``/testbed/.git`` 的 HEAD 是 base commit
之后一个提交（``git rev-list base..HEAD`` = 1），也就是可能含修复提交的对象库。

本用例只做一件与产品行为有关的事：用一个**不含**构建期文件的工作区跑真实测试，
断言容器里看不见那些文件，也看不见 ``.git``。
"""

from __future__ import annotations

from typing import Any

import pytest

from codepilot.schemas.enums import ExecutionStatus
from conftest import assert_no_run_residue, spec_with

PROBE = '''"""容器内工作区内容与本轮上传完全一致的探针。"""

from __future__ import annotations

import os


def test_only_this_rounds_files_are_present() -> None:
    assert os.path.isfile("/workspace/marker_this_round.py")


def test_build_time_image_content_is_not_visible() -> None:
    # 这个文件存在于环境镜像的 /workspace 里（构建夹具留下的）。
    assert not os.path.exists("/workspace/test_image_probe.py"), (
        "镜像预填内容进了卷：卷没有被清空"
    )


def test_no_git_metadata_inside_the_container() -> None:
    assert not os.path.exists("/workspace/.git"), "Git 元数据不应进容器"
'''


@pytest.mark.docker
def test_workspace_volume_is_not_populated_from_the_image(
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    acceptance_run_id: str,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace(
        {
            "test_transfer_probe.py": PROBE,
            "marker_this_round.py": "VALUE = 1\n",
        },
        name="population-check",
    )
    sandbox, _client = make_sandbox()

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec_with(timeout_seconds=180),
        tests=("test_transfer_probe.py",),
    )
    record_evidence(
        "S-24-volume-population",
        {
            "status": result.status.value,
            "exit_code": result.exit_code,
            "tests_total": len(result.tests),
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
            "failed_nodes": [case.node_id for case in result.failed_tests],
            "images_at_build_path": "test_image_probe.py",
        },
    )
    failures = [(case.node_id, case.message) for case in result.failed_tests]
    assert result.status is ExecutionStatus.PASS, failures
    assert result.tests_passed == 3, [case.node_id for case in result.tests]
    assert result.exit_code == 0
    assert assert_no_run_residue(docker_client, acceptance_run_id) == []
