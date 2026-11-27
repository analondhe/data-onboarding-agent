// Client for the FastAPI service. The web app talks to nothing else.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export type RunStatus =
  | "created"
  | "profiling"
  | "mapping"
  | "awaiting_review"
  | "building"
  | "done"
  | "failed";

export interface FileInfo {
  id: number;
  filename: string;
  row_count: number;
  profiled: boolean;
}

export type RunType = "onboarding" | "recurring";

export type DriftCheck =
  | "column_added"
  | "column_missing"
  | "column_renamed"
  | "format_change"
  | "new_category"
  | "null_rate_jump"
  | "volume_anomaly";

export type DriftAction = "propose" | "block" | "warn";

/** One difference between a recurring load's file and the saved source. */
export interface Finding {
  check: DriftCheck;
  source_column: string | null;
  target_column: string | null;
  action: DriftAction;
  message: string;
  details: Record<string, unknown>;
}

export interface Run {
  id: string;
  customer_name: string;
  target_schema: string;
  status: RunStatus;
  phase: string;
  steps: number;
  pending_reviews: number;
  busy: boolean;
  error: string | null;
  model_name: string | null;
  trace_id: string | null;
  files: FileInfo[];
  created_at: string;
  updated_at: string;
  run_type: RunType;
  contract_id: number | null;
  drift: Finding[] | null; // null until drift checks ran (always null for onboarding runs)
  saved_contract_id: number | null; // contract version saved from this (done, onboarding) run
}

export interface SchemaColumn {
  name: string;
  type: string;
  required: boolean;
  unique: boolean;
  allowed_values?: string[];
  description: string;
}

export interface TargetSchema {
  table: string;
  description: string;
  columns: SchemaColumn[];
}

export interface TopValue {
  value: string;
  count: number;
}

export interface Mapping {
  id: number;
  file_id: number;
  source_column: string | null;
  target_table: string;
  target_column: string;
  transform_sql: string | null;
  confidence: number | null;
  reason: string;
  status: "proposed" | "approved" | "edited" | "rejected";
  needs_review: boolean;
  review_reason: string | null;
  sample_values: TopValue[];
}

export interface Defect {
  id: number;
  file_id: number;
  column_name: string | null;
  target_column: string | null;
  defect_type: string;
  affected_rows: number | null;
  description: string;
  proposed_fix: string;
  status: string;
}

export type Action = "approve" | "edit" | "reject";

export interface Decision {
  mapping_id: number;
  action: Action;
  transform_sql?: string;
}

export interface ReviewResult {
  pending_reviews: number;
  building: boolean;
  run: Run;
}

export interface NodeResult {
  name: string;
  kind: string;
  status: string;
  failures: number | null;
  message: string | null;
}

export interface FileAccounting {
  file_id: string;
  filename: string | null;
  rows_in: number;
  loaded: number;
  rejected: number;
  reject_reasons: Record<string, number>;
  balanced: boolean;
}

/** Rows upserted into the source's persistent table. */
export interface LoadResult {
  table: string;
  inserted: number;
  updated: number;
  total_rows: number;
}

export interface BuildReport {
  status: string;
  repairs: number;
  dbt: {
    success: boolean;
    models_passed: number;
    models_failed: number;
    tests_passed: number;
    tests_failed: number;
    elapsed_seconds: number;
    results: NodeResult[];
    error: string | null;
  };
  row_accounting: FileAccounting[];
  load?: LoadResult;
}

export interface Report {
  run: Run;
  duration_seconds: number | null;
  model_calls: number;
  tool_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  trace_id: string | null;
  trace_url: string | null;
  build: BuildReport | null;
}

export interface ContractSummary {
  id: number;
  customer_name: string;
  source_name: string;
  target_schema: string;
  version: number;
  created_at: string;
  fingerprint: string;
  loads: number;
  last_load: { run_id: string; status: RunStatus; created_at: string } | null;
  table_rows: number | null;
}

export interface ContractMapping {
  source_column: string;
  target_column: string;
  transform_sql: string | null;
}

export interface ContractVersion {
  id: number;
  version: number;
  created_at: string;
  created_from_run_id: string;
}

export interface ContractOut extends ContractSummary {
  created_from_run_id: string;
  row_count: number;
  mappings: ContractMapping[];
  columns: { name: string; inferred_type: string }[];
  versions: ContractVersion[];
  recent_loads: Run[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const UNREACHABLE = `Cannot reach the API at ${API_URL}. Is the stack running (make up)?`;

function parseJson(text: string): unknown {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

/** FastAPI errors are {"detail": "text"} or {"detail": [{loc, msg}, ...]} for invalid bodies. */
function detailMessage(body: unknown, fallback: string): string {
  if (!body || typeof body !== "object" || !("detail" in body)) return fallback;
  const { detail } = body;
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return fallback;
  return detail
    .map((issue: unknown) => {
      if (!issue || typeof issue !== "object" || !("msg" in issue)) return String(issue);
      const loc = "loc" in issue && Array.isArray(issue.loc) ? issue.loc : [];
      const field = loc.slice(1).join(".");
      return field ? `${field}: ${String(issue.msg)}` : String(issue.msg);
    })
    .join("; ");
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, { ...init, cache: "no-store" });
  } catch {
    throw new ApiError(0, UNREACHABLE);
  }
  const body = parseJson(await response.text());
  if (!response.ok) {
    throw new ApiError(response.status, detailMessage(body, `${response.status} ${response.statusText}`));
  }
  return body as T;
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  schemas: () => request<TargetSchema[]>("/schemas"),
  runs: () => request<Run[]>("/runs"),
  run: (id: string) => request<Run>(`/runs/${id}`),
  createRun: (customer_name: string, target_schema: string) =>
    postJson<Run>("/runs", { customer_name, target_schema }),
  start: (id: string) => request<Run>(`/runs/${id}/start`, { method: "POST" }),
  mappings: (id: string) => request<Mapping[]>(`/runs/${id}/mappings`),
  defects: (id: string) => request<Defect[]>(`/runs/${id}/defects`),
  review: (id: string, decisions: Decision[]) =>
    postJson<ReviewResult>(`/runs/${id}/review`, { decisions, build: true }),
  report: (id: string) => request<Report>(`/runs/${id}/report`),
  contracts: () => request<ContractSummary[]>("/contracts"),
  contract: (id: number | string) => request<ContractOut>(`/contracts/${id}`),
  saveContract: (runId: string, source_name: string) =>
    postJson<ContractOut>(`/runs/${runId}/contract`, { source_name }),
};

/** POST one file as multipart with progress (fetch cannot report upload progress). */
function upload(path: string, field: string, file: File, onProgress: (fraction: number) => void): Promise<Run> {
  const { promise, resolve, reject } = Promise.withResolvers<Run>();
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `${API_URL}${path}`);
  xhr.upload.onprogress = (event) => {
    if (event.lengthComputable) onProgress(event.loaded / event.total);
  };
  xhr.onload = () => {
    const body = parseJson(xhr.responseText);
    if (xhr.status >= 200 && xhr.status < 300) resolve(body as Run);
    else reject(new ApiError(xhr.status, detailMessage(body, `Upload failed (${xhr.status})`)));
  };
  xhr.onerror = () => reject(new ApiError(0, UNREACHABLE));
  const form = new FormData();
  form.append(field, file, file.name);
  xhr.send(form);
  return promise;
}

/** Add one file to an onboarding run. */
export function uploadFile(runId: string, file: File, onProgress: (fraction: number) => void): Promise<Run> {
  return upload(`/runs/${runId}/files`, "files", file, onProgress);
}

/** Start a recurring load of a saved source with one new file. */
export function uploadLoad(
  contractId: number | string,
  file: File,
  onProgress: (fraction: number) => void,
): Promise<Run> {
  return upload(`/contracts/${contractId}/loads`, "file", file, onProgress);
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
