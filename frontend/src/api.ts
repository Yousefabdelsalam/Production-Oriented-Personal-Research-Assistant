import type {
  AskResponse, EvalRun, Execution, Finding, Health, Knowledge, MemoryOp, Passage, Source, SourceDetail, SubmitResult, SummaryDiff,
  SummaryVersion, SummaryVersionRow, Turn, Conversation, Workspace, WorkflowDef, Relationship,
} from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

const TOKEN_KEY = "pra.api_token";
export const getToken = (): string => {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
};
export const setToken = (t: string): void => {
  try {
    localStorage.setItem(TOKEN_KEY, t);
  } catch {
    /* storage may be blocked; token then lives only for this page load */
  }
};

/** Turn any FastAPI/AppError payload into one readable message. */
export function errorMessage(body: unknown, fallback: string): { code: string; message: string } {
  const b = body as { error?: { code?: string; message?: string }; detail?: unknown } | null;
  if (b?.error) return { code: b.error.code ?? "error", message: b.error.message ?? fallback };
  const d = b?.detail as { code?: string; message?: string } | string | { msg?: string }[] | undefined;
  if (typeof d === "string") return { code: "error", message: d };
  if (Array.isArray(d)) return { code: "validation", message: d.map((x) => x.msg).filter(Boolean).join("; ") || fallback };
  if (d && typeof d === "object") return { code: d.code ?? "error", message: d.message ?? fallback };
  return { code: "error", message: fallback };
}

async function request<T>(method: string, path: string, body?: unknown, form?: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`/api${path}`, { method, headers, body: form ?? (body !== undefined ? JSON.stringify(body) : undefined) });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the backend. Is it running on port 8000?");
  }
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const { code, message } = errorMessage(data, `${res.status} ${res.statusText}`);
    throw new ApiError(res.status, code, message);
  }
  return data as T;
}

const get = <T,>(p: string) => request<T>("GET", p);
const post = <T,>(p: string, b?: unknown) => request<T>("POST", p, b ?? {});

export const api = {
  health: () => get<Health>("/health"),
  workflows: () => get<WorkflowDef[]>("/workflows"),
  workspaces: () => get<Workspace[]>("/workspaces"),
  workspace: (id: string) => get<Workspace>(`/workspaces/${id}`),
  createWorkspace: (name: string, description: string) => post<Workspace>("/workspaces", { name, description }),

  sources: (ws: string, q = "", status = "") =>
    get<Source[]>(`/workspaces/${ws}/sources?${new URLSearchParams({ ...(q ? { q } : {}), ...(status ? { status } : {}) })}`),
  source: (ws: string, id: string) => get<SourceDetail>(`/workspaces/${ws}/sources/${id}`),
  sourceText: (ws: string, id: string) => get<{ text: string; version: number; meta: Record<string, unknown>; quality: Record<string, unknown> }>(`/workspaces/${ws}/sources/${id}/text`),
  sourcePassages: (ws: string, id: string) => get<Passage[]>(`/workspaces/${ws}/sources/${id}/passages`),
  sourceFindings: (ws: string, id: string) => get<Finding[]>(`/workspaces/${ws}/sources/${id}/findings`),
  passage: (ws: string, id: string) => get<Passage>(`/workspaces/${ws}/passages/${id}`),
  uploadPdf: (ws: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<SubmitResult>("POST", `/workspaces/${ws}/sources/pdf`, undefined, fd);
  },
  addUrl: (ws: string, url: string, refresh = false) => post<SubmitResult>(`/workspaces/${ws}/sources/url`, { url, refresh }),
  addNote: (ws: string, title: string, text: string) => post<SubmitResult>(`/workspaces/${ws}/sources/note`, { title, text }),
  editNote: (ws: string, id: string, title: string, text: string) => request<SubmitResult>("PUT", `/workspaces/${ws}/sources/${id}/note`, { title, text }),
  retrySource: (ws: string, id: string) => post<{ execution_id: string }>(`/workspaces/${ws}/sources/${id}/retry`),

  conversations: (ws: string) => get<Conversation[]>(`/workspaces/${ws}/conversations`),
  turns: (ws: string, cid: string) => get<Turn[]>(`/workspaces/${ws}/conversations/${cid}/turns`),
  ask: (ws: string, question: string, conversation_id?: string) => post<AskResponse>(`/workspaces/${ws}/ask`, { question, conversation_id }),

  knowledge: (ws: string) => get<Knowledge>(`/workspaces/${ws}/knowledge`),
  setRelationship: (ws: string, id: string, status: string) => request<Relationship>("PATCH", `/workspaces/${ws}/relationships/${id}`, { status }),
  synthesize: (ws: string) => post<{ execution_id: string }>(`/workspaces/${ws}/synthesize`),

  summary: (ws: string) => get<SummaryVersion>(`/workspaces/${ws}/summary`),
  summaryVersions: (ws: string) => get<SummaryVersionRow[]>(`/workspaces/${ws}/summary/versions`),
  summaryVersion: (ws: string, n: number) => get<SummaryVersion>(`/workspaces/${ws}/summary/versions/${n}`),
  summaryDiff: (ws: string, from: number, to: number) => get<SummaryDiff>(`/workspaces/${ws}/summary/diff?from=${from}&to=${to}`),
  updateSummary: (ws: string) =>
    post<{ execution_id: string; status: string; result?: { outcome?: string; created_version?: number | null; warnings?: string[]; deferred_items?: number }; error?: string | null }>(
      `/workspaces/${ws}/summary/update?wait=true`),
  rollbackSummary: (ws: string, version: number) =>
    post<{ execution_id: string; status: string; result?: { created_version?: number | null }; error?: string | null }>(`/workspaces/${ws}/summary/rollback?wait=true`, { version }),
  consolidated: (ws: string) => get<{ available: boolean; view: { content: string; is_stale: boolean; last_refreshed_at: string | null } | null; disclaimer: string }>(`/workspaces/${ws}/memory/consolidated`),
  consolidate: (ws: string) => post<{ execution_id: string }>(`/workspaces/${ws}/memory/consolidate`),
  memoryOps: (ws: string) => get<MemoryOp[]>(`/workspaces/${ws}/memory/operations`),

  executions: (params: Record<string, string> = {}) => get<Execution[]>(`/executions?${new URLSearchParams(params)}`),
  execution: (id: string) => get<Execution>(`/executions/${id}`),
  resume: (id: string) => post<{ execution_id: string }>(`/executions/${id}/resume`),

  evaluations: () => get<EvalRun[]>("/evaluations"),
  runEvaluation: () => post<{ execution_id: string; note: string }>("/evaluations/run"),
};
