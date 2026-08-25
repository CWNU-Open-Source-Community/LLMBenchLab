import { describe, expect, it } from "vitest";

import {
  displayAnswer,
  formatCost,
  formatLatency,
  formatPercent,
  formatTokenTotal,
  formatTokens,
  formatUtc8,
  shortHash,
} from "../src/lib/format";

describe("format helpers", () => {
  it("formats percentage scores and missing values", () => {
    expect(formatPercent(87.56)).toBe("87.6%");
    expect(formatPercent(87.56, 2)).toBe("87.56%");
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(Number.NaN)).toBe("—");
  });

  it("formats latency in milliseconds and seconds", () => {
    expect(formatLatency(42.25)).toBe("42.3 ms");
    expect(formatLatency(1250)).toBe("1.25 s");
    expect(formatLatency(undefined)).toBe("—");
  });

  it("formats token and cost totals", () => {
    expect(formatTokens(1234)).toBe("1,234");
    expect(formatTokens(null)).toBe("—");
    expect(formatTokenTotal(100, 25)).toBe("125");
    expect(formatTokenTotal(100, null)).toBe("—");
    expect(formatCost(0.001234)).toBe("$0.001234");
    expect(formatCost(2.5)).toBe("$2.5000");
  });

  it("renders timestamps in UTC+8 and rejects invalid dates", () => {
    const timestamp = formatUtc8("2026-08-24T00:00:00Z");

    expect(timestamp).toContain("08:00:00");
    expect(timestamp).toContain("(UTC+8)");
    expect(formatUtc8("not-a-date")).toBe("—");
  });

  it("shortens hashes and serializes non-string answers", () => {
    expect(shortHash("0123456789abcdef", 8)).toBe("01234567…");
    expect(shortHash(null)).toBe("—");
    expect(displayAnswer("B")).toBe("B");
    expect(displayAnswer({ value: 42 })).toBe('{"value":42}');
    expect(displayAnswer(undefined)).toBe("—");
  });
});
