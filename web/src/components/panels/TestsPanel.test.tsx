import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { sessionFixture } from "../../test/fixtures";
import { TestsPanel } from "./TestsPanel";

describe("TestsPanel", () => {
  it("shows official and internal verdicts separately", () => {
    render(<TestsPanel session={sessionFixture()} />);
    expect(screen.getByText("RESOLVED")).toBeInTheDocument();
    expect(screen.getAllByText("Budget exhausted").length).toBeGreaterThan(0);
    expect(screen.getByText(/independent of the official verdict/i)).toBeInTheDocument();
  });

  it("explains a budget stop instead of presenting a model failure", () => {
    render(<TestsPanel session={sessionFixture()} />);
    expect(screen.getByText("Stopped by a budget rule.")).toBeInTheDocument();
    expect(screen.getByText(/not a model failure claim/i)).toBeInTheDocument();
  });

  it("keeps unexecuted review explicit", () => {
    render(<TestsPanel session={sessionFixture()} />);
    expect(screen.getAllByText("NOT EXECUTED").length).toBeGreaterThan(0);
    expect(screen.getByText("review:no result")).toBeInTheDocument();
  });
});
