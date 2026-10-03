export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL !== undefined
    ? process.env.NEXT_PUBLIC_API_URL
    : 'http://127.0.0.1:8000';

/* ── Shared API types (kept in sync with backend/app/api.py) ─────────── */

export interface CaseRow {
  case_id: string;
  invoice_id: string;
  canonical_invoice_id?: string;
  vendor_code: string;
  invoice_date?: string;
  ledger_ref?: string | null;
  gst_ref?: string | null;
  issue_type: string;
  issue_label: string;
  category: string;
  severity: string;
  match_confidence: number;
  confidence_score: number;
  confidence_breakdown?: string;
  evidence: string;
  likely_reason: string;
  financial_exposure: number;
  financial_impact_type?: string;
  financial_impact_amount?: number;
  financial_impact_explanation?: string;
  priority_score: number;
  priority_band: string;
  final_priority_score?: number;
  ranking_explanation?: string;
  feedback_adjustment?: number;
  feedback_source?: string;
  ml_p_confirmed?: number | null;
  ml_boost?: number;
  recommended_action: string;
  status: string;
  human_decision: string;
  review_notes: string;
  pattern_count?: number;
  pattern_ids?: string;
  pattern_confidence?: number;
  pattern_explanation?: string;
  pattern_signal?: string;
  anomaly_score?: number;
  ml_anomaly?: number | boolean;
  anomaly_reason?: string;
  investigation_signal?: string;
  root_cause?: string;
  root_cause_detail?: string;
  investigation_explanation?: string;
  evidence_detail?: string;
  financial_exposure_refined?: number;
  pattern_context?: string;
  ml_context?: string;
  recommended_action_refined?: string;
  review_question?: string;
  gst_rule_context?: string;
  gst_rule_title?: string;
  gst_rule_source?: string;
  gst_rule_url?: string;
  gst_rule_relevance?: number;
  gst_rule_disclaimer?: string;
  [key: string]: unknown;
}

export interface CaseDetail {
  case: CaseRow;
  reviews: ReviewRow[];
  audit: AuditRow[];
}

export interface ReviewRow {
  case_id: string;
  decision: string;
  reviewer: string;
  notes: string | null;
  decided_at: string;
  previous_status?: string | null;
}

export interface AuditRow {
  id: number;
  case_id: string;
  event_type: string;
  actor: string;
  event_data: string | null;
  created_at: string;
}

export interface PatternRow {
  pattern_id: string;
  pattern_type: string;
  pattern_label: string;
  vendor_code: string;
  issue_type: string;
  period: string;
  occurrence_count: number;
  affected_vendors: number;
  affected_invoices: number;
  concentration: number;
  pattern_confidence: number;
  financial_exposure: number;
  trend?: string;
  first_seen?: string | null;
  last_seen?: string | null;
  member_case_ids?: string;
  explanation: string;
  [key: string]: unknown;
}

export interface StageResult {
  name: string;
  rows: number;
  elapsed_s: number;
  detail: string;
  error?: string;
}

export interface PipelineStatus {
  state: 'idle' | 'running' | 'ok' | 'failed' | 'blocked';
  started_at?: string | null;
  finished_at?: string | null;
  current_stage?: string | null;
  stages: StageResult[];
  error?: string;
  failed_stage?: string;
  ok?: boolean | null;
  carried_over_reviews?: number;
  orphaned_reviews?: number;
  populated: boolean;
  total_cases: number;
  task_done?: boolean | null;
}

export interface PipelineRunResult {
  ok: boolean;
  total_elapsed_s: number;
  error: string;
  failed_stage?: string;
  carried_over_reviews?: number;
  orphaned_reviews?: number;
  stages: StageResult[];
}

export interface UploadPreviewSource {
  source: string;
  rows_in: number;
  rows_out: number;
  columns_mapped: Record<string, string>;
  columns_unmapped: string[];
  missing_required_columns: string[];
  rejected_count: number;
  rejected_sample: { row: number; reason: string }[];
  warnings: string[];
  errors: string[];
}

export interface UploadPreview {
  ok: boolean;
  errors: string[];
  sources: Record<string, UploadPreviewSource>;
}

export interface UploadResult {
  uploaded: Record<string, { rows: number; columns_mapped: Record<string, string>; rejected: number }>;
  preview: UploadPreview;
  backup_dir: string | null;
  pipeline_started: boolean;
  status_url: string;
}

export interface MetricsResponse {
  available: boolean;
  reason?: string;
  metrics: {
    discrepancy_type: string;
    true_positive: number;
    false_positive: number;
    false_negative: number;
    precision: number;
    recall: number;
    f1: number;
  }[];
}

export interface FeedbackPanel {
  reviewed_cases: number;
  by_decision?: Record<string, number>;
  by_issue?: Record<string, Record<string, number>>;
  stage_b_active: boolean;
  stage_b_min_labels: number;
}

export interface DashboardData {
  cases: {
    total: number;
    open_cases: number;
    confirmed_cases: number;
    rejected_cases: number;
    needs_review: number;
    total_exposure: number;
    critical_priority: number;
    high_priority: number;
    medium_priority: number;
    low_priority: number;
  };
  issue_breakdown: { issue_type: string; count: number }[];
  review_breakdown: { decision: string; count: number }[];
  top_vendors: { vendor_code: string; case_count: number; total_exposure: number }[];
  severity_breakdown: { severity: string; count: number }[];
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(options?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status} ${text}`);
  }
  return res.json();
}

export const api = {
  // Health
  health: () => request<{ status: string }>('/api/health'),

  // Dashboard
  dashboard: () => request<DashboardData>('/api/dashboard'),

  // Cases
  cases: (params = '') => request<{ total: number; limit: number; offset: number; items: CaseRow[] }>(`/api/cases${params ? `?${params}` : ''}`),
  caseById: (id: string) => request<CaseDetail>(`/api/cases/${encodeURIComponent(id)}`),
  review: (id: string, decision: string, notes: string, reviewer: string) =>
    request<{ case_id: string; decision: string; status: string }>(`/api/cases/${encodeURIComponent(id)}/review`, {
      method: 'POST',
      body: JSON.stringify({ decision, notes, reviewer }),
    }),

  // Patterns
  patterns: () => request<{ items: PatternRow[] }>('/api/patterns'),
  pattern: (id: string) => request<{ pattern: PatternRow; memberships: { pattern_id: string; case_id: string; membership_reason: string }[]; cases: CaseRow[] }>(`/api/patterns/${encodeURIComponent(id)}`),

  // Audit
  audit: (caseId: string) => request<{ case_id: string; events: AuditRow[] }>(`/api/audit/${encodeURIComponent(caseId)}`),

  // Pipeline
  runPipeline: () => request<{ started: boolean; state: string; status_url: string }>('/api/pipeline/run', { method: 'POST' }),
  pipelineStatus: () => request<PipelineStatus>('/api/pipeline/status'),
  pipelineRuns: () => request<{ runs: PipelineRunResult[] }>('/api/pipeline/runs'),

  // Upload
  uploadPreview: (files: { invoices: File; ledger: File; gst: File }) => {
    const form = new FormData();
    form.append('invoices', files.invoices);
    form.append('ledger', files.ledger);
    form.append('gst', files.gst);
    return fetch(`${API_BASE}/api/upload/preview`, { method: 'POST', body: form }).then((res) => {
      if (!res.ok) return res.json().then((body) => body as UploadPreview & { detail?: unknown });
      return res.json() as Promise<UploadPreview>;
    });
  },
  upload: (files: { invoices: File; ledger: File; gst: File; vendors?: File | null }) => {
    const form = new FormData();
    form.append('invoices', files.invoices);
    form.append('ledger', files.ledger);
    form.append('gst', files.gst);
    if (files.vendors) form.append('vendors', files.vendors);
    return fetch(`${API_BASE}/api/upload`, { method: 'POST', body: form }).then(async (res) => {
      const body = await res.json();
      if (!res.ok) throw new Error(body?.detail?.message || body?.detail || `Upload failed (${res.status})`);
      return body as UploadResult;
    });
  },

  // Feedback
  feedback: () => request<FeedbackPanel>('/api/feedback'),
  feedbackRerank: () => request<{ [key: string]: unknown }>('/api/feedback/rerank', { method: 'POST' }),

  // Export
  exportCases: (format: 'csv' | 'json' = 'csv') =>
    `${API_BASE}/api/export/cases?format=${format}`,
  exportAudit: () => `${API_BASE}/api/export/audit`,

  // Metrics
  metrics: () => request<MetricsResponse>('/api/metrics'),

  // Rules
  searchRules: (q: string) => request<{ query: string; items: { id: string; title: string; text: string; source: string; url: string; lexical_score: number }[] }>(`/api/rules/search?q=${encodeURIComponent(q)}`),
};

/* ── Formatting helpers ───────────────────────────── */

export function formatMoney(n: number | string | null | undefined): string {
  const v = Number(n || 0);
  return `₹${v.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

export function formatPercent(n: number | string | null | undefined): string {
  return `${Number(n || 0).toFixed(0)}%`;
}

export function humanIssue(s: string): string {
  return (s || '').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

export function priorityColor(band: string): string {
  const b = (band || '').toUpperCase();
  if (b === 'CRITICAL') return 'critical';
  if (b === 'HIGH') return 'high';
  if (b === 'MEDIUM') return 'medium';
  return 'low';
}

export function statusColor(status: string): string {
  const s = (status || '').toUpperCase();
  if (s === 'CONFIRMED') return 'confirmed';
  if (s === 'REJECTED') return 'rejected';
  if (s === 'NEEDS_REVIEW') return 'needs_review';
  return 'open';
}
