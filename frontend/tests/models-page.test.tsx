import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelConfig } from "../src/api/types";
import { ModelsPage } from "../src/pages/ModelsPage";

const existingModel: ModelConfig = {
  id: "model-existing",
  name: "Existing Compatible Model",
  provider_type: "openai_compatible",
  base_url: "https://provider.example/v1",
  remote_model_name: "provider-model",
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

const legacyEnvironmentModel: ModelConfig = {
  ...existingModel,
  id: "model-legacy-environment",
  name: "Legacy Environment Model",
  credential_source: "environment",
  has_api_key: false,
  api_key_env: "LEGACY_SECRET_ENV_MUST_NOT_RENDER",
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function modelsResponse(items: ModelConfig[]) {
  return { items, total: items.length, offset: 0, limit: 100 };
}

describe("ModelsPage API Key input", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("requires a masked API Key when creating an OpenAI-compatible model", async () => {
    const user = userEvent.setup();
    const requests: Array<{ method: string; body: Record<string, unknown> }> = [];
    let items: ModelConfig[] = [];
    vi.mocked(fetch).mockImplementation(async (_request, init) => {
      const method = init?.method || "GET";
      if (method === "POST") {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        requests.push({ method, body });
        items = [
          {
            ...existingModel,
            id: "model-created",
            name: String(body.name),
            base_url: String(body.base_url),
            remote_model_name: String(body.remote_model_name),
            api_key_env: null,
          },
        ];
        return jsonResponse(items[0], 201);
      }
      return jsonResponse(modelsResponse(items));
    });

    render(<ModelsPage />);

    await screen.findByText("还没有模型");
    await user.click(screen.getByRole("button", { name: "添加模型" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "OpenAI-compatible" }));

    const apiKeyInput = within(dialog).getByLabelText(/API Key/) as HTMLInputElement;
    expect(apiKeyInput).toHaveAttribute("type", "password");
    expect(apiKeyInput).toHaveAttribute("minlength", "8");
    expect(apiKeyInput).toBeRequired();
    expect(apiKeyInput).toHaveValue("");

    const submit = within(dialog).getByRole("button", { name: "添加模型" });
    await user.type(within(dialog).getByLabelText(/显示名称/), "Browser Provider");
    await user.type(within(dialog).getByLabelText(/Base URL/), "https://provider.example/v1");
    await user.type(within(dialog).getByLabelText(/远端模型名/), "provider-model");
    expect(submit).toBeDisabled();

    await user.type(apiKeyInput, "test-browser-key-123");
    expect(submit).toBeEnabled();
    await user.click(submit);

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toEqual({
      method: "POST",
      body: expect.objectContaining({ api_key: "test-browser-key-123" }),
    });
    expect(requests[0].body).not.toHaveProperty("api_key_env");
    expect(await screen.findByText("Browser Provider")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("test-browser-key-123")).not.toBeInTheDocument();
  });

  it("clears the API Key while the create request is still pending", async () => {
    const user = userEvent.setup();
    const submittedKey = "test-pending-key-234";
    let resolveCreate: ((response: Response) => void) | null = null;
    let createBody: Record<string, unknown> | null = null;
    let items: ModelConfig[] = [];
    vi.mocked(fetch).mockImplementation(async (_request, init) => {
      const method = init?.method || "GET";
      if (method === "POST") {
        createBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return new Promise<Response>((resolve) => {
          resolveCreate = resolve;
        });
      }
      return jsonResponse(modelsResponse(items));
    });

    render(<ModelsPage />);

    await screen.findByText("还没有模型");
    await user.click(screen.getByRole("button", { name: "添加模型" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "OpenAI-compatible" }));
    await user.type(within(dialog).getByLabelText(/显示名称/), "Pending Provider");
    await user.type(within(dialog).getByLabelText(/Base URL/), "https://provider.example/v1");
    await user.type(within(dialog).getByLabelText(/远端模型名/), "provider-model");
    const apiKeyInput = within(dialog).getByLabelText(/API Key/);
    await user.type(apiKeyInput, submittedKey);
    await user.click(within(dialog).getByRole("button", { name: "添加模型" }));

    await waitFor(() => expect(createBody).not.toBeNull());
    expect(apiKeyInput).toHaveValue("");
    expect(within(dialog).getByRole("button", { name: "保存中…" })).toBeDisabled();
    expect(document.body).not.toHaveTextContent(submittedKey);

    items = [
      {
        ...existingModel,
        id: "model-pending-created",
        name: "Pending Provider",
      },
    ];
    await act(async () => {
      resolveCreate?.(jsonResponse(items[0], 201));
    });
    expect(createBody).toEqual(expect.objectContaining({ api_key: submittedKey }));
    expect(await screen.findByText("Pending Provider")).toBeInTheDocument();
  });

  it("omits a blank stored API Key when editing", async () => {
    const user = userEvent.setup();
    let patchBody: Record<string, unknown> | null = null;
    vi.mocked(fetch).mockImplementation(async (_request, init) => {
      const method = init?.method || "GET";
      if (method === "PATCH") {
        patchBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return jsonResponse(existingModel);
      }
      return jsonResponse(modelsResponse([existingModel]));
    });

    render(<ModelsPage />);

    expect(await screen.findByText("Existing Compatible Model")).toBeInTheDocument();
    expect(screen.getByText("已安全保存")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "编辑 Existing Compatible Model" }));
    const dialog = screen.getByRole("dialog");
    const apiKeyInput = within(dialog).getByLabelText(/API Key/) as HTMLInputElement;
    expect(apiKeyInput).toHaveAttribute("type", "password");
    expect(apiKeyInput).not.toBeRequired();
    expect(apiKeyInput).toHaveValue("");
    expect(apiKeyInput).toHaveAttribute("placeholder", "留空表示保留现有凭据");

    await user.click(within(dialog).getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).not.toHaveProperty("api_key");
    expect(patchBody).not.toHaveProperty("api_key_env");
  });

  it("requires a new API Key when an existing compatible model has none", async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(
        modelsResponse([
          {
            ...existingModel,
            credential_source: "none",
            has_api_key: false,
            api_key_env: null,
          },
        ]),
      ),
    );

    render(<ModelsPage />);

    await screen.findByText("Existing Compatible Model");
    expect(screen.getByText("未配置")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑 Existing Compatible Model" }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByLabelText(/API Key/)).toBeRequired();
    expect(within(dialog).getByRole("button", { name: "保存修改" })).toBeDisabled();
  });

  it("keeps a legacy environment credential only within the same Provider origin", async () => {
    const user = userEvent.setup();
    let patchBody: Record<string, unknown> | null = null;
    vi.mocked(fetch).mockImplementation(async (_request, init) => {
      const method = init?.method || "GET";
      if (method === "PATCH") {
        patchBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return jsonResponse({ ...legacyEnvironmentModel, base_url: String(patchBody.base_url) });
      }
      return jsonResponse(modelsResponse([legacyEnvironmentModel]));
    });

    render(<ModelsPage />);

    await screen.findByText("Legacy Environment Model");
    expect(screen.getByText("环境变量兼容配置")).toBeInTheDocument();
    expect(screen.queryByText("LEGACY_SECRET_ENV_MUST_NOT_RENDER")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑 Legacy Environment Model" }));
    const dialog = screen.getByRole("dialog");
    const baseUrlInput = within(dialog).getByLabelText(/Base URL/);
    const apiKeyInput = within(dialog).getByLabelText(/API Key/);

    await user.clear(baseUrlInput);
    await user.type(baseUrlInput, "https://provider.example/another-compatible-path");
    expect(apiKeyInput).not.toBeRequired();
    expect(within(dialog).getByText(/当前使用环境变量兼容配置/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).not.toHaveProperty("api_key");
    expect(patchBody).not.toHaveProperty("api_key_env");
  });

  it("requires a new Key for a changed Base URL and clears it after a failed request", async () => {
    const user = userEvent.setup();
    const submittedKey = "test-replacement-key-456";
    let patchBody: Record<string, unknown> | null = null;
    vi.mocked(fetch).mockImplementation(async (_request, init) => {
      const method = init?.method || "GET";
      if (method === "PATCH") {
        patchBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return jsonResponse(
          { detail: { code: "provider_rejected", message: `恶意错误回显：${submittedKey}` } },
          400,
        );
      }
      return jsonResponse(modelsResponse([existingModel]));
    });

    render(<ModelsPage />);

    await screen.findByText("Existing Compatible Model");
    await user.click(screen.getByRole("button", { name: "编辑 Existing Compatible Model" }));
    const dialog = screen.getByRole("dialog");
    const baseUrlInput = within(dialog).getByLabelText(/Base URL/);
    const apiKeyInput = within(dialog).getByLabelText(/API Key/);
    const submit = within(dialog).getByRole("button", { name: "保存修改" });

    await user.clear(baseUrlInput);
    await user.type(baseUrlInput, "https://new-provider.example/v1");
    expect(apiKeyInput).toBeRequired();
    expect(submit).toBeDisabled();
    expect(within(dialog).getByText(/Base URL 已更改/)).toBeInTheDocument();

    await user.type(apiKeyInput, submittedKey);
    expect(submit).toBeEnabled();
    await user.click(submit);

    expect(await within(dialog).findByText("恶意错误回显：[REDACTED]")).toBeInTheDocument();
    expect(apiKeyInput).toHaveValue("");
    expect(document.body).not.toHaveTextContent(submittedKey);
    expect(screen.queryByDisplayValue(submittedKey)).not.toBeInTheDocument();
    expect(patchBody).toEqual(
      expect.objectContaining({
        base_url: "https://new-provider.example/v1",
        api_key: submittedKey,
      }),
    );
  });

  it("clears the Key on Provider switch and dialog close without using storage or console", async () => {
    const user = userEvent.setup();
    const storageSet = vi.spyOn(Storage.prototype, "setItem");
    const consoleDebug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const consoleInfo = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const consoleLog = vi.spyOn(console, "log").mockImplementation(() => undefined);
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.mocked(fetch).mockResolvedValue(jsonResponse(modelsResponse([])));

    render(<ModelsPage />);

    await screen.findByText("还没有模型");
    await user.click(screen.getByRole("button", { name: "添加模型" }));
    let dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "OpenAI-compatible" }));
    let apiKeyInput = within(dialog).getByLabelText(/API Key/);
    await user.type(apiKeyInput, "test-switch-key-567");
    await user.click(within(dialog).getByRole("button", { name: "Mock" }));
    expect(within(dialog).queryByLabelText(/API Key/)).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "OpenAI-compatible" }));
    apiKeyInput = within(dialog).getByLabelText(/API Key/);
    expect(apiKeyInput).toHaveValue("");
    await user.type(apiKeyInput, "test-close-key-678");
    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "添加模型" }));
    dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "OpenAI-compatible" }));
    expect(within(dialog).getByLabelText(/API Key/)).toHaveValue("");
    expect(storageSet).not.toHaveBeenCalled();
    expect(consoleDebug).not.toHaveBeenCalled();
    expect(consoleInfo).not.toHaveBeenCalled();
    expect(consoleLog).not.toHaveBeenCalled();
    expect(consoleWarn).not.toHaveBeenCalled();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("aborts a pending credential request and leaves no Key behind when unmounted", async () => {
    const user = userEvent.setup();
    const submittedKey = "test-unmount-key-789";
    const storageSet = vi.spyOn(Storage.prototype, "setItem");
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    let submittedSignal: AbortSignal | null = null;
    vi.mocked(fetch).mockImplementation(async (_request, init) => {
      const method = init?.method || "GET";
      if (method === "POST") {
        submittedSignal = init?.signal as AbortSignal;
        return new Promise<Response>((_resolve, reject) => {
          submittedSignal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted by lifecycle test", "AbortError"));
          });
        });
      }
      return jsonResponse(modelsResponse([]));
    });

    const rendered = render(<ModelsPage />);

    await screen.findByText("还没有模型");
    await user.click(screen.getByRole("button", { name: "添加模型" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "OpenAI-compatible" }));
    await user.type(within(dialog).getByLabelText(/显示名称/), "Unmount Provider");
    await user.type(within(dialog).getByLabelText(/Base URL/), "https://provider.example/v1");
    await user.type(within(dialog).getByLabelText(/远端模型名/), "provider-model");
    await user.type(within(dialog).getByLabelText(/API Key/), submittedKey);
    await user.click(within(dialog).getByRole("button", { name: "添加模型" }));
    await waitFor(() => expect(submittedSignal).not.toBeNull());

    rendered.unmount();

    await waitFor(() => expect(submittedSignal?.aborted).toBe(true));
    expect(document.body).not.toHaveTextContent(submittedKey);
    expect(screen.queryByDisplayValue(submittedKey)).not.toBeInTheDocument();
    expect(storageSet).not.toHaveBeenCalled();
    expect(consoleError).not.toHaveBeenCalled();
  });
});
