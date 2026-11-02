export type SourceStatus = "queued" | "processing" | "completed" | "partial" | "failed";
export type ExecStatus = "queued" | "running" | "succeeded" | "failed" | "interrupted" | "cancelled";
export type AnswerMode = "supported" | "partial" | "conflicting" | "insufficient";
export type ClaimKind = "source_stated" | "user_note" | "inference" | "uncertain";

export interface Health {
  status: string;
  database: { ok: boolean };
  hindsight: { ok: boolean; version: string | null; detail: string | null; base_url: string };
  generation_mode: "llm" | "extractive";
  llm_provider: string;
  llm_model: string | null;
  embedding: string;
  worker: { enabled: boolean };
  notes: string[];
}

export interface Workspace {
  id: string;
  slug: string;
  name: string;
  description: string;
  bank_id: string;
  created_at: string;
  updated_at: string;
  summary_version: number;
  sources: number;
  sources_by_status: Record<string, number>;
  passages: number;
  findings: number;
  relationships: number;
  jobs_active: number;
  jobs_failed: number;
}

export interface Source {
  id: string;
  workspace_id: string;
  kind: "pdf" | "url" | "note";
  title: string;
  url: string | null;
  status: SourceStatus;
  version: number;
  last_stage: string | null;
  error: string | null;
  duplicate_of: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceDetail extends Source {
  versions: { version: number; content_hash: string; size_bytes: number; mime: string | null; chars: number; meta: Record<string, unknown>; created_at: string }[];
  passages: number;
  flagged_passages: number;
  findings: { validated: number; rejected: number; retained: number };
  latest_execution: Execution | null;
}

export interface SubmitResult {
  source_id: string;
  execution_id: string | null;
  duplicate: boolean;
  state: string;
  message: string;
}

export interface Passage {
  id: string;
  source_id: string;
  source_title: string | null;
  source_kind: string | null;
  text: string;
  page_start: number | null;
  page_end: number | null;
  section: string | null;
  char_start: number;
  char_end: number;
  suspicious: boolean;
  flags: string[];
  context_before?: string | null;
  context_after?: string | null;
}

export interface Finding {
  id: string;
  source_id: string;
  passage_id: string;
  kind: string;
  statement: string;
  quote: string;
  epistemic: ClaimKind;
  concepts: string[];
  page: number | null;
  section: string | null;
  confidence: number;
  extraction_method: string;
  validated: boolean;
  validation: { ok?: boolean; reasons?: string[]; retention_rejected?: string[] };
  memory_state: string;
}

export interface Citation {
  passage_id: string;
  source_id: string;
  source_title: string;
  source_kind: string;
  page: number | null;
  section: string | null;
  quote: string;
  verified: boolean;
}

export interface Claim {
  id: string;
  text: string;
  kind: ClaimKind;
  citations: Citation[];
  memory_refs: string[];
  support: number | null;
  status: "supported" | "unsupported" | "flagged";
  notes: string[];
}

export interface ConflictNote {
  description: string;
  relationship_id: string | null;
  status: string;
  sides: { source_id: string; source_title: string; passage_id: string; page: number | null; text: string; cited: boolean }[];
}

export interface Answer {
  mode: AnswerMode;
  answer: string;
  claims: Claim[];
  conflicts: ConflictNote[];
  memory_used: { id: string; text: string; source_ids: string[]; role: string }[];
  warnings: string[];
  plan: { intent?: string; rationale?: string[]; evidence_k?: number; source_ids?: string[]; memory_budget?: string };
  generation_mode: string;
  evidence_considered: number;
  dropped_claims: number;
}

export interface AskResponse {
  execution_id: string;
  conversation_id: string;
  answer: Answer;
  experience: { retained: boolean; reason?: string } | null;
}

export interface Turn {
  id: string;
  question: string;
  answer: Answer;
  execution_id: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
}

export interface Step {
  id: number;
  stage: string;
  attempt: number;
  status: "running" | "succeeded" | "failed" | "skipped";
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  detail: Record<string, unknown>;
  error: string | null;
}

export interface Execution {
  id: string;
  workflow: string;
  workspace_id: string | null;
  source_id: string | null;
  correlation_id: string;
  status: ExecStatus;
  current_stage: string | null;
  failed_stage: string | null;
  trigger: Record<string, unknown>;
  error: string | null;
  resume_count: number;
  tokens_in: number;
  tokens_out: number;
  model_calls: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  steps?: Step[];
  result?: Record<string, unknown>;
}

export interface WorkflowDef {
  name: string;
  start: string;
  stages: { name: string; next: string[]; retries: number }[];
}

export interface SummaryCitation {
  passage_id: string;
  source_id: string;
  source_title: string;
  page: number | null;
  section: string | null;
  quote: string;
}

export interface SummaryItem {
  key: string;
  section: string;
  text: string;
  kind: string;
  origin: string;
  status: "new" | "established" | "contested" | "hypothesis";
  finding_ids: string[];
  source_ids: string[];
  citations: SummaryCitation[];
  first_version: number;
}

export interface SummaryVersion {
  version: number;
  kind?: string;
  parent_version?: number | null;
  created_at?: string;
  markdown?: string;
  message?: string;
  change?: {
    summary?: string;
    added?: { key: string; section: string; text: string }[];
    removed?: { key: string; section: string; text: string; reason: string }[];
    changed?: { key: string; before: string; after: string }[];
    new_source_ids?: string[];
  };
  content?: { overview: string; overview_mode: string; sections: Record<string, SummaryItem[]>; source_ids: string[]; stats: Record<string, unknown> };
}

export interface SummaryVersionRow {
  version: number;
  kind: string;
  parent_version: number | null;
  created_at: string;
  summary: string | null;
  sources: number;
  execution_id: string | null;
}

export interface SummaryDiff {
  from: number;
  to: number;
  added: { key: string; section: string; text: string }[];
  removed: { key: string; section: string; text: string; reason: string }[];
  changed: { key: string; section: string; fields: string[]; before: string; after: string }[];
  unchanged: number;
  meaningful: boolean;
}

export interface Relationship {
  id: string;
  predicate: string;
  status: "supported" | "hypothesis" | "rejected";
  rationale: string;
  detected_by: string;
  a: RelSide;
  b: RelSide;
}
export interface RelSide {
  finding_id: string;
  statement: string;
  quote: string;
  page: number | null;
  passage_id: string;
  source_id: string;
  source_title: string;
}
export interface Knowledge {
  concepts: { name: string; finding_count: number; source_count: number; sources: { id: string; title: string }[] }[];
  relationships: Relationship[];
  stats: { findings: number; sources: number; relationships: number; note: string };
}

export interface Gate {
  name: string;
  op: string;
  threshold: number;
  value: number | null;
  passed: boolean | null;
  skipped: boolean;
  reason?: string;
}
export interface EvalRun {
  id: string;
  started_at: string;
  finished_at: string | null;
  passed: boolean | null;
  gates: Gate[];
  environment: { generation_mode: string; hindsight_live: boolean; notes: string[] };
  metrics: Record<string, Record<string, unknown>>;
}

export interface MemoryOp {
  id: string;
  execution_id: string | null;
  source_id: string | null;
  op: string;
  bank_id: string;
  status: string;
  request: Record<string, unknown>;
  response: Record<string, unknown>;
  duration_ms: number | null;
  created_at: string;
}
