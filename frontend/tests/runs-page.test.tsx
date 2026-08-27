import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluationRun, RunStatus } from "../src/api/types";
import { RunsPage } from "../src/pages/RunsPage";

function runFixture(status: RunStatus, overrides: Partial<EvaluationRun> = {}): EvaluationRun {
  return {
    id: `run-${status}`,
    model_id: "model-001",
    benchmark_id: "benchmark-001",
    status,
    protocol_version: "llmbenchlab-protocol-v1",
    model_parameters_snapshot: {
      model: { name: "Frozen Model" },
      benchmark: { name: "GPQA-Diamond", slug: "gpqa-diamond", version: "1.0.0" },
    },
    benchmark_hash_snapshot: "a".repeat(64),
    prompt_template_snapshot: {},
    code_commit_sha: null,
    total_questions: 10,
    completed_questions: 5,
    correct_questions: 4,
    error_questions: 1,
    score: 40,
    completion_rate: 50,
    answered_accuracy: 80,
    average_latency_ms: 123,
    input_tokens: 100,
    output_tokens: 20,
    estimated_cost: 0.01,
    cancellation_requested: false,
    started_at: "2026-08-27T08:00:00Z",
    finished_at: status === "pending" || status === "running" ? null : "2026-08-27T08:02:00Z",
    created_at: "2026-08-27T07:59:00Z",
    error_message: null,
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function listResponse(items: EvaluationRun[], total = items.length) {
  return { items, total, offset: 0, limit: 20 };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

describe("RunsPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders snapshot identities, all statuses, progress, and detail links", async () => {
    const runs = (["pending", "running", "completed", "failed", "cancelled"] as RunStatus[])
      .map((status) => runFixture(status));
    vi.mocked(fetch).mockResolvedValue(jsonResponse(listResponse(runs)));

    render(<MemoryRouter><RunsPage /></MemoryRouter>);

    expect(screen.getByText("正在读取评测记录")).toBeInTheDocument();
    const modelLinks = await screen.findAllByRole("link", { name: "Frozen Model" });
    expect(modelLinks).toHaveLength(5);
    expect(modelLinks[1]).toHaveAttribute("href", "/runs/run-running");
    expect(screen.getAllByText("GPQA-Diamond · v1.0.0")).toHaveLength(5);
    expect(screen.getAllByText("5 / 10 · 50%")).toHaveLength(5);
    expect(screen.getAllByText("40.0%")).toHaveLength(5);

    const table = screen.getByRole("table");
    for (const label of ["等待中", "运行中", "已完成", "失败", "已取消"]) {
      expect(within(table).getByText(label)).toBeInTheDocument();
    }

    const runningRow = modelLinks[1].closest("tr");
    expect(runningRow).not.toBeNull();
    expect(within(runningRow as HTMLTableRowElement).getByRole("link", { name: /查看详情/ }))
      .toHaveAttribute("href", "/runs/run-running");
  });

  it("filters by status, paginates in groups of 20, and refreshes manually", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation((request: RequestInfo | URL) => {
      const url = new URL(String(request));
      const offset = Number(url.searchParams.get("offset") || 0);
      const status = url.searchParams.get("run_status") as RunStatus | null;
      return Promise.resolve(jsonResponse(listResponse([
        runFixture(status || "completed", { id: `run-${status || "all"}-${offset}` }),
      ], status ? 1 : 41)));
    });
    const user = userEvent.setup();

    render(<MemoryRouter><RunsPage /></MemoryRouter>);
    await screen.findByRole("link", { name: "Frozen Model" });

    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      const url = new URL(String(fetchMock.mock.calls.at(-1)?.[0]));
      expect(url.searchParams.get("offset")).toBe("20");
      expect(url.searchParams.get("limit")).toBe("20");
    });

    await user.selectOptions(screen.getByRole("combobox", { name: "状态" }), "running");
    await waitFor(() => {
      const url = new URL(String(fetchMock.mock.calls.at(-1)?.[0]));
      expect(url.searchParams.get("offset")).toBe("0");
      expect(url.searchParams.get("run_status")).toBe("running");
    });

    const callsBeforeRefresh = fetchMock.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(callsBeforeRefresh + 1));
  });

  it("polls an active page every two seconds and clears the timer on unmount", async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse(listResponse([runFixture("running")])));
    const intervalSpy = vi.spyOn(window, "setInterval");
    const clearSpy = vi.spyOn(window, "clearInterval");

    const view = render(<MemoryRouter><RunsPage /></MemoryRouter>);
    await screen.findByRole("table");

    expect(intervalSpy).toHaveBeenCalledWith(expect.any(Function), 2000);
    const timer = intervalSpy.mock.results.at(-1)?.value;
    view.unmount();
    expect(clearSpy).toHaveBeenCalledWith(timer);
  });

  it("does not let a poll supersede a slower pagination request", async () => {
    const fetchMock = vi.mocked(fetch);
    const secondPage = deferred<Response>();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(listResponse([runFixture("running")], 41)))
      .mockReturnValueOnce(secondPage.promise);
    const intervalSpy = vi.spyOn(window, "setInterval");
    const user = userEvent.setup();

    render(<MemoryRouter><RunsPage /></MemoryRouter>);
    await screen.findByRole("table");
    const poll = intervalSpy.mock.calls.at(-1)?.[0] as TimerHandler;

    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.getByText("正在读取评测记录")).toBeInTheDocument();

    act(() => { if (typeof poll === "function") poll(); });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      secondPage.resolve(jsonResponse(listResponse([
        runFixture("completed", { id: "run-page-two" }),
      ], 41)));
      await secondPage.promise;
    });
    expect(await screen.findByRole("link", { name: "Frozen Model" }))
      .toHaveAttribute("href", "/runs/run-page-two");
    expect(screen.queryByText("正在读取评测记录")).not.toBeInTheDocument();
  });

  it("returns to the last valid page when a filtered total shrinks", async () => {
    const fetchMock = vi.mocked(fetch);
    let shrink = false;
    fetchMock.mockImplementation((request: RequestInfo | URL) => {
      const offset = Number(new URL(String(request)).searchParams.get("offset") || 0);
      if (offset === 20 && shrink) {
        return Promise.resolve(jsonResponse(listResponse([], 1)));
      }
      if (offset === 0 && shrink) {
        return Promise.resolve(jsonResponse(listResponse([
          runFixture("completed", { id: "run-last-valid" }),
        ], 1)));
      }
      return Promise.resolve(jsonResponse(listResponse([
        runFixture("completed", { id: `run-page-${offset}` }),
      ], 41)));
    });
    const user = userEvent.setup();

    render(<MemoryRouter><RunsPage /></MemoryRouter>);
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getByText("第 2 / 3 页")).toBeInTheDocument());

    shrink = true;
    await user.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => {
      expect(screen.getByRole("link", { name: "Frozen Model" }))
        .toHaveAttribute("href", "/runs/run-last-valid");
    });
    expect(screen.getByText("第 1 / 1 页")).toBeInTheDocument();
  });

  it("shows empty and recoverable error states", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(jsonResponse(listResponse([])));

    const emptyView = render(<MemoryRouter><RunsPage /></MemoryRouter>);
    expect(await screen.findByText("暂无评测记录")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /新建评测/ })[0]).toHaveAttribute("href", "/runs/new");
    emptyView.unmount();

    fetchMock.mockRejectedValueOnce(new Error("offline by test"));
    await act(async () => {
      render(<MemoryRouter><RunsPage /></MemoryRouter>);
    });
    expect(await screen.findByText("暂时无法完成请求")).toBeInTheDocument();
    expect(screen.getByText("无法连接后端：offline by test")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重试/ })).toBeInTheDocument();
  });
});
