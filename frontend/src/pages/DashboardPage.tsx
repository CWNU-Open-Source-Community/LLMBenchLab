import { useCallback, useEffect, useState } from "react";
import { ArrowRight, Boxes, CheckCircle2, Clock3, FlaskConical, Play, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { api, ApiError } from "../api/client";
import type { DashboardSummary } from "../api/types";
import { ErrorState, LoadingState } from "../components/AsyncState";
import { StatusBadge } from "../components/StatusBadge";
import { formatCost, formatLatency, formatPercent, formatTokens, formatTokenTotal, formatUtc8 } from "../lib/format";

export function DashboardPage() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setSummary(await api.summary()); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取概览。"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (loading) return <LoadingState label="正在生成评测概览" />;
  if (error || !summary) return <ErrorState message={error || "概览数据为空。"} retry={() => void load()} />;
  const chartData = summary.recent_runs.slice().reverse().map((run) => ({
    name: run.model_name.length > 10 ? `${run.model_name.slice(0, 10)}…` : run.model_name,
    score: run.score,
  }));
  return <>
    <section className="hero-panel dashboard-hero">
      <div>
        <p className="kicker">可复现，不靠印象</p>
        <h1>让每一次模型评测，<br /><span>都有证据可循。</span></h1>
        <p className="hero-copy">从原始回答到严格总分，保存完整协议、数据版本与逐题证据。用离线 Mock Demo 验证第一条评测链路。</p>
        <Link className="primary-action" to="/runs/new"><Play size={17} fill="currentColor" /> 新建评测 <ArrowRight size={17} /></Link>
      </div>
      <div className="score-orbit" aria-label="平均严格总分">
        <div className="orbit-ring"><span>平均严格总分</span><strong>{summary.average_score == null ? "—" : summary.average_score.toFixed(0)}</strong><small>{summary.completed_run_count} 个完成 Run</small></div>
      </div>
    </section>
    <section className="metric-grid">
      <article><span><Boxes size={14} /> 已注册模型</span><strong>{summary.model_count}</strong><small>Mock / Compatible</small></article>
      <article><span><FlaskConical size={14} /> Benchmark</span><strong>{summary.benchmark_count}</strong><small>版本与 Hash 固化</small></article>
      <article><span><CheckCircle2 size={14} /> Run 总数</span><strong>{summary.run_count}</strong><small>成功 Run {summary.completed_run_count} · 失败 {summary.failed_run_count}</small></article>
      <article className="accent"><span><Sparkles size={14} /> 累计 Token</span><strong>{formatTokenTotal(summary.total_input_tokens, summary.total_output_tokens)}</strong><small>{formatCost(summary.total_estimated_cost)} 估算成本</small></article>
    </section>
    <section className="dashboard-grid">
      <article className="panel chart-panel">
        <div className="panel-heading"><div><span className="section-index">01</span><h2>最近得分</h2></div><small>严格总分 / 100</small></div>
        {chartData.length ? <div className="chart-wrap"><ResponsiveContainer width="100%" height={230}><BarChart data={chartData} margin={{ top: 12, right: 8, left: -22, bottom: 0 }}><CartesianGrid stroke="#e3e0d8" vertical={false} /><XAxis dataKey="name" tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#69706c" }} /><YAxis domain={[0, 100]} tickLine={false} axisLine={false} tick={{ fontSize: 10, fill: "#8a8f8b" }} /><Tooltip cursor={{ fill: "rgba(232,93,60,.06)" }} contentStyle={{ border: "1px solid #d9d6ce", borderRadius: 8, fontSize: 12 }} /><Bar dataKey="score" fill="#e85d3c" radius={[4,4,0,0]} maxBarSize={38} /></BarChart></ResponsiveContainer></div> : <div className="inline-empty">完成首个 Run 后显示得分趋势。</div>}
      </article>
      <article className="panel overview-panel">
        <div className="panel-heading"><div><span className="section-index">02</span><h2>性能概览</h2></div></div>
        <dl className="summary-list"><div><dt><Clock3 size={15} /> 平均延迟</dt><dd>{formatLatency(summary.average_latency_ms)}</dd></div><div><dt>输入 Token</dt><dd>{formatTokens(summary.total_input_tokens)}</dd></div><div><dt>输出 Token</dt><dd>{formatTokens(summary.total_output_tokens)}</dd></div><div><dt>估算成本</dt><dd>{formatCost(summary.total_estimated_cost)}</dd></div></dl>
      </article>
    </section>
    <section className="panel recent-panel">
      <div className="panel-heading"><div><span className="section-index">03</span><h2>最近运行</h2></div><Link to="/leaderboard" className="text-link">查看排行榜 <ArrowRight size={14} /></Link></div>
      {summary.recent_runs.length ? <div className="table-scroll"><table><thead><tr><th>模型 / Benchmark</th><th>状态</th><th>严格总分</th><th>完成率</th><th>延迟</th><th>完成时间</th></tr></thead><tbody>{summary.recent_runs.map((run) => <tr key={run.run_id}><td><Link className="row-title" to={`/runs/${run.run_id}`}>{run.model_name}</Link><small>{run.benchmark_name} · v{run.benchmark_version}{run.is_demo && <b className="demo-pill">Demo</b>}</small></td><td><StatusBadge status="completed" /></td><td className="score-cell">{formatPercent(run.score)}</td><td>{formatPercent(run.completion_rate)}</td><td>{formatLatency(run.average_latency_ms)}</td><td>{formatUtc8(run.finished_at)}</td></tr>)}</tbody></table></div> : <div className="inline-empty">暂无运行记录。注册 Mock 模型并载入 Demo 后开始评测。</div>}
    </section>
  </>;
}
