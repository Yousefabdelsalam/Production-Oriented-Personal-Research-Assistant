# Architecture

## Goals
Accumulate research knowledge over time, keep **original evidence** separate from **learned memory**, answer with
verifiable citations, and make every consequential step observable and recoverable.

## Components
```
 React UI ──HTTP──► FastAPI (backend/app/api)
                       │
                       ▼
              Harness (backend/app/harness)
   ┌───────────────────┴────────────────────────────────────────────┐
   │ engine: persisted stage runner  policies/permissions  injection │
   │ workflows: ingestion · qa · synthesis · summary · memory · eval │
   └───────┬──────────────┬─────────────────┬──────────────┬────────┘
           ▼              ▼                 ▼              ▼
     SQLite (app DB)  Hindsight gateway   LLM runtime    Embeddings
     sources/versions  (bank per          (extractive     (optional)
     passages+FTS5     workspace)          or OpenAI-compat)
     findings/rels
     summaries/executions
```

| Component | Responsibility | Source of truth for |
|---|---|---|
| App DB (SQLite+FTS5) | sources, versions, original text, passages (page/section), findings, relationships, summary versions, workflow executions/steps, memory-operation ledger, eval runs | evidence & provenance, workflow state |
| Hindsight (one bank per workspace, `pra-<slug>`) | long-term memory: retained findings, relationships, user research context; recall; consolidation via mental model | learned memory only |
| Harness | orders stages, enforces permissions/policies, retries, checkpoints, validation | control flow |
| LLM runtime | *proposes* structured outputs (findings, answers, relationships, summaries); `none` ⇒ deterministic extractive strategies | nothing (stateless) |
| Retrieval | workspace-scoped hybrid search: FTS5 BM25 (+ cosine on embeddings when configured), dedup, per-source diversification | — |

## Epistemic model
Every finding/claim carries `origin` and `kind`:
`source_stated` (backed by a verbatim quote from a stored passage) · `user_note` (user-authored) ·
`inference` (AI conclusion — must cite supporting passages, never shown as a quote) · `uncertain`.
Consolidated conclusions live in the summary (items have status `established | new | contested | hypothesis`).

## Data flow
1. **Ingest**: upload/URL/note → persisted execution → extract → normalize → dedupe → chunk+index → extract findings →
   validate findings → retain in Hindsight (policy filtered, idempotent) → update knowledge index → mark complete.
   If Hindsight is down the source ends `partial` (evidence searchable) and is resumable; see `harness-design.md`.
2. **Ask**: classify intent → plan which retrievals are necessary → recall memory (bounded) + retrieve passages →
   merge/rank/dedupe → generate structured answer → deterministic + checker validation → return →
   policy-gated experience retention.
3. **Synthesize**: pick candidate finding pairs across sources → propose relationships → validate evidence →
   persist as `supported` or `hypothesis`.
4. **Summary**: load current version → gather findings (new vs existing) → propose → validate/preserve → diff →
   persist new version or record "no change"; rollback = new version copied from an old one.

## Serving and configuration
- One process: FastAPI serves `/api/*` and, when `frontend/dist` exists (`npm run build`), the single-page UI at `/` (unknown `/api/*` stays a JSON 404; static paths are confined to `dist`). `FRONTEND_DIST` overrides the location.
- Settings come from environment / `.env` (surrounding whitespace in values is stripped). **`DATABASE_URL`, if set to a relative SQLite path, is resolved against the process working directory**, so starting the server from `backend/` vs the repo root uses *different* databases (`backend/data/app.db` vs `data/app.db`). Leave `DATABASE_URL` unset to use `<repo>/data/app.db` regardless of working directory, or set an absolute path.
- LLM/embeddings use any OpenAI-compatible endpoint. The client sends `max_tokens` + `temperature: 0` first and adapts once if the server rejects them (e.g. GPT-5-family models need `max_completion_tokens` and the default temperature).

## Why these choices
- SQLite+FTS5: zero infrastructure on Windows; Postgres/pgvector is the scale-up path (schema is portable except FTS).
- No LangGraph: workflows are mostly linear with a handful of guarded branches; a ~300-line persisted runner gives the
  needed checkpointing/trace/resume with less magic.
- DB-backed queue + in-process worker: ingestion is long-running (Hindsight retain ≈ 10–15 s) but low volume; no Redis needed.
- Per-workspace banks give hard memory isolation; tags (`source:<id>`, `kind:<k>`) allow source-aware recall.

(Details of state machines, policies and retries: see `harness-design.md`. Metrics and gates: `evaluation.md`.)
