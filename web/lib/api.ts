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
};

/** Upload one file with progress (fetch cannot report upload progress). */
export function uploadFile(
  runId: string,
  file: File,
  onProgress: (fraction: number) => void,
): Promise<Run> {
  const { promise, resolve, reject } = Promise.withResolvers<Run>();
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `${API_URL}/runs/${runId}/files`);
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
  form.append("files", file, file.name);
  xhr.send(form);
  return promise;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
