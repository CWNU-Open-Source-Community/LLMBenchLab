import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LeaderboardPage } from "../src/pages/LeaderboardPage";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("LeaderboardPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((request: RequestInfo | URL) => {
        const url = String(request);
        if (url.includes("/models")) {
          return Promise.resolve(jsonResponse({ items: [], total: 0, offset: 0, limit: 20 }));
        }
        if (url.includes("/benchmarks")) {
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  id: "benchmark-001",
                  slug: "demo-general",
                  name: "Demo General",
                  version: "1.0.0",
                  description: "Demo 数据，不代表正式模型能力",
                  dimension: "general",
                  language: "mul",
                  license: "MIT",
                  source: "original",
                  evaluator_type: "builtin-objective",
                  evaluator_config: {},
                  prompt_template: {},
                  schema_version: "llmbenchlab-dataset-v1",
                  dataset_hash: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
                  question_count: 15,
                  is_demo: true,
                  created_at: "2026-08-24T00:00:00Z",
                },
              ],
              total: 1,
              offset: 0,
              limit: 20,
            }),
          );
        }
        if (url.includes("/leaderboard")) {
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  run_id: "run-001",
                  model_id: "model-001",
                  model_name: "Frozen Model",
                  benchmark_id: "benchmark-001",
                  benchmark_slug: "demo-general",
                  benchmark_name: "Demo General",
                  benchmark_version: "1.0.0",
                  benchmark_hash:
                    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
                  is_demo: true,
                  protocol_version: "llmbenchlab-protocol-v1",
                  score: 100,
                  answered_accuracy: 100,
                  completion_rate: 100,
                  average_latency_ms: 1,
                  input_tokens: null,
                  output_tokens: null,
                  estimated_cost: null,
                  started_at: "2026-08-24T00:00:00Z",
                  finished_at: "2026-08-24T00:00:01Z",
                },
              ],
              total: 1,
              offset: 0,
              limit: 20,
            }),
          );
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("ranks inside one Benchmark hash partition and preserves unknown usage", async () => {
    render(
      <MemoryRouter>
        <LeaderboardPage />
      </MemoryRouter>,
    );

    const modelLink = await screen.findByRole("link", { name: "Frozen Model" });
    expect(screen.getByRole("combobox", { name: "Benchmark 分区" })).toHaveValue(
      "benchmark-001",
    );
    expect(screen.getByText("abcdef012345…")).toHaveAttribute(
      "title",
      "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
    );

    const row = modelLink.closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLTableRowElement).getAllByText("—")).toHaveLength(2);

    const calls = vi.mocked(fetch).mock.calls.map(([url]) => String(url));
    expect(calls.some((url) => url.includes("benchmark_id=benchmark-001"))).toBe(true);
  });
});
