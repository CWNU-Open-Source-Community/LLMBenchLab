import { memo, useEffect, useId, useMemo, useRef, useState } from "react";

import type { RunProgressCell, RunProgressIndex, RunProgressOutcome } from "../api/types";
import { formatCost, formatLatency, formatTokens } from "../lib/format";

const CELL_GAP = 3;
const ROW_HEIGHT = 19;
const VIEWPORT_HEIGHT = 266;
const OVERSCAN_ROWS = 4;
const FALLBACK_WIDTH = 640;

type DisplayOutcome = RunProgressOutcome | "pending";

const outcomeLabels: Record<DisplayOutcome, string> = {
  passed: "通过",
  wrong: "答案错误",
  error: "执行异常",
  pending: "未执行",
};

type RunProgressHeatmapProps = {
  index: RunProgressIndex;
  items: RunProgressCell[];
  syncing?: boolean;
};

function reported(value: number | null, formatter: (known: number) => string): string {
  return value == null ? "未上报" : formatter(value);
}

function cellLabel(position: number, cell: RunProgressCell | undefined): string {
  const outcome: DisplayOutcome = cell?.outcome ?? "pending";
  if (!cell) return `第 ${position + 1} 题，${outcomeLabels[outcome]}，Token 未上报，运行时间未上报`;
  return [
    `第 ${position + 1} 题`,
    outcomeLabels[outcome],
    `输入 Token ${reported(cell.input_tokens, formatTokens)}`,
    `输出 Token ${reported(cell.output_tokens, formatTokens)}`,
    `运行时间 ${reported(cell.latency_ms, formatLatency)}`,
  ].join("，");
}

function RunProgressHeatmapView({ index, items, syncing = false }: RunProgressHeatmapProps) {
  const headingId = useId();
  const tooltipId = useId();
  const cellIdPrefix = useId();
  const viewportRef = useRef<HTMLDivElement>(null);
  const [viewportWidth, setViewportWidth] = useState(FALLBACK_WIDTH);
  const [scrollTop, setScrollTop] = useState(0);
  const [activePosition, setActivePosition] = useState(0);
  const [hoverPosition, setHoverPosition] = useState<number | null>(null);
  const [hasFocus, setHasFocus] = useState(false);
  const [pinned, setPinned] = useState(false);
  const cellByPosition = useMemo(
    () => new Map(items.map((item) => [item.position, item])),
    [items],
  );
  const columns = Math.max(8, Math.floor(viewportWidth / 19));
  const rowCount = Math.ceil(index.total_questions / columns);
  const viewportHeight = Math.min(
    VIEWPORT_HEIGHT,
    Math.max(ROW_HEIGHT, rowCount * ROW_HEIGHT + 4),
  );
  const visibleRowCount = Math.ceil(viewportHeight / ROW_HEIGHT);
  const firstVisibleRow = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN_ROWS);
  const lastVisibleRow = Math.min(
    rowCount - 1,
    Math.ceil((scrollTop + viewportHeight) / ROW_HEIGHT) + OVERSCAN_ROWS,
  );
  const activeRow = Math.floor(activePosition / columns);
  const renderedRows = useMemo(() => {
    const rows = new Set<number>();
    for (let row = firstVisibleRow; row <= lastVisibleRow; row += 1) rows.add(row);
    if (index.total_questions > 0) rows.add(activeRow);
    return [...rows].sort((left, right) => left - right);
  }, [activeRow, firstVisibleRow, index.total_questions, lastVisibleRow]);
  const selectedPosition = hoverPosition ?? ((hasFocus || pinned) ? activePosition : null);
  const selectedCell = selectedPosition == null ? undefined : cellByPosition.get(selectedPosition);
  const selectedOutcome: DisplayOutcome = selectedCell?.outcome ?? "pending";
  const passed = index.correct_questions;
  const errors = index.error_questions;
  const wrong = Math.max(0, index.completed_questions - passed - errors);
  const pending = Math.max(0, index.total_questions - index.completed_questions);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const measure = () => setViewportWidth(viewport.clientWidth || FALLBACK_WIDTH);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setActivePosition((current) => Math.min(current, Math.max(0, index.total_questions - 1)));
  }, [index.total_questions]);

  const revealPosition = (position: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const row = Math.floor(position / columns);
    const top = row * ROW_HEIGHT;
    const bottom = top + ROW_HEIGHT;
    if (top < viewport.scrollTop) viewport.scrollTop = top;
    else if (bottom > viewport.scrollTop + viewportHeight) {
      viewport.scrollTop = Math.max(0, bottom - viewportHeight);
    }
    setScrollTop(viewport.scrollTop);
  };

  const selectPosition = (position: number) => {
    if (index.total_questions === 0) return;
    const next = Math.max(0, Math.min(index.total_questions - 1, position));
    setActivePosition(next);
    revealPosition(next);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (index.total_questions === 0) return;
    const currentRow = Math.floor(activePosition / columns);
    let next: number | null = null;
    switch (event.key) {
      case "ArrowRight": next = activePosition + 1; break;
      case "ArrowLeft": next = activePosition - 1; break;
      case "ArrowDown": next = activePosition + columns; break;
      case "ArrowUp": next = activePosition - columns; break;
      case "Home": next = event.ctrlKey || event.metaKey ? 0 : currentRow * columns; break;
      case "End": next = event.ctrlKey || event.metaKey
        ? index.total_questions - 1
        : Math.min(index.total_questions - 1, (currentRow + 1) * columns - 1); break;
      case "PageDown": next = activePosition + visibleRowCount * columns; break;
      case "PageUp": next = activePosition - visibleRowCount * columns; break;
      case "Enter":
      case " ":
        event.preventDefault();
        setHoverPosition(null);
        setPinned(true);
        return;
      case "Escape":
        event.preventDefault();
        setPinned(false);
        setHoverPosition(null);
        return;
      default:
        return;
    }
    event.preventDefault();
    setHoverPosition(null);
    setPinned(false);
    selectPosition(next);
  };

  return (
    <section className="panel run-progress-panel" aria-labelledby={headingId}>
      <div className="run-progress-heading">
        <div>
          <span className="section-index">PROGRESS MAP</span>
          <h2 id={headingId}>逐题进度热力图</h2>
          <p>每格对应 Benchmark 中的绝对题号；白格也可能是正在执行但尚未保存结果。</p>
        </div>
        <span className="run-progress-sync" aria-hidden="true">
          {syncing ? "同步中…" : `${index.completed_questions} / ${index.total_questions}`}
        </span>
      </div>
      <div className="run-progress-legend" aria-label="题目状态图例">
        <span><i className="progress-swatch progress-passed" />通过 {passed}</span>
        <span><i className="progress-swatch progress-wrong" />答案错误 {wrong}</span>
        <span><i className="progress-swatch progress-error">×</i>执行异常 {errors}</span>
        <span><i className="progress-swatch progress-pending" />未执行 {pending}</span>
      </div>
      {index.total_questions > 0 ? (
        <div
          ref={viewportRef}
          className="run-progress-viewport"
          style={{ height: viewportHeight }}
          role="grid"
          tabIndex={0}
          aria-label="逐题评测进度，使用方向键浏览，回车固定详情"
          aria-rowcount={rowCount}
          aria-colcount={columns}
          aria-activedescendant={`${cellIdPrefix}-cell-${activePosition}`}
          aria-describedby={selectedPosition == null ? undefined : tooltipId}
          aria-busy={syncing}
          onFocus={() => setHasFocus(true)}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setHasFocus(false);
          }}
          onKeyDown={handleKeyDown}
          onMouseLeave={() => setHoverPosition(null)}
          onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
        >
          <div className="run-progress-spacer" style={{ height: rowCount * ROW_HEIGHT }}>
            {renderedRows.map((row) => {
              const rowStart = row * columns;
              const rowEnd = Math.min(index.total_questions, rowStart + columns);
              return (
                <div
                  className="run-progress-grid-row"
                  role="row"
                  aria-rowindex={row + 1}
                  key={row}
                  style={{
                    top: row * ROW_HEIGHT,
                    gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
                    gap: CELL_GAP,
                  }}
                >
                  {Array.from({ length: rowEnd - rowStart }, (_, offset) => {
                    const position = rowStart + offset;
                    const cell = cellByPosition.get(position);
                    const outcome: DisplayOutcome = cell?.outcome ?? "pending";
                    return (
                      <button
                        type="button"
                        role="gridcell"
                        tabIndex={-1}
                        id={`${cellIdPrefix}-cell-${position}`}
                        key={position}
                        className={`run-progress-cell progress-${outcome}`}
                        data-outcome={outcome}
                        aria-colindex={offset + 1}
                        aria-label={cellLabel(position, cell)}
                        aria-selected={activePosition === position}
                        onMouseEnter={() => setHoverPosition(position)}
                        onClick={() => {
                          setActivePosition(position);
                          setPinned(true);
                          viewportRef.current?.focus();
                        }}
                      >
                        {outcome === "error" && <span aria-hidden="true">×</span>}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>
        </div>
      ) : <div className="inline-empty">该 Run 没有计划题目。</div>}
      <span className="sr-only">
        已完成 {index.completed_questions} / {index.total_questions} 题
      </span>
      {selectedPosition != null && index.total_questions > 0 && (
        <div className="run-progress-tooltip" id={tooltipId} role="tooltip">
          <div>
            <strong>第 {selectedPosition + 1} 题</strong>
            <span className={`progress-detail-status progress-detail-${selectedOutcome}`}>
              {outcomeLabels[selectedOutcome]}
            </span>
          </div>
          {!selectedCell && <p>未执行或尚无已保存结果。</p>}
          <dl>
            <div><dt>得分</dt><dd>{selectedCell ? `${selectedCell.score} / 1` : "未上报"}</dd></div>
            <div><dt>输入 Token</dt><dd>{reported(selectedCell?.input_tokens ?? null, formatTokens)}</dd></div>
            <div><dt>输出 Token</dt><dd>{reported(selectedCell?.output_tokens ?? null, formatTokens)}</dd></div>
            <div><dt>Token 合计</dt><dd>{selectedCell?.input_tokens != null && selectedCell.output_tokens != null ? formatTokens(selectedCell.input_tokens + selectedCell.output_tokens) : "未上报"}</dd></div>
            <div><dt>运行时间</dt><dd>{reported(selectedCell?.latency_ms ?? null, formatLatency)}</dd></div>
            <div><dt>估算成本</dt><dd>{reported(selectedCell?.estimated_cost ?? null, formatCost)}</dd></div>
            {selectedCell?.error_type && <div><dt>异常类型</dt><dd>{selectedCell.error_type}</dd></div>}
          </dl>
        </div>
      )}
    </section>
  );
}

export const RunProgressHeatmap = memo(RunProgressHeatmapView);
