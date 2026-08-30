import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type {
  RunProgressBlock,
  RunProgressBlockSummary,
  RunProgressCell,
  RunProgressIndex,
  RunStatus,
} from "../api/types";

const POLL_INTERVAL_MS = 1000;
const BLOCK_REQUEST_CONCURRENCY = 4;
const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set(["completed", "failed", "cancelled"]);

type ProgressState = {
  index: RunProgressIndex | null;
  cells: RunProgressCell[];
  ready: boolean;
  syncing: boolean;
  error: string | null;
  terminalVerified: boolean;
};

const initialState: ProgressState = {
  index: null,
  cells: [],
  ready: false,
  syncing: false,
  error: null,
  terminalVerified: false,
};

function isAbortError(reason: unknown): boolean {
  return reason instanceof Error && reason.name === "AbortError";
}

function normalizedBlocks(index: RunProgressIndex): RunProgressBlockSummary[] {
  if (index.block_size <= 0 || index.total_questions < 0) return [];
  const blockCount = Math.ceil(index.total_questions / index.block_size);
  const reported = new Map(index.blocks.map((block) => [block.block_index, block.response_count]));
  return Array.from({ length: blockCount }, (_, blockIndex) => ({
    block_index: blockIndex,
    response_count: reported.get(blockIndex) ?? 0,
  }));
}

function validateBlock(
  block: RunProgressBlock,
  blockIndex: number,
  index: RunProgressIndex,
): RunProgressCell[] {
  if (block.block_index !== blockIndex) throw new Error("progress_block_mismatch");
  const start = blockIndex * index.block_size;
  const end = Math.min(start + index.block_size, index.total_questions);
  const seen = new Set<number>();
  for (const cell of block.items) {
    if (cell.position < start || cell.position >= end || seen.has(cell.position)) {
      throw new Error("progress_cell_out_of_range");
    }
    seen.add(cell.position);
  }
  return [...block.items].sort((left, right) => left.position - right.position);
}

async function fetchWithConcurrency(
  blocks: RunProgressBlockSummary[],
  worker: (block: RunProgressBlockSummary) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  const workers = Array.from(
    { length: Math.min(BLOCK_REQUEST_CONCURRENCY, blocks.length) },
    async () => {
      while (cursor < blocks.length) {
        const block = blocks[cursor];
        cursor += 1;
        await worker(block);
      }
    },
  );
  await Promise.all(workers);
}

function flattenBlocks(blocks: Map<number, RunProgressCell[]>): RunProgressCell[] {
  return [...blocks.entries()]
    .sort(([left], [right]) => left - right)
    .flatMap(([, cells]) => cells);
}

export type UseRunProgressResult = {
  index: RunProgressIndex | null;
  cells: RunProgressCell[];
  ready: boolean;
  syncing: boolean;
  error: string | null;
  reconciled: boolean;
  refresh: () => void;
};

export function useRunProgress(
  runId: string,
  runStatus: RunStatus | null,
  expectedCompletedQuestions: number | null,
): UseRunProgressResult {
  const [state, setState] = useState<ProgressState>(initialState);
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  const blockCells = useRef(new Map<number, RunProgressCell[]>());
  const loadedBlockCounts = useRef(new Map<number, number>());
  const requestSequence = useRef(0);
  const requestInFlight = useRef(false);
  const abortController = useRef<AbortController | null>(null);
  const publishedRunId = useRef<string | null>(null);
  const terminalRef = useRef(false);
  const expectedCompletedRef = useRef<number | null>(expectedCompletedQuestions);

  const terminal = runStatus != null && TERMINAL_STATUSES.has(runStatus);
  terminalRef.current = terminal;
  expectedCompletedRef.current = expectedCompletedQuestions;

  const sync = useCallback(async () => {
    if (!runId || document.visibilityState === "hidden" || requestInFlight.current) return;
    const requestId = ++requestSequence.current;
    const controller = new AbortController();
    abortController.current = controller;
    requestInFlight.current = true;
    setState((current) => ({ ...current, syncing: true }));

    try {
      const index = await api.runProgressIndex(runId, controller.signal);
      if (requestId !== requestSequence.current || controller.signal.aborted) return;

      const blocks = normalizedBlocks(index);
      const expectedResponseCount = blocks.reduce(
        (total, block) => total + block.response_count,
        0,
      );
      const changed = blocks.filter(
        (block) => loadedBlockCounts.current.get(block.block_index) !== block.response_count,
      );
      let blockRequestFailed = false;

      for (const block of changed.filter((item) => item.response_count === 0)) {
        blockCells.current.delete(block.block_index);
        loadedBlockCounts.current.set(block.block_index, 0);
      }

      await fetchWithConcurrency(
        changed.filter((item) => item.response_count > 0),
        async (summary) => {
          try {
            const block = await api.runProgressBlock(runId, summary.block_index, controller.signal);
            if (requestId !== requestSequence.current || controller.signal.aborted) return;
            const cells = validateBlock(block, summary.block_index, index);
            blockCells.current.set(summary.block_index, cells);
            // A Response may commit between the index and block reads. Keeping
            // the observed count forces the next index poll to reconcile it.
            loadedBlockCounts.current.set(summary.block_index, cells.length);
          } catch (reason) {
            if (!isAbortError(reason) && !controller.signal.aborted) blockRequestFailed = true;
          }
        },
      );
      if (requestId !== requestSequence.current || controller.signal.aborted) return;

      const blocksMatch = blocks.every(
        (block) => loadedBlockCounts.current.get(block.block_index) === block.response_count,
      );
      const indexConsistent = expectedResponseCount === index.completed_questions;
      const snapshotCurrent = blocksMatch && indexConsistent && !blockRequestFailed;
      if (!snapshotCurrent) {
        setState((current) => ({
          ...current,
          syncing: false,
          error: blockRequestFailed
            ? "部分题目进度暂时无法同步，将自动重试。"
            : "题目进度索引暂未收敛，将自动重试。",
          terminalVerified: false,
        }));
        return;
      }
      publishedRunId.current = runId;
      const terminalVerified = terminalRef.current
        && expectedCompletedRef.current === index.completed_questions;

      setState({
        index,
        cells: flattenBlocks(blockCells.current),
        ready: true,
        syncing: false,
        error: null,
        terminalVerified,
      });
    } catch (reason) {
      if (requestId === requestSequence.current && !isAbortError(reason) && !controller.signal.aborted) {
        setState((current) => ({
          ...current,
          syncing: false,
          error: "题目进度暂时无法同步，将自动重试。",
          terminalVerified: false,
        }));
      }
    } finally {
      if (requestId === requestSequence.current) {
        requestInFlight.current = false;
        if (abortController.current === controller) abortController.current = null;
      }
    }
  }, [runId]);

  useEffect(() => {
    requestSequence.current += 1;
    abortController.current?.abort();
    abortController.current = null;
    requestInFlight.current = false;
    blockCells.current = new Map();
    loadedBlockCounts.current = new Map();
    publishedRunId.current = null;
    setState(initialState);
  }, [runId]);

  useEffect(() => {
    if (terminal) {
      setState((current) => ({ ...current, terminalVerified: false }));
      void sync();
    }
  }, [expectedCompletedQuestions, sync, terminal]);

  const stateMatchesRun = publishedRunId.current === runId;
  const reconciled = stateMatchesRun && state.ready
    && (!terminal || (
      state.terminalVerified
      && state.index?.completed_questions === expectedCompletedQuestions
    ));
  const shouldPoll = visible && (!terminal || !reconciled);

  useEffect(() => {
    if (!shouldPoll) return;
    void sync();
    const timer = window.setInterval(() => void sync(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [shouldPoll, sync]);

  useEffect(() => {
    const handleVisibility = () => {
      const nextVisible = document.visibilityState !== "hidden";
      if (!nextVisible) {
        requestSequence.current += 1;
        abortController.current?.abort();
        abortController.current = null;
        requestInFlight.current = false;
        setState((current) => ({ ...current, syncing: false, terminalVerified: false }));
      }
      setVisible(nextVisible);
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => () => {
    requestSequence.current += 1;
    abortController.current?.abort();
  }, []);

  return {
    index: stateMatchesRun ? state.index : null,
    cells: stateMatchesRun ? state.cells : [],
    ready: stateMatchesRun && state.ready,
    syncing: state.syncing,
    error: state.error,
    reconciled,
    refresh: () => void sync(),
  };
}
