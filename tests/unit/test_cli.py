"""F-17 的证明：CLI 可用；未实现的子命令**明确报错**而不是静默返回成功。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from codepilot.cli import _PLANNED, app

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "codepilot" in result.stdout


def test_help_lists_all_planned_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in _PLANNED:
        assert command in result.stdout


def test_config_check_runs_against_repository_config() -> None:
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0
    assert "llm.yaml" in result.stdout


def test_config_check_strict_can_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 key 时严格检查必须失败，不依赖开发者本机密钥或模型占位值。"""
    from codepilot.llm.config import ProviderSpec

    monkeypatch.setattr(ProviderSpec, "has_api_key", lambda self: False)
    result = runner.invoke(app, ["config", "check", "--strict"])
    assert result.exit_code == 1
    assert "缺 API key" in result.stdout


def test_config_check_strict_passes_with_complete_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from codepilot.llm.config import ProviderSpec

    monkeypatch.setattr(ProviderSpec, "has_api_key", lambda self: True)
    result = runner.invoke(app, ["config", "check", "--strict"])
    assert result.exit_code == 0
    assert "配置完整" in result.stdout


def test_config_show_does_not_leak_secrets() -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    # 只回报"是否已配置"，不回报值本身。
    assert "API key 已配置" in result.stdout


def test_doctor_runs() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Python" in result.stdout


def test_doctor_survives_a_stopped_docker_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker 在 PATH 上、daemon 没起来：``docker info`` 退出码 1，stdout 只有一个
    ``"\\n"``。实测（Docker Desktop 未启动）修复前 doctor 在这里 IndexError 崩掉，
    而它恰恰是用来诊断"docker 为什么不能用"的命令。"""
    import shutil
    import subprocess

    import codepilot.cli as cli

    real_which = shutil.which
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "/fake/docker" if name == "docker" else real_which(name)
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, stdout="\n", stderr="failed to connect to the docker API\n"
        ),
    )

    result = runner.invoke(app, ["doctor"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "failed to connect to the docker API" in result.stdout


@pytest.mark.parametrize("command", sorted(_PLANNED))
def test_unimplemented_commands_fail_loudly(command: str) -> None:
    """03 §2.2：不得把未完成的实现呈现为完成。

    退出码 2 是一个刻意的选择：它不是 argparse 的用法错误（1），
    也不是成功（0），CI 里任何把它当成功的写法都会立刻暴露。
    """
    args = [command]
    if command in {"index", "retrieve", "run-tests", "solve", "bench", "report"}:
        args.append("dummy")
    if command == "solve":
        args += ["--issue", "x", "--base-commit", "abc"]

    result = runner.invoke(app, args)
    assert result.exit_code == 2


def test_unimplemented_message_names_the_phase() -> None:
    result = runner.invoke(app, ["solve", "dummy", "--issue", "x", "--base-commit", "abc"])
    combined = result.stdout + (result.stderr or "")
    assert "尚未实现" in combined
    assert "Phase 5" in combined
