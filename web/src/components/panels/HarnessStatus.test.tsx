import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { sessionFixture } from "../../test/fixtures";
import { HarnessStatus } from "./HarnessStatus";
import { mergeHarness } from "./harnessProjection";

// Authored for future validation; not executed during the static-only phase.
describe("HarnessStatus", () => {
  it("separates cancel requested from confirmed and shows live stage evidence", () => {
    const session = sessionFixture();
    session.status = "running";
    session.cancelRequested = true;
    session.events = [{ run_id: session.runId!, task_id: "task", seq: 1, type: "lifecycle", node: "generate_patch", timestamp: null,
      extra: { hook: "stage_start", facts: { runtime: { stage: "generate_patch", stage_elapsed_seconds: 3 } } } }];
    render(<HarnessStatus session={session} />);
    expect(screen.getByText("generate_patch")).toBeInTheDocument();
    expect(screen.getByText("Requested; waiting for a safe boundary")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("reports requested while the cancel request is in flight", () => {
    const session = sessionFixture();
    session.status = "running";
    session.cancelRequested = false;
    session.cancelling = true;
    render(<HarnessStatus session={session} />);
    expect(screen.getByText("Requested; waiting for a safe boundary")).toBeInTheDocument();
  });

  it("reports confirmed once the terminal cancelled state lands", () => {
    const session = sessionFixture();
    session.status = "cancelled";
    session.cancelling = true;
    render(<HarnessStatus session={session} />);
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
  });
});


describe("projection ordering (authored, not executed)", () => {
  it("keeps newer API elapsed time over old SSE stage_start", () => {
    const session = sessionFixture({ status: "running" });
    session.report!.harness = { runtime: { run_id: session.runId, event_seq: 12, revision: 2, stage: "plan", stage_elapsed_seconds: 17 } };
    session.events = [{ run_id: session.runId!, task_id: "task", seq: 10, type: "lifecycle", node: "plan", timestamp: null,
      extra: { hook: "stage_start", facts: { runtime: { revision: 2, stage: "plan", stage_elapsed_seconds: 0 } } } }];
    expect(mergeHarness(session).runtime.stage_elapsed_seconds).toBe(17);
  });
  it("sorts duplicate/out of order events and freezes terminal elapsed", () => {
    const session = sessionFixture({ status: "cancelled" });
    session.report!.harness = {};
    const start = { run_id: session.runId!, task_id: "task", seq: 1, type: "lifecycle", node: "plan", timestamp: null,
      extra: { hook: "stage_start", facts: { runtime: { revision: 1, stage: "plan", stage_elapsed_seconds: 0 } } } };
    const end = { ...start, seq: 2, extra: { hook: "stage_end", facts: { runtime: { revision: 1, stage: "plan", stage_elapsed_seconds: 9 } } } };
    session.events = [end, start, start];
    const result = mergeHarness(session).runtime;
    expect(result.stage_elapsed_seconds).toBe(9);
    expect(result.activity).toBe("finished");
  });
  it("does not accept a different run's events after reconnect", () => {
    const session = sessionFixture({ status: "running" });
    session.report!.harness = {};
    session.events = [{ run_id: "previous-run", task_id: "task", seq: 100, type: "lifecycle", node: "plan", timestamp: null,
      extra: { hook: "stage_start", facts: { runtime: { stage: "wrong-run" } } } }];
    expect(mergeHarness(session).runtime.stage).toBeUndefined();
  });
  it("existing patch exploration does not invent effective progress", () => {
    const session = sessionFixture({ status: "running" });
    session.report!.harness = {};
    session.events = [{ run_id: session.runId!, task_id: "task", seq: 1, type: "lifecycle", node: "generate_patch", timestamp: "2026-10-28T00:00:00Z",
      extra: { hook: "exploration", facts: { has_modification: true } } }];
    const result = mergeHarness(session).runtime;
    expect(result.has_modification).toBe(true);
    expect(result.last_effective_progress).toBeUndefined();
  });
});
