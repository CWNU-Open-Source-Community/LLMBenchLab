import type { PropsWithChildren } from "react";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DashboardSummary } from "../src/api/types";
import { DashboardPage } from "../src/pages/DashboardPage";

vi.mock("recharts", () => {
  const Container = ({ children }: PropsWithChildren) => <div>{children}</div>;
  const Element = () => <div />;

  return {
    Bar: Element,
    BarChart: Container,
    CartesianGrid: Element,
    ResponsiveContainer: Container,
    Tooltip: Element,
    XAxis: Element,
    YAxis: Element,
  };
});

const summary: DashboardSummary = {
  model_count: 2,
  benchmark_count: 1,
  run_count: 4,
  completed_run_count: 3,
  failed_run_count: 1,
  average_score: 84.2,
  average_latency_ms: 1250,
  total_input_tokens: 1200,
  total_output_tokens: 345,
  total_estimated_cost: 0.001234,
  recent_runs: [
    {
      run_id: "run-001",
      model_id: "model-001",
      model_name: "Mock Deterministic",
      benchmark_id: "benchmark-001",
      benchmark_slug: "demo-mixed-qa",
      benchmark_name: "Demo Mixed QA",
      benchmark_version: "1.0.0",
      benchmark_hash: "abc123",
      is_demo: true,
      protocol_version: "protocol-v1",
      score: 82.5,
      answered_accuracy: 88,
      completion_rate: 93.3,
      average_latency_ms: 48.2,
      input_tokens: 400,
      output_tokens: 80,
      estimated_cost: 0,
      started_at: "2026-08-24T00:00:00Z",
      finished_at: "2026-08-24T00:00:01Z",
    },
  ],
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("DashboardPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads and displays the main dashboard with score and completion metrics", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValue(jsonResponse(summary));

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("正在生成评测概览")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /让每一次模型评测/ })).toBeInTheDocument();
    expect(screen.getByText("84")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("1.25 s")).toBeInTheDocument();
    const runMetric = screen.getByText("Run 总数").closest("article");
    expect(runMetric).not.toBeNull();
    expect(within(runMetric as HTMLElement).getByText("4")).toBeInTheDocument();
    expect(within(runMetric as HTMLElement).getByText(/成功 Run 3/)).toBeInTheDocument();

    const runRow = screen.getByRole("link", { name: "Mock Deterministic" }).closest("tr");
    expect(runRow).not.toBeNull();
    expect(within(runRow as HTMLTableRowElement).getByText("已完成")).toHaveClass("status-completed");
    expect(within(runRow as HTMLTableRowElement).getByText("82.5%")).toBeInTheDocument();
    expect(within(runRow as HTMLTableRowElement).getByText("93.3%")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v1\/metrics\/summary$/);
  });

  it("shows an actionable API error state when the backend is unreachable", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockRejectedValue(new Error("offline by test"));

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("暂时无法完成请求")).toBeInTheDocument();
    expect(screen.getByText("无法连接后端：offline by test")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重试/ })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
