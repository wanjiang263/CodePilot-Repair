"""Explicit exact-input reuse of settled responses during a live recovery.

This is not offline replay: misses intentionally use the authorized live provider.
Every hit is separately recorded with historical provenance and zero new cost.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

from codepilot.llm.base import LLMProvider
from codepilot.llm.record import CallRecord
from codepilot.llm.replay import load_records
from codepilot.llm.types import LLMRequest, LLMResponse, request_fingerprint


class SettledResponseCache:
    def __init__(self, inner: LLMProvider, *, archive: Path, alias: str, evidence: Path) -> None:
        self.inner = inner
        self.evidence = evidence
        self.archive = archive
        self.records: dict[str, deque[CallRecord]] = defaultdict(deque)
        for record in load_records(archive):
            if record.error is None and record.response is not None and record.model_alias == alias:
                self.records[record.fingerprint].append(record)

    def complete(self, request: LLMRequest) -> LLMResponse:
        fingerprint = request_fingerprint(request)
        records = self.records.get(fingerprint)
        if not records:
            return self.inner.complete(request)
        record = records.popleft()
        response = record.to_response()
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        with self.evidence.open("a", encoding="utf8") as handle:
            handle.write(
                json.dumps(
                    {
                        "archive": str(self.archive),
                        "source_seq": record.seq,
                        "fingerprint": fingerprint,
                        "purpose": request.purpose,
                        "historical_usage": {
                            k: (record.response or {}).get(k)
                            for k in ("input_tokens", "output_tokens", "cost")
                        },
                        "new_cost_cny": 0,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return response.model_copy(
            update={
                "cost": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "latency_ms": 0,
            }
        )

    def close(self) -> None:
        self.inner.close()


def settled_archive_eligible(ledgers: list[Path]) -> bool:
    """Ambiguous/frozen archives disable response reuse, not stage recovery."""
    if len(ledgers) != 1:
        return False
    ledger = json.loads(ledgers[0].read_text(encoding="utf-8"))
    return not ledger["frozen"] and all(row["status"] == "settled" for row in ledger["requests"])
