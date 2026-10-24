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
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from codepilot.llm.config import ModelSpec
from codepilot.llm.cost import compute_cost


class DispatchBudgetStoppedError(RuntimeError):
    """No further HTTP dispatch is safe (deliberately not retryable)."""


def _validate_per_instance_opening(
    opening: float, source: dict[str, Any] | None
) -> None:
    """Reject an instance opening that is not the validated manifest share.

    A ledger SHA alone is not enough: the same manifest can be paired with a
    different amount or another instance. All of those must fail before the
    first model request, not after the batch has already been charged.
    """
    if source is None or source.get("kind") != "per_instance":
        return
    import hashlib
    from pathlib import Path

    from codepilot.llm.dispatch_recovery import (
        instance_opening_cny,
        load_recovery_manifest,
    )

    instance_id = source.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise ValueError("per-instance opening requires instance_id")
    declared = source.get("instance_opening_cny")
    if isinstance(declared, bool) or not isinstance(declared, (int, float)):
        raise ValueError("per-instance opening requires numeric instance_opening_cny")
    if not math.isclose(float(declared), float(opening), rel_tol=0, abs_tol=1e-9):
        raise ValueError("opening balance does not match the declared instance opening")
    raw_manifest = source.get("recovery_manifest")
    if not isinstance(raw_manifest, str) or not raw_manifest.strip():
        raise ValueError("per-instance opening requires recovery_manifest")
    manifest_path = Path(raw_manifest)
    if not manifest_path.is_file():
        raise ValueError(f"recovery manifest is missing: {manifest_path}")
    expected_sha = source.get("recovery_manifest_sha256")
    actual_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if expected_sha != actual_sha:
        raise ValueError("recovery manifest hash does not match the opening source")
    repo_root = Path(str(source.get("repo_root") or Path.cwd()))
    manifest = load_recovery_manifest(manifest_path)
    computed = instance_opening_cny(manifest, instance_id.strip(), repo_root=repo_root)
    if not math.isclose(computed, float(opening), rel_tol=0, abs_tol=1e-9):
        raise ValueError(
            "opening balance does not equal the manifest instance share: "
            f"{opening} != {computed}"
        )


class DispatchBudget:
    def __init__(
        self,
        limit: float,
        path: Path,
        *,
        opening_balance: float = 0.0,
        opening_source: dict[str, Any] | None = None,
    ) -> None:
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError("dispatch budget must be finite and positive")
        if not math.isfinite(opening_balance) or opening_balance < 0:
            raise ValueError("opening balance must be finite and non-negative")
        if opening_source is not None and not isinstance(opening_source, dict):
            raise ValueError("opening source must be a mapping")
        _validate_per_instance_opening(opening_balance, opening_source)
        self.limit = limit
        self.path = path
        self.opening_balance = opening_balance
        self.opening_source = opening_source
        self.total = opening_balance
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
            "opening_balance": self.opening_balance,
            "opening_source": self.opening_source,
            "requests": self.records,
        }
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        # Windows Defender/indexers can briefly hold the destination open; a
        # replace failure must not corrupt the ledger or drop a reservation.
        for attempt in range(6):
            try:
                staging.replace(self.path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

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
            opening_raw = os.environ.get("CODEPILOT_DISPATCH_OPENING_CNY")
            opening = float(opening_raw) if opening_raw else 0.0
            source_raw = os.environ.get("CODEPILOT_DISPATCH_OPENING_SOURCE")
            opening_source = json.loads(source_raw) if source_raw else None
            _guard = DispatchBudget(
                float(limit),
                Path(os.environ["CODEPILOT_DISPATCH_LEDGER"]),
                opening_balance=opening,
                opening_source=opening_source,
            )
    return _guard.send(spec, body, send)
