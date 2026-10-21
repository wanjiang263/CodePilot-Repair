import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { sessionFixture } from "../../test/fixtures";
import { RunPanel } from "./RunPanel";

describe("RunPanel", () => {
  it("shows budget readings from the closure instead of recomputing them", () => {
    render(<RunPanel session={sessionFixture()} />);
    expect(screen.getAllByText("12/12").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1/5").length).toBeGreaterThan(0);
    expect(screen.getByText("10/30")).toBeInTheDocument();
    expect(screen.getByText("¥0.1958 used · ¥9.80 remaining")).toBeInTheDocument();
  });
});
