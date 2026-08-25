import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RunStatus } from "../src/api/types";
import { StatusBadge } from "../src/components/StatusBadge";

describe("StatusBadge", () => {
  it.each<[RunStatus, string]>([
    ["pending", "等待中"],
    ["running", "运行中"],
    ["completed", "已完成"],
    ["failed", "失败"],
    ["cancelled", "已取消"],
  ])("renders %s runs as %s", (status, label) => {
    render(<StatusBadge status={status} />);

    expect(screen.getByText(label)).toHaveClass("status-badge", `status-${status}`);
  });
});
