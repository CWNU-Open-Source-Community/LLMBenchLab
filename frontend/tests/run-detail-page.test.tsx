import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EvaluationResponse, EvaluationResponseList, EvaluationRun } from "../src/api/types";
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
    attempt_count: 1,
    max_attempts: 3,
    failed_attempt_count: 0,
    dispatch_count: 1,
    last_scheduled_at: "2026-08-27T08:00:00Z",
    governance_policy_id: "policy-001",
    governance_status: "managed",
    governance_reason: null,
    governance_not_before: null,
    input_token_reservation: null,
    lifetime_request_budget: null,
    lifetime_token_budget: null,
    lifetime_cost_budget_usd: null,
    lease_owner: null,
    lease_token: 1,
    lease_expires_at: null,
    heartbeat_at: null,
    next_attempt_at: null,
    last_enqueued_at: "2026-08-27T07:59:01Z",
    last_error: null,
    dead_lettered_at: null,
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
      known_input_tokens: 1000,
      known_output_tokens: 2000,
      input_token_reported_responses: 198,
      output_token_reported_responses: 198,
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
    expect(screen.getByText("显示 1–1 / 共 198 条 · 本页 0 条未得分 · 0 条执行异常"))
      .toBeInTheDocument();
    expect(screen.queryByText(/共 1 条/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "评测记录" })).toHaveAttribute("href", "/runs");
    expect(screen.getByText("数据库治理已启用")).toBeInTheDocument();
  });

  it("separates ordinary wrong answers from exceptional responses", async () => {
    apiMocks.run.mockResolvedValue(runFixture({
      correct_questions: 179,
      error_questions: 2,
      score: 90.404,
    }));

    renderPage();

    const metric = (await screen.findByText("未得分")).closest("article");
    expect(metric).not.toBeNull();
    expect(within(metric!).getByText("19")).toBeInTheDocument();
    expect(within(metric!).getByText("普通答错 17 · 执行异常 2 · 正确 179"))
      .toBeInTheDocument();
    expect(screen.queryByText("错误题")).not.toBeInTheDocument();
  });

  it("separates unscored and exceptional responses on the current page", async () => {
    apiMocks.responses.mockResolvedValue({
      items: [
        { ...responseFixture(1), score: 0, parsed_answer: "B" },
        {
          ...responseFixture(2),
          score: 0,
          raw_response: null,
          parsed_answer: null,
          error_type: "output_truncated",
          error_message: "Provider stream ended early",
        },
        responseFixture(3),
      ],
      total: 3,
      offset: 0,
      limit: 100,
      known_input_tokens: 30,
      known_output_tokens: 15,
      input_token_reported_responses: 3,
      output_token_reported_responses: 3,
    });

    renderPage();

    expect(await screen.findByText("显示 1–3 / 共 3 条 · 本页 2 条未得分 · 1 条执行异常"))
      .toBeInTheDocument();
  });

  it("shows the known token subtotal and coverage when the exact total is unknown", async () => {
    apiMocks.run.mockResolvedValue(runFixture({
      correct_questions: 179,
      error_questions: 2,
      input_tokens: null,
      output_tokens: null,
    }));
    apiMocks.responses.mockResolvedValue({
      items: [responseFixture(1)],
      total: 198,
      offset: 0,
      limit: 100,
      known_input_tokens: 45_509,
      known_output_tokens: 4_561_625,
      input_token_reported_responses: 196,
      output_token_reported_responses: 196,
    });

    renderPage();

    const metric = (await screen.findByText("Token")).closest("article");
    expect(metric).not.toBeNull();
    expect(within(metric!).getByText("已知小计 460.7万")).toBeInTheDocument();
    expect(within(metric!).getByText(/输入 4.6万 \/ 输出 456.2万/)).toBeInTheDocument();
    expect(within(metric!).getByText(/输入\/输出覆盖各 196\/198 题，完整总量未知/))
      .toBeInTheDocument();
  });

  it("keeps the exact Run token total authoritative when it is available", async () => {
    apiMocks.responses.mockResolvedValue({
      items: [responseFixture(1)],
      total: 198,
      offset: 0,
      limit: 100,
      known_input_tokens: 1000,
      known_output_tokens: 2000,
      input_token_reported_responses: 198,
      output_token_reported_responses: 198,
    });

    renderPage();

    const metric = (await screen.findByText("Token")).closest("article");
    expect(metric).not.toBeNull();
    expect(within(metric!).getByText("3,000")).toBeInTheDocument();
    expect(within(metric!).getByText("输入 1,000 / 输出 2,000")).toBeInTheDocument();
    expect(within(metric!).queryByText(/完整总量未知/)).not.toBeInTheDocument();
  });

  it("uses the response subtotal when parallel Run and evidence snapshots disagree", async () => {
    apiMocks.run.mockResolvedValue(runFixture({
      status: "running",
      total_questions: 200,
      completed_questions: 198,
      finished_at: null,
    }));
    apiMocks.responses.mockResolvedValue({
      items: [responseFixture(1)],
      total: 199,
      offset: 0,
      limit: 100,
      known_input_tokens: 1010,
      known_output_tokens: 2005,
      input_token_reported_responses: 199,
      output_token_reported_responses: 199,
    });

    renderPage();

    const metric = (await screen.findByText("Token")).closest("article");
    expect(metric).not.toBeNull();
    expect(within(metric!).getByText("已知小计 3,015")).toBeInTheDocument();
    expect(within(metric!).getByText(/输入\/输出覆盖各 199\/199 题，完整总量未知/))
      .toBeInTheDocument();
  });

  it("does not invent a known token subtotal when no response reports usage", async () => {
    apiMocks.run.mockResolvedValue(runFixture({ input_tokens: null, output_tokens: null }));
    apiMocks.responses.mockResolvedValue({
      items: [responseFixture(1)],
      total: 3,
      offset: 0,
      limit: 100,
      known_input_tokens: 0,
      known_output_tokens: 0,
      input_token_reported_responses: 0,
      output_token_reported_responses: 0,
    });

    renderPage();

    const metric = (await screen.findByText("Token")).closest("article");
    expect(metric).not.toBeNull();
    expect(within(metric!).getByText("—")).toBeInTheDocument();
    expect(within(metric!).getByText("输入/输出覆盖均为 0/3 题，完整总量未知"))
      .toBeInTheDocument();
    expect(within(metric!).queryByText(/已知小计 0/)).not.toBeInTheDocument();
  });

  it("shows an empty usage state before the first response is stored", async () => {
    apiMocks.run.mockResolvedValue(runFixture({
      status: "pending",
      completed_questions: 0,
      correct_questions: 0,
      error_questions: 0,
      input_tokens: null,
      output_tokens: null,
      started_at: null,
      finished_at: null,
    }));
    apiMocks.responses.mockResolvedValue({
      items: [],
      total: 0,
      offset: 0,
      limit: 100,
      known_input_tokens: 0,
      known_output_tokens: 0,
      input_token_reported_responses: 0,
      output_token_reported_responses: 0,
    });

    renderPage();

    const metric = (await screen.findByText("Token")).closest("article");
    expect(metric).not.toBeNull();
    expect(within(metric!).getByText("—")).toBeInTheDocument();
    expect(within(metric!).getByText("暂无逐题 usage")).toBeInTheDocument();
  });

  it("reports input and output coverage separately when providers omit one side", async () => {
    apiMocks.run.mockResolvedValue(runFixture({ input_tokens: null, output_tokens: null }));
    apiMocks.responses.mockResolvedValue({
      items: [responseFixture(1)],
      total: 3,
      offset: 0,
      limit: 100,
      known_input_tokens: 30,
      known_output_tokens: 5,
      input_token_reported_responses: 2,
      output_token_reported_responses: 1,
    });

    renderPage();

    const metric = (await screen.findByText("Token")).closest("article");
    expect(metric).not.toBeNull();
    expect(within(metric!).getByText("已知小计 35")).toBeInTheDocument();
    expect(within(metric!).getByText(/输入覆盖 2\/3 题 · 输出覆盖 1\/3 题/))
      .toBeInTheDocument();
  });

  it("shows a delayed backpressure reason and the earliest database time explicitly in UTC", async () => {
    apiMocks.run.mockResolvedValue(runFixture({
      status: "pending",
      governance_status: "delayed",
      governance_reason: "governance_provider_rpm",
      governance_not_before: "2026-08-27T09:00:00Z",
      started_at: null,
      finished_at: null,
    }));

    renderPage();

    expect(await screen.findByRole("heading", { name: "治理背压中，等待重新调度" }))
      .toBeInTheDocument();
    expect(screen.getByText("治理背压中，Run 已暂缓（deferred）")).toBeInTheDocument();
    expect(screen.getByText(/Provider每分钟请求额度暂时占满/)).toBeInTheDocument();
    expect(screen.getByText(/2026-08-27 09:00:00\.000 UTC/)).toBeInTheDocument();
    expect(screen.queryByText("governance_provider_rpm")).not.toBeInTheDocument();
  });

  it("describes overdraw as actual usage exceeding an explicit reservation", async () => {
    apiMocks.run.mockResolvedValue(runFixture({
      status: "failed",
      governance_status: "exhausted",
      governance_reason: "governance_global_overdrawn",
      error_message: null,
    }));

    renderPage();

    expect(await screen.findByText("治理硬边界已终止 Run")).toBeInTheDocument();
    expect(screen.getByText(/全局实际用量曾被判定超过预留/)).toBeInTheDocument();
    expect(screen.queryByText(/保守结算超额/)).not.toBeInTheDocument();
  });

  it("explains exhausted and legacy-unmanaged boundaries without reflecting unknown reasons", async () => {
    const unsafeReason = "untrusted-reason-that-must-not-be-reflected";
    apiMocks.run.mockResolvedValueOnce(runFixture({
      status: "failed",
      governance_status: "exhausted",
      governance_reason: unsafeReason,
      error_message: null,
    }));
    const exhausted = renderPage();

    expect(await screen.findByText("治理硬边界已终止 Run")).toBeInTheDocument();
    expect(screen.getByText(/未公开原因/)).toBeInTheDocument();
    expect(screen.queryByText(unsafeReason)).not.toBeInTheDocument();
    exhausted.unmount();

    apiMocks.run.mockResolvedValueOnce(runFixture({
      governance_policy_id: null,
      governance_status: "legacy_unmanaged",
    }));
    renderPage();

    expect(await screen.findByText("此 Run 未纳入数据库治理")).toBeInTheDocument();
    expect(screen.getByText(/当前 Web\/API policy 不保证/)).toBeInTheDocument();
  });

  it("requests offset 100 and numbers the first item on page two as 101", async () => {
    apiMocks.responses.mockImplementation((_: string, params: { offset?: number }) => {
      const offset = params.offset ?? 0;
      return Promise.resolve({
        items: [responseFixture(offset + 1)],
        total: 198,
        offset,
        limit: 100,
        known_input_tokens: 1000,
        known_output_tokens: 2000,
        input_token_reported_responses: 198,
        output_token_reported_responses: 198,
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
    expect(screen.getByText("显示 101–101 / 共 198 条 · 本页 0 条未得分 · 0 条执行异常"))
      .toBeInTheDocument();
  });

  it("does not let polling supersede a slower evidence page request", async () => {
    const nextRun = deferred<EvaluationRun>();
    const nextEvidence = deferred<EvaluationResponseList>();
    apiMocks.run
      .mockResolvedValueOnce(runFixture({ status: "running", finished_at: null }))
      .mockReturnValueOnce(nextRun.promise);
    apiMocks.responses
      .mockResolvedValueOnce({
        items: [responseFixture(1)],
        total: 198,
        offset: 0,
        limit: 100,
        known_input_tokens: 1000,
        known_output_tokens: 2000,
        input_token_reported_responses: 198,
        output_token_reported_responses: 198,
      })
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
        known_input_tokens: 1000,
        known_output_tokens: 2000,
        input_token_reported_responses: 198,
        output_token_reported_responses: 198,
      });
      await Promise.all([nextRun.promise, nextEvidence.promise]);
    });
    expect(await screen.findByText("gpqa-101")).toBeInTheDocument();
    expect(screen.queryByText("正在读取运行证据")).not.toBeInTheDocument();
  });

  it("returns to the runs list when the run does not exist", async () => {
    apiMocks.run.mockResolvedValue(null);
    apiMocks.responses.mockResolvedValue({
      items: [],
      total: 0,
      offset: 0,
      limit: 100,
      known_input_tokens: 0,
      known_output_tokens: 0,
      input_token_reported_responses: 0,
      output_token_reported_responses: 0,
    });
    renderPage();

    expect(await screen.findByText("Run 不存在")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回评测记录" })).toHaveAttribute("href", "/runs");
  });
});
