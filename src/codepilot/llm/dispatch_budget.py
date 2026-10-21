"""Optional process-wide, fail-closed HTTP budget for a single live experiment.

Each HTTP attempt reserves the full model context at uncached prices. Missing
usage or any uncertain response retains the reservation and freezes dispatch.
The ledger must be new: recovery requires explicit reconciliation, never reset.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from codepilot.llm.config import ModelSpec
from codepilot.llm.cost import compute_cost


class DispatchBudgetStoppedError(RuntimeError):
    """No further HTTP dispatch is safe (deliberately not retryable)."""


class DispatchBudget:
    def __init__(self, limit: float, path: Path) -> None:
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("dispatch budget must be finite and positive")
        self.limit = limit
        self.path = path
        self.total = 0.0
        self.frozen = False
        self.records: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write("{}\n")
        self._save()

    def _save(self) -> None:
        payload = {
            "currency": "CNY",
            "limit": self.limit,
            "accounted_or_reserved": self.total,
            "frozen": self.frozen,
            "requests": self.records,
        }
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        staging.replace(self.path)

    def send(
        self, spec: ModelSpec, body: dict[str, Any], send: Callable[[], httpx.Response]
    ) -> httpx.Response:
        with self.lock:
            price = spec.price
            output_limit = body.get("max_tokens", body.get("max_completion_tokens"))
            if (
                self.frozen
                or price is None
                or not price.is_complete
                or price.currency != "CNY"
                or not isinstance(output_limit, int)
                or output_limit <= 0
            ):
                raise DispatchBudgetStoppedError("budget_stopped: unknown cost or frozen ledger")
            reserve = compute_cost(
                price=price, input_tokens=spec.context_window, output_tokens=output_limit
            ).amount
            if not math.isfinite(reserve) or reserve <= 0 or self.total + reserve > self.limit:
                raise DispatchBudgetStoppedError(
                    "budget_stopped: insufficient pre-dispatch reserve"
                )
            self.total += reserve
            record: dict[str, Any] = {
                "seq": len(self.records) + 1,
                "model": spec.model,
                "reserved_cny": reserve,
                "status": "reserved",
                "cost_cny": None,
            }
            self.records.append(record)
            self._save()  # must persist before network dispatch
            try:
                response = send()
                response.raise_for_status()
                usage = response.json()["usage"]
                prompt, output = usage["prompt_tokens"], usage["completion_tokens"]
                cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                if (
                    any(type(v) is not int or v < 0 for v in (prompt, output, cached))
                    or cached > prompt
                    or prompt > spec.context_window
                    or output > output_limit
                ):
                    raise ValueError("usage outside reserved bounds")
                amount = compute_cost(
                    price=price,
                    input_tokens=prompt,
                    output_tokens=output,
                    cached_input_tokens=cached,
                ).amount
                if not math.isfinite(amount) or not 0 <= amount <= reserve:
                    raise ValueError("cost outside reservation")
            except BaseException as exc:
                self.frozen = True
                record.update(status="unknown_frozen", error_type=type(exc).__name__)
                self._save()
                raise DispatchBudgetStoppedError(
                    "budget_stopped: response/cost uncertain; further dispatch frozen"
                ) from exc
            self.total += amount - reserve
            record.update(
                status="settled",
                cost_cny=amount,
                input_tokens=prompt,
                cached_input_tokens=cached,
                output_tokens=output,
            )
            self._save()
            return response

    def outstanding(self) -> float:
        """Reserved but not yet settled: in-flight requests and frozen outcomes.

        ``total`` already folds a settled record back from its reservation to its
        real cost, so anything still carrying a non-settled status is money the
        graph's own ledger (``state.usage``) has not seen yet.
        """
        return sum(
            float(record["reserved_cny"])
            for record in self.records
            if record.get("status") != "settled"
        )


_guard: DispatchBudget | None = None
_init_lock = threading.Lock()


def current_outstanding() -> tuple[float, str] | None:
    """Outstanding pre-charge of the process-wide ledger, or ``None`` when there is none."""
    guard = _guard
    if guard is None:
        return None
    amount = guard.outstanding()
    return (amount, "CNY") if amount > 0 else None


def guarded_send(
    spec: ModelSpec, body: dict[str, Any], send: Callable[[], httpx.Response]
) -> httpx.Response:
    global _guard
    limit = os.environ.get("CODEPILOT_DISPATCH_BUDGET_CNY")
    if limit is None:
        return send()
    with _init_lock:
        if _guard is None:
            _guard = DispatchBudget(float(limit), Path(os.environ["CODEPILOT_DISPATCH_LEDGER"]))
    return _guard.send(spec, body, send)
