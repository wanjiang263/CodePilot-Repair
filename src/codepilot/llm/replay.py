"""回放录制的 LLM 调用（02 §12，L-12）。

**绝不在找不到记录时回退到真实接口。** 这条规则是整个模块存在的意义：
一旦允许回退，"离线回放"就会在无人察觉的情况下变成一次真实调用——
它要花钱、结果不可复现、而且因为只回退了一部分，产生的是一份
"一半是录制结果、一半是新结果"的混合数据，比全部重跑更误导人。

同一个请求被调用多次时，按录制顺序依次返回（每个指纹一个队列）。
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from codepilot.llm.base import LLMProvider
from codepilot.llm.errors import ReplayMissError
from codepilot.llm.record import CallRecord, error_from_record
from codepilot.llm.types import LLMRequest, LLMResponse, request_fingerprint

__all__ = ["ReplayLLM", "load_records"]


def load_records(path: Path) -> list[CallRecord]:
    """读取录制的 JSONL。空行跳过，坏行直接报错。

    坏行静默跳过会让回放"少了一次调用"，而症状是下游某一步莫名走错分支，
    排查方向会完全跑偏。
    """
    if not path.exists():
        raise FileNotFoundError(f"录制文件不存在: {path}")

    records: list[CallRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(CallRecord.from_json(line))
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{path}:{lineno} 不是合法的录制行: {exc}") from exc
    return records


class ReplayLLM:
    """按指纹回放录制的响应。"""

    def __init__(self, path: Path) -> None:
        self._path = path

        self._queues: dict[str, deque[CallRecord]] = defaultdict(deque)
        self._total = 0
        for record in load_records(path):
            self._queues[record.fingerprint].append(record)
            self._total += 1

        self.calls: list[LLMRequest] = []
        self.misses: list[str] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        fingerprint = request_fingerprint(request)
        queue = self._queues.get(fingerprint)

        if not queue:
            self.misses.append(fingerprint)
            raise ReplayMissError(
                f"回放时找不到匹配的录制（第 {len(self.calls)} 次调用，"
                f"purpose={request.purpose!r}）。"
                "回放不会回退到真实接口——要么重新录制，要么检查流程是否变了。",
                fingerprint=fingerprint,
            )

        record = queue.popleft()
        if record.error is not None:
            # 失败也要如实重放：失败分支是最需要回归的部分。
            raise error_from_record(record)
        return record.to_response()

    def close(self) -> None:
        """无资源可释放。保留是为了满足 ``LLMProvider`` 协议。"""

    # ---------------------------------------------------------------- 完整性检查

    @property
    def total_records(self) -> int:
        return self._total

    @property
    def remaining(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def unconsumed(self) -> dict[str, int]:
        """尚未被消费的指纹及剩余条数。

        回放结束时非空，说明流程比录制时**少走了**几步。这是一种真实的回归，
        但不会表现为报错，所以需要显式检查（见 ``assert_fully_consumed``）。
        """
        return {fp: len(q) for fp, q in self._queues.items() if q}

    def assert_fully_consumed(self) -> None:
        """断言所有录制记录都被用掉了。

        用例：修改了流程导致某个 Agent 不再被调用。回放仍然"成功"，
        但覆盖的是一个和录制时不同的流程。这条断言能把它变成失败。
        """
        leftover = self.unconsumed()
        if leftover:
            raise AssertionError(
                f"回放结束后仍有 {self.remaining} 条录制未被消费"
                f"（{len(leftover)} 个不同的请求）。"
                "流程与录制时不一致——要么流程少走了步骤，要么参数变了。"
            )


def as_provider(replay: ReplayLLM) -> LLMProvider:
    """显式表明 ``ReplayLLM`` 满足 ``LLMProvider`` 协议。

    放一个函数是为了让类型检查器在协议改动时立刻报错，
    而不是等到运行时才发现某个包装器少实现了方法。
    """
    return replay
