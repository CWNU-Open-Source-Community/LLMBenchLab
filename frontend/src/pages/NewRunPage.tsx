import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowRight, Beaker, SlidersHorizontal } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { Benchmark, ModelConfig, RunPayload } from "../api/types";
import { ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { shortHash } from "../lib/format";

export function NewRunPage() {
  const navigate = useNavigate();
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [payload, setPayload] = useState<RunPayload>({ model_id: "", benchmark_id: "", temperature: 0, top_p: 1, max_tokens: 256, seed: 42, concurrency: 1 });
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [modelData, benchmarkData] = await Promise.all([api.models({ enabled: true }), api.benchmarks()]);
      setModels(modelData.items); setBenchmarks(benchmarkData.items);
      setPayload((current) => ({ ...current, model_id: current.model_id || modelData.items[0]?.id || "", benchmark_id: current.benchmark_id || benchmarkData.items[0]?.id || "" }));
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取评测选项。"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const selectedModel = useMemo(() => models.find((model) => model.id === payload.model_id), [models, payload.model_id]);
  const selectedBenchmark = useMemo(() => benchmarks.find((benchmark) => benchmark.id === payload.benchmark_id), [benchmarks, payload.benchmark_id]);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setError(null);
    try { const run = await api.createRun(payload); navigate(`/runs/${run.id}`); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "创建 Run 失败。"); }
    finally { setSaving(false); }
  };

  if (loading) return <LoadingState label="正在准备评测协议" />;
  return <>
    <PageHeader eyebrow="NEW EVALUATION" title="新建评测" description="创建时冻结模型、数据集、Prompt、生成参数和协议快照；请求会立即返回 Run ID。" />
    {error && <ErrorState message={error} retry={() => void load()} />}
    {(!models.length || !benchmarks.length) ? <div className="prerequisite-card"><AlertCircle size={24} /><h2>还差一步才能开始</h2><p>{!models.length ? "至少需要一个已启用模型。" : "至少需要一个 Benchmark。"}</p><div>{!models.length && <Link className="primary-button" to="/models">添加模型 <ArrowRight size={15} /></Link>}{!benchmarks.length && <Link className="secondary-button" to="/benchmarks">载入 Demo <ArrowRight size={15} /></Link>}</div></div> : <form className="run-form" onSubmit={(event) => void submit(event)}><section className="panel form-section"><div className="form-section-title"><span>01</span><div><h2>选择评测对象</h2><p>禁用模型不会出现在列表中。</p></div></div><div className="form-grid"><label>模型<select value={payload.model_id} onChange={(event) => setPayload({ ...payload, model_id: event.target.value })}>{models.map((model) => <option value={model.id} key={model.id}>{model.name} · {model.provider_type === "mock" ? "Mock" : model.remote_model_name}</option>)}</select>{selectedModel && <small>{selectedModel.provider_type === "mock" ? "完全离线，不调用外部网络" : selectedModel.base_url}</small>}</label><label>Benchmark<select value={payload.benchmark_id} onChange={(event) => setPayload({ ...payload, benchmark_id: event.target.value })}>{benchmarks.map((benchmark) => <option value={benchmark.id} key={benchmark.id}>{benchmark.name} · v{benchmark.version}</option>)}</select>{selectedBenchmark && <small>{selectedBenchmark.question_count} 题 · Hash {shortHash(selectedBenchmark.dataset_hash)}</small>}</label></div>{selectedBenchmark?.is_demo && <div className="inline-warning"><Beaker size={16} /><span>这是 Demo 数据，只用于验证链路，不代表正式模型能力。</span></div>}</section><section className="panel form-section"><div className="form-section-title"><span>02</span><div><h2>固定生成参数</h2><p>公平默认值：temperature 0、top_p 1、固定 seed。</p></div></div><div className="form-grid parameter-grid"><label>Temperature<input type="number" min="0" max="2" step="0.1" value={payload.temperature} onChange={(event) => setPayload({ ...payload, temperature: Number(event.target.value) })} /></label><label>Top-p<input type="number" min="0.01" max="1" step="0.01" value={payload.top_p} onChange={(event) => setPayload({ ...payload, top_p: Number(event.target.value) })} /></label><label>Max tokens<input type="number" min="1" max="32768" value={payload.max_tokens} onChange={(event) => setPayload({ ...payload, max_tokens: Number(event.target.value) })} /></label><label>Seed<input type="number" value={payload.seed ?? ""} onChange={(event) => setPayload({ ...payload, seed: event.target.value === "" ? null : Number(event.target.value) })} /></label><label>并发度<select value={payload.concurrency} onChange={(event) => setPayload({ ...payload, concurrency: Number(event.target.value) })}><option value="1">1（推荐）</option><option value="2">2</option><option value="4">4</option></select></label><label className="span-2">System prompt（可选覆盖）<textarea rows={3} value={payload.system_prompt || ""} onChange={(event) => setPayload({ ...payload, system_prompt: event.target.value || null })} placeholder="留空则使用 Benchmark 的固定模板" /></label></div></section><footer className="run-submit"><div><SlidersHorizontal size={17} /><span>协议：<strong>llmbenchlab-protocol-v1</strong><small>不同协议结果不会无提示混合。</small></span></div><button className="primary-button" disabled={saving}>{saving ? "正在创建…" : "创建并开始评测"}<ArrowRight size={16} /></button></footer></form>}
  </>;
}
