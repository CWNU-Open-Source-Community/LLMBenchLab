import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  RunProgressBlock,
  RunProgressCell,
  RunProgressIndex,
  RunStatus,
} from "../src/api/types";
import { RunProgressHeatmap } from "../src/components/RunProgressHeatmap";
import { useRunProgress } from "../src/hooks/useRunProgress";

const apiMocks = vi.hoisted(() => ({
  runProgressIndex: vi.fn(),
  runProgressBlock: vi.fn(),
}));

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      runProgressIndex: apiMocks.runProgressIndex,
      runProgressBlock: apiMocks.runProgressBlock,
    },
  };
});

afterEach(() => cleanup());

function progressCell(
  position: number,
  outcome: RunProgressCell["outcome"],
  overrides: Partial<RunProgressCell> = {},
): RunProgressCell {
  return {
    position,
    outcome,
    score: outcome === "passed" ? 1 : 0,
    latency_ms: 1234,
    input_tokens: 120,
    output_tokens: 30,
    estimated_cost: 0.0012,
    error_type: outcome === "error" ? "provider_error" : null,
    ...overrides,
  };
}

function progressIndex(overrides: Partial<RunProgressIndex> = {}): RunProgressIndex {
  return {
    block_size: 512,
    total_questions: 4,
    completed_questions: 3,
    correct_questions: 1,
    error_questions: 1,
    score: 25,
    completion_rate: 50,
    answered_accuracy: 50,
    average_latency_ms: 1234,
    known_input_tokens: 360,
    known_output_tokens: 90,
    input_token_reported_responses: 3,
    output_token_reported_responses: 3,
    known_estimated_cost: 0.0036,
    estimated_cost_reported_responses: 3,
    blocks: [{ block_index: 0, response_count: 3 }],
    ...overrides,
  };
}

function block(blockIndex: number, items: RunProgressCell[]): RunProgressBlock {
  return { block_index: blockIndex, items };
}

describe("RunProgressHeatmap", () => {
  it("renders four non-color-only outcomes with one keyboard tab stop", () => {
    render(
      <RunProgressHeatmap
        index={progressIndex()}
        items={[
          progressCell(0, "passed"),
          progressCell(1, "wrong"),
          progressCell(2, "error"),
        ]}
      />,
    );

    expect(screen.getByText("通过 1")).toBeInTheDocument();
    expect(screen.getByText("答案错误 1")).toBeInTheDocument();
    expect(screen.getByText("执行异常 1")).toBeInTheDocument();
    expect(screen.getByText("未执行 1")).toBeInTheDocument();
    const grid = screen.getByRole("grid", { name: /逐题评测进度/ });
    expect(grid).toHaveAttribute("tabindex", "0");
    expect(grid).toHaveAttribute("aria-rowcount", "1");
    expect(screen.getByRole("gridcell", { name: /第 1 题，通过/ }))
      .toHaveAttribute("data-outcome", "passed");
    expect(screen.getByRole("gridcell", { name: /第 2 题，答案错误/ }))
      .toHaveAttribute("data-outcome", "wrong");
    expect(screen.getByRole("gridcell", { name: /第 3 题，执行异常/ }))
      .toHaveAttribute("data-outcome", "error");
    expect(screen.getByRole("gridcell", { name: /第 4 题，未执行/ }))
      .toHaveAttribute("data-outcome", "pending");
    expect(screen.getAllByRole("gridcell").every((cell) => cell.tabIndex === -1)).toBe(true);
  });

  it("shares hover, keyboard focus and tap details, including known totals", async () => {
    const user = userEvent.setup();
    render(
      <RunProgressHeatmap
        index={progressIndex()}
        items={[
          progressCell(0, "passed"),
          progressCell(1, "wrong", {
            input_tokens: null,
            output_tokens: null,
            estimated_cost: null,
          }),
          progressCell(2, "error", { latency_ms: null }),
        ]}
      />,
    );

    await user.hover(screen.getByRole("gridcell", { name: /第 1 题，通过/ }));
    let tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("输入 Token120");
    expect(tooltip).toHaveTextContent("输出 Token30");
    expect(tooltip).toHaveTextContent("Token 合计150");
    expect(tooltip).toHaveTextContent("运行时间1.23 s");
    expect(tooltip).toHaveTextContent("$0.001200");
    expect(tooltip).toHaveTextContent("1 / 1");

    await user.unhover(screen.getByRole("gridcell", { name: /第 1 题，通过/ }));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

    const grid = screen.getByRole("grid", { name: /逐题评测进度/ });
    grid.focus();
    await user.keyboard("{ArrowRight}");
    expect(grid.getAttribute("aria-activedescendant")).toMatch(/-cell-1$/);
    tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("第 2 题");
    expect(tooltip).toHaveTextContent("输入 Token未上报");
    expect(tooltip).toHaveTextContent("Token 合计未上报");

    await user.click(screen.getByRole("gridcell", { name: /第 3 题，执行异常/ }));
    tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("provider_error");
    expect(tooltip).toHaveTextContent("运行时间未上报");
  });

  it("lets keyboard navigation take over from a stationary mouse hover", async () => {
    const user = userEvent.setup();
    render(
      <RunProgressHeatmap
        index={progressIndex()}
        items={[
          progressCell(0, "passed"),
          progressCell(1, "wrong"),
          progressCell(2, "error"),
        ]}
      />,
    );

    await user.hover(screen.getByRole("gridcell", { name: /第 1 题，通过/ }));
    const grid = screen.getByRole("grid", { name: /逐题评测进度/ });
    grid.focus();
    await user.keyboard("{End}");

    expect(grid.getAttribute("aria-activedescendant")).toMatch(/-cell-3$/);
    expect(screen.getByRole("tooltip")).toHaveTextContent("第 4 题");
    expect(screen.getByRole("tooltip")).toHaveTextContent("未执行");
  });

  it("describes a cell without persisted evidence without inventing usage", async () => {
    const user = userEvent.setup();
    render(
      <RunProgressHeatmap
        index={progressIndex({
          total_questions: 1,
          completed_questions: 0,
          correct_questions: 0,
          error_questions: 0,
          score: 0,
          completion_rate: 0,
          answered_accuracy: null,
          average_latency_ms: null,
          known_input_tokens: 0,
          known_output_tokens: 0,
          input_token_reported_responses: 0,
          output_token_reported_responses: 0,
          known_estimated_cost: 0,
          estimated_cost_reported_responses: 0,
          blocks: [{ block_index: 0, response_count: 0 }],
        })}
        items={[]}
      />,
    );

    await user.hover(screen.getByRole("gridcell", { name: /第 1 题，未执行/ }));
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("未执行或尚无已保存结果");
    expect(tooltip).toHaveTextContent("输入 Token未上报");
    expect(tooltip).toHaveTextContent("输出 Token未上报");
    expect(tooltip).toHaveTextContent("运行时间未上报");
  });

  it.each([12_032, 20_000])(
    "virtualizes a %i-question grid and keeps the active descendant mounted",
    async (totalQuestions) => {
    const user = userEvent.setup();
    render(
      <RunProgressHeatmap
        index={progressIndex({
          total_questions: totalQuestions,
          completed_questions: 0,
          correct_questions: 0,
          error_questions: 0,
          score: 0,
          completion_rate: 0,
          answered_accuracy: null,
          average_latency_ms: null,
          known_input_tokens: 0,
          known_output_tokens: 0,
          input_token_reported_responses: 0,
          output_token_reported_responses: 0,
          known_estimated_cost: 0,
          estimated_cost_reported_responses: 0,
          blocks: Array.from({ length: Math.ceil(totalQuestions / 512) }, (_, blockIndex) => ({
            block_index: blockIndex,
            response_count: 0,
          })),
        })}
        items={[]}
      />,
    );

    expect(screen.getAllByRole("gridcell").length).toBeLessThan(1000);
    const grid = screen.getByRole("grid", { name: /逐题评测进度/ });
    grid.focus();
    await user.keyboard("{Control>}{End}{/Control}");
    expect(grid.getAttribute("aria-activedescendant"))
      .toMatch(new RegExp(`-cell-${totalQuestions - 1}$`));
    expect(screen.getByRole("gridcell", {
      name: new RegExp(`第 ${totalQuestions} 题，未执行`),
    })).toBeInTheDocument();
    expect(screen.getAllByRole("gridcell").length).toBeLessThan(1100);
    },
  );
});

describe("useRunProgress", () => {
  beforeEach(() => {
    apiMocks.runProgressIndex.mockReset();
    apiMocks.runProgressBlock.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("hydrates non-empty blocks and then refetches only changed response counts", async () => {
    const first = progressIndex({
      total_questions: 1024,
      completed_questions: 2,
      correct_questions: 1,
      error_questions: 0,
      score: 0.1,
      completion_rate: 0.2,
      blocks: [
        { block_index: 0, response_count: 1 },
        { block_index: 1, response_count: 1 },
      ],
    });
    const second = progressIndex({
      ...first,
      completed_questions: 3,
      correct_questions: 2,
      blocks: [
        { block_index: 0, response_count: 1 },
        { block_index: 1, response_count: 2 },
      ],
    });
    apiMocks.runProgressIndex.mockResolvedValueOnce(first).mockResolvedValue(second);
    let secondBlockReads = 0;
    apiMocks.runProgressBlock.mockImplementation((_: string, blockIndex: number) => {
      if (blockIndex === 0) return Promise.resolve(block(0, [progressCell(0, "passed")]));
      secondBlockReads += 1;
      return Promise.resolve(block(1, secondBlockReads === 1
        ? [progressCell(512, "wrong")]
        : [progressCell(512, "wrong"), progressCell(513, "passed")]));
    });

    const { result } = renderHook(() => useRunProgress("run-1", "running", 2));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.cells.map((cell) => cell.position)).toEqual([0, 512]);

    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.cells).toHaveLength(3));
    expect(apiMocks.runProgressBlock.mock.calls.filter((call) => call[1] === 0)).toHaveLength(1);
    expect(apiMocks.runProgressBlock.mock.calls.filter((call) => call[1] === 1)).toHaveLength(2);
  });

  it("does not publish missing initial blocks as pending and retries locally", async () => {
    apiMocks.runProgressIndex.mockResolvedValue(progressIndex());
    apiMocks.runProgressBlock
      .mockRejectedValueOnce(new Error("temporary block failure"))
      .mockResolvedValue(block(0, [
        progressCell(0, "passed"),
        progressCell(1, "wrong"),
        progressCell(2, "error"),
      ]));

    const { result } = renderHook(() => useRunProgress("run-1", "running", 3));
    await waitFor(() => expect(result.current.error).toMatch(/部分题目进度/));
    expect(result.current.ready).toBe(false);
    expect(result.current.cells).toEqual([]);

    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.error).toBeNull();
    expect(result.current.cells).toHaveLength(3);
  });

  it("keeps the last coherent snapshot when a changed block temporarily fails", async () => {
    const first = progressIndex({
      completed_questions: 1,
      correct_questions: 1,
      error_questions: 0,
      blocks: [{ block_index: 0, response_count: 1 }],
    });
    const second = progressIndex({
      completed_questions: 2,
      correct_questions: 1,
      error_questions: 0,
      blocks: [{ block_index: 0, response_count: 2 }],
    });
    apiMocks.runProgressIndex.mockResolvedValueOnce(first).mockResolvedValue(second);
    apiMocks.runProgressBlock
      .mockResolvedValueOnce(block(0, [progressCell(0, "passed")]))
      .mockRejectedValue(new Error("temporary changed-block failure"));

    const { result } = renderHook(() => useRunProgress("run-1", "running", 1));
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.error).toMatch(/部分题目进度/));
    expect(result.current.ready).toBe(true);
    expect(result.current.index?.completed_questions).toBe(1);
    expect(result.current.cells.map((cell) => cell.position)).toEqual([0]);
  });

  it("hides a previously published snapshot immediately when the Run id changes", async () => {
    let resolveNextIndex!: (value: RunProgressIndex) => void;
    const nextIndex = new Promise<RunProgressIndex>((resolve) => {
      resolveNextIndex = resolve;
    });
    apiMocks.runProgressIndex
      .mockResolvedValueOnce(progressIndex())
      .mockReturnValueOnce(nextIndex);
    apiMocks.runProgressBlock.mockResolvedValue(block(0, [
      progressCell(0, "passed"),
      progressCell(1, "wrong"),
      progressCell(2, "error"),
    ]));

    const { result, rerender } = renderHook(
      ({ runId }) => useRunProgress(runId, "running", 3),
      { initialProps: { runId: "run-1" } },
    );
    await waitFor(() => expect(result.current.ready).toBe(true));

    rerender({ runId: "run-2" });
    expect(result.current.index).toBeNull();
    expect(result.current.cells).toEqual([]);
    expect(result.current.ready).toBe(false);

    resolveNextIndex(progressIndex());
    await waitFor(() => expect(result.current.ready).toBe(true));
  });

  it("keeps polling a terminal run until the index reaches the final completed count", async () => {
    const first = progressIndex({
      completed_questions: 1,
      correct_questions: 1,
      error_questions: 0,
      blocks: [{ block_index: 0, response_count: 1 }],
    });
    const final = progressIndex();
    apiMocks.runProgressIndex.mockResolvedValueOnce(first).mockResolvedValue(final);
    apiMocks.runProgressBlock
      .mockResolvedValueOnce(block(0, [progressCell(0, "passed")]))
      .mockResolvedValue(block(0, [
        progressCell(0, "passed"),
        progressCell(1, "wrong"),
        progressCell(2, "error"),
      ]));

    const { result } = renderHook(() => useRunProgress("run-1", "completed", 3));
    await waitFor(() => expect(result.current.ready).toBe(true));
    expect(result.current.reconciled).toBe(false);

    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.reconciled).toBe(true));
    const callsAfterReconcile = apiMocks.runProgressIndex.mock.calls.length;
    await new Promise((resolve) => window.setTimeout(resolve, 1100));
    expect(apiMocks.runProgressIndex).toHaveBeenCalledTimes(callsAfterReconcile);
  });

  it("pauses while the page is hidden and synchronizes immediately when visible", async () => {
    let visibility: DocumentVisibilityState = "hidden";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    apiMocks.runProgressIndex.mockResolvedValue(progressIndex({
      completed_questions: 0,
      correct_questions: 0,
      error_questions: 0,
      blocks: [{ block_index: 0, response_count: 0 }],
    }));

    const { result } = renderHook(() => useRunProgress("run-1", "running" as RunStatus, 0));
    await act(async () => Promise.resolve());
    expect(apiMocks.runProgressIndex).not.toHaveBeenCalled();

    visibility = "visible";
    fireEvent(document, new Event("visibilitychange"));
    await waitFor(() => expect(apiMocks.runProgressIndex).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.ready).toBe(true));
  });
});
