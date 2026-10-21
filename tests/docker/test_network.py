"""S-19：真实 Docker 网络隔离验收。

隔离结论必须同时有**行为证据**（容器内部确实连不出去）和**配置证据**
（daemon inspect 显示测试阶段没有任何网络入口），两者都来自真实容器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codepilot.sandbox.docker import execution_container_config
from codepilot.schemas.enums import ExecutionStatus
from conftest import ContainerObserver, spec_with

#: 六条探针全部在容器内执行。任何一条"连上了"都会让该用例失败，
#: 因此不存在"网络其实通着但报告显示通过"的假阳性。
NETWORK_PROBE = '''"""S-19 受控探针：验证测试执行阶段真的没有网络。"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request


def _interface_names() -> list[str]:
    with open("/proc/net/dev", encoding="utf-8") as handle:
        return [line.split(":", 1)[0].strip() for line in list(handle)[2:]]


def test_only_loopback_interface_exists() -> None:
    assert _interface_names() == ["lo"]


def test_no_default_route() -> None:
    with open("/proc/net/route", encoding="utf-8") as handle:
        rows = [line.split() for line in list(handle)[1:]]
    assert [row for row in rows if len(row) > 1 and row[1] == "00000000"] == []


def test_tcp_connection_to_public_resolver_fails() -> None:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=5)
    except OSError:
        return
    raise AssertionError("测试阶段仍然能建立外部 TCP 连接")


def test_dns_lookup_of_external_host_fails() -> None:
    try:
        socket.getaddrinfo("pypi.org", 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return
    raise AssertionError("测试阶段仍然能解析外部域名")


def test_https_fetch_fails() -> None:
    try:
        urllib.request.urlopen("https://pypi.org/simple/", timeout=5)
    except (urllib.error.URLError, OSError):
        return
    raise AssertionError("测试阶段仍然能访问外部 HTTPS")


def test_pytest_is_available_from_the_built_image() -> None:
    """依赖是联网的环境构建阶段装进去的，测试阶段只是继承了产物而非网络。"""
    import pytest

    assert pytest.__version__
'''

PASSING_TEST = "def test_probe_anchor():\n    assert True\n"


def _host_config(attrs: dict[str, Any]) -> dict[str, Any]:
    return attrs.get("HostConfig", {})


@pytest.mark.docker
def test_in_container_connectivity_probes_all_fail(
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace({"test_network_isolation.py": NETWORK_PROBE})
    sandbox, _client = make_sandbox()

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec_with(),
        tests=("test_network_isolation.py",),
    )

    record_evidence(
        "S-19-in-container-probes",
        {
            "status": result.status.value,
            "tests_total": len(result.tests),
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
            "node_ids": [case.node_id for case in result.tests],
            "exit_code": result.exit_code,
        },
    )

    # 探针全绿 == 容器内确实连不上外网；有一条连上了就会是 FAIL 而不是 PASS。
    assert result.status is ExecutionStatus.PASS, result.model_dump()
    assert result.tests_failed == 0
    assert result.tests_passed == 6, [case.node_id for case in result.tests]


@pytest.mark.docker
def test_execution_container_has_no_network_entrypoint(
    docker_client: Any,
    sandbox_environment: Any,
    fixture_workspace: Any,
    make_sandbox: Any,
    observer: ContainerObserver,
    acceptance_run_id: str,
    record_evidence: Any,
) -> None:
    repo = fixture_workspace({"test_anchor.py": PASSING_TEST})
    sandbox, _client = make_sandbox()

    result = sandbox.run_tests(
        workspace=repo.path,
        environment=sandbox_environment,
        env_spec=spec_with(),
        tests=("test_anchor.py::test_probe_anchor",),
    )
    observed = observer.ids_for_run(acceptance_run_id)
    assert observed, "没有观察到真实容器，配置证据不成立"

    # 本轮会观察到两种容器：跑测试的那个（只读根、挂 /workspace 与 /out）和
    # 负责上传的 staging 容器（挂 /stage-ws 与 /stage-out）。断言按角色挑容器，
    # 而不是按出现顺序取第一个。
    inspected = {identifier: observer.inspect_of(identifier) for identifier in observed}

    def destinations(attrs: dict[str, Any]) -> set[str]:
        return {item.get("Destination") for item in (attrs.get("Mounts") or [])}

    test_ids = [
        identifier
        for identifier, attrs in inspected.items()
        if {"/workspace", "/out"} <= destinations(attrs)
    ]
    stage_ids = [
        identifier
        for identifier, attrs in inspected.items()
        if destinations(attrs) & {"/stage-ws", "/stage-out"}
    ]
    assert len(test_ids) == 1, [destinations(item) for item in inspected.values()]
    assert len(stage_ids) == 1, "工作区上传必须由一个 staging 容器代做"
    attrs = inspected[test_ids[0]]
    host = _host_config(attrs)
    mounts = attrs.get("Mounts") or []

    record_evidence(
        "S-19-daemon-config",
        {
            "container_id": test_ids[0],
            "staging_container_id": stage_ids[0],
            "network_mode": host.get("NetworkMode"),
            "privileged": host.get("Privileged"),
            "cap_drop": host.get("CapDrop"),
            "security_opt": host.get("SecurityOpt"),
            "binds": host.get("Binds"),
            "mount_destinations": [item.get("Destination") for item in mounts],
            "mount_sources": [item.get("Source") for item in mounts],
            "status": result.status.value,
            "staging_binds": (_host_config(inspected[stage_ids[0]]).get("Binds") or []),
            "staging_network_mode": _host_config(inspected[stage_ids[0]]).get("NetworkMode"),
            "staging_read_only": _host_config(inspected[stage_ids[0]]).get("ReadonlyRootfs"),
        },
    )

    assert result.status is ExecutionStatus.PASS, result.model_dump()
    assert host.get("NetworkMode") == "none"
    assert host.get("Privileged") is False
    assert host.get("NetworkMode") not in {"host", "bridge", "container:base"}
    assert host.get("CapDrop") == ["ALL"]
    assert "no-new-privileges" in (host.get("SecurityOpt") or [])

    # 传输载体是 daemon 侧的命名卷：**不允许任何宿主 bind**（宿主路径在
    # WSL→Docker Desktop 这种拓扑下根本不被 daemon 看见，容器会拿到空目录）。
    assert {item.get("Destination") for item in mounts} == {"/workspace", "/out"}
    assert all(item.get("Type") == "volume" for item in mounts), mounts
    assert not [item for item in mounts if item.get("Type") == "bind"]
    for item in mounts:
        assert "docker.sock" not in str(item.get("Source", "")).lower()
        assert "docker.sock" not in str(item.get("Destination", "")).lower()
    binds = host.get("Binds") or []
    assert all(not str(item).split(":")[0].startswith(("/", "\\")) for item in binds), binds
    assert not [b for b in binds if "docker.sock" in str(b).lower()]

    # staging 容器同样没有网络、也没有宿主路径可写：它只是把归档搬进卷。
    staging_host = _host_config(inspected[stage_ids[0]])
    assert staging_host.get("NetworkMode") == "none"
    assert not [
        b for b in (staging_host.get("Binds") or []) if str(b).split(":")[0].startswith("/")
    ]


@pytest.mark.docker
def test_execution_config_contains_no_isolation_bypass() -> None:
    """代码审计：配置里根本不存在可以绕过隔离的参数，也不存在宿主路径。"""
    config = execution_container_config(
        image_ref="image",
        workspace_volume="codepilot-token-ws",
        output_volume="codepilot-token-out",
        command="python -m pytest --junitxml=/out/junit.xml",
        environment={},
        created_at=1.0,
    )
    assert config["network_mode"] == "none"
    assert config["privileged"] is False
    assert config["cap_drop"] == ["ALL"]
    assert config["security_opt"] == ["no-new-privileges"]
    assert config["read_only"] is True
    assert config["user"] == "1000:1000"
    assert sorted(config["volumes"]) == ["codepilot-token-out", "codepilot-token-ws"]
    assert all(not Path(name).is_absolute() for name in config["volumes"])
    assert all("docker.sock" not in name.lower() for name in config["volumes"])
