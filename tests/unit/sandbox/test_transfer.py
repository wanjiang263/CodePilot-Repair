"""归档构造、回传解包与 staging 上传的边界测试。

``FakeDaemon`` 复刻了本轮实测到的两条 daemon 行为，产品代码必须绕开它们：

* 只读 rootfs 的容器上 ``put_archive`` 直接 400（``container rootfs is marked
  read-only``），所以上传必须由 staging 容器代做；
* ``get_archive`` 以请求目录名为归档根返回条目（``out/junit.xml``）。
"""

from __future__ import annotations

import io
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from codepilot.sandbox import transfer
from codepilot.sandbox.transfer import (
    CONTAINER_UID,
    MAX_RETURNED_FILE_BYTES,
    TransferError,
    _extract_returned_archive,
    collect_output,
    stage_into_volumes,
    upload_archive,
)

LABELS = {"codepilot": "1"}


class ReadOnlyRootfsError(RuntimeError):
    """daemon 对只读 rootfs 容器的 ``put_archive`` 行为。"""


class FakeStaging:
    def __init__(self, daemon: FakeDaemon, kwargs: dict[str, Any]) -> None:
        self.daemon = daemon
        self.kwargs = kwargs
        self.id = f"stage-{len(daemon.created)}"
        self.removed = False

    def start(self) -> None:
        self.daemon.started.append(self.id)

    def put_archive(self, path: str, data: bytes) -> bool:
        self.daemon.put_archive_calls.append((self.id, path, len(data)))
        self.daemon.events.append("put")
        if self.kwargs.get("read_only"):
            raise ReadOnlyRootfsError("container rootfs is marked read-only")
        target = {"/stage-ws": "ws", "/stage-out": "out"}[path]
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    handle = archive.extractfile(member)
                    self.daemon.volumes[target][member.name] = handle.read() if handle else b""
        return self.daemon.put_archive_result

    def exec_run(self, command: Any, **kwargs: Any) -> Any:
        text = str(command)
        self.daemon.exec_commands.append(command)
        if "chown" in text:
            self.daemon.events.append("chown")
            return type("Exec", (), {"exit_code": self.daemon.exec_exit_code, "output": b"dir"})()
        if "wipe" in text:
            self.daemon.events.append("wipe")
            if self.daemon.wipe_exit_code == 0:
                # 真实 daemon 里这一步会删掉镜像预填进卷的内容（含 .git）。
                for files in self.daemon.volumes.values():
                    for name in [key for key in files if key.startswith(".git")]:
                        del files[name]
            return type(
                "Exec",
                (),
                {
                    "exit_code": self.daemon.wipe_exit_code,
                    "output": b"not-empty:/stage-ws",
                },
            )()
        self.daemon.install_runs += 1
        return type("Exec", (), {"exit_code": 0, "output": b"installed"})()

    def remove(self, *, force: bool) -> None:
        assert force is True
        if self.daemon.remove_error:
            raise RuntimeError("daemon went away while removing")
        self.removed = True


class FakeContainersView:
    def __init__(self, daemon: FakeDaemon) -> None:
        self.daemon = daemon

    def create(self, **kwargs: Any) -> FakeStaging:
        container = FakeStaging(self.daemon, kwargs)
        self.daemon.created.append(container)
        return container


class FakeApi:
    def __init__(self, daemon: FakeDaemon) -> None:
        self.daemon = daemon

    def get_archive(self, container: str, path: str, **kwargs: Any) -> tuple[Any, dict[str, int]]:
        assert path == "/out", path
        payload = _returned_tar(self.daemon.volumes["out"])
        return (_chunks(payload), {"size": len(payload)})


def _returned_tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        root = tarfile.TarInfo("out")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, payload in files.items():
            info = tarfile.TarInfo(f"out/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _chunks(payload: bytes, size: int = 4096) -> Iterator[bytes]:
    stream = io.BytesIO(payload)
    while chunk := stream.read(size):
        yield chunk


class FakeDaemon:
    def __init__(self) -> None:
        #: 新卷会被镜像在该路径下的内容预填，这是 staging 必须先清空的原因。
        self.volumes: dict[str, dict[str, bytes]] = {
            "ws": {".git/HEAD": b"ref: refs/heads/main"},
            "out": {},
        }
        self.created: list[FakeStaging] = []
        self.started: list[str] = []
        self.events: list[str] = []
        self.install_runs = 0
        self.put_archive_calls: list[tuple[str, str, int]] = []
        self.exec_commands: list[Any] = []
        self.put_archive_result = True
        self.exec_exit_code = 0
        self.wipe_exit_code = 0
        self.remove_error = False
        self.containers = FakeContainersView(self)
        self.api = FakeApi(self)


def stage(client: FakeDaemon, workspace: Path) -> int:
    return stage_into_volumes(
        client,
        image_ref="image",
        workspace=workspace,
        workspace_volume="codepilot-t-ws",
        output_volume="codepilot-t-out",
        labels=LABELS,
    )


def member_names(payload: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as archive:
        return {member.name.rstrip("/"): member for member in archive.getmembers()}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "empty_dir").mkdir()
    script = root / "run.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script.chmod(0o755)
    return root


def test_upload_keeps_sources_and_exec_bit_and_drops_evaluation_material(
    workspace: Path,
) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("url = https://t@x/y.git\n", encoding="utf-8")
    (workspace / "eval").mkdir()
    (workspace / "eval" / "gold.patch").write_text("gold\n", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    members = member_names(upload_archive(workspace))
    assert "src/pkg/mod.py" in members
    assert "empty_dir" in members, "空目录也要留在容器里，否则被测代码可能找不到路径"
    assert members["run.sh"].mode & 0o111, "可执行位必须保留，否则构建脚本跑不起来"
    for name in members:
        assert not name.startswith(("workspace/", "./")), name
    forbidden = [name for name in members if ".git" in name or name.startswith(("eval/", ".env"))]
    assert forbidden == []


def test_upload_uses_the_build_shape_when_arcname_is_requested(workspace: Path) -> None:
    members = member_names(upload_archive(workspace, arcname="workspace"))
    assert "workspace/src/pkg/mod.py" in members, "环境构建路径沿用 workspace/ 根"


def test_upload_normalizes_ownership_to_the_container_user(workspace: Path) -> None:
    for member in member_names(upload_archive(workspace)).values():
        assert (member.uid, member.gid) == (CONTAINER_UID, CONTAINER_UID)


def test_upload_accepts_unicode_names(workspace: Path) -> None:
    (workspace / "docs").mkdir()
    (workspace / "docs" / "中文说明.txt").write_text("你好\n", encoding="utf-8")
    assert "docs/中文说明.txt" in member_names(upload_archive(workspace))


def test_upload_keeps_internal_symlink_as_relative_link(workspace: Path) -> None:
    (workspace / "link.py").symlink_to("src/pkg/mod.py")
    member = member_names(upload_archive(workspace))["link.py"]
    assert member.issym()
    assert member.linkname == "src/pkg/mod.py"


def test_upload_rejects_symlink_that_escapes_workspace(workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN=secret\n", encoding="utf-8")
    (workspace / "escape.py").symlink_to(secret)
    with pytest.raises(TransferError, match="逃逸"):
        upload_archive(workspace)


def test_upload_rejects_special_files(workspace: Path) -> None:
    os.mkfifo(workspace / "pipe")
    with pytest.raises(TransferError, match="special file"):
        upload_archive(workspace)


def test_stage_handsover_through_one_writable_container_and_cleans_it(workspace: Path) -> None:
    daemon = FakeDaemon()
    outcome = stage(daemon, workspace)
    staging = daemon.created[0]
    assert len(daemon.created) == 1, "只允许一个 staging 容器"
    assert staging.kwargs["read_only"] is False, "只读容器收不下上传，见模块 docstring"
    assert staging.kwargs["user"] == "0:0"
    assert staging.kwargs["network_mode"] == "none"
    assert staging.kwargs["cap_drop"] == ["ALL"]
    assert staging.kwargs["cap_add"] == ["CHOWN", "FOWNER", "DAC_OVERRIDE"]
    assert outcome.uploaded_bytes > 0
    assert outcome.cleanup_error is None
    assert daemon.events == ["wipe", "put", "chown"], "必须先清空预填内容，再上传，再移交所有权"
    assert [call[1] for call in daemon.put_archive_calls] == [transfer.STAGE_WORKSPACE]
    assert daemon.volumes["ws"]["src/pkg/mod.py"] == b"VALUE = 1\n"
    assert not any(name.startswith(".git") for name in daemon.volumes["ws"]), (
        "镜像预填进卷的 .git（可能含 base 之后的提交）必须被清掉"
    )
    chown = daemon.exec_commands[-1]
    assert chown[:2] == ["/bin/sh", "-lc"]
    assert "chown 1000:1000 /stage-ws /stage-out" in chown[2], "两个卷根都要移交所有权"
    assert staging.removed, "staging 容器必须用完即删"


def test_stage_refuses_to_upload_into_a_prefilled_volume(workspace: Path) -> None:
    """清空失败就终止，绝不带着镜像内容（含未来的 Git 提交）往下跑。"""
    daemon = FakeDaemon()
    daemon.wipe_exit_code = 17
    with pytest.raises(TransferError, match="预填内容未能清空"):
        stage(daemon, workspace)
    assert daemon.put_archive_calls == []
    assert daemon.created[0].removed


def test_stage_cleanup_failure_is_recorded_without_losing_the_upload(workspace: Path) -> None:
    daemon = FakeDaemon()
    daemon.remove_error = True
    outcome = stage(daemon, workspace)
    assert outcome.uploaded_bytes > 0
    assert "staging 容器清理失败" in (outcome.cleanup_error or "")
    assert daemon.volumes["ws"], "上传本身是成功的"


def test_cleanup_failure_does_not_swallow_an_upload_refusal(workspace: Path) -> None:
    """在 ``finally`` 里 return 会把"上传失败"改写掉，测试就会跑在空工作区上。"""
    daemon = FakeDaemon()
    daemon.put_archive_result = False
    daemon.remove_error = True
    with pytest.raises(TransferError, match="上传失败"):
        stage(daemon, workspace)


def test_stage_reports_refused_upload_as_transfer_error(workspace: Path) -> None:
    daemon = FakeDaemon()
    daemon.put_archive_result = False
    with pytest.raises(TransferError, match="上传失败"):
        stage(daemon, workspace)
    assert daemon.created[0].removed


def test_stage_reports_chown_failure(workspace: Path) -> None:
    daemon = FakeDaemon()
    daemon.exec_exit_code = 1
    with pytest.raises(TransferError, match="所有权"):
        stage(daemon, workspace)
    assert daemon.created[0].removed


def test_collect_returns_files_into_task_directory(tmp_path: Path) -> None:
    daemon = FakeDaemon()
    daemon.volumes["out"]["junit.xml"] = b"<testsuite/>"
    daemon.volumes["out"]["nested/stdout.log"] = "hello🙂\n".encode()
    destination = tmp_path / "collected"
    destination.mkdir()
    names = collect_output(daemon, "container-id", destination=destination)
    assert sorted(names) == ["junit.xml", "nested/stdout.log"]
    assert (destination / "junit.xml").read_bytes() == b"<testsuite/>"
    assert (destination / "nested" / "stdout.log").read_text(encoding="utf-8") == "hello🙂\n"


def test_collect_refuses_before_writing_when_stream_is_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = FakeDaemon()
    destination = tmp_path / "collected"
    destination.mkdir()

    def huge() -> Iterator[bytes]:
        for _ in range(4):
            yield b"x" * 4096

    monkeypatch.setattr(daemon.api, "get_archive", lambda *args, **kwargs: (huge(), {"size": 9}))
    with pytest.raises(TransferError, match="上限"):
        collect_output(daemon, "container-id", destination=destination, max_bytes=8000)
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "out/../escape.txt", "out/../../escape.txt"],
)
def test_extraction_refuses_paths_outside_the_task_directory(tmp_path: Path, name: str) -> None:
    destination = tmp_path / "collected"
    destination.mkdir()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(TransferError):
        _extract_returned_archive(
            io.BytesIO(buffer.getvalue()), destination=destination, max_bytes=999
        )
    assert list(destination.iterdir()) == []


def test_extraction_refuses_links_and_special_entries(tmp_path: Path) -> None:
    destination = tmp_path / "collected"
    destination.mkdir()
    for name, entry_type in (
        ("out/sneaky", tarfile.SYMTYPE),
        ("out/hard", tarfile.LNKTYPE),
        ("out/fifo", tarfile.FIFOTYPE),
    ):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(name)
            info.type = entry_type
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with pytest.raises(TransferError, match="非预期条目类型"):
            _extract_returned_archive(
                io.BytesIO(buffer.getvalue()), destination=destination, max_bytes=999
            )
    assert list(destination.iterdir()) == []


def test_extraction_enforces_the_per_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "collected"
    destination.mkdir()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("out/big.log")
        info.size = 10
        archive.addfile(info, io.BytesIO(b"x" * 10))
    assert MAX_RETURNED_FILE_BYTES > 10
    monkeypatch.setattr(transfer, "MAX_RETURNED_FILE_BYTES", 5)
    with pytest.raises(TransferError, match="单文件上限"):
        _extract_returned_archive(
            io.BytesIO(buffer.getvalue()), destination=destination, max_bytes=999
        )
    assert list(destination.iterdir()) == []
