import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../src/App";

describe("App run navigation", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ items: [], total: 0, offset: 0, limit: 20 }),
    } as unknown as Response));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("exposes the evaluation list in the main navigation and routes to it", async () => {
    render(<MemoryRouter initialEntries={["/runs"]}><App /></MemoryRouter>);

    const navLink = screen.getByRole("link", { name: "评测记录" });
    expect(navLink).toHaveAttribute("href", "/runs");
    expect(navLink).toHaveClass("active");
    expect(await screen.findByRole("heading", { name: "评测记录", level: 1 })).toBeInTheDocument();
  });
});
