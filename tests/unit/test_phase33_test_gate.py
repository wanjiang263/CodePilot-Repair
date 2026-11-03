"""Phase33 T03: phase32_test_gate.py must isolate pytest basetemp per group run.

D2 旧行为（已删除，非改口径）：``pytest_temp_root()`` 返回全局
``%TEMP%/pytest-of-<user>``，``clear_pytest_temp()`` 在每组运行前删除其**全部**
子项，会误删并发 pytest 进程的数据。新行为：每组每次运行生成独立
``<SCRATCH>/pytest/<suite>-run<n>-<timestamp>`` 并作为 ``--basetemp`` 传给子进程，
运行结束只删除该目录本身。

所有测试都不真跑 pytest：monkeypatch ``subprocess.run`` 捕获命令。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "phase32_test_gate.py"

_spec = importlib.util.spec_from_file_location("phase32_test_gate_t03", SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

BASETEMP_NAME_RE = re.compile(r"^(?P<suite>[A-Za-z0-9_.-]+)-run(?P<run>\d+)-\d{8}T\d{6}-\d{6}$")


def _install_fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Monkeypatch gate.subprocess.run; materialise basetemp dirs like real pytest."""
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):  # test double; annotations intentionally loose
        commands.append([str(part) for part in command])
        if command[1:3] == ["-m", "pytest"]:
            for part in command:
                text = str(part)
                if text.startswith("--basetemp="):
                    basetemp = Path(text.split("=", 1)[1])
                    (basetemp / "test_tmp0").mkdir(parents=True, exist_ok=True)
                    (basetemp / "test_tmp0" / "junk.bin").write_bytes(b"x" * 2048)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="ripgrep 14.1.0 (fake)\n", stderr="")

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    return commands


def _pytest_command(commands: list[list[str]]) -> list[str]:
    matches = [cmd for cmd in commands if cmd[1:3] == ["-m", "pytest"]]
    assert len(matches) == 1, f"expected exactly one pytest invocation, got {commands!r}"
    return matches[0]


def _basetemp_of(command: list[str]) -> Path:
    tokens = [part for part in command if part.startswith("--basetemp=")]
    assert len(tokens) == 1, f"pytest command must carry exactly one --basetemp: {command!r}"
    return Path(tokens[0].split("=", 1)[1])


def test_global_temp_helpers_are_deleted_not_reworded() -> None:
    """旧全局根 API 必须彻底消失，且模块不再引用 tempfile。"""
    assert not hasattr(gate, "pytest_temp_root")
    assert not hasattr(gate, "clear_pytest_temp")
    source = SCRIPT.read_text(encoding="utf-8")
    assert "import tempfile" not in source
    assert "gettempdir" not in source


def test_default_scratch_stays_backward_compatible() -> None:
    assert gate.resolve_scratch(None) == ROOT / ".codex-pytest-tmp" / "phase32"
    assert gate.resolve_scratch(".codex-pytest-tmp/phase33") == (
        ROOT / ".codex-pytest-tmp" / "phase33"
    )
    absolute = Path("C:/somewhere/else")
    assert gate.resolve_scratch(str(absolute)) == absolute


def test_run_group_command_has_isolated_basetemp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _install_fake_subprocess(monkeypatch)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    record = gate.run_group("unit", 1, dict(os.environ), "tests/unit/test_x.py", [], scratch)

    command = _pytest_command(commands)
    basetemp = _basetemp_of(command)
    assert basetemp.parent == scratch / "pytest"
    match = BASETEMP_NAME_RE.match(basetemp.name)
    assert match is not None, basetemp.name
    assert match.group("suite") == "unit"
    assert match.group("run") == "1"

    # 组目录在运行结束后被清理，且清理结果记录在 record 里。
    assert not basetemp.exists()
    reclaimed = record["temp_reclaimed"]
    assert reclaimed == {
        "path": str(basetemp),
        "existed": True,
        "removed": True,
        "bytes_removed": 2048,
    }
    assert record["exit_code"] == 0
    assert f"--basetemp={basetemp}" in record["command"]


def test_main_leaves_fake_global_pytest_root_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """sentinel：伪造全局 %TEMP%/pytest-of-<user>，跑完 gate 主流程后必须原样存在。"""
    commands = _install_fake_subprocess(monkeypatch)
    fake_temp = tmp_path / "fake-temp"
    sentinel_group = fake_temp / "pytest-of-testuser" / "pytest-42"
    sentinel_group.mkdir(parents=True)
    (sentinel_group / "concurrent-run-data.txt").write_text("do-not-delete", encoding="utf-8")
    monkeypatch.setenv("TEMP", str(fake_temp))
    monkeypatch.setenv("TMP", str(fake_temp))

    scratch = tmp_path / "gate-scratch"
    summary = scratch / "test-gate.json"
    exit_code = gate.main(
        [
            "--scratch",
            str(scratch),
            "--target",
            "smoke=tests/unit/test_phase33_test_gate.py",
            "--repeat",
            "1",
            "--min-free-mb",
            "0",
            "--min-headroom-mb",
            "0",
        ]
    )

    assert exit_code == 0
    # sentinel 全局根一根手指都没碰
    assert sentinel_group.is_dir()
    assert (sentinel_group / "concurrent-run-data.txt").read_text(encoding="utf-8") == (
        "do-not-delete"
    )
    assert sorted(p.name for p in (fake_temp / "pytest-of-testuser").iterdir()) == ["pytest-42"]
    # 没有任何子进程命令指向伪造全局根
    for command in commands:
        for part in command:
            assert str(fake_temp) not in part

    # gate 自己的组目录被清理，scratch/pytest 下不留残渣
    assert (scratch / "pytest").is_dir()
    assert list((scratch / "pytest").iterdir()) == []

    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["failed_runs"] == []
    assert payload["blocked_runs"] == []
    assert payload["scratch"] == str(scratch)
    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["suite"] == "smoke"
    assert run["temp_reclaimed"]["removed"] is True
    assert Path(run["basetemp"]).parent == scratch / "pytest"
    assert not Path(run["basetemp"]).exists()
    assert run["log"] == gate.display_path(scratch / "gate-smoke-run1.log")
    assert run["junit"] == gate.display_path(scratch / "gate-smoke-run1.xml")
    assert "summary_line" in run


def test_each_repeat_gets_a_distinct_basetemp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _install_fake_subprocess(monkeypatch)
    scratch = tmp_path / "scratch"
    summary = scratch / "test-gate.json"
    exit_code = gate.main(
        [
            "--scratch",
            str(scratch),
            "--target",
            "smoke=tests/unit/test_phase33_test_gate.py",
            "--repeat",
            "2",
            "--min-free-mb",
            "0",
            "--min-headroom-mb",
            "0",
            "--summary",
            str(summary),
        ]
    )
    assert exit_code == 0
    basetemps = [_basetemp_of(cmd) for cmd in commands if cmd[1:3] == ["-m", "pytest"]]
    assert len(basetemps) == 2
    assert basetemps[0] != basetemps[1]
    assert {b.parent for b in basetemps} == {scratch / "pytest"}
    assert all(not b.exists() for b in basetemps)
    assert list((scratch / "pytest").iterdir()) == []
