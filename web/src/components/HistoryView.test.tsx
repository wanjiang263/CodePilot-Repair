import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { HistoryItem } from "../api/types";
import { HistoryView } from "./HistoryView";

const ITEM: HistoryItem = {
  history_id: "glm14-marshmallow-1343",
  run_id: "swebench-run",
  label: "GLM14 · marshmallow-1343",
  instance_id: "marshmallow-code__marshmallow-1343",
  repository: "https://github.com/marshmallow-code/marshmallow",
  model: "glm-5.3-flash-main",
  recorded_at: "2026-10-20T13:31:52Z",
  status: "budget_exhausted",
  raw_status: "budget_exhausted",
  ui_status: "stopped",
  stop_reason: "budget_test_runs",
  official_verdict: "RESOLVED",
  verification_level: "budget_exhausted",
  event_count: 67,
  patch_available: true,
  report_available: true,
  note: null,
};

describe("HistoryView", () => {
  it("renders official and internal conclusions separately", () => {
    render(<HistoryView history={[ITEM]} loading={false} onOpenReplay={vi.fn()} />);
    expect(screen.getByText("official: RESOLVED")).toBeInTheDocument();
    expect(screen.getByText("internal: Budget exhausted")).toBeInTheDocument();
    expect(screen.getAllByText(/Stopped/).length).toBeGreaterThan(0);
  });

  it("opens the same replay session on click", async () => {
    const onOpen = vi.fn();
    const user = userEvent.setup();
    render(<HistoryView history={[ITEM]} loading={false} onOpenReplay={onOpen} />);
    await user.click(screen.getByRole("button", { name: /GLM14/ }));
    expect(onOpen).toHaveBeenCalledWith("glm14-marshmallow-1343");
  });
});
