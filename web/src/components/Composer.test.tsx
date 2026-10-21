import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { metaFixture } from "../test/fixtures";
import { Composer } from "./Composer";

describe("Composer", () => {
  it("requires a meaningful issue before enabling Start repair", async () => {
    const user = userEvent.setup();
    render(<Composer meta={metaFixture()} apiKey="k" onCreated={vi.fn()} />);
    const button = screen.getByRole("button", { name: /Start repair/i });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText("Issue description"), "short");
    expect(button).toBeDisabled();
    await user.type(
      screen.getByLabelText("Issue description"),
      " but long enough now to be a real issue",
    );
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("enforces the server-side repository host allow-list in the form", async () => {
    const user = userEvent.setup();
    render(<Composer meta={metaFixture()} apiKey="k" onCreated={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "HTTPS repository" }));
    await user.type(screen.getByLabelText("HTTPS repository URL"), "https://evil.example/repo");
    await user.type(screen.getByLabelText("Issue description"), "add returns the wrong value");
    expect(await screen.findByText(/Host not allow-listed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start repair/i })).toBeDisabled();
  });
});
