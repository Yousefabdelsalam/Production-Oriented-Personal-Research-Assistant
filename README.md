
# Production-Oriented Personal Research Assistant

A research workspace app. Add PDFs, web pages and your own notes to topic workspaces, and the system:

- extracts **cited evidence** from every source,
- stores useful long-term knowledge in [Hindsight](https://github.com/vectorize-io/hindsight) (a memory server),
- answers questions with **verifiable citations** (quotes are verbatim substrings of the stored passage),
- finds **relationships and contradictions** across sources,
- maintains a **versioned living summary** with diff and rollback.

Everything consequential runs through an explicit, persisted, observable **harness** (state machines), never through one
big autonomous prompt. The language model only *proposes*; the harness validates every output and performs every write.

## Key ideas

| Principle | What it means here |
|---|---|
| Harness first | Ingestion, Q&A, synthesis, summary, memory and evaluation are stage machines. Each stage is stored in the database with a checkpoint, so a failed run can be resumed without repeating completed side effects. |
| Evidence is not memory | Original text lives in the app database. Hindsight holds learned memory only, so memory alone never proves a claim. |
| Honest labels | Every claim is `source_stated`, `user_note`, `inference` or `uncertain`. An inference is never shown as a quotation. |
| Sources are untrusted | Documents are passed to models only inside delimited data blocks. Instruction-like passages are flagged and excluded from findings and memory. |
| No silent failures | A failed stage ends as `failed` or `partial` with an error. It is never reported as success. |
| Honest modes | With `LLM_PROVIDER=none` the app runs in deterministic *extractive* mode and says so in every response. |
| Workspace isolation | Every query is scoped by workspace, and each workspace has its own Hindsight memory bank. |

## Architecture

```
 React UI ──HTTP──► FastAPI (backend/app/api)
                       │
                       ▼
              Harness (backend/app/harness)
         engine · policies · permissions · injection guard
         workflows: ingestion · qa · synthesis · summary · memory · evaluation
           │               │                 │               │
           ▼               ▼                 ▼               ▼
     SQLite + FTS5    Hindsight gateway   LLM runtime     Embeddings
     (evidence,       (one bank per       (extractive     (optional)
      state, summaries) workspace)         or OpenAI-compatible)
```

More detail: [docs/architecture.md](docs/architecture.md), [docs/harness-design.md](docs/harness-design.md),
[docs/evaluation.md](docs/evaluation.md).

## Tech stack

- **Backend:** Python, FastAPI, SQLAlchemy 2, Alembic, SQLite (WAL) with FTS5, Pydantic
- **Frontend:** React, TypeScript, Vite, Vitest
- **Memory:** Hindsight 0.10.2 (Docker Compose, with its own Postgres/pgvector)
- **LLM / embeddings:** optional, any OpenAI-compatible endpoint

## Repository layout

```
backend/app/            FastAPI app: api, harness (engine, policies, workflows), services, models
backend/migrations/     Alembic migrations (run automatically on startup)
backend/tests/          unit / integration / end-to-end tests (pytest)
backend/evaldata/       deterministic evaluation corpus and CLI runner
frontend/               React + TypeScript UI (the built UI is served by the backend at /)
scripts/                Hindsight helper scripts (PowerShell + Python)
docs/                   architecture, harness design, evaluation, setup, Hindsight deployment
docker-compose.hindsight.yml   dedicated Hindsight + Postgres stack
```

## Getting started (Windows / PowerShell)

**Requirements:** Python 3.11+, Node.js 18+, and Docker Desktop (only for Hindsight).

### 1. Configure

```powershell
copy .env.example .env                        # app settings (works as-is: no LLM, extractive mode)
copy .env.hindsight.example .env.hindsight    # Hindsight server settings
```

Edit `.env.hindsight` and set `PRA_HINDSIGHT_DB_PASSWORD` (a long random value) and `HINDSIGHT_API_LLM_API_KEY`
(an OpenAI key used by the Hindsight server for fact extraction). Both files are git-ignored; **never commit them.**

To let the app itself use a language model, set `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL`, `LLM_API_KEY` and
`LLM_MODEL` in `.env`. Leave `DATABASE_URL` unset, or use an absolute path (a relative SQLite path depends on the
directory you start the server from).

### 2. Start Hindsight (memory server)

```powershell
.\scripts\hindsight.ps1 up        # start
.\scripts\hindsight.ps1 status    # containers + API version
.\scripts\hindsight.ps1 smoke     # real retain -> recall on a disposable bank
```

The API listens on `http://127.0.0.1:8890`. See [docs/hindsight-deployment.md](docs/hindsight-deployment.md) for ports,
volumes, backup/restore and troubleshooting.

### 3. Run the backend

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r backend\requirements.txt
cd backend
..\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

### 4. Build the UI

```powershell
cd frontend
npm install
npm run build        # type-check, then write frontend/dist
```

Open **http://localhost:8000**. For frontend development use `npm run dev` (http://localhost:5173, proxies `/api` to the backend).
Health check: `http://localhost:8000/api/health` reports the database, Hindsight and the generation mode.

## Using the app

1. Create a **workspace** for a research topic.
2. Add **sources**: upload a PDF, add a URL, or write a note.
3. **Ask** questions in the chat. Every claim links to a cited passage with the verbatim quote highlighted.
4. Review **relationships and contradictions** on the Knowledge page, and confirm or reject them.
5. Keep a **living summary** that updates as new evidence arrives, with version diff and rollback.
6. Use the **harness inspector** to see every run, its stages, and to resume a failed run from the failed stage.

## Tests and evaluation

```powershell
cd backend
..\.venv\Scripts\python -m pytest -q -m "not hindsight"    # offline tests (fast)
..\.venv\Scripts\python -m pytest -q                       # everything, including live Hindsight tests (~10 min)
..\.venv\Scripts\python -m evaldata.run                    # deterministic evaluation suite

cd ..\frontend
npm test
```

Live Hindsight tests need the Hindsight stack running with a valid LLM key, and use disposable `pra-test-*` banks.
If Hindsight is unreachable they skip with a loud reason. `evaldata.run` exits 0 when all gates pass, 1 when a gate fails
and 2 when the result is inconclusive.

## Configuration

All settings come from environment variables or `.env`; see [.env.example](.env.example) for every option with comments.
Secrets are read only from the environment and are never logged or stored in Hindsight.

## Security notes

- URL fetching blocks private, loopback and link-local targets, including via redirects (SSRF protection).
- Set `APP_API_TOKEN` to require `Authorization: Bearer <token>` on every `/api` request.
- Uploaded files, the database and Hindsight backups stay on your machine (`data/`, `backups/` are git-ignored).

## Documentation

| Document | Contents |
|---|---|
| [docs/setup.md](docs/setup.md) | Detailed setup and troubleshooting |
| [docs/architecture.md](docs/architecture.md) | Components, data flow, design choices |
| [docs/harness-design.md](docs/harness-design.md) | Stage machines, retries, resume, idempotency |
| [docs/evaluation.md](docs/evaluation.md) | Evaluation dataset, metrics and gates |
| [docs/hindsight-deployment.md](docs/hindsight-deployment.md) | Running and operating the Hindsight stack |
=======

