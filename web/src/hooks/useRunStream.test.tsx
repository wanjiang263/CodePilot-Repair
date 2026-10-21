import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRunStream } from "./useRunStream";

function sseResponse(blocks: string): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(blocks));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useRunStream", () => {
  it("reconnects without duplicating events and ends on a control event", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        sseResponse(
          'id: 1\nevent: repair\ndata: {"seq":1,"run_id":"r","task_id":"t","type":"node_start","node":"plan"}\n\n',
        ),
      )
      .mockResolvedValueOnce(
        sseResponse(
          'id: 2\nevent: control\ndata: {"type":"stream_end","run_id":"r","status":"succeeded","ui_status":"completed"}\n\n',
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useRunStream("r", "key", true));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.events).toHaveLength(1));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2), { timeout: 4000 });
    await waitFor(() => expect(result.current.connection).toBe("ended"));

    const second = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(String(second[0])).toContain("after_seq=1");
    expect((second[1].headers as Record<string, string>)["Last-Event-ID"]).toBe("1");
    expect(result.current.events.map((event) => event.seq)).toEqual([1]);
    expect(result.current.control?.ui_status).toBe("completed");
  });
});
