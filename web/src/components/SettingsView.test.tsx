import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { SettingsDocument } from "../api/types";
import i18n from "../i18n";
import { SettingsView } from "./SettingsView";

const DOCUMENT: SettingsDocument = {
  version: 4,
  general: {
    language: "system",
    theme: "system",
    default_execution_mode: "offline_fake",
    show_reasoning: true,
    activity_density: "normal",
  },
  agents: {
    default_provider_id: "mock",
    default_model_id: "mock-model",
    roles: {},
  },
  execution: {
    budget_currency: "CNY",
    exchange_rates: { CNY: 1 },
    max_iterations: 5,
    max_test_runs: 12,
    max_files_changed: 5,
    max_lines_changed: 150,
    max_tool_calls_per_iteration: 30,
    max_review_calls: 2,
    max_runtime_seconds: 1800,
    max_total_tokens: 1000000,
    max_cost: null,
    max_parallel_tasks: 1,
  },
  workspace: { default_repository_kind: "demo", default_repository: "" },
  providers: [
    {
      provider_id: "mock",
      name: "Mock Provider",
      protocol: "openai_compatible",
      base_url: "https://mock.example/v1",
      enabled: true,
      custom: false,
      catalog_id: "mock",
      timeout_seconds: 60,
      max_retries: 2,
      max_concurrency: 2,
      region: null,
      aws_profile: null,
      models: [
        {
          model_id: "mock-model",
          display_name: "Mock Model",
          context_window: 32768,
          max_output_tokens: 4096,
          supports_tools: true,
          supports_json_mode: true,
          supports_temperature: true,
          supports_thinking: false,
          price: {
            currency: "CNY",
            input_per_mtok: 1,
            output_per_mtok: 2,
            cached_input_per_mtok: null,
          },
          source: "manual",
          source_note: null,
          favorite: true,
          routing_ready: true,
          blockers: [],
          verification: {
            config_complete: true,
            missing: [],
            credential: { available: true, reason: "environment variable MOCK_API_KEY" },
            protocol: { available: true, reason: "native adapter available" },
            routing: { eligible: true, blockers: [] },
            inference: {
              state: "not_tested",
              checked_at: null,
              detail: null,
              latency_ms: null,
              model_echo: null,
              tool_calling: "declared_not_verified",
            },
            tool_calling: "declared_not_verified",
          },
        },
      ],
      credential: {
        kind: "env",
        configured: true,
        available: true,
        reason: "environment variable MOCK_API_KEY",
        env_name: "MOCK_API_KEY",
        updated_at: null,
      },
      protocol_supported: true,
      protocol_reason: "native adapter available",
      verification: {
        routing_eligible: 1,
        inference_verified: 0,
        config_test: {
          state: "not_tested",
          checked_at: null,
          detail: null,
          latency_ms: null,
        },
        models_test: {
          state: "not_tested",
          checked_at: null,
          detail: null,
          latency_ms: null,
        },
      },
      editable: {
        provider_id: "mock",
        name: "Mock Provider",
        protocol: "openai_compatible",
        base_url: "https://mock.example/v1",
        enabled: true,
        custom: false,
        models: [],
      },
      created_at: null,
      updated_at: null,
    },
  ],
  updated_at: null,
  secure_storage_available: true,
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeAll(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/settings") && method === "GET") return json(DOCUMENT);
      if (url.endsWith("/settings/catalog")) return json([]);
      if (url.endsWith("/settings/runtime")) {
        return json({
          product: "CodePilot-Repair",
          version: "0.1.0",
          git_commit: null,
          worker: {
            counts: {},
            queued: 0,
            running: 0,
            completed: 0,
            stopped: 0,
            failed: 0,
            cancelled: 0,
            uncertain: 0,
            last_worker: null,
            lease_seconds: 120,
            poll_seconds: 0.5,
            execution_backend: "fake",
          },
          docker: { available: false, server_version: null, latency_ms: 1, error: "not running" },
          event_sink: { kind: "sqlite-archive", path: "artifacts/api/api.sqlite3", status: "available" },
          checkpointer: { kind: "langgraph-sqlite", scope: "per-run", status: "available" },
          storage: { artifact_bytes: 0, artifact_files: 0, artifact_root: "artifacts/runs" },
          secure_storage_available: true,
          settings_version: 4,
          providers: [],
          audit: [],
        });
      }
      if (url.endsWith("/settings/logs")) return json([]);
      if (url.includes("/settings/sections/") && method === "PUT") {
        const body = JSON.parse(String(init?.body ?? "{}")) as {
          version: number;
          values: Record<string, unknown>;
        };
        const section = url.split("/sections/")[1];
        return json({
          ...DOCUMENT,
          version: body.version + 1,
          [section]: { ...(DOCUMENT as unknown as Record<string, object>)[section], ...body.values },
        });
      }
      return json({ error: { code: "not_found", message: url } }, 404);
    }),
  );
});

afterEach(async () => {
  await i18n.changeLanguage("en-US");
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
});

describe("SettingsView", () => {
  it("renders the settings sections and provider inventory", async () => {
    render(
      <SettingsView
        meta={null}
        apiKey="test-key-0123456789"
        onSaveKey={vi.fn()}
        onClearKey={vi.fn()}
        themeMode="system"
        setThemeMode={vi.fn()}
        languagePreference="system"
        setLanguagePreference={vi.fn()}
        onSettingsSaved={vi.fn()}
      />,
    );
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Models & Providers/i }));
    expect(await screen.findByText("Mock Provider")).toBeInTheDocument();
    expect(screen.getByText("mock-model")).toBeInTheDocument();
    // Routing eligibility and real inference verification must be separate claims.
    expect(screen.getByText("Live routing conditions met")).toBeInTheDocument();
    expect(screen.getByText("Real inference not tested")).toBeInTheDocument();
    expect(screen.getByText("Tool calling: declared, not verified")).toBeInTheDocument();
    expect(screen.queryByText("Real inference verified")).not.toBeInTheDocument();
  });

  it("opens provider and model actions without cluttering the row", async () => {
    render(
      <SettingsView
        meta={null}
        apiKey="test-key-0123456789"
        onSaveKey={vi.fn()}
        onClearKey={vi.fn()}
        themeMode="system"
        setThemeMode={vi.fn()}
        languagePreference="system"
        setLanguagePreference={vi.fn()}
        onSettingsSaved={vi.fn()}
      />,
    );
    await screen.findByRole("heading", { name: "Settings" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Models & Providers/i }));
    await screen.findByText("Mock Provider");
    await user.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByRole("menuitem", { name: "Edit provider" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Credential" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Discover models" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Actions for model mock-model" }));
    expect(screen.getByRole("menuitem", { name: "Set as default model" })).toBeDisabled();
    expect(screen.getByRole("menuitem", { name: "Test inference" })).toBeInTheDocument();
  });

  it("switches language instantly and persists the UI preference", async () => {
    const setLanguage = vi.fn((preference: string) => {
      void i18n.changeLanguage(preference);
    });
    render(
      <SettingsView
        meta={null}
        apiKey="test-key-0123456789"
        onSaveKey={vi.fn()}
        onClearKey={vi.fn()}
        themeMode="system"
        setThemeMode={vi.fn()}
        languagePreference="en-US"
        setLanguagePreference={setLanguage}
        onSettingsSaved={vi.fn()}
      />,
    );
    await screen.findByRole("heading", { name: "Settings" });
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Language"), "zh-CN");
    await waitFor(() => expect(screen.getByRole("heading", { name: "设置" })).toBeInTheDocument());
    expect(setLanguage).toHaveBeenCalledWith("zh-CN");
  });
});
