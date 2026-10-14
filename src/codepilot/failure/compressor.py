"""Loss-accounted deduplication and priority-preserving context admission."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from pydantic import BaseModel, Field

from codepilot.context.builder import Context, ContextBuilder, ContextLevel, ContextPart
from codepilot.failure.schemas import ParsedFailure
from codepilot.failure.signature import failure_signature
from codepilot.observability.redaction import redact_text


class FailureGroup(BaseModel):
    signature: str
    sample: ParsedFailure
    other_test_ids: list[str]
    count: int
    artifact_refs: list[str]
    omitted_frames: int
    truncated_characters: int
    omitted_traceback_characters: int


class CompressedFailures(BaseModel):
    groups: list[FailureGroup]
    total_failures: int
    all_test_ids: list[str]
    context: Context
    parts: list[ContextPart] = Field(default_factory=list)


def compress_failures(
    failures: Sequence[ParsedFailure],
    *,
    target_tests: Collection[str] = (),
    reproduction_tests: Collection[str] = (),
    regression_tests: Collection[str] = (),
    builder: ContextBuilder | None = None,
    budget: int | None = None,
) -> CompressedFailures:
    context_builder = builder or ContextBuilder()

    def priority(failure: ParsedFailure) -> tuple[int, str, str]:
        rank = next(
            (
                n
                for n, group in enumerate((target_tests, reproduction_tests, regression_tests))
                if failure.test_id in group
            ),
            3,
        )
        return rank, failure.test_id, failure.model_dump_json()

    grouped: dict[str, list[ParsedFailure]] = {}
    for failure in sorted(failures, key=priority):
        grouped.setdefault(failure_signature(failure), []).append(failure)
    groups = []
    parts = []
    for signature, members in grouped.items():
        first = members[0]
        last_external = next(
            (i for i in reversed(range(len(first.frames))) if not first.frames[i].in_repo), None
        )
        frames = [
            frame.model_copy(update={"code": redact_text(frame.code) if frame.code else None})
            for i, frame in enumerate(first.frames)
            if frame.in_repo or i == last_external
        ]
        message = redact_text(first.message)
        # Raw traceback is replaced by observed frames, never reconstructed as a fake log.
        sample = first.model_copy(
            update={
                "frames": frames,
                "message": message[:2000],
                "traceback": "",
                "deepest_repo_frame": next((f for f in reversed(frames) if f.in_repo), None),
            }
        )
        group = FailureGroup(
            signature=signature,
            sample=sample,
            other_test_ids=sorted(f.test_id for f in members[1:]),
            count=len(members),
            artifact_refs=sorted({f.artifact_ref for f in members}),
            omitted_frames=len(first.frames) - len(frames),
            truncated_characters=max(0, len(message) - 2000),
            omitted_traceback_characters=len(first.traceback),
        )
        groups.append(group)
        # One atomic JSON record: cannot cut away identity or create malformed evidence.
        parts.append(
            ContextPart(
                level=ContextLevel.ISSUE_TEXT,
                label=f"failure:{signature}",
                source=first.test_id.split("::", 1)[0],
                text=redact_text(group.model_dump_json()),
                truncatable=False,
            )
        )
    return CompressedFailures(
        groups=groups,
        total_failures=len(failures),
        all_test_ids=sorted(f.test_id for f in failures),
        context=context_builder.build("diagnoser", *parts, budget=budget),
        parts=parts,
    )
