import { AlertTriangle, ArrowRight, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { EvaluationRun, ListResponse, RunStatus } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { formatPercent, formatUtc8, statusLabels } from "../lib/format";

const PAGE_SIZE = 20;
const ACTIVE_STATUSES = new Set<RunStatus>(["pending", "running"]);

function snapshotRecord(run: EvaluationRun, group: "model" | "benchmark"): Record<string, unknown> {
  const value = run.model_parameters_snapshot[group];
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}

function firstSnapshotString(
  run: EvaluationRun,
  group: "model" | "benchmark",
  keys: string[],
  fallback: string,
): string {
  const snapshot = snapshotRecord(run, group);
  for (const key of keys) {
    if (typeof snapshot[key] === "string" && snapshot[key]) return snapshot[key];
  }
  return fallback;
}

function progressPercent(run: EvaluationRun): number {
  if (run.total_questions <= 0) return 0;
  return Math.min(100, run.completed_questions / run.total_questions * 100);
}

export function RunsPage() {
  const [page, setPage] = useState<ListResponse<EvaluationRun> | null>(null);
  const [offset, setOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState<RunStatus | "">("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSequence = useRef(0);
  const requestsInFlight = useRef(0);

  const load = useCallback(async (quiet = false) => {
    // A polling request must never supersede a user-triggered page/filter load.
    // Non-quiet requests may supersede an older poll, and their sequence id will
    // safely discard that older result.
    if (quiet && requestsInFlight.current > 0) return;
    const requestId = ++requestSequence.current;
    requestsInFlight.current += 1;
    if (!quiet) setLoading(true);
    try {
      const result = await api.runs({
        offset,
        limit: PAGE_SIZE,
        run_status: statusFilter || undefined,
      });
      if (requestId !== requestSequence.current) return;
      const lastOffset = result.total === 0
        ? 0
        : Math.floor((result.total - 1) / PAGE_SIZE) * PAGE_SIZE;
      if (offset > lastOffset) {
        setOffset(lastOffset);
        return;
      }
      setPage(result);
      setError(null);
    } catch (reason) {
      if (requestId !== requestSequence.current) return;
      setError(reason instanceof ApiError ? reason.message : "无法读取评测记录。");
    } finally {
      requestsInFlight.current -= 1;
      if (!quiet && requestId === requestSequence.current) setLoading(false);
    }
  }, [offset, statusFilter]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => { requestSequence.current += 1; }, []);

  const hasActiveRuns = useMemo(
    () => page?.items.some((run) => ACTIVE_STATUSES.has(run.status)) ?? false,
    [page],
  );

  useEffect(() => {
    if (!hasActiveRuns) return;
    const timer = window.setInterval(() => void load(true), 2000);
    return () => window.clearInterval(timer);
  }, [hasActiveRuns, load]);

  const refresh = async () => {
    setRefreshing(true);
    await load(true);
    setRefreshing(false);
  };

  const total = page?.total ?? 0;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  if (loading) return <LoadingState label="正在读取评测记录" />;
  if (error && !page) return <ErrorState message={error} retry={() => void load()} />;

  return <>
    <PageHeader
      eyebrow="EVALUATION RUNS"
      title="评测记录"
      description="查看等待中、运行中和已结束的全部评测，并随时返回逐题证据。"
      actions={<>
        <button
          className="secondary-button"
          type="button"
          disabled={refreshing}
          onClick={() => void refresh()}
        >
          <RefreshCw className={refreshing ? "spin" : undefined} size={15} />
          {refreshing ? "刷新中…" : "刷新"}
        </button>
        <Link className="primary-button" to="/runs/new">
          <Play size={15} fill="currentColor" /> 新建评测
        </Link>
      </>}
    />

    <section className="run-list-toolbar" aria-label="评测记录筛选">
      <label htmlFor="run-status-filter">
        状态
        <select
          id="run-status-filter"
          value={statusFilter}
          onChange={(event) => {
            setOffset(0);
            setStatusFilter(event.target.value as RunStatus | "");
          }}
        >
          <option value="">全部状态</option>
          {(Object.entries(statusLabels) as [RunStatus, string][]).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </label>
      <span className="run-list-total">共 {total} 条记录</span>
    </section>

    {error && page && <div className="run-list-error" role="alert">
      <AlertTriangle size={16} />
      <span>{error}</span>
      <button className="text-link" type="button" onClick={() => void refresh()}>重试</button>
    </div>}

    {!page?.items.length ? <EmptyState
      title={statusFilter ? "没有符合条件的评测" : "暂无评测记录"}
      message={statusFilter ? "请选择其他状态，或创建一项新评测。" : "创建评测后，可以从这里随时返回运行详情。"}
      action={<Link className="primary-button" to="/runs/new">新建评测 <ArrowRight size={15} /></Link>}
    /> : <section className="panel run-list-panel">
      <div className="table-scroll">
        <table className="run-list-table">
          <thead>
            <tr>
              <th>模型 / Benchmark</th>
              <th>状态</th>
              <th>进度</th>
              <th>严格总分</th>
              <th>创建时间</th>
              <th>结束时间</th>
              <th><span className="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            {page.items.map((run) => {
              const modelName = firstSnapshotString(run, "model", ["name"], run.model_id);
              const benchmarkName = firstSnapshotString(
                run,
                "benchmark",
                ["name", "slug"],
                run.benchmark_id,
              );
              const benchmarkVersion = firstSnapshotString(run, "benchmark", ["version"], "");
              const progress = progressPercent(run);
              return <tr key={run.id}>
                <td>
                  <Link className="row-title" to={`/runs/${run.id}`}>{modelName}</Link>
                  <small>{benchmarkName}{benchmarkVersion && ` · v${benchmarkVersion}`}</small>
                </td>
                <td><StatusBadge status={run.status} /></td>
                <td>
                  <div className="run-list-progress">
                    <progress value={run.completed_questions} max={Math.max(1, run.total_questions)} />
                    <span>{run.completed_questions} / {run.total_questions} · {progress.toFixed(0)}%</span>
                  </div>
                </td>
                <td className="score-cell">{formatPercent(run.score)}</td>
                <td>{formatUtc8(run.created_at)}</td>
                <td>{formatUtc8(run.finished_at)}</td>
                <td><Link className="run-detail-link" to={`/runs/${run.id}`}>查看详情 <ArrowRight size={14} /></Link></td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
      <footer className="run-list-pagination" aria-label="评测记录分页">
        <span>第 {currentPage} / {totalPages} 页</span>
        <div>
          <button
            className="secondary-button"
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
          >上一页</button>
          <button
            className="secondary-button"
            type="button"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset((value) => value + PAGE_SIZE)}
          >下一页</button>
        </div>
      </footer>
    </section>}
  </>;
}
