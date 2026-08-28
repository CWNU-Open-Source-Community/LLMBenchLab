import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, ArrowRight, Beaker, Clock3, Coins, SlidersHorizontal } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { Benchmark, ModelConfig, RunPayload } from "../api/types";
import { ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { shortHash } from "../lib/format";

const MAX_GENERATION_TOKENS = 131_072;
const MAX_READ_TIMEOUT_SECONDS = 1_800;

type Recommendation = {
  maxTokens: number;
  readTimeoutSeconds: number;
  reason: string;
};

type TouchedFields = {
  temperature: boolean;
  topP: boolean;
  maxTokens: boolean;
  seed: boolean;
  readTimeout: boolean;
};

const untouchedFields: TouchedFields = {
  temperature: false,
  topP: false,
  maxTokens: false,
  seed: false,
  readTimeout: false,
};

function recommendationFor(benchmark?: Benchmark): Recommendation {
  if (!benchmark || benchmark.is_demo) {
    return { maxTokens: 256, readTimeoutSeconds: 60, reason: "Demo 只验证评测链路" };
  }
  if (benchmark.slug === "gpqa-diamond") {
    return { maxTokens: 8_192, readTimeoutSeconds: 600, reason: "GPQA 通常需要较长的多步推理" };
  }
  if (benchmark.slug === "mmlu-pro-official-cot") {
    return { maxTokens: 4_000, readTimeoutSeconds: 300, reason: "官方 CoT 模板包含示例并要求完整推理" };
  }
  if (benchmark.slug === "mmlu-pro-direct") {
    return { maxTokens: 1_024, readTimeoutSeconds: 180, reason: "Direct 模板只要求最终选项" };
  }
  return { maxTokens: 4_096, readTimeoutSeconds: 300, reason: "正式数据集采用保守的长回答预算" };
}

function finiteDefault(model: ModelConfig | undefined, key: string, fallback: number): number {
  const value = model?.default_parameters[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function maxTokensDefault(model: ModelConfig | undefined, fallback: number): number | null {
  if (model && Object.prototype.hasOwnProperty.call(model.default_parameters, "max_tokens")) {
    const value = model.default_parameters.max_tokens;
    if (value === null) return null;
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return fallback;
}

function seedDefault(model: ModelConfig | undefined, fallback: number): number | null {
  if (model && Object.prototype.hasOwnProperty.call(model.default_parameters, "seed")) {
    const value = model.default_parameters.seed;
    if (value === null) return null;
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return fallback;
}

function payloadFor(model: ModelConfig | undefined, benchmark: Benchmark | undefined): RunPayload {
  const recommendation = recommendationFor(benchmark);
  return {
    model_id: model?.id || "",
    benchmark_id: benchmark?.id || "",
    temperature: finiteDefault(model, "temperature", 0),
    top_p: finiteDefault(model, "top_p", 1),
    max_tokens: maxTokensDefault(model, recommendation.maxTokens),
    seed: seedDefault(model, 42),
    concurrency: 1,
    read_timeout_seconds: recommendation.readTimeoutSeconds,
  };
}

export function NewRunPage() {
  const navigate = useNavigate();
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [touched, setTouched] = useState<TouchedFields>(untouchedFields);
  const [payload, setPayload] = useState<RunPayload>(() => payloadFor(undefined, undefined));

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [modelData, benchmarkData] = await Promise.all([api.models({ enabled: true }), api.benchmarks()]);
      setModels(modelData.items);
      setBenchmarks(benchmarkData.items);
      setPayload((current) => {
        if (current.model_id || current.benchmark_id) return current;
        return payloadFor(modelData.items[0], benchmarkData.items[0]);
      });
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "无法读取评测选项。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const selectedModel = useMemo(
    () => models.find((model) => model.id === payload.model_id),
    [models, payload.model_id],
  );
  const selectedBenchmark = useMemo(
    () => benchmarks.find((benchmark) => benchmark.id === payload.benchmark_id),
    [benchmarks, payload.benchmark_id],
  );
  const recommendation = useMemo(() => recommendationFor(selectedBenchmark), [selectedBenchmark]);
  const belowRecommendation = payload.max_tokens !== null && payload.max_tokens < recommendation.maxTokens;

  const chooseModel = (modelId: string) => {
    const model = models.find((item) => item.id === modelId);
    setPayload((current) => ({
      ...current,
      model_id: modelId,
      temperature: touched.temperature ? current.temperature : finiteDefault(model, "temperature", 0),
      top_p: touched.topP ? current.top_p : finiteDefault(model, "top_p", 1),
      max_tokens: touched.maxTokens ? current.max_tokens : maxTokensDefault(model, recommendation.maxTokens),
      seed: touched.seed ? current.seed : seedDefault(model, 42),
    }));
  };

  const chooseBenchmark = (benchmarkId: string) => {
    const benchmark = benchmarks.find((item) => item.id === benchmarkId);
    const nextRecommendation = recommendationFor(benchmark);
    setPayload((current) => ({
      ...current,
      benchmark_id: benchmarkId,
      max_tokens: touched.maxTokens
        ? current.max_tokens
        : maxTokensDefault(selectedModel, nextRecommendation.maxTokens),
      read_timeout_seconds: touched.readTimeout
        ? current.read_timeout_seconds
        : nextRecommendation.readTimeoutSeconds,
    }));
  };

  const applyRecommendation = () => {
    setPayload((current) => ({
      ...current,
      max_tokens: recommendation.maxTokens,
      read_timeout_seconds: recommendation.readTimeoutSeconds,
    }));
    setTouched((current) => ({ ...current, maxTokens: false, readTimeout: false }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const run = await api.createRun(payload);
      navigate(`/runs/${run.id}`);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "创建 Run 失败。");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <LoadingState label="正在准备评测协议" />;

  return <>
    <PageHeader
      eyebrow="NEW EVALUATION"
      title="新建评测"
      description="创建时冻结模型、数据集、Prompt、生成参数和超时设置；请求会立即返回 Run ID。"
    />
    {error && <ErrorState message={error} retry={() => void load()} />}
    {!models.length || !benchmarks.length ? (
      <div className="prerequisite-card">
        <AlertCircle size={24} />
        <h2>还差一步才能开始</h2>
        <p>{!models.length ? "至少需要一个已启用模型。" : "至少需要一个 Benchmark。"}</p>
        <div>
          {!models.length && <Link className="primary-button" to="/models">添加模型 <ArrowRight size={15} /></Link>}
          {!benchmarks.length && <Link className="secondary-button" to="/benchmarks">载入 Demo <ArrowRight size={15} /></Link>}
        </div>
      </div>
    ) : (
      <form className="run-form" onSubmit={(event) => void submit(event)}>
        <section className="panel form-section">
          <div className="form-section-title">
            <span>01</span>
            <div><h2>选择评测对象</h2><p>禁用模型不会出现在列表中。</p></div>
          </div>
          <div className="form-grid">
            <label>
              模型
              <select aria-label="模型" value={payload.model_id} onChange={(event) => chooseModel(event.target.value)}>
                {models.map((model) => <option value={model.id} key={model.id}>{model.name} · {model.provider_type === "mock" ? "Mock" : model.remote_model_name}</option>)}
              </select>
              {selectedModel && <small>{selectedModel.provider_type === "mock" ? "完全离线，不调用外部网络" : selectedModel.base_url}</small>}
            </label>
            <label>
              Benchmark
              <select aria-label="Benchmark" value={payload.benchmark_id} onChange={(event) => chooseBenchmark(event.target.value)}>
                {benchmarks.map((benchmark) => <option value={benchmark.id} key={benchmark.id}>{benchmark.name} · v{benchmark.version}</option>)}
              </select>
              {selectedBenchmark && <small>{selectedBenchmark.question_count} 题 · Hash {shortHash(selectedBenchmark.dataset_hash)}</small>}
            </label>
          </div>
          {selectedBenchmark?.is_demo && <div className="inline-warning"><Beaker size={16} /><span>这是 Demo 数据，只用于验证链路，不代表正式模型能力。</span></div>}
        </section>

        <section className="panel form-section">
          <div className="form-section-title">
            <span>02</span>
            <div><h2>固定生成参数</h2><p>参数会写入不可变快照；正式数据集应为长推理预留足够预算和时间。</p></div>
          </div>

          <div className={`generation-guidance${belowRecommendation ? " guidance-warning" : ""}`}>
            <Clock3 size={17} />
            <div>
              <strong>当前 Benchmark 建议：{recommendation.maxTokens.toLocaleString()} tokens · {recommendation.readTimeoutSeconds} 秒</strong>
              <span>{recommendation.reason}。这是可调整的起点，不代表 Provider 一定支持该上下文长度。</span>
              {belowRecommendation && <b>当前输出预算低于建议值，长推理可能被截断并导致答案为空或无法解析。</b>}
            </div>
            <button className="secondary-button compact-button" type="button" onClick={applyRecommendation}>应用建议</button>
          </div>

          <div className="form-grid parameter-grid">
            <label>
              Temperature
              <input aria-label="Temperature" type="number" min="0" max="2" step="0.1" value={payload.temperature} onChange={(event) => {
                setTouched((current) => ({ ...current, temperature: true }));
                setPayload((current) => ({ ...current, temperature: Number(event.target.value) }));
              }} />
            </label>
            <label>
              Top-p
              <input aria-label="Top-p" type="number" min="0.01" max="1" step="0.01" value={payload.top_p} onChange={(event) => {
                setTouched((current) => ({ ...current, topP: true }));
                setPayload((current) => ({ ...current, top_p: Number(event.target.value) }));
              }} />
            </label>
            <div className="field-group">
              <label htmlFor="max-tokens">Max tokens</label>
              <input
                id="max-tokens"
                aria-label="Max tokens"
                type="number"
                min="1"
                max={MAX_GENERATION_TOKENS}
                required={payload.max_tokens !== null}
                disabled={payload.max_tokens === null}
                value={payload.max_tokens ?? ""}
                onChange={(event) => {
                  setTouched((current) => ({ ...current, maxTokens: true }));
                  setPayload((current) => ({ ...current, max_tokens: Number(event.target.value) }));
                }}
              />
              <label className="inline-checkbox">
                <input
                  aria-label="由 Provider 决定 max tokens"
                  type="checkbox"
                  checked={payload.max_tokens === null}
                  onChange={(event) => {
                    setTouched((current) => ({ ...current, maxTokens: true }));
                    setPayload((current) => ({ ...current, max_tokens: event.target.checked ? null : recommendation.maxTokens }));
                  }}
                />
                由 Provider 决定
              </label>
              <small>数字上限 {MAX_GENERATION_TOKENS.toLocaleString()}；勾选后不发送 max_tokens，并不等于无限输出。</small>
            </div>
            <label>
              Seed
              <input aria-label="Seed" type="number" value={payload.seed ?? ""} onChange={(event) => {
                setTouched((current) => ({ ...current, seed: true }));
                setPayload((current) => ({ ...current, seed: event.target.value === "" ? null : Number(event.target.value) }));
              }} />
            </label>
            <label>
              并发度
              <select aria-label="并发度" value={payload.concurrency} onChange={(event) => setPayload((current) => ({ ...current, concurrency: Number(event.target.value) }))}>
                <option value="1">1（推荐）</option><option value="2">2</option><option value="4">4</option>
              </select>
            </label>
            <label>
              单次读取超时（秒）
              <input
                aria-label="单次读取超时（秒）"
                type="number"
                min="1"
                max={MAX_READ_TIMEOUT_SECONDS}
                required
                value={payload.read_timeout_seconds}
                onChange={(event) => {
                  setTouched((current) => ({ ...current, readTimeout: true }));
                  setPayload((current) => ({ ...current, read_timeout_seconds: Number(event.target.value) }));
                }}
              />
              <small>允许 1–{MAX_READ_TIMEOUT_SECONDS} 秒；慢推理模型只提高 token 仍可能在这里超时。</small>
            </label>
            <label className="span-all">
              System prompt（可选覆盖）
              <textarea rows={3} maxLength={4000} value={payload.system_prompt || ""} onChange={(event) => setPayload((current) => ({ ...current, system_prompt: event.target.value || null }))} placeholder="留空则使用 Benchmark 的固定模板" />
            </label>
          </div>

          {selectedModel?.provider_type !== "mock" && <div className="cost-warning"><Coins size={16} /><span>此模型会逐题调用外部 Provider。更高输出预算、超时和并发度都可能增加费用或触发限流；请先用小样本确认配置。</span></div>}
        </section>

        <footer className="run-submit">
          <div><SlidersHorizontal size={17} /><span>协议：<strong>llmbenchlab-protocol-v1</strong><small>不同协议结果不会无提示混合。</small></span></div>
          <button className="primary-button" disabled={saving}>{saving ? "正在创建…" : "创建并开始评测"}<ArrowRight size={16} /></button>
        </footer>
      </form>
    )}
  </>;
}
