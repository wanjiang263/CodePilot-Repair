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
    attrs = observer.inspect_of(observed[0])
    host = _host_config(attrs)
    mounts = attrs.get("Mounts") or []

    record_evidence(
        "S-19-daemon-config",
        {
            "container_id": observed[0],
            "network_mode": host.get("NetworkMode"),
            "privileged": host.get("Privileged"),
            "cap_drop": host.get("CapDrop"),
            "security_opt": host.get("SecurityOpt"),
            "binds": host.get("Binds"),
            "mount_destinations": [item.get("Destination") for item in mounts],
            "mount_sources": [item.get("Source") for item in mounts],
            "status": result.status.value,
        },
    )

    assert result.status is ExecutionStatus.PASS, result.model_dump()
    assert host.get("NetworkMode") == "none"
    assert host.get("Privileged") is False
    assert host.get("NetworkMode") not in {"host", "bridge", "container:base"}
    assert host.get("CapDrop") == ["ALL"]
    assert "no-new-privileges" in (host.get("SecurityOpt") or [])

    # 只有 workspace 与 out 两个 bind 挂载，没有 Docker Socket、没有额外卷。
    assert {item.get("Destination") for item in mounts} == {"/workspace", "/out"}
    assert all(item.get("Type") == "bind" for item in mounts)
    for item in mounts:
        assert "docker.sock" not in str(item.get("Source", "")).lower()
        assert "docker.sock" not in str(item.get("Destination", "")).lower()
    assert not [b for b in (host.get("Binds") or []) if "docker.sock" in str(b).lower()]


@pytest.mark.docker
def test_execution_config_contains_no_isolation_bypass(tmp_path: Path) -> None:
    """代码审计：配置里根本不存在可以绕过隔离的参数。"""
    workspace = tmp_path / "workspace"
    output = tmp_path / "out"
    workspace.mkdir()
    output.mkdir()
    config = execution_container_config(
        image_ref="image",
        workspace=workspace,
        output_dir=output,
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
    assert all("docker.sock" not in str(key).lower() for key in config["volumes"])
