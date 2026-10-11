"""Assemble and audit explainable retrieval snapshots without persistence (V-20)."""

from collections.abc import Mapping, Sequence
from typing import cast

from codepilot.repo.models import Symbol, SymbolKind
from codepilot.retrieval.aggregate import aggregate_files, unique_symbols
from codepilot.retrieval.config import CHANNELS
from codepilot.retrieval.fusion import (
    DEFAULT_WEIGHTS,
    min_max,
    pin_traceback,
    rank_with_pinned,
    rrf,
    symbol_key,
    weighted_fusion,
    without_pinned,
)
from codepilot.schemas.retrieval import (
    ChannelScore,
    FusionMethod,
    RankedSymbol,
    RetrievalQuery,
    RetrievalResult,
)


def _channel_weights(names: Sequence[str], weights: Mapping[str, float] | None) -> dict[str, float]:
    configured = DEFAULT_WEIGHTS if weights is None else weights
    weighted_fusion({}, configured)  # Validate even unused weights.
    if "lexical" in configured and any(name in configured for name in ("bm25", "ripgrep")):
        raise ValueError("lexical group weights cannot be mixed with bm25/ripgrep weights")
    lexical = [name for name in names if name in ("bm25", "ripgrep")]
    return {
        name: configured.get("lexical", 0.0) / len(lexical)
        if name in lexical and "lexical" in configured
        else configured.get(name, 0.0)
        for name in names
    }


def _canonical_channels(
    channels: Mapping[str, Sequence[RankedSymbol]],
) -> dict[str, list[RankedSymbol]]:
    if any(name not in CHANNELS for name in channels):
        raise ValueError("unknown retrieval channel")
    result: dict[str, list[RankedSymbol]] = {}
    for name in CHANNELS:
        if name not in channels:
            continue
        candidates = [RankedSymbol.model_validate(c.model_dump()) for c in channels[name]]
        if any(not c.symbol_id for c in candidates):
            raise ValueError("channel candidates require nonempty symbol IDs")
        candidates.sort(key=lambda c: (-c.score, *symbol_key(c), c.model_dump_json()))
        normalized = min_max({str(i): c.score for i, c in enumerate(candidates)})
        for i, candidate in enumerate(candidates):
            candidate.channel_scores = [
                ChannelScore(channel=name, raw=candidate.score, normalized=normalized[str(i)])
            ]
        result[name] = candidates
    return result


def build_result(
    query: RetrievalQuery,
    per_channel: Mapping[str, Sequence[RankedSymbol]],
    *,
    repository_symbols: Sequence[Symbol] = (),
    fusion_method: FusionMethod = FusionMethod.WEIGHTED,
    weights: Mapping[str, float] | None = None,
    k: int = 60,
    limit: int = 20,
    snapshot_id: str = "",
) -> RetrievalResult:
    """Full raw channels + full fused ranking + Top-K symbol/file views.

    BM25 and Ripgrep independently normalize; enabled lexical channels split the
    documented lexical weight equally. Explicit channel weights are also valid.
    Pins are removed before eligible normalization and RRF rank assignment. Thus
    per_channel shows full channel-local normalization, while fused explanations
    show the eligible scores actually used by fusion. Missing evidence is 0.

    snapshot_id is caller-owned; writing snapshots belongs to later integration.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if fusion_method not in (FusionMethod.WEIGHTED, FusionMethod.RRF):
        raise ValueError("fusion_method must be weighted or rrf")
    fusion_method = FusionMethod(fusion_method)
    if fusion_method == FusionMethod.RRF and weights is not None:
        raise ValueError("weights apply only to weighted fusion")
    canonical = _canonical_channels(per_channel)
    metadata = unique_symbols(
        [c.model_copy(update={"pinned": False}) for hits in canonical.values() for c in hits]
    )
    by_id = {c.symbol_id: c for c in metadata}
    # Channel-local duplicate hits remain in per_channel, but fusion uses the
    # strongest raw hit once. This never compares different raw score units.
    raw: dict[str, dict[str, float]] = {}
    for name, hits in canonical.items():
        raw[name] = {}
        for candidate in hits:
            raw[name].setdefault(candidate.symbol_id, candidate.score)
    pins = pin_traceback(query, repository_symbols)
    eligible = without_pinned(raw, pins)
    normalized = {name: min_max(scores) for name, scores in eligible.items()}
    rankings = {
        name: sorted(scores, key=lambda sid: (-scores[sid], *symbol_key(by_id[sid])))
        for name, scores in eligible.items()
    }
    ranks = {
        name: {sid: i for i, sid in enumerate(ranked, start=1)} for name, ranked in rankings.items()
    }
    resolved = (
        _channel_weights(list(canonical), weights) if fusion_method == FusionMethod.WEIGHTED else {}
    )
    fused = (
        weighted_fusion(eligible, resolved)
        if fusion_method == FusionMethod.WEIGHTED
        else rrf(rankings, k)
    )
    ordinary = []
    for sid, score in fused.items():
        explanations = []
        channel_scores = []
        for name in canonical:
            norm = normalized[name].get(sid, 0.0)
            channel_scores.append(
                ChannelScore(channel=name, raw=raw[name].get(sid, 0.0), normalized=norm)
            )
            if sid not in raw[name]:
                explanations.append(f"{name} missing (0)")
            else:
                detail = f"{name} raw={raw[name][sid]:.12g} normalized={norm:.12g} rank={ranks[name][sid]}"
                if fusion_method == FusionMethod.WEIGHTED:
                    detail += f" weight={resolved[name]:.12g}"
                explanations.append(detail)
        ordinary.append(
            by_id[sid].model_copy(
                deep=True,
                update={
                    "score": score,
                    "pinned": False,
                    "channel_scores": channel_scores,
                    "reason": f"{fusion_method.value} fusion: " + "; ".join(explanations),
                },
            )
        )
    for pin in pins:
        # Pins retain raw and full-channel normalized evidence, but no contribution.
        pin.channel_scores = [
            next(
                (c.channel_scores[0] for c in canonical[name] if c.symbol_id == pin.symbol_id),
                ChannelScore(channel=name, raw=0.0, normalized=0.0),
            )
            for name in canonical
        ]
    ranked = rank_with_pinned(ordinary, pins)
    return RetrievalResult(
        query=query.model_copy(deep=True),
        per_channel=canonical,
        enabled_channels=list(canonical),
        channel_weights=resolved,
        fusion_method=fusion_method,
        fused_symbols=ranked,
        symbols=ranked[:limit],
        files=aggregate_files(ranked)[:limit],
        snapshot_id=snapshot_id,
        rrf_k=k if fusion_method == FusionMethod.RRF else 60,
    )


def validate_result(result: RetrievalResult) -> None:
    """Recompute a deserialized fused snapshot and reject inconsistent evidence.

    This is opt-in, so legacy schemas and single-channel snapshots keep their
    original validation semantics. It audits arithmetic/order/explanations; the
    query parser and repository index establish provenance before construction.
    RRF parameters are taken from the snapshot, including a nondefault rrf_k.
    """
    try:
        validated = RetrievalResult.model_validate(result.model_dump())
        if validated.fusion_method == FusionMethod.NONE:
            return
        indexed_pins = [
            Symbol(
                name=c.qualified_name.rsplit(".", 1)[-1],
                qualified_name=c.qualified_name,
                kind=cast(SymbolKind, c.kind or "function"),
                id=c.symbol_id,
                path=c.file,
                start_line=c.start_line or 0,
                end_line=c.end_line or 0,
            )
            for c in validated.fused_symbols
            if c.pinned
        ]
        expected = build_result(
            validated.query,
            validated.per_channel,
            repository_symbols=indexed_pins,
            fusion_method=validated.fusion_method,
            weights=validated.channel_weights
            if validated.fusion_method == FusionMethod.WEIGHTED
            else None,
            k=validated.rrf_k,
            limit=max(len(validated.symbols), len(validated.files), 1),
            snapshot_id=validated.snapshot_id,
        )
        if (
            validated.enabled_channels != expected.enabled_channels
            or validated.channel_weights != expected.channel_weights
            or validated.per_channel != expected.per_channel
            or validated.fused_symbols != expected.fused_symbols
            or validated.symbols != expected.fused_symbols[: len(validated.symbols)]
            or validated.files != aggregate_files(expected.fused_symbols)[: len(validated.files)]
        ):
            raise ValueError("inconsistent retrieval result")
    except ValueError as exc:
        raise ValueError(f"inconsistent retrieval result: {exc}") from exc
