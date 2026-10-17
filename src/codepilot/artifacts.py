"""Artifact 目录（02 §18.3）。

``artifacts/runs/<run_id>/`` 是一次运行的全部现场。目录布局在 02 里已经定死，
所以路径拼接集中在这里，不在各模块里手写字符串——否则某个模块少写了一层
``attempts/`` 就会让 Run Report 里的链接全部失效，而且不会有任何报错。

所有写入路径都要过 ``_resolve``：``run_id`` 与 ``relpath`` 都可能来自外部
输入（API 请求、benchmark 实例 id），拼出 ``../`` 就能写到 artifacts 外面去。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from codepilot.observability.redaction import redact
from codepilot.schemas.retrieval import RetrievalResult

__all__ = ["ArtifactStore", "RetrievalSnapshotStore", "SnapshotError"]

#: 02 §18.3 定义的固定文件名。集中列出，让拼错成为 ImportError 而不是静默的
#: 空文件。
CONFIG_YAML = "config.yaml"
STATE_FINAL = "state_final.json"
EVENTS_JSONL = "events.jsonl"
LLM_CALLS_JSONL = "llm_calls.jsonl"
LLM_REQUESTS_JSONL = "llm_requests.jsonl"
PLAN_TOOLS_JSON = "plan_tools.json"
PLAN_SEMANTIC_REJECTIONS_JSONL = "plan_rejections.jsonl"
RETRIEVAL_JSON = "retrieval.json"
REGRESSION_JSON = "regression.json"
REVIEW_JSON = "review.json"
REPORT_JSON = "report.json"
REPORT_MD = "report.md"

PATCH_DIFF = "patch.diff"
VALIDATION_JSON = "validation.json"
JUNIT_XML = "junit.xml"
STDOUT_LOG = "stdout.log"
STDERR_LOG = "stderr.log"
DIAGNOSIS_JSON = "diagnosis.json"
EXECUTION_JSON = "execution.json"


class ArtifactStore:
    """一次运行的 artifact 根目录。

    典型用法::

        store = ArtifactStore(root=settings.artifacts_dir, run_id="run_001")
        store.write_json(ArtifactStore.STATE_FINAL, state.model_dump(mode="json"))
    """

    def __init__(self, root: Path, run_id: str) -> None:
        if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
            raise ValueError(f"非法 run_id: {run_id!r}")
        self._root = Path(root).resolve()
        self._run_id = run_id
        self._run_dir = self._root / "runs" / run_id
        self._run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def attempt_dir(self, iteration: int) -> Path:
        """``attempts/01/``、``attempts/02/``……两位数，便于字典序排序。"""
        if iteration < 1:
            raise ValueError(f"iteration 从 1 开始，收到 {iteration}")
        path = self._run_dir / "attempts" / f"{iteration:02d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve(self, relpath: str) -> Path:
        """解析并做越界检查，**不**产生任何文件系统副作用。"""
        target = (self._run_dir / relpath).resolve()
        if not target.is_relative_to(self._run_dir):
            raise ValueError(f"artifact 路径越界: {relpath!r}")
        return target

    def path_for(self, relpath: str) -> Path:
        """把相对路径解析成绝对路径，确认未越界，并创建父目录。

        读路径（``read_text`` / ``exists``）不走这里——否则一次"文件在不在"
        的检查会顺手把空目录建出来，让目录树看起来像有产物。
        """
        target = self._resolve(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def write_text(self, relpath: str, text: str) -> Path:
        target = self.path_for(relpath)
        target.write_text(text, encoding="utf-8", newline="\n")
        return target

    def write_bytes(self, relpath: str, data: bytes) -> Path:
        target = self.path_for(relpath)
        target.write_bytes(data)
        return target

    def write_json(self, relpath: str, obj: Any, *, redact_values: bool = False) -> Path:
        """写 JSON。

        ``redact_values=False`` 是默认值，因为多数 artifact（patch、junit、
        状态快照）本来就不含密钥，逐次遍历大对象纯属浪费。写**配置**或
        原始 LLM 请求/响应时必须显式传 ``redact_values=True``。
        """
        payload = redact(obj) if redact_values else obj
        return self.write_text(relpath, json.dumps(payload, ensure_ascii=False, indent=2))

    def read_text(self, relpath: str) -> str:
        return self._resolve(relpath).read_text(encoding="utf-8")

    def read_json(self, relpath: str) -> Any:
        return json.loads(self.read_text(relpath))

    def exists(self, relpath: str) -> bool:
        return self._resolve(relpath).exists()

    def attempt_relpath(self, iteration: int, name: str) -> str:
        """给 patch/junit 这类轮次内文件用的相对路径。"""
        return f"attempts/{iteration:02d}/{name}"

    def relpath(self, path: Path) -> str:
        """把本 run 内的绝对路径转成可写进 Report 的相对路径（POSIX 分隔符）。

        Report 里的路径要跨平台可读，Windows 的反斜杠写进去在别处就打不开了。
        """
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self._run_dir):
            raise ValueError(f"路径不在本 run 目录内: {path}")
        return resolved.relative_to(self._run_dir).as_posix()


class SnapshotError(ValueError):
    """A retrieval snapshot is missing, corrupted or could not be committed."""


class RetrievalSnapshotStore:
    """Content-addressed complete RetrievalResult inside the existing run artifacts.

    IDs hash the versioned canonical payload including the upstream snapshot_id.
    Writes use a temporary file + fsync + atomic replace. No successful ID is
    returned until readback validates both schema and content hash.
    """

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def path_for(self, snapshot_id: str) -> Path:
        if re.fullmatch(r"retrieval-sha256-[0-9a-f]{64}", snapshot_id) is None:
            raise SnapshotError("Invalid retrieval snapshot ID")
        return self.artifacts._resolve(f"retrieval_snapshots/{snapshot_id}.json")

    def save(self, retrieval: RetrievalResult) -> str:
        # Revalidate rather than trusting model_copy(update=...) bypasses.
        result = RetrievalResult.model_validate_json(retrieval.model_dump_json())
        payload = {"version": 1, "retrieval": result.model_dump(mode="json")}
        canonical = _snapshot_json(payload)
        snapshot_id = "retrieval-sha256-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        target = self.path_for(snapshot_id)
        if target.exists():
            self.load(snapshot_id)
            return snapshot_id
        temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=target.parent, suffix=".tmp", delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(canonical.encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(target)
            self.load(snapshot_id)
        except OSError as error:
            raise SnapshotError(f"Cannot commit retrieval snapshot: {snapshot_id}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return snapshot_id

    def load(self, snapshot_id: str) -> RetrievalResult:
        target = self.path_for(snapshot_id)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            digest = hashlib.sha256(_snapshot_json(payload).encode("utf-8")).hexdigest()
            if snapshot_id != "retrieval-sha256-" + digest or payload["version"] != 1:
                raise ValueError("Snapshot hash/version mismatch")
            return RetrievalResult.model_validate(payload["retrieval"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise SnapshotError(
                f"Missing or corrupted retrieval snapshot: {snapshot_id}"
            ) from error


def _snapshot_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
