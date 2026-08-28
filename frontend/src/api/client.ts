import type {
  Benchmark,
  DashboardSummary,
  EvaluationResponse,
  EvaluationRun,
  LeaderboardEntry,
  ListResponse,
  ModelConfig,
  ModelPayload,
  RunPayload,
  RunStatus,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1").replace(/\/$/, "");

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(message: string, status = 0, code = "request_failed") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function errorDetails(payload: unknown): { code: string; message: string } {
  if (typeof payload !== "object" || payload === null) {
    return { code: "request_failed", message: "服务返回了无法识别的错误。" };
  }
  const body = payload as Record<string, unknown>;
  const detail = typeof body.detail === "object" && body.detail !== null
    ? body.detail as Record<string, unknown>
    : body;
  if (Array.isArray(body.detail)) {
    const first = body.detail[0] as Record<string, unknown> | undefined;
    return {
      code: "validation_error",
      message: typeof first?.msg === "string" ? first.msg : "提交的数据未通过校验。",
    };
  }
  return {
    code: typeof detail.code === "string" ? detail.code : "request_failed",
    message: typeof detail.message === "string" ? detail.message : "请求失败，请稍后重试。",
  };
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch (error) {
    throw new ApiError(
      error instanceof Error ? `无法连接后端：${error.message}` : "无法连接后端服务。",
      0,
      "network_error",
    );
  }
  if (response.status === 204) return undefined as T;
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const details = errorDetails(payload);
    throw new ApiError(details.message, response.status, details.code);
  }
  return payload as T;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export const api = {
  summary: () => request<DashboardSummary>("/metrics/summary"),
  models: (params: { limit?: number; enabled?: boolean } = {}) =>
    request<ListResponse<ModelConfig>>(`/models${query({ limit: params.limit ?? 100, enabled: params.enabled })}`),
  createModel: (payload: ModelPayload, signal?: AbortSignal) =>
    request<ModelConfig>("/models", { method: "POST", body: JSON.stringify(payload), signal }),
  updateModel: (id: string, payload: Partial<ModelPayload>, signal?: AbortSignal) =>
    request<ModelConfig>(`/models/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
      signal,
    }),
  deleteModel: (id: string) => request<void>(`/models/${id}`, { method: "DELETE" }),
  benchmarks: () => request<ListResponse<Benchmark>>("/benchmarks?limit=100"),
  benchmark: (id: string) => request<Benchmark>(`/benchmarks/${id}`),
  reloadDemo: () => request<Benchmark>("/benchmarks/reload-demo", { method: "POST" }),
  importBenchmark: (archive: File) => {
    const data = new FormData();
    data.set("archive", archive);
    return request<Benchmark>("/benchmarks/import", { method: "POST", body: data });
  },
  runs: (params: {
    offset?: number;
    limit?: number;
    run_status?: RunStatus;
    model_id?: string;
    benchmark_id?: string;
    protocol_version?: string;
  } = {}) => request<ListResponse<EvaluationRun>>(`/runs${query({ ...params, limit: params.limit ?? 20 })}`),
  run: (id: string) => request<EvaluationRun>(`/runs/${id}`),
  createRun: (payload: RunPayload) => request<EvaluationRun>("/runs", { method: "POST", body: JSON.stringify(payload) }),
  cancelRun: (id: string) => request<EvaluationRun>(`/runs/${id}/cancel`, { method: "POST" }),
  responses: (runId: string, params: { offset?: number; limit?: number } = {}) =>
    request<ListResponse<EvaluationResponse>>(
      `/runs/${runId}/responses${query({ offset: params.offset, limit: params.limit ?? 100 })}`,
    ),
  leaderboard: (params: { model_id?: string; benchmark_id?: string; order?: string } = {}) =>
    request<ListResponse<LeaderboardEntry>>(`/leaderboard${query({ ...params, limit: 100 })}`),
};
