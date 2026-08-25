import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Check, KeyRound, Pencil, Plus, Server, Trash2, X } from "lucide-react";

import { api, ApiError } from "../api/client";
import type { ModelConfig, ModelPayload, ProviderType } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { formatUtc8 } from "../lib/format";

const emptyPayload: ModelPayload = {
  name: "",
  provider_type: "mock",
  base_url: null,
  remote_model_name: null,
  api_key_env: null,
  enabled: true,
  input_price_per_million: 0,
  output_price_per_million: 0,
  default_parameters: {},
};

function toPayload(model: ModelConfig): ModelPayload {
  return {
    name: model.name,
    provider_type: model.provider_type,
    base_url: model.base_url,
    remote_model_name: model.remote_model_name,
    api_key_env: model.api_key_env,
    enabled: model.enabled,
    input_price_per_million: model.input_price_per_million,
    output_price_per_million: model.output_price_per_million,
    default_parameters: model.default_parameters,
  };
}

export function ModelsPage() {
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ModelConfig | null>(null);
  const [payload, setPayload] = useState<ModelPayload>(emptyPayload);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setModels((await api.models()).items); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "无法读取模型列表。"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const openCreate = () => { setEditing(null); setPayload(emptyPayload); setFormError(null); setFormOpen(true); };
  const openEdit = (model: ModelConfig) => { setEditing(model); setPayload(toPayload(model)); setFormError(null); setFormOpen(true); };
  const setProvider = (provider_type: ProviderType) => setPayload((current) => ({
    ...current,
    provider_type,
    ...(provider_type === "mock"
      ? { base_url: null, remote_model_name: null, api_key_env: null, input_price_per_million: 0, output_price_per_million: 0 }
      : { input_price_per_million: null, output_price_per_million: null }),
  }));
  const isCompatible = payload.provider_type === "openai_compatible";
  const canSave = useMemo(() => payload.name.trim() && (!isCompatible || (payload.base_url && payload.remote_model_name && payload.api_key_env)), [payload, isCompatible]);

  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (!canSave) return;
    setSaving(true); setFormError(null);
    try {
      if (editing) await api.updateModel(editing.id, payload); else await api.createModel(payload);
      setFormOpen(false); await load();
    } catch (reason) { setFormError(reason instanceof ApiError ? reason.message : "保存失败。"); }
    finally { setSaving(false); }
  };
  const remove = async (model: ModelConfig) => {
    if (!window.confirm(`确认删除模型“${model.name}”？已有历史 Run 的模型不会被删除。`)) return;
    try { await api.deleteModel(model.id); await load(); }
    catch (reason) { setError(reason instanceof ApiError ? reason.message : "删除失败。"); }
  };

  return <>
    <PageHeader eyebrow="MODEL REGISTRY" title="模型" description="保存连接配置与价格，不保存任何真实密钥。API Key 只引用服务端环境变量名。" actions={<button className="primary-button" onClick={openCreate}><Plus size={16} /> 添加模型</button>} />
    <div className="security-note"><KeyRound size={17} /><div><strong>密钥不进入数据库</strong><span>请在后端环境中设置密钥值，此处只填写例如 <code>OPENAI_API_KEY</code> 的变量名称。</span></div></div>
    {loading ? <LoadingState label="正在读取模型" /> : error ? <ErrorState message={error} retry={() => void load()} /> : models.length === 0 ? <EmptyState title="还没有模型" message="先添加一个完全离线的 Mock 模型，无需 API Key。" action={<button className="secondary-button" onClick={openCreate}><Plus size={15} /> 添加 Mock 模型</button>} /> : <div className="card-grid">{models.map((model) => <article className="model-card" key={model.id}><div className="card-top"><span className={`provider-icon provider-${model.provider_type}`}><Server size={19} /></span><span className={model.enabled ? "enabled-dot" : "disabled-dot"}>{model.enabled ? "已启用" : "已停用"}</span></div><h2>{model.name}</h2><p>{model.provider_type === "mock" ? "Mock · 完全离线" : `OpenAI-compatible · ${model.remote_model_name}`}</p><dl><div><dt>Base URL</dt><dd>{model.base_url || "本地 Mock"}</dd></div><div><dt>密钥变量</dt><dd>{model.api_key_env || "不需要"}</dd></div><div><dt>更新时间</dt><dd>{formatUtc8(model.updated_at)}</dd></div></dl><footer><button className="icon-button" onClick={() => openEdit(model)} aria-label={`编辑 ${model.name}`}><Pencil size={15} /> 编辑</button><button className="icon-button danger" onClick={() => void remove(model)} aria-label={`删除 ${model.name}`}><Trash2 size={15} /></button></footer></article>)}</div>}
    {formOpen && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setFormOpen(false); }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="model-dialog-title"><header><div><span className="eyebrow">MODEL CONFIGURATION</span><h2 id="model-dialog-title">{editing ? "编辑模型" : "添加模型"}</h2></div><button className="close-button" onClick={() => setFormOpen(false)} aria-label="关闭"><X size={19} /></button></header><form onSubmit={(event) => void submit(event)}><label>显示名称<input required value={payload.name} onChange={(event) => setPayload({ ...payload, name: event.target.value })} placeholder="例如：Local Mock" /></label><fieldset><legend>Provider 类型</legend><div className="segmented"><button type="button" className={!isCompatible ? "selected" : ""} onClick={() => setProvider("mock")}>Mock</button><button type="button" className={isCompatible ? "selected" : ""} onClick={() => setProvider("openai_compatible")}>OpenAI-compatible</button></div></fieldset>{isCompatible && <div className="form-grid"><label className="span-2">Base URL<input required type="url" value={payload.base_url || ""} onChange={(event) => setPayload({ ...payload, base_url: event.target.value })} placeholder="https://provider.example/v1" /></label><label>远端模型名<input required value={payload.remote_model_name || ""} onChange={(event) => setPayload({ ...payload, remote_model_name: event.target.value })} placeholder="model-name" /></label><label>API Key 环境变量名<input required pattern="[A-Za-z_][A-Za-z0-9_]*" value={payload.api_key_env || ""} onChange={(event) => setPayload({ ...payload, api_key_env: event.target.value })} placeholder="PROVIDER_API_KEY" /></label><label>输入价格 / 百万 Token（可选）<input min="0" step="0.0001" type="number" value={payload.input_price_per_million ?? ""} onChange={(event) => setPayload({ ...payload, input_price_per_million: event.target.value === "" ? null : Number(event.target.value) })} placeholder="留空表示未知" /></label><label>输出价格 / 百万 Token（可选）<input min="0" step="0.0001" type="number" value={payload.output_price_per_million ?? ""} onChange={(event) => setPayload({ ...payload, output_price_per_million: event.target.value === "" ? null : Number(event.target.value) })} placeholder="留空表示未知" /></label></div>}<label className="checkbox-label"><input type="checkbox" checked={payload.enabled} onChange={(event) => setPayload({ ...payload, enabled: event.target.checked })} /><span><Check size={14} /> 启用此模型</span></label>{formError && <p className="form-error">{formError}</p>}<footer><button type="button" className="secondary-button" onClick={() => setFormOpen(false)}>取消</button><button className="primary-button" disabled={!canSave || saving}>{saving ? "保存中…" : editing ? "保存修改" : "添加模型"}</button></footer></form></section></div>}
  </>;
}
