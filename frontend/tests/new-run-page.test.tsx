import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Benchmark, ModelConfig } from "../src/api/types";
import { NewRunPage } from "../src/pages/NewRunPage";

const model: ModelConfig = {
  id: "model-compatible",
  name: "Compatible Reasoning Model",
  provider_type: "openai_compatible",
  base_url: "https://provider.example/v1",
  remote_model_name: "reasoning-model",
  credential_source: "stored",
  has_api_key: true,
  api_key_env: null,
  enabled: true,
  input_price_per_million: null,
  output_price_per_million: null,
  default_parameters: {},
  created_at: "2026-08-27T00:00:00Z",
  updated_at: "2026-08-27T00:00:00Z",
};

function benchmark(
  id: string,
  slug: string,
  name: string,
  questionCount: number,
): Benchmark {
  return {
    id,
    slug,
    name,
    version: "1.0.0",
    description: `${name} fixture`,
    dimension: "reasoning",
    language: "en",
    license: "test-only",
    source: "offline test fixture",
    evaluator_type: "builtin-objective",
    evaluator_config: {},
    prompt_template: { system: "", user: "{prompt}\n{choices}" },
    schema_version: "llmbenchlab-dataset-v1",
    dataset_hash: id.padEnd(64, "0").slice(0, 64),
    question_count: questionCount,
    is_demo: false,
    created_at: "2026-08-27T00:00:00Z",
  };
}

const benchmarks = [
  benchmark("gpqa", "gpqa-diamond", "GPQA-Diamond", 198),
  benchmark("mmlu-cot", "mmlu-pro-official-cot", "MMLU-Pro (official_cot)", 12_032),
  benchmark("mmlu-direct", "mmlu-pro-direct", "MMLU-Pro (direct)", 12_032),
];

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function listResponse<T>(items: T[]) {
  return { items, total: items.length, offset: 0, limit: 100 };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/runs/new"]}>
      <Routes>
        <Route path="/runs/new" element={<NewRunPage />} />
        <Route path="/runs/:runId" element={<div>Run detail target</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function installFetch(
  postBodies: Array<Record<string, unknown>> = [],
  modelItems: ModelConfig[] = [model],
  benchmarkItems: Benchmark[] = benchmarks,
) {
  const fetchMock = vi.mocked(fetch);
  fetchMock.mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = init?.method || "GET";
    if (method === "POST" && url.endsWith("/api/v1/runs")) {
      postBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return jsonResponse({ id: "run-created" }, 202);
    }
    if (method === "GET" && url.includes("/api/v1/models")) {
      return jsonResponse(listResponse(modelItems));
    }
    if (method === "GET" && url.endsWith("/api/v1/benchmarks?limit=100")) {
      return jsonResponse(listResponse(benchmarkItems));
    }
    throw new Error(`Unexpected fetch in NewRunPage test: ${method} ${url}`);
  });
  return fetchMock;
}

describe("NewRunPage generation recommendations", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("starts GPQA with its long-reasoning recommendation and renders safety limits", async () => {
    installFetch();
    renderPage();

    await screen.findByRole("heading", { name: "新建评测" });
    const maxTokens = screen.getByLabelText("Max tokens");
    const readTimeout = screen.getByLabelText("单次读取超时（秒）");

    expect(screen.getByRole("combobox", { name: "Benchmark" })).toHaveValue("gpqa");
    expect(maxTokens).toHaveValue(8192);
    expect(readTimeout).toHaveValue(600);
    expect(maxTokens).toHaveAttribute("max", "131072");
    expect(readTimeout).toHaveAttribute("max", "1800");
    expect(screen.getByText(/当前 Benchmark 建议：8,192 tokens · 600 秒/)).toBeInTheDocument();
  });

  it("updates untouched values when switching between formal benchmark profiles", async () => {
    const user = userEvent.setup();
    installFetch();
    renderPage();

    const benchmarkSelect = await screen.findByRole("combobox", { name: "Benchmark" });
    const maxTokens = screen.getByLabelText("Max tokens");
    const readTimeout = screen.getByLabelText("单次读取超时（秒）");

    await user.selectOptions(benchmarkSelect, "mmlu-cot");
    expect(maxTokens).toHaveValue(4000);
    expect(readTimeout).toHaveValue(300);

    await user.selectOptions(benchmarkSelect, "mmlu-direct");
    expect(maxTokens).toHaveValue(1024);
    expect(readTimeout).toHaveValue(180);
  });

  it("preserves a manual token budget, reapplies the recommendation, and submits Provider-managed output", async () => {
    const user = userEvent.setup();
    const postBodies: Array<Record<string, unknown>> = [];
    installFetch(postBodies);
    renderPage();

    const benchmarkSelect = await screen.findByRole("combobox", { name: "Benchmark" });
    const maxTokens = screen.getByLabelText("Max tokens");
    const readTimeout = screen.getByLabelText("单次读取超时（秒）");

    await user.clear(maxTokens);
    await user.type(maxTokens, "12345");
    await user.selectOptions(benchmarkSelect, "mmlu-cot");
    expect(maxTokens).toHaveValue(12345);
    expect(readTimeout).toHaveValue(300);

    await user.click(screen.getByRole("button", { name: "应用建议" }));
    expect(maxTokens).toHaveValue(4000);
    expect(readTimeout).toHaveValue(300);

    await user.click(screen.getByRole("checkbox", { name: "由 Provider 决定 max tokens" }));
    expect(maxTokens).toBeDisabled();
    expect(maxTokens).toHaveValue(null);
    await user.clear(readTimeout);
    await user.type(readTimeout, "900");
    await user.click(screen.getByRole("button", { name: "创建并开始评测" }));

    await waitFor(() => expect(postBodies).toHaveLength(1));
    expect(postBodies[0]).toEqual(
      expect.objectContaining({
        model_id: "model-compatible",
        benchmark_id: "mmlu-cot",
        max_tokens: null,
        read_timeout_seconds: 900,
      }),
    );
    expect(await screen.findByText("Run detail target")).toBeInTheDocument();
  });

  it("preserves null model defaults and warns based on provider type", async () => {
    const user = userEvent.setup();
    const postBodies: Array<Record<string, unknown>> = [];
    const providerManagedModel = {
      ...model,
      default_parameters: { max_tokens: null, seed: null },
    };
    const demo = { ...benchmarks[0], id: "demo", is_demo: true };
    installFetch(postBodies, [providerManagedModel], [demo]);
    const view = renderPage();

    await screen.findByRole("heading", { name: "新建评测" });
    expect(screen.getByLabelText("Max tokens")).toBeDisabled();
    expect(screen.getByLabelText("Seed")).toHaveValue(null);
    expect(screen.getByText(/此模型会逐题调用外部 Provider/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "创建并开始评测" }));
    await waitFor(() => expect(postBodies).toHaveLength(1));
    expect(postBodies[0]).toEqual(expect.objectContaining({ max_tokens: null, seed: null }));
    view.unmount();

    const mockModel: ModelConfig = {
      ...model,
      id: "model-mock",
      name: "Offline Mock",
      provider_type: "mock",
      base_url: null,
      remote_model_name: null,
      credential_source: "none",
      has_api_key: false,
    };
    installFetch([], [mockModel], benchmarks);
    renderPage();
    await screen.findByRole("heading", { name: "新建评测" });
    expect(screen.queryByText(/逐题调用外部 Provider/)).not.toBeInTheDocument();
  });

  it("uses Messages defaults and recommendations until max tokens are manually changed", async () => {
    const user = userEvent.setup();
    const postBodies: Array<Record<string, unknown>> = [];
    const messagesWithDefault: ModelConfig = {
      ...model,
      id: "messages-with-default",
      name: "Messages With Default",
      provider_type: "anthropic_messages",
      default_parameters: { max_tokens: 2048 },
    };
    const messagesWithRecommendation: ModelConfig = {
      ...model,
      id: "messages-with-recommendation",
      name: "Messages With Recommendation",
      provider_type: "anthropic_messages",
      default_parameters: { max_tokens: null },
    };
    installFetch(postBodies, [model, messagesWithDefault, messagesWithRecommendation]);
    renderPage();

    const modelSelect = await screen.findByRole("combobox", { name: "模型" });
    const benchmarkSelect = screen.getByRole("combobox", { name: "Benchmark" });
    const maxTokens = screen.getByLabelText("Max tokens");

    await user.selectOptions(modelSelect, messagesWithDefault.id);
    expect(maxTokens).toHaveValue(2048);

    await user.selectOptions(modelSelect, messagesWithRecommendation.id);
    expect(maxTokens).toHaveValue(8192);
    await user.selectOptions(benchmarkSelect, "mmlu-direct");
    expect(maxTokens).toHaveValue(1024);

    await user.clear(maxTokens);
    await user.type(maxTokens, "777");
    await user.selectOptions(benchmarkSelect, "mmlu-cot");
    expect(maxTokens).toHaveValue(777);

    await user.click(screen.getByRole("button", { name: "创建并开始评测" }));
    await waitFor(() => expect(postBodies).toHaveLength(1));
    expect(postBodies[0]).toEqual(
      expect.objectContaining({
        model_id: messagesWithRecommendation.id,
        benchmark_id: "mmlu-cot",
        max_tokens: 777,
        seed: null,
      }),
    );
  });

  it("restores per-model manual seeds, including null, across unsupported protocols", async () => {
    const user = userEvent.setup();
    const secondCompatibleModel: ModelConfig = {
      ...model,
      id: "model-compatible-second",
      name: "Second Compatible Model",
      default_parameters: { seed: 99 },
    };
    const responsesModel: ModelConfig = {
      ...model,
      id: "model-responses-seed-bridge",
      name: "Responses Seed Bridge",
      provider_type: "openai_responses",
      default_parameters: { seed: null },
    };
    installFetch([], [model, secondCompatibleModel, responsesModel]);
    renderPage();

    const modelSelect = await screen.findByRole("combobox", { name: "模型" });
    const seed = screen.getByLabelText("Seed");

    await user.clear(seed);
    await user.type(seed, "123");
    await user.selectOptions(modelSelect, responsesModel.id);
    expect(seed).toBeDisabled();
    expect(seed).toHaveValue(null);

    await user.selectOptions(modelSelect, secondCompatibleModel.id);
    expect(seed).toBeEnabled();
    expect(seed).toHaveValue(99);
    await user.clear(seed);
    expect(seed).toHaveValue(null);

    await user.selectOptions(modelSelect, responsesModel.id);
    await user.selectOptions(modelSelect, model.id);
    expect(seed).toHaveValue(123);

    await user.selectOptions(modelSelect, secondCompatibleModel.id);
    expect(seed).toHaveValue(null);
  });

  it("enforces protocol-specific seed and max-token controls when switching models", async () => {
    const user = userEvent.setup();
    const responsesModel: ModelConfig = {
      ...model,
      id: "model-responses",
      name: "Responses Model",
      provider_type: "openai_responses",
      remote_model_name: "gpt-5.6-luna",
      default_parameters: { max_tokens: null, seed: 42 },
    };
    const messagesModel: ModelConfig = {
      ...model,
      id: "model-messages",
      name: "Messages Model",
      provider_type: "anthropic_messages",
      remote_model_name: "qwen3.8-flash",
      default_parameters: { max_tokens: null, seed: 42 },
    };
    installFetch([], [model, responsesModel, messagesModel]);
    renderPage();

    const modelSelect = await screen.findByRole("combobox", { name: "模型" });
    const seed = screen.getByLabelText("Seed");
    const temperature = screen.getByLabelText("Temperature");
    const topP = screen.getByLabelText("Top-p");
    const maxTokens = screen.getByLabelText("Max tokens");
    const providerManaged = screen.getByRole("checkbox", { name: "由 Provider 决定 max tokens" });

    await user.selectOptions(modelSelect, "model-responses");
    expect(seed).toBeDisabled();
    expect(seed).toHaveValue(null);
    expect(temperature).toHaveValue(null);
    expect(topP).toHaveValue(null);
    expect(screen.getByText(/Responses API 不支持 seed/)).toBeInTheDocument();
    expect(providerManaged).toBeEnabled();
    expect(maxTokens).toBeDisabled();

    await user.selectOptions(modelSelect, "model-messages");
    expect(seed).toBeDisabled();
    expect(seed).toHaveValue(null);
    expect(temperature).toHaveValue(null);
    expect(temperature).toHaveAttribute("max", "1");
    expect(topP).toHaveValue(null);
    expect(providerManaged).toBeDisabled();
    expect(providerManaged).not.toBeChecked();
    expect(maxTokens).toBeEnabled();
    expect(maxTokens).toHaveValue(8192);
    expect(screen.getByText(/Messages API 必须提供有限的 max tokens/)).toBeInTheDocument();

    await user.selectOptions(modelSelect, "model-compatible");
    expect(seed).toBeEnabled();
    expect(seed).toHaveValue(42);
    expect(temperature).toHaveValue(0);
    expect(temperature).toHaveAttribute("max", "2");
    expect(topP).toHaveValue(1);
    expect(providerManaged).toBeEnabled();
  });
});
