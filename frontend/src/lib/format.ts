import type { RunStatus } from "../api/types";

export const statusLabels: Record<RunStatus, string> = {
  pending: "等待中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function formatPercent(value: number | null | undefined, digits = 1): string {
  return value == null || Number.isNaN(value) ? "—" : `${value.toFixed(digits)}%`;
}

export function formatLatency(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(1)} ms`;
}

export function formatTokens(value: number | null | undefined): string {
  if (value == null) return "—";
  return new Intl.NumberFormat("zh-CN", { notation: value >= 10_000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value);
}

export function formatTokenTotal(
  inputTokens: number | null | undefined,
  outputTokens: number | null | undefined,
): string {
  return inputTokens == null || outputTokens == null
    ? "—"
    : formatTokens(inputTokens + outputTokens);
}

export function formatCost(value: number | null | undefined): string {
  if (value == null) return "—";
  return `$${value.toFixed(value < 0.01 ? 6 : 4)}`;
}

export function formatUtc8(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const formatted = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
  return `${formatted} (UTC+8)`;
}

export function shortHash(value: string | null | undefined, length = 10): string {
  return value ? `${value.slice(0, length)}…` : "—";
}

export function displayAnswer(value: unknown): string {
  if (value == null) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}
