import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Ban, CheckCircle2, ChevronDown, Clock3, Copy, Cpu, Hash } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { EvaluationResponse, EvaluationRun } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { displayAnswer, formatCost, formatLatency, formatPercent, formatTokens, formatTokenTotal, formatUtc8, shortHash } from "../lib/format";

function snapshotLabel(run: EvaluationRun, group: "model" | "benchmark", key: string): string {
  const value = run.model_parameters_snapshot[group];
  if (typeof value !== "object" || value === null) return "—";
  const selected = (value as Record<string, unknown>)[key];
  return typeof selected === "string" ? selected : "—";
}

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [responses, setResponses] = useState<EvaluationResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const terminal = run ? ["completed", "failed", "cancelled"].includes(run.status) : false;
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true); setError(null);
    try {
      const current = await api.run(runId); setRun(current);
      const evidence = await api.responses(runId); setResponses(evidence.items);
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取 Run。"); }
    finally { if (!quiet) setLoading(false); }
  }, [runId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!run || terminal) return;
    const timer = window.setInterval(() => void load(true), 1000);
    return () => window.clearInterval(timer);
  }, [load, run, terminal]);
  const progress = run?.total_questions ? Math.min(100, run.completed_questions / run.total_questions * 100) : 0;
  const copyId = () => void navigator.clipboard?.writeText(runId);
  const cancel = async () => {
    setCancelling(true);
    try { setRun(await api.cancelRun(runId)); await load(true); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "取消失败。"); }
    finally { setCancelling(false); }
  };
  const errors = useMemo(() => responses.filter((item) => item.error_type), [responses]);

  if (loading) return <LoadingState label="正在读取运行证据" />;
  if (error && !run) return <ErrorState message={error} retry={() => void load()} />;
  if (!run) return <EmptyState title="Run 不存在" message="该运行可能已被移除，或链接不完整。" action={<Link className="secondary-button" to="/">返回概览</Link>} />;
  return <>
    <PageHeader eyebrow="EVALUATION EVIDENCE" title={snapshotLabel(run, "model", "name")} description={`${snapshotLabel(run, "benchmark", "slug")} · ${run.protocol_version}`} actions={<><button className="secondary-button" onClick={copyId}><Copy size={14} /> 复制 Run ID</button>{!terminal && <button className="danger-button" disabled={cancelling} onClick={() => void cancel()}><Ban size={14} /> {cancelling ? "取消中…" : "取消 Run"}</button>}</>} />
    {error && <ErrorState message={error} retry={() => void load()} />}
    <section className="run-status-panel panel"><div className="run-status-head"><div><StatusBadge status={run.status} /><h2>{run.status === "running" ? "评测正在逐题执行" : run.status === "completed" ? "评测证据已完整保存" : run.status === "pending" ? "等待后台任务领取" : "运行已进入终态"}</h2><p>创建于 {formatUtc8(run.created_at)}</p></div><div className="score-block"><span>严格总分</span><strong>{run.score == null ? "—" : run.score.toFixed(1)}</strong><small>/ 100</small></div></div><div className="progress-row"><div className="progress-track"><i style={{ width: `${progress}%` }} /></div><span>{run.completed_questions} / {run.total_questions} 题 · {progress.toFixed(0)}%</span></div>{run.error_message && <div className="run-error"><AlertTriangle size={15} />{run.error_message}</div>}</section>
    <section className="run-metrics"><article><span><CheckCircle2 size={14} /> 回答准确率</span><strong>{formatPercent(run.answered_accuracy)}</strong><small>仅可评答案</small></article><article><span>完成率</span><strong>{formatPercent(run.completion_rate)}</strong><small>成功非空响应</small></article><article><span><Clock3 size={14} /> 平均延迟</span><strong>{formatLatency(run.average_latency_ms)}</strong><small>已报告题目</small></article><article><span><Cpu size={14} /> Token</span><strong>{formatTokenTotal(run.input_tokens, run.output_tokens)}</strong><small>输入 {formatTokens(run.input_tokens)} / 输出 {formatTokens(run.output_tokens)}</small></article><article><span>估算成本</span><strong>{formatCost(run.estimated_cost)}</strong><small>按快照价格；缺失 usage 时未知</small></article><article><span>错误题</span><strong>{run.error_questions}</strong><small>正确 {run.correct_questions}</small></article></section>
    <section className="panel evidence-panel"><div className="panel-heading"><div><span className="section-index">EVIDENCE</span><h2>逐题结果</h2></div><span className="evidence-count">{responses.length} 条已保存 · {errors.length} 条错误</span></div>{responses.length ? <div className="evidence-list">{responses.map((response, index) => <details key={response.id} className={response.error_type ? "response-item response-error" : "response-item"}><summary><span className="question-index">{String(index + 1).padStart(2, "0")}</span><span className="question-summary"><strong>{response.question_external_id}</strong><small>{response.question_type} · {response.evaluator_name}</small></span><span className={`point ${response.score === 1 ? "correct" : "wrong"}`}>{response.score === 1 ? "1 / 1" : "0 / 1"}</span><ChevronDown size={17} /></summary><div className="response-body"><div className="prompt-box"><span>题目</span><p>{response.prompt}</p>{response.choices && <ul>{Object.entries(response.choices).map(([key, value]) => <li key={key}><b>{key}</b>{value}</li>)}</ul>}</div><div className="answer-grid"><div><span>原始回答</span><pre>{response.raw_response || "—"}</pre></div><div><span>解析答案</span><strong>{displayAnswer(response.parsed_answer)}</strong></div><div><span>标准答案</span><strong>{displayAnswer(response.reference_answer_snapshot)}</strong></div></div>{response.error_type && <div className="response-error-message"><AlertTriangle size={15} /><span><strong>{response.error_type}</strong>{response.error_message}</span></div>}<footer><span>{formatLatency(response.latency_ms)}</span><span>{formatTokens(response.input_tokens)} in / {formatTokens(response.output_tokens)} out</span><span>{formatCost(response.estimated_cost)}</span></footer></div></details>)}</div> : <div className="inline-empty">{terminal ? "该 Run 没有逐题结果。" : "等待第一道题完成，页面会每秒自动更新。"}</div>}</section>
    <section className="snapshot-layout"><article className="panel"><div className="panel-heading"><div><span className="section-index">CONFIG</span><h2>评测配置快照</h2></div></div><pre className="json-view">{JSON.stringify(run.model_parameters_snapshot, null, 2)}</pre></article><article className="panel immutable-list"><div className="panel-heading"><div><span className="section-index">IDENTITY</span><h2>复现标识</h2></div></div><dl><div><dt><Hash size={14} /> Dataset SHA-256</dt><dd title={run.benchmark_hash_snapshot}>{shortHash(run.benchmark_hash_snapshot, 20)}</dd></div><div><dt>Git commit</dt><dd>{shortHash(run.code_commit_sha, 12)}</dd></div><div><dt>开始时间</dt><dd>{formatUtc8(run.started_at)}</dd></div><div><dt>结束时间</dt><dd>{formatUtc8(run.finished_at)}</dd></div></dl></article></section>
  </>;
}
