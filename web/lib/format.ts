import type { RunStatus } from "./api";

export const STATUS_LABEL: Record<RunStatus, string> = {
  created: "Not started",
  profiling: "Profiling",
  mapping: "Mapping",
  awaiting_review: "Needs review",
  building: "Building",
  done: "Done",
  failed: "Failed",
};

/** Plain-language names for the agent's defect types. */
export const DEFECT_LABEL: Record<string, string> = {
  duplicate_rows: "Duplicate rows",
  null_variants: "Empty values written in different ways",
  type_noise: "Badly formatted values",
  value_drift: "Inconsistent category values",
  mixed_date_formats: "Mixed date formats",
  merged_column: "Two fields in one column",
  missing_required_column: "Required column missing",
};

export function formatNumber(n: number): string {
  return n.toLocaleString("en-US");
}

export function formatPercent(fraction: number): string {
  return `${Math.round(fraction * 100)}%`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "–";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds - minutes * 60)} s`;
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}
