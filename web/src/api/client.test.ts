import { describe, expect, it, vi } from "vitest";

import { handleBlock } from "./client";

describe("SSE block parser", () => {
  it("parses repair events", () => {
    const onEvent = vi.fn();
    const onControl = vi.fn();
    handleBlock(
      'id: 12\nevent: repair\ndata: {"seq":12,"node":"plan","type":"node_end","status":"ok"}',
      { key: "k", runId: "r", onEvent, onControl },
    );
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ seq: 12, node: "plan", status: "ok" }),
    );
    expect(onControl).not.toHaveBeenCalled();
  });

  it("parses terminal control events", () => {
    const onEvent = vi.fn();
    const onControl = vi.fn();
    handleBlock(
      'event: control\ndata: {"type":"stream_end","run_id":"r","status":"cancelled","ui_status":"cancelled"}',
      { key: "k", runId: "r", onEvent, onControl },
    );
    expect(onControl).toHaveBeenCalledWith(expect.objectContaining({ ui_status: "cancelled" }));
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("ignores heartbeat comments and malformed JSON", () => {
    const onEvent = vi.fn();
    const onControl = vi.fn();
    handleBlock(": ping", { key: "k", runId: "r", onEvent, onControl });
    handleBlock("event: repair\ndata: not-json", { key: "k", runId: "r", onEvent, onControl });
    expect(onEvent).not.toHaveBeenCalled();
    expect(onControl).not.toHaveBeenCalled();
  });
});
