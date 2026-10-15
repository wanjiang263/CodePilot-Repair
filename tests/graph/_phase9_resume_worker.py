"""Hard process exits at Phase 9 side-effect/checkpoint boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from harness import FINISH, build_harness, edit, passed_result

from codepilot.agents.reviewer import PatchReviewer
from codepilot.graph.checkpoint import open_checkpointer, resume_run, run_with_checkpoint
from codepilot.graph.regression import RegressionRunner
from codepilot.llm.fake import FakeLLM

root = Path(sys.argv[2])
mode = sys.argv[1]
resume = mode == "resume"
harness = build_harness(
    root,
    results=[] if resume else [passed_result() for _ in range(3)],
    baseline_results=[] if resume else None,
    reuse=resume,
    coder_rounds=[]
    if resume
    else [[edit("src/pkg/core.py", "return value - 1", "return value"), FINISH]],
)
harness.deps.regression_runner = RegressionRunner()


class CrashReviewer(PatchReviewer):
    def review(self, state, deps):
        if mode == "after-regression":
            os._exit(7)
        result = super().review(state, deps)
        (root / "usage-before-crash.json").write_text(
            json.dumps({"calls": deps.usage.llm_calls, "tests": deps.test_runs}), encoding="utf-8"
        )
        os._exit(7)
        return result


harness.deps.reviewer = (PatchReviewer if resume else CrashReviewer)(FakeLLM([{"approved": True}]))
if mode in {"in-flight", "after-artifact"}:
    inner = harness.deps.accounting.inner
    original = inner.run_tests

    def crash_run(**kwargs):
        if harness.deps.test_runs == 4:
            if mode == "after-artifact":
                result = original(**kwargs)
                harness.deps.artifacts.write_json(
                    "crash-execution.json", result.model_dump(mode="json"), redact_values=True
                )
            os._exit(7)
        return original(**kwargs)

    inner.run_tests = crash_run


async def run():
    path = harness.deps.artifacts.run_dir / "checkpoints.sqlite"
    async with await open_checkpointer(path, harness.initial.run_id) as handle:
        if resume:
            state = await resume_run(harness.deps, handle, harness.initial.run_id)
            (root / "resumed.json").write_text(state.model_dump_json(), encoding="utf-8")
        else:
            await run_with_checkpoint(harness.initial, harness.deps, handle)


asyncio.run(run())
