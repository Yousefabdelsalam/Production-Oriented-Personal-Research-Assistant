# Harness design

> Status: implemented and audited against the code on 2026-10-21 (stage tables below were read from `GET /api/workflows`).
> Where a behaviour was verified in a real browser / against live services, the verification is named in the text.

## Runner (`backend/app/harness/engine.py`)
- A `WorkflowDef` is a set of `Stage(name, handler, next, retries)` plus a start stage and optional `on_success` / `on_failure`
  hooks. Handlers receive a `RunContext` and a typed Pydantic **state**; they may return `StageResult(goto=...)`, which must be
  one of the stage's declared `next` values, otherwise the execution fails with `IllegalTransition` (no free jumps).
- Each attempt writes a `workflow_steps` row first (status `running`, in its own transaction, so the inspector shows in-flight
  work). On success the handler's DB writes, the step (`succeeded`, duration, trace `detail`), the typed state checkpoint
  (`workflow_executions.state`) and the stage pointer commit **in one transaction**.
- Failure: only `RetryableError` (or an `AppError` marked retryable) is retried, with exponential backoff
  (`RETRY_BACKOFF_BASE_S * 2^(attempt-1)`). A stage with `retries=N` runs at most **N+1** times (e.g. retention: 3 attempts, observed live).
  Anything else, or an exhausted budget, sets the execution `failed` with `failed_stage` and `error`, and runs `on_failure`
  (which marks the source `failed`/`partial`). A failed attempt's writes are rolled back.
- `resume(execution_id)` (manual: UI/API) re-queues a `failed`/`interrupted` execution with `resume_count + 1`; it restarts at the
  failed stage using the persisted state, so **completed stages and their side effects are never repeated**. Retrying a failed
  stage re-issues that stage's own work (including its model calls); there is no per-call response cache.
- Crash recovery: at startup `recover_interrupted()` marks every execution left `running` as `interrupted` and closes the step row
  that was in flight (status `failed`, error "Interrupted: the process stopped…"; added 2026-10-21 so the trace no longer shows a phantom running step).
  The worker then re-runs interrupted executions automatically, bounded by `WORKER_AUTO_RESUME_MAX` (`auto_resumes`).
  Verified by killing the server process mid-`RETAIN_USEFUL_MEMORY` against live Hindsight (no duplicate memory; one ledger row).

## Queue / worker (`worker.py`)
The execution row *is* the job. A worker thread claims `queued` rows with an atomic `UPDATE … WHERE status='queued'`
(exclusive claim), then `interrupted` rows under the auto-resume bound. Request/response workflows (QA) run synchronously via `start_and_run`.

## Idempotency
- Source identity (`UNIQUE(workspace_id, dedupe_key)`): sha256 of the bytes for PDFs, canonical URL for URLs, a generated key for
  notes (an edit creates a new version of the same note). Re-submission returns the existing source (`duplicate: true`).
  Identical normalised text under a different identity is a content duplicate (`duplicate_of`): no re-extraction, no re-retain.
- Hindsight writes go through the `memory_operations` ledger: a retain is keyed by (source, version, payload hash); a replay finds the
  row and skips. The Hindsight `document_id` is the source id with `update_mode=replace`, so even a retain whose ledger row was lost
  (process killed after the HTTP call) replaces rather than duplicates. Experience retention uses `exp:<workspace>:<kind>:<hash>`.
- Workflow creation accepts an `idempotency_key` (unique); manual summary/synthesis/consolidation runs generate a fresh key each time.

## Policies (`policies.py`) — deterministic code, none of it delegated to a prompt
**Capability matrix** (enforced by `policies.require`, called as `ctx.require(...)` inside handlers; a violation raises `PolicyViolation`), narrowed per stage by `STAGE_RESTRICTED`:

| Workflow | Capabilities |
|---|---|
| ingestion | fetch_url, read_sources, write_sources, write_findings, write_memory (only `RETAIN_USEFUL_MEMORY`), queue_followups |
| qa | read_sources, recall_memory, write_memory (only `RECORD_USEFUL_EXPERIENCE`) |
| synthesis | read_sources, write_relationships (only `PERSIST_RELATIONSHIPS`), recall_memory, write_memory (only `RETAIN_RELATIONSHIP_MEMORY`), queue_followups |
| summary | read_sources, recall_memory, modify_summary (only `PERSIST_VERSION`), queue_followups (only `QUEUE_CONSOLIDATION`) |
| memory | read_sources, recall_memory, write_memory, consolidate_memory (only `CONSOLIDATE`) |
| evaluation | read_sources, recall_memory (writes only inside its own sandbox workspaces) |

**Memory-retention policy** (`evaluate_retention`): only validated findings with provenance, of a retainable kind
(`finding, definition, method, result, limitation, open_question, relationship, user_context, synthesis`), 20–700 characters
(raw chunks are never retained), with no credential-like text and no instruction-like text. Rejections are recorded on the finding
(`validation.retention_rejected`), never silent.

**Relationships**: predicates `supports, contradicts, extends, uses, depends_on, applies_to`; status `supported | hypothesis | rejected`.
Heuristic links are always `hypothesis`; only LLM-corroborated or user-confirmed links may be `supported`; a user decision
(`detected_by=user`) is never overwritten by later automated runs.

## Untrusted content
Documents are data. They are (1) scanned for injection patterns (`injection.scan_text`) and passages flagged; (2) wrapped in
`<<<UNTRUSTED …>>>` blocks in prompts together with fixed security rules; (3) never given tool access (the LLM returns JSON validated
against a schema); (4) never executed. **Instruction-like sentences are excluded from findings, memory, and (since 2026-10-21) from
answers**: the extractive picker skips them and the claim checker rejects any drafted claim that trips the detector. Previously the
question-answering path could quote an attacker's sentence as a "supported" claim (found in browser testing, fixed, regression-tested).

## Workflows (actual stages; `next` with `|` = guarded branch)
**ingestion** — RECEIVED → VALIDATE_INPUT → REGISTER_SOURCE → EXTRACT_CONTENT → NORMALIZE_CONTENT → DEDUPLICATE → (CHUNK_AND_INDEX | MARK_COMPLETED) →
EXTRACT_RESEARCH_FINDINGS → VALIDATE_FINDINGS → RETAIN_USEFUL_MEMORY → UPDATE_KNOWLEDGE_INDEX → MARK_COMPLETED.
Retries: VALIDATE_INPUT 2, EXTRACT_CONTENT 2, CHUNK_AND_INDEX 2, EXTRACT_RESEARCH_FINDINGS 1, RETAIN_USEFUL_MEMORY 2.
Memory unavailable ⇒ source `partial` (evidence searchable, not marked complete) unless `HINDSIGHT_REQUIRED=false` (then `completed` with a recorded warning and `memory: deferred`).

**qa** — QUESTION_RECEIVED → CLASSIFY_INTENT → RESOLVE_WORKSPACE → (RECALL_HINDSIGHT_MEMORY | RETURN_ANSWER) → RETRIEVE_SOURCE_EVIDENCE →
MERGE_AND_RANK_EVIDENCE → GENERATE_GROUNDED_ANSWER (retries 1) → VALIDATE_CITATIONS_AND_CLAIMS → RETURN_ANSWER → RECORD_USEFUL_EXPERIENCE.
Recall failure degrades (trace `degraded: true`, answer warning "Hindsight memory was unavailable…"); it never fails the question.

**synthesis** — LOAD_FINDINGS → SELECT_CANDIDATE_PAIRS → PROPOSE_RELATIONSHIPS (1) → VALIDATE_RELATIONSHIPS → PERSIST_RELATIONSHIPS → RETAIN_RELATIONSHIP_MEMORY (2) → COMPLETE.

**summary** — LOAD_CURRENT → GATHER_EVIDENCE → PROPOSE_SUMMARY (1) → VALIDATE_PROPOSAL → COMPARE_WITH_CURRENT → (PERSIST_VERSION | DONE) → QUEUE_CONSOLIDATION → DONE.
A validation failure fails the run and keeps the last valid version. No meaningful diff ⇒ `no_change`, no version. Rollback creates a **new** version copied from an old one.
Growth is bounded: at most `MAX_NEW_PER_SECTION` (20) *new* items per section per update; the remainder is reported as `deferred_items`
in the version's change line and the API result, and is added by later updates (so a large workspace needs several updates to converge).

**memory** — ENSURE_BANK (2) → SELECT_UNRETAINED → RETAIN_PENDING (2) → CONSOLIDATE (2) → DONE.

**evaluation** — SETUP_SANDBOX → INGEST_CORPUS → EVALUATE_RETRIEVAL_AND_ANSWERS → EVALUATE_SYNTHESIS → EVALUATE_MEMORY → EVALUATE_SUMMARY → EVALUATE_SAFETY → EVALUATE_GATES → RECORD_RESULTS.

## Observability
Every run is inspectable (`GET /api/executions/{id}`, UI "Harness inspector"): ordered steps with attempt, duration, error, and per-stage trace
(plan, retrieved evidence with reasons, validations, memory/policy decisions, model calls and tokens). Logs are JSON with correlation and
execution ids; secrets are redacted and full source text is never logged.
