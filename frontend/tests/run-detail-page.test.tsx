import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluationResponse, EvaluationRun } from "../src/api/types";
import { RunDetailPage } from "../src/pages/RunDetailPage";

const apiMocks = vi.hoisted(() => ({
  run: vi.fn(),
  responses: vi.fn(),
  cancelRun: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      run: apiMocks.run,
      responses: apiMocks.responses,
      cancelRun: apiMocks.cancelRun,
    },
  };
});

function runFixture(overrides: Partial<EvaluationRun> = {}): EvaluationRun {
  return {
    id: "run-001",
    model_id: "model-001",
    benchmark_id: "benchmark-001",
    status: "completed",
    protocol_version: "llmbenchlab-protocol-v1",
    model_parameters_snapshot: {
      model: { name: "Frozen Model" },
      benchmark: { slug: "gpqa-diamond" },
    },
    benchmark_hash_snapshot: "a".repeat(64),
    prompt_template_snapshot: {},
    code_commit_sha: null,
    total_questions: 198,
    completed_questions: 198,
    correct_questions: 150,
    error_questions: 2,
    score: 75.76,
    completion_rate: 98.99,
    answered_accuracy: 76.53,
    average_latency_ms: 1234,
    input_tokens: 1000,
    output_tokens: 2000,
    estimated_cost: 0.1234,
    cancellation_requested: false,
    started_at: "2026-08-27T08:00:00Z",
    finished_at: "2026-08-27T08:30:00Z",
    created_at: "2026-08-27T07:59:00Z",
    error_message: null,
    ...overrides,
  };
}

function responseFixture(position: number): EvaluationResponse {
  return {
    id: `response-${position}`,
    run_id: "run-001",
    question_id: `question-${position}`,
    question_external_id: `gpqa-${position}`,
    question_type: "multiple_choice",
    prompt: `Question ${position}`,
    choices: { A: "Alpha", B: "Beta" },
    raw_response: "Answer: A",
    parsed_answer: "A",
    reference_answer_snapshot: "A",
    score: 1,
    evaluator_name: "multiple_choice_v1",
    latency_ms: 100,
    input_tokens: 10,
    output_tokens: 5,
    estimated_cost: 0.001,
    error_type: null,
    error_message: null,
    created_at: "2026-08-27T08:01:00Z",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/runs/run-001"]}>
      <Routes>
        <Route path="/runs/:runId" element={<RunDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RunDetailPage", () => {
  beforeEach(() => {
    apiMocks.run.mockResolvedValue(runFixture());
    apiMocks.responses.mockResolvedValue({
      items: [responseFixture(1)],
      total: 198,
      offset: 0,
      limit: 100,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("requests the first evidence page, displays the API total, and links back to runs", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Frozen Model" })).toBeInTheDocument();
    expect(apiMocks.run).toHaveBeenCalledWith("run-001");
    expect(apiMocks.responses).toHaveBeenCalledWith("run-001", { offset: 0, limit: 100 });
    expect(screen.getByText("显示 1–1 / 共 198 条 · 本页 0 条错误")).toBeInTheDocument();
    expect(screen.queryByText(/共 1 条/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "评测记录" })).toHaveAttribute("href", "/runs");
  });

  it("requests offset 100 and numbers the first item on page two as 101", async () => {
    apiMocks.responses.mockImplementation((_: string, params: { offset?: number }) => {
      const offset = params.offset ?? 0;
      return Promise.resolve({
        items: [responseFixture(offset + 1)],
        total: 198,
        offset,
        limit: 100,
      });
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("gpqa-1");

    await user.click(screen.getByRole("button", { name: "下一页" }));

    await waitFor(() => {
      expect(apiMocks.responses).toHaveBeenLastCalledWith(
        "run-001",
        { offset: 100, limit: 100 },
      );
    });
    expect(await screen.findByText("gpqa-101")).toBeInTheDocument();
    expect(screen.getByText("101", { selector: ".question-index" })).toBeInTheDocument();
    expect(screen.getByText("显示 101–101 / 共 198 条 · 本页 0 条错误"))
      .toBeInTheDocument();
  });

  it("does not let polling supersede a slower evidence page request", async () => {
    const nextRun = deferred<EvaluationRun>();
    const nextEvidence = deferred<{
      items: EvaluationResponse[];
      total: number;
      offset: number;
      limit: number;
    }>();
    apiMocks.run
      .mockResolvedValueOnce(runFixture({ status: "running", finished_at: null }))
      .mockReturnValueOnce(nextRun.promise);
    apiMocks.responses
      .mockResolvedValueOnce({ items: [responseFixture(1)], total: 198, offset: 0, limit: 100 })
      .mockReturnValueOnce(nextEvidence.promise);
    const intervalSpy = vi.spyOn(window, "setInterval");
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("gpqa-1");
    const poll = intervalSpy.mock.calls.at(-1)?.[0] as TimerHandler;

    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(apiMocks.run).toHaveBeenCalledTimes(2));
    expect(screen.getByText("正在读取运行证据")).toBeInTheDocument();

    act(() => { if (typeof poll === "function") poll(); });
    expect(apiMocks.run).toHaveBeenCalledTimes(2);
    expect(apiMocks.responses).toHaveBeenCalledTimes(2);

    await act(async () => {
      nextRun.resolve(runFixture());
      nextEvidence.resolve({
        items: [responseFixture(101)],
        total: 198,
        offset: 100,
        limit: 100,
      });
      await Promise.all([nextRun.promise, nextEvidence.promise]);
    });
    expect(await screen.findByText("gpqa-101")).toBeInTheDocument();
    expect(screen.queryByText("正在读取运行证据")).not.toBeInTheDocument();
  });

  it("returns to the runs list when the run does not exist", async () => {
    apiMocks.run.mockResolvedValue(null);
    apiMocks.responses.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 100 });
    renderPage();

    expect(await screen.findByText("Run 不存在")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回评测记录" })).toHaveAttribute("href", "/runs");
  });
});
