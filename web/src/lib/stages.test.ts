import { describe, expect, it } from "vitest";

import type { EventRecord } from "../api/types";
import { groupEvents, visibleEvents } from "./stages";

function event(seq: number, type: string, node: string, status: string, summary?: string): EventRecord {
  return {
    run_id: "r",
    task_id: "t",
    seq,
    type,
    node,
    timestamp: `2026-01-01T00:00:0${seq}Z`,
    status,
    summary,
  };
}

describe("event grouping", () => {
  it("keeps budget readings out of the human node summary", () => {
    const groups = groupEvents([
      event(1, "node_start", "run_target_tests", "started"),
      event(2, "budget_check", "run_target_tests", "ok", "{'tokens': '0/943808'}"),
      event(3, "node_end", "run_target_tests", "PASS"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].status).toBe("PASS");
    expect(groups[0].summary).toBeNull();
  });

  it("keeps a stop reason visible", () => {
    const groups = groupEvents([
      event(1, "node_start", "finalize", "started"),
      event(2, "stop", "finalize", "budget_test_runs", "stopped on the test budget"),
    ]);
    expect(groups[0].status).toBe("budget_test_runs");
    expect(groups[0].summary).toContain("test budget");
  });

  it("hides low-signal events at normal density", () => {
    const events = [
      event(1, "node_start", "plan", "started"),
      event(2, "budget_check", "plan", "ok", "reading"),
      event(3, "llm_call", "plan", "ok"),
      event(4, "node_end", "plan", "ok", "plan ready"),
    ];
    const group = groupEvents(events)[0];
    expect(visibleEvents(group, "normal").map((item) => item.type)).toEqual(["node_end"]);
  });

  it("merges the role-level reviewer event into the review stage", () => {
    const events = [
      event(1, "budget_check", "review", "ok"),
      { ...event(2, "llm_call", "reviewer", "ok"), duration_ms: 13171 },
      event(3, "node_end", "review", "changes-requested", "needs work"),
    ];
    const groups = groupEvents(events);
    expect(groups).toHaveLength(1);
    expect(groups[0].node).toBe("review");
    expect(groups[0].status).toBe("changes-requested");
    expect(groups[0].events.map((item) => item.seq)).toEqual([1, 2, 3]);
    // The original event (and its role) is preserved for audit and replay.
    expect(groups[0].events[1].node).toBe("reviewer");
  });

  it("keeps genuinely repeated review operations as separate stages", () => {
    const events = [
      event(1, "node_end", "review", "changes-requested"),
      event(2, "node_end", "repair_patch", "ok"),
      event(3, "node_end", "review", "approved"),
    ];
    const groups = groupEvents(events);
    expect(groups.map((group) => group.node)).toEqual(["review", "repair_patch", "review"]);
    expect(groups.map((group) => group.status)).toEqual([
      "changes-requested",
      "ok",
      "approved",
    ]);
  });

  it("keeps both duplicate historical reviewer events visible in replay", () => {
    const events = [
      event(1, "llm_call", "reviewer", "ok", "reviewer: 2993 in / 533 out"),
      event(2, "llm_call", "review", "ok", "reviewer: 2993 in / 533 out"),
      event(3, "node_end", "review", "changes-requested"),
    ];
    const groups = groupEvents(events);
    expect(groups).toHaveLength(1);
    expect(groups[0].events).toHaveLength(3);
    expect(groups[0].events.map((item) => item.node)).toEqual(["reviewer", "review", "review"]);
  });

  it("merges localizer and planner role events into their graph stages", () => {
    const events = [
      event(1, "llm_call", "localizer", "ok"),
      event(2, "node_end", "localize", "ok"),
      event(3, "llm_call", "planner", "ok"),
      event(4, "node_end", "plan", "ok"),
    ];
    const groups = groupEvents(events);
    expect(groups.map((group) => group.node)).toEqual(["localize", "plan"]);
    expect(groups.map((group) => group.events.length)).toEqual([2, 2]);
  });

  it("labels an orphan reviewer event as the review stage", () => {
    const groups = groupEvents([event(1, "llm_call", "reviewer", "ok")]);
    expect(groups[0].node).toBe("review");
    expect(groups[0].label).toBe("stages.nodes.review");
  });
});
