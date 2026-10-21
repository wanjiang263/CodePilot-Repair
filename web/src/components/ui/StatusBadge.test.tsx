import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders a budget stop as stopped, not failed", () => {
    render(<StatusBadge status="stopped" stopReason="budget_test_runs" />);
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getByText(/Test-run budget/)).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("renders cancellation distinctly", () => {
    render(<StatusBadge status="cancelled" />);
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
  });
});
