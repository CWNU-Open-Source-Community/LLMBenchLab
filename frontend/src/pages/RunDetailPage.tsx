import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, Ban, CheckCircle2, ChevronDown, Clock3, Copy, Cpu, Hash } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { EvaluationResponse, EvaluationRun, GovernanceRunStatus } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { displayAnswer, formatCost, formatLatency, formatPercent, formatTokens, formatTokenTotal, formatUtc8, shortHash } from "../lib/format";

const RESPONSE_PAGE_SIZE = 100;

function snapshotLabel(run: EvaluationRun, group: "model" | "benchmark", key: string): string {
  const value = run.model_parameters_snapshot[group];
  if (typeof value !== "object" || value === null) return "—";
  const selected = (value as Record<string, unknown>)[key];
  return typeof selected === "string" ? selected : "—";
}

type GovernanceNotice = {
  tone: GovernanceRunStatus | "unknown";
  title: string;
  detail: string;
};

const governanceScopeLabels = {
  global: "全局",
  provider: "Provider",
  model: "模型",
  run: "Run",
} as const;

const governanceLimitLabels = {
  concurrency: "并发额度暂时占满",
  rpm: "每分钟请求额度暂时占满",
  tpm: "每分钟 Token 额度暂时占满",
  overdrawn: "已发生保守结算超额",
  request_budget_exhausted: "累计请求硬预算已耗尽",
  token_budget_exhausted: "累计 Token 硬预算已耗尽",
  cost_budget_exhausted: "累计费用硬预算已耗尽",
} as const;

function governanceReasonLabel(reason: string | null): string {
  if (!reason) return "治理服务未提供可公开原因";
  const scoped = /^governance_(global|provider|model|run)_(concurrency|rpm|tpm|overdrawn|request_budget_exhausted|token_budget_exhausted|cost_budget_exhausted)$/.exec(reason);
  if (scoped) {
    const scope = governanceScopeLabels[scoped[1] as keyof typeof governanceScopeLabels];
    const limit = governanceLimitLabels[scoped[2] as keyof typeof governanceLimitLabels];
    return `${scope}${limit}`;
  }
  const labels: Record<string, string> = {
    governance_input_bound_unknown: "缺少显式输入 Token 上界",
    governance_unbounded_output: "缺少有限输出 Token 上界",
    governance_pricing_unknown: "缺少冻结的输入或输出价格",
    governance_provider_retry_exhausted: "Provider HTTP 重试次数已耗尽",
    governance_integrity_error: "治理事实未通过完整性校验",
  };
  return labels[reason] ?? "治理服务返回了未公开原因；请查阅受控审计事件";
}

function formatDatabaseUtc(value: string | null): string {
  if (!value) return "数据库尚未提供最早重新调度时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "数据库返回的重新调度时间无效";
  return `数据库最早重新调度时间：${date.toISOString().replace("T", " ").replace("Z", " UTC")}`;
}

function governanceNotice(run: EvaluationRun): GovernanceNotice {
  switch (run.governance_status) {
    case "managed":
      return {
        tone: "managed",
        title: "数据库治理已启用",
        detail: "此 Run 按创建时冻结的并发、速率与累计预算策略执行。",
      };
    case "delayed":
      return {
        tone: "delayed",
        title: "治理背压中，Run 已暂缓（deferred）",
        detail: `${governanceReasonLabel(run.governance_reason)}。${formatDatabaseUtc(run.governance_not_before)}；到达该 UTC 时间不保证立即取得 Worker。`,
      };
    case "exhausted":
      return {
        tone: "exhausted",
        title: "治理硬边界已终止 Run",
        detail: `${governanceReasonLabel(run.governance_reason)}。此终态不会自动重试，请核对冻结策略后再创建新 Run。`,
      };
    case "legacy_unmanaged":
      return {
        tone: "legacy_unmanaged",
        title: "此 Run 未纳入数据库治理",
        detail: "legacy_unmanaged 仅兼容旧 Run 或可信本地 CLI；当前 Web/API policy 不保证其并发、RPM/TPM 或累计费用硬边界。",
      };
    default:
      return {
        tone: "unknown",
        title: "治理状态不可识别",
        detail: "页面不会展示未经验证的状态或原因；请以受控审计与数据库事实为准。",
      };
  }
}

function runStatusHeading(run: EvaluationRun): string {
  if (run.governance_status === "delayed") return "治理背压中，等待重新调度";
  if (run.governance_status === "exhausted") return "治理硬边界已终止运行";
  if (run.status === "running") return "评测正在逐题执行";
  if (run.status === "completed") return "评测证据已完整保存";
  if (run.status === "pending") return "等待后台任务领取";
  return "运行已进入终态";
}

export function RunDetailPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [responses, setResponses] = useState<EvaluationResponse[]>([]);
  const [responseOffset, setResponseOffset] = useState(0);
  const [responseTotal, setResponseTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const requestSequence = useRef(0);
  const requestsInFlight = useRef(0);
  const terminal = run ? ["completed", "failed", "cancelled"].includes(run.status) : false;
  const load = useCallback(async (quiet = false) => {
    // Do not let a timer tick supersede a slower page navigation request. A
    // user-triggered load may still supersede an older poll.
    if (quiet && requestsInFlight.current > 0) return;
    const requestId = ++requestSequence.current;
    requestsInFlight.current += 1;
    if (!quiet) setLoading(true); setError(null);
    try {
      const [current, evidence] = await Promise.all([
        api.run(runId),
        api.responses(runId, { offset: responseOffset, limit: RESPONSE_PAGE_SIZE }),
      ]);
      if (requestId !== requestSequence.current) return;
      setRun(current);
      setResponses(evidence.items);
      setResponseTotal(evidence.total);
    } catch (reason) {
      if (requestId === requestSequence.current) {
        setError(reason instanceof ApiError ? reason.message : "无法读取 Run。");
      }
    } finally {
      requestsInFlight.current -= 1;
      if (!quiet && requestId === requestSequence.current) setLoading(false);
    }
  }, [responseOffset, runId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => { requestSequence.current += 1; }, []);
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
  const responseStart = responseTotal ? responseOffset + 1 : 0;
  const responseEnd = Math.min(responseOffset + responses.length, responseTotal);
  const governance = run ? governanceNotice(run) : null;

  if (loading) return <LoadingState label="正在读取运行证据" />;
  if (error && !run) return <ErrorState message={error} retry={() => void load()} />;
  if (!run) return <EmptyState title="Run 不存在" message="该运行可能已被移除，或链接不完整。" action={<Link className="secondary-button" to="/runs">返回评测记录</Link>} />;
  return <>
    <PageHeader eyebrow="EVALUATION EVIDENCE" title={snapshotLabel(run, "model", "name")} description={`${snapshotLabel(run, "benchmark", "slug")} · ${run.protocol_version}`} actions={<><Link className="secondary-button" to="/runs"><ArrowLeft size={14} /> 评测记录</Link><button className="secondary-button" onClick={copyId}><Copy size={14} /> 复制 Run ID</button>{!terminal && <button className="danger-button" disabled={cancelling} onClick={() => void cancel()}><Ban size={14} /> {cancelling ? "取消中…" : "取消 Run"}</button>}</>} />
    {error && <ErrorState message={error} retry={() => void load()} />}
    <section className="run-status-panel panel">
      <div className="run-status-head"><div><StatusBadge status={run.status} /><h2>{runStatusHeading(run)}</h2><p>创建于 {formatUtc8(run.created_at)}</p></div><div className="score-block"><span>严格总分</span><strong>{run.score == null ? "—" : run.score.toFixed(1)}</strong><small>/ 100</small></div></div>
      <div className="progress-row"><div className="progress-track"><i style={{ width: `${progress}%` }} /></div><span>{run.completed_questions} / {run.total_questions} 题 · {progress.toFixed(0)}%</span></div>
      {governance && <div className={`governance-notice governance-${governance.tone}`} aria-live="polite">{governance.tone === "managed" ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}<span><strong>{governance.title}</strong><small>{governance.detail}</small></span></div>}
      {run.error_message && <div className="run-error"><AlertTriangle size={15} />{run.error_message}</div>}
    </section>
    <section className="run-metrics"><article><span><CheckCircle2 size={14} /> 回答准确率</span><strong>{formatPercent(run.answered_accuracy)}</strong><small>仅可评答案</small></article><article><span>完成率</span><strong>{formatPercent(run.completion_rate)}</strong><small>成功非空响应</small></article><article><span><Clock3 size={14} /> 平均延迟</span><strong>{formatLatency(run.average_latency_ms)}</strong><small>已报告题目</small></article><article><span><Cpu size={14} /> Token</span><strong>{formatTokenTotal(run.input_tokens, run.output_tokens)}</strong><small>输入 {formatTokens(run.input_tokens)} / 输出 {formatTokens(run.output_tokens)}</small></article><article><span>估算成本</span><strong>{formatCost(run.estimated_cost)}</strong><small>按快照价格；缺失 usage 时未知</small></article><article><span>错误题</span><strong>{run.error_questions}</strong><small>正确 {run.correct_questions}</small></article></section>
    <section className="panel evidence-panel">
      <div className="panel-heading"><div><span className="section-index">EVIDENCE</span><h2>逐题结果</h2></div><span className="evidence-count">显示 {responseStart}–{responseEnd} / 共 {responseTotal} 条 · 本页 {errors.length} 条错误</span></div>
      {responses.length ? <div className="evidence-list">{responses.map((response, index) => <details key={response.id} className={response.error_type ? "response-item response-error" : "response-item"}><summary><span className="question-index">{String(responseOffset + index + 1).padStart(2, "0")}</span><span className="question-summary"><strong>{response.question_external_id}</strong><small>{response.question_type} · {response.evaluator_name}</small></span><span className={`point ${response.score === 1 ? "correct" : "wrong"}`}>{response.score === 1 ? "1 / 1" : "0 / 1"}</span><ChevronDown size={17} /></summary><div className="response-body"><div className="prompt-box"><span>题目</span><p>{response.prompt}</p>{response.choices && <ul>{Object.entries(response.choices).map(([key, value]) => <li key={key}><b>{key}</b>{value}</li>)}</ul>}</div><div className="answer-grid"><div><span>原始回答</span><pre>{response.raw_response || "—"}</pre></div><div><span>解析答案</span><strong>{displayAnswer(response.parsed_answer)}</strong></div><div><span>标准答案</span><strong>{displayAnswer(response.reference_answer_snapshot)}</strong></div></div>{response.error_type && <div className="response-error-message"><AlertTriangle size={15} /><span><strong>{response.error_type}</strong>{response.error_message}</span></div>}<footer><span>{formatLatency(response.latency_ms)}</span><span>{formatTokens(response.input_tokens)} in / {formatTokens(response.output_tokens)} out</span><span>{formatCost(response.estimated_cost)}</span></footer></div></details>)}</div> : <div className="inline-empty">{terminal ? "该 Run 没有逐题结果。" : "等待第一道题完成，页面会每秒自动更新。"}</div>}
      {responseTotal > RESPONSE_PAGE_SIZE && <nav className="evidence-pagination" aria-label="逐题结果分页"><button className="secondary-button" type="button" disabled={responseOffset === 0} onClick={() => setResponseOffset((current) => Math.max(0, current - RESPONSE_PAGE_SIZE))}>上一页</button><span>{responseStart}–{responseEnd} / {responseTotal}</span><button className="secondary-button" type="button" disabled={responseOffset + RESPONSE_PAGE_SIZE >= responseTotal} onClick={() => setResponseOffset((current) => current + RESPONSE_PAGE_SIZE)}>下一页</button></nav>}
    </section>
    <section className="snapshot-layout"><article className="panel"><div className="panel-heading"><div><span className="section-index">CONFIG</span><h2>评测配置快照</h2></div></div><pre className="json-view">{JSON.stringify(run.model_parameters_snapshot, null, 2)}</pre></article><article className="panel immutable-list"><div className="panel-heading"><div><span className="section-index">IDENTITY</span><h2>复现标识</h2></div></div><dl><div><dt><Hash size={14} /> Dataset SHA-256</dt><dd title={run.benchmark_hash_snapshot}>{shortHash(run.benchmark_hash_snapshot, 20)}</dd></div><div><dt>Git commit</dt><dd>{shortHash(run.code_commit_sha, 12)}</dd></div><div><dt>开始时间</dt><dd>{formatUtc8(run.started_at)}</dd></div><div><dt>结束时间</dt><dd>{formatUtc8(run.finished_at)}</dd></div></dl></article></section>
  </>;
}
