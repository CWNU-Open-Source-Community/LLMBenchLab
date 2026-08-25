import { type ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, FileArchive, Hash, Languages, RefreshCcw, ShieldCheck, Upload } from "lucide-react";

import { api, ApiError } from "../api/client";
import type { Benchmark } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { formatUtc8, shortHash } from "../lib/format";

export function BenchmarksPage() {
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [selected, setSelected] = useState<Benchmark | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const items = (await api.benchmarks()).items;
      setBenchmarks(items);
      setSelected((current) => current ? items.find((item) => item.id === current.id) || items[0] || null : items[0] || null);
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取 Benchmark。"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const reloadDemo = async () => {
    setBusy(true); setError(null);
    try { await api.reloadDemo(); await load(); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "Demo 载入失败。"); }
    finally { setBusy(false); }
  };
  const importArchive = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]; if (!file) return;
    setBusy(true); setError(null);
    try { const imported = await api.importBenchmark(file); await load(); setSelected(imported); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "导入失败。"); }
    finally { setBusy(false); event.target.value = ""; }
  };

  return <>
    <PageHeader eyebrow="VERSIONED DATASETS" title="Benchmark" description="每个评测集都固定 Schema、版本与内容 Hash；同版本内容变化会被拒绝。" actions={<><input ref={fileRef} className="sr-only" type="file" accept=".zip,application/zip" onChange={(event) => void importArchive(event)} /><button className="secondary-button" onClick={() => fileRef.current?.click()} disabled={busy}><Upload size={15} /> 导入 ZIP</button><button className="primary-button" onClick={() => void reloadDemo()} disabled={busy}><RefreshCcw className={busy ? "spin" : ""} size={15} /> 载入 Demo</button></>} />
    <div className="demo-banner"><ShieldCheck size={18} /><div><strong>Demo 数据，不代表正式模型能力</strong><span>内置题目只验证平台链路，不能与 MMLU、GPQA 等正式 Benchmark 等价比较。</span></div></div>
    {error && <ErrorState message={error} retry={() => void load()} />}
    {loading ? <LoadingState label="正在校验 Benchmark 清单" /> : benchmarks.length === 0 ? <EmptyState title="暂无 Benchmark" message="一键载入内置 Demo，或导入只包含 manifest.json 与 questions.jsonl 的 ZIP。" action={<button className="secondary-button" onClick={() => void reloadDemo()}><RefreshCcw size={15} /> 载入 Demo</button>} /> : <div className="benchmark-layout"><div className="benchmark-list">{benchmarks.map((benchmark) => <button key={benchmark.id} className={`benchmark-row ${selected?.id === benchmark.id ? "selected" : ""}`} onClick={() => setSelected(benchmark)}><span className="dataset-icon"><BookOpen size={18} /></span><span className="benchmark-main"><strong>{benchmark.name}{benchmark.is_demo && <b className="demo-pill">Demo</b>}</strong><small>{benchmark.dimension} · {benchmark.language} · {benchmark.question_count} 题</small></span><span className="version-pill">v{benchmark.version}</span></button>)}</div>{selected && <aside className="detail-panel"><div className="detail-kicker">DATASET DETAIL</div><h2>{selected.name}</h2><p>{selected.description}</p><dl className="detail-list"><div><dt><Hash size={14} /> Dataset Hash</dt><dd title={selected.dataset_hash}><code>{shortHash(selected.dataset_hash, 16)}</code></dd></div><div><dt><Languages size={14} /> 语言 / 维度</dt><dd>{selected.language} / {selected.dimension}</dd></div><div><dt><FileArchive size={14} /> Schema</dt><dd>{selected.schema_version}</dd></div><div><dt>Evaluator</dt><dd>{selected.evaluator_type}</dd></div><div><dt>License</dt><dd>{selected.license}</dd></div><div><dt>导入时间</dt><dd>{formatUtc8(selected.created_at)}</dd></div></dl><div className="prompt-preview"><span>Prompt Template</span><pre>{JSON.stringify(selected.prompt_template, null, 2)}</pre></div></aside>}</div>}
    <section className="format-guide panel"><div className="panel-heading"><div><span className="section-index">FORMAT</span><h2>支持的数据格式</h2></div></div><div className="format-columns"><div><strong>manifest.json</strong><p>定义 schema_version、版本、Evaluator 映射、Prompt 模板、许可证与题数。</p></div><div><strong>questions.jsonl</strong><p>每行一道题；支持 exact_match、multiple_choice 与 numeric（绝对/相对容差）。</p></div><div><strong>安全边界</strong><p>仅接受固定根文件，限制体积并拒绝路径穿越、压缩炸弹和任意代码执行。</p></div></div></section>
  </>;
}
