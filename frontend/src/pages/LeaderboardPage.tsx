import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowUpDown, Filter, Medal } from "lucide-react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { Benchmark, LeaderboardEntry, ModelConfig } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import {
  formatCost,
  formatLatency,
  formatPercent,
  formatTokenTotal,
  formatUtc8,
  shortHash,
} from "../lib/format";

export function LeaderboardPage() {
  const [items, setItems] = useState<LeaderboardEntry[]>([]);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [modelId, setModelId] = useState("");
  const [benchmarkId, setBenchmarkId] = useState("");
  const [order, setOrder] = useState("score_desc");
  const [optionsLoaded, setOptionsLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadOptions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [modelData, benchmarkData] = await Promise.all([api.models(), api.benchmarks()]);
      setModels(modelData.items);
      setBenchmarks(benchmarkData.items);
      setBenchmarkId((current) => current || benchmarkData.items[0]?.id || "");
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "无法读取排行榜筛选项。");
    } finally {
      setOptionsLoaded(true);
    }
  }, []);

  const load = useCallback(async () => {
    if (!optionsLoaded) return;
    if (!benchmarkId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await api.leaderboard({
        model_id: modelId || undefined,
        benchmark_id: benchmarkId,
        order,
      });
      setItems(result.items);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "无法读取排行榜。");
    } finally {
      setLoading(false);
    }
  }, [benchmarkId, modelId, optionsLoaded, order]);

  useEffect(() => {
    void loadOptions();
  }, [loadOptions]);
  useEffect(() => {
    void load();
  }, [load]);

  const selectedBenchmark = useMemo(
    () => benchmarks.find((benchmark) => benchmark.id === benchmarkId),
    [benchmarkId, benchmarks],
  );
  const retry = () => void (benchmarkId ? load() : loadOptions());

  return (
    <>
      <PageHeader
        eyebrow="STRICT SCOREBOARD"
        title="排行榜"
        description="默认按全部计划题目计算严格总分；请求错误、空答和解析失败均计 0。"
      />
      <div className="leaderboard-note">
        <Medal size={18} />
        <div>
          <strong>比较边界：llmbenchlab-protocol-v1</strong>
          <span>
            名次只在所选 Benchmark 的同一 version 与 dataset hash 内计算，不跨不可比数据集混排。
          </span>
        </div>
      </div>
      <section className="filter-bar">
        <span>
          <Filter size={15} /> 筛选
        </span>
        <label>
          模型
          <select value={modelId} onChange={(event) => setModelId(event.target.value)}>
            <option value="">全部模型</option>
            {models.map((model) => (
              <option key={model.id} value={model.id}>
                {model.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Benchmark 分区
          <select value={benchmarkId} onChange={(event) => setBenchmarkId(event.target.value)}>
            {benchmarks.map((benchmark) => (
              <option key={benchmark.id} value={benchmark.id}>
                {benchmark.name} v{benchmark.version}
              </option>
            ))}
          </select>
        </label>
        <label>
          <ArrowUpDown size={14} /> 排序
          <select value={order} onChange={(event) => setOrder(event.target.value)}>
            <option value="score_desc">得分从高到低</option>
            <option value="score_asc">得分从低到高</option>
            <option value="latency_asc">延迟从低到高</option>
            <option value="newest">最近完成</option>
          </select>
        </label>
      </section>
      {selectedBenchmark && (
        <p className="partition-caption">
          当前分区：{selectedBenchmark.name} · v{selectedBenchmark.version} · Hash{" "}
          <span title={selectedBenchmark.dataset_hash}>
            {shortHash(selectedBenchmark.dataset_hash, 16)}
          </span>
        </p>
      )}
      {loading ? (
        <LoadingState label="正在计算严格排名" />
      ) : error ? (
        <ErrorState message={error} retry={retry} />
      ) : !benchmarks.length ? (
        <EmptyState
          title="还没有 Benchmark"
          message="先载入内置 Demo 或导入一个版本化 Benchmark。"
          action={
            <Link className="primary-button" to="/benchmarks">
              前往评测集
            </Link>
          }
        />
      ) : items.length === 0 ? (
        <EmptyState
          title="当前分区暂无结果"
          message="只有所选 Benchmark 下的 completed Run 会进入此排名。"
          action={
            <Link className="primary-button" to="/runs/new">
              新建评测
            </Link>
          }
        />
      ) : (
        <section className="panel leaderboard-panel">
          <div className="table-scroll">
            <table className="leaderboard-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>模型 / Benchmark</th>
                  <th>严格总分</th>
                  <th>回答准确率</th>
                  <th>完成率</th>
                  <th>平均延迟</th>
                  <th>Token</th>
                  <th>成本</th>
                  <th>完成时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map((entry, index) => (
                  <tr key={entry.run_id}>
                    <td>
                      <span className={`rank rank-${index + 1}`}>{index + 1}</span>
                    </td>
                    <td>
                      <Link className="row-title" to={`/runs/${entry.run_id}`}>
                        {entry.model_name}
                      </Link>
                      <small>
                        {entry.benchmark_name} · v{entry.benchmark_version}
                        {entry.is_demo && <b className="demo-pill">Demo</b>}
                        <br />
                        {entry.protocol_version} · Hash{" "}
                        <span title={entry.benchmark_hash}>
                          {shortHash(entry.benchmark_hash, 12)}
                        </span>
                      </small>
                    </td>
                    <td className="score-cell">{formatPercent(entry.score)}</td>
                    <td>{formatPercent(entry.answered_accuracy)}</td>
                    <td>{formatPercent(entry.completion_rate)}</td>
                    <td>{formatLatency(entry.average_latency_ms)}</td>
                    <td>{formatTokenTotal(entry.input_tokens, entry.output_tokens)}</td>
                    <td>{formatCost(entry.estimated_cost)}</td>
                    <td>{formatUtc8(entry.finished_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}
