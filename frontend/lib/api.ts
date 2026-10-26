"use client";

/**
 * Thin typed client for the backend. Every call goes to this app's own origin
 * (`/api/...`) and is proxied server-side, so the browser never sees GitLab
 * credentials and never calls GitLab directly.
 */

export type AnyRecord = Record<string, any>;

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T = AnyRecord>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const detail =
      (body && (body.detail || body.error)) ||
      (typeof body === "string" ? body : "") ||
      res.statusText;
    throw new ApiError(res.status, String(detail));
  }
  return body as T;
}

export const api = {
  get: <T = AnyRecord>(p: string) => request<T>(p),
  post: <T = AnyRecord>(p: string, body?: AnyRecord) =>
    request<T>(p, { method: "POST", body: body ? JSON.stringify(body) : undefined }),
  put: <T = AnyRecord>(p: string, body?: AnyRecord) =>
    request<T>(p, { method: "PUT", body: body ? JSON.stringify(body) : undefined }),
};

export const unwrap = <T,>(v: T | { [k: string]: T }, key: string): T[] =>
  Array.isArray(v) ? (v as T[]) : ((v as any)?.[key] ?? []);

/* ------------------------------------------------------------------ types */
export interface Health {
  status: string;
  version: string;
  environment: string;
  database: string;
  database_dialect: string;
  isolation: AnyRecord;
  gitlab: AnyRecord;
  duo: AnyRecord;
  demo: AnyRecord;
  demo_mode_available: boolean;
}
export interface Project {
  id: number;
  name: string;
  path_with_namespace: string;
  default_branch: string;
  origin: string;
  web_url?: string;
  local_path?: string;
}
export interface MergeRequest {
  iid: number;
  title: string;
  source_branch: string;
  target_branch: string;
  state?: string;
  web_url?: string;
  changed_files?: number;
}
export interface Audit {
  id: number;
  public_id?: string;
  status: string;
  stage: string;
  progress_pct: number;
  mode: string;
  engine: string;
  merge_request_title?: string;
  source_branch?: string;
  target_branch?: string;
  source_commit_sha?: string;
  target_commit_sha?: string;
  findings_count?: number;
  patches_count?: number;
  test_runs_count?: number;
  outcome?: string;
  error_detail?: string;
  duo_availability?: AnyRecord;
  created_at?: string;
  started_at?: string;
  completed_at?: string;
}
export interface Evidence {
  kind: string;
  description: string;
  file_path: string;
  line_start?: number;
  line_end?: number;
  content?: string;
  note?: string;
  check?: string;
}
export interface Finding {
  id: number | string;
  public_id: string;
  rule_id?: string;
  category: string;
  severity: string;
  title: string;
  file_path: string;
  line_start?: number;
  line_end?: number;
  symbol?: string;
  verification_status: string;
  repro_outcome?: string;
  expected_behavior?: string;
  suspected_behavior?: string;
  evidence?: Evidence[];
  facts?: string[];
  hypotheses?: string[];
  missing_information?: string[];
  investigation?: AnyRecord;
  confidence?: AnyRecord;
  repro_test_file?: string;
  repro_expectations?: AnyRecord[];
}
export interface Patch {
  id: number | string;
  public_id: string;
  audit_id: number;
  finding_id?: number;
  file_path: string;
  original_code: string;
  proposed_code: string;
  patch_text: string;
  explanation: string;
  side_effects?: string[];
  related_tests?: string[];
  approval_status: string;
  decided_by?: string;
  decision_note?: string;
  decided_at?: string;
  verification_outcome: string;
  verification_detail?: AnyRecord;
  engine?: string;
}
export interface TestRun {
  id: number;
  public_id: string;
  finding_id?: number;
  patch_id?: number;
  code_revision: string;
  revision_label: string;
  test_type: string;
  execution_status: string;
  exit_code?: number;
  tests_total: number;
  tests_passed: number;
  tests_failed: number;
  tests_skipped: number;
  tests_errored: number;
  intended_tests_executed: boolean;
  intended_assertion_failed?: boolean;
  assertion_signature?: string;
  isolation_level: string;
  network_disabled: boolean;
  duration_ms: number;
  test_file_name: string;
  patch_applied: boolean;
  sanitized_output?: string;
  failures?: AnyRecord[];
  test_source?: string;
  expectations?: AnyRecord[];
  intended_ids?: string[];
  executed_ids?: string[];
}
export interface AuditEvent {
  id: number;
  seq: number;
  agent_role: string;
  event_type: string;
  status: string;
  message: string;
  payload?: AnyRecord;
  engine?: string;
  created_at?: string;
}
export interface Verification {
  patch_id: number;
  outcome: string;
  summary: string;
  reasons: string[];
  remaining_concerns: string[];
  intended_tests_executed: boolean;
  regressions: AnyRecord[];
  runs: AnyRecord[];
  scope_statement: string;
}
export interface Report {
  report_id: number;
  public_id: string;
  audit_id: number;
  outcome: string;
  outcome_explanation: string;
  summary: string;
  created_at: string;
  report: AnyRecord;
}

/* ------------------------------------------------------------- status meta */
export const STATUS_META: Record<string, { tone: Tone; label: string }> = {
  queued: { tone: "muted", label: "queued" },
  fetching_context: { tone: "info", label: "fetching change" },
  analyzing: { tone: "info", label: "analyzing" },
  investigating: { tone: "info", label: "investigating" },
  generating_tests: { tone: "info", label: "generating tests" },
  proposing: { tone: "info", label: "proposing patch" },
  awaiting_approval: { tone: "warn", label: "awaiting approval" },
  verifying: { tone: "info", label: "verifying" },
  reporting: { tone: "info", label: "reporting" },
  completed: { tone: "ok", label: "completed" },
  completed_with_errors: { tone: "warn", label: "completed with errors" },
  failed: { tone: "bad", label: "failed" },
};

export type Tone = "ok" | "bad" | "warn" | "info" | "muted" | "vio";

export const VERIFICATION_META: Record<string, Tone> = {
  fix_verified: "ok",
  verified_for_tested_behavior: "ok",
  reproduced: "warn",
  fix_proposed: "info",
  hypothesis: "muted",
  evidenced: "info",
  not_reproduced: "muted",
  inconclusive: "muted",
  execution_error: "bad",
  fix_verification_failed: "bad",
  regression_detected: "bad",
  not_attempted: "muted",
};

export const SEVERITY_TONE: Record<string, Tone> = {
  critical: "bad",
  high: "bad",
  medium: "warn",
  low: "muted",
  info: "info",
};

export function timeAgo(iso?: string): string {
  if (!iso) return "—";
  const then = new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime();
  if (Number.isNaN(then)) return iso;
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const short = (sha?: string) => (sha ? sha.slice(0, 12) : "—");

/** Render-only coercion: the raw report column nests objects where the
 *  typed ReportResponse uses strings. Never pass an object to JSX. */
export const asText = (v: unknown): string => {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
};
