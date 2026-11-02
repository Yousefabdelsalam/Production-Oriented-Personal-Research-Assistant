# Setup and troubleshooting (Windows / PowerShell)

All commands run from the repository root unless stated otherwise.

## 1. Backend
```
python -m venv .venv
.\.venv\Scripts\python -m pip install -r backend\requirements.txt
copy .env.example .env          # optional; defaults: no LLM (extractive mode) and the dedicated Hindsight on 127.0.0.1:8890
cd backend                      # start the server from backend\ (see the database note below)
..\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```
Database migrations (Alembic) run on startup. With `DATABASE_URL` unset, the SQLite file is `<repo>\data\app.db` regardless of the
working directory. **If you set a relative `DATABASE_URL` (e.g. `sqlite:///./data/app.db`) it is resolved against the directory you start
the server from**, so starting from `backend\` and from the repo root would use two different databases. Use an absolute path or leave it unset.
(An existing setup that has `DATABASE_URL=sqlite:///./data/app.db` in `.env` keeps its data in `backend\data\app.db` as long as the server is
always started from `backend\`. Changing Hindsight never changes this path.)

Check it: `http://localhost:8000/api/health` reports database, Hindsight reachability/version, and the generation mode.

## 2. Frontend
Node.js is required. If it is not installed system-wide, the repo can use a local copy in `.tools\node`:
```
$env:Path = "$PWD\.tools\node;" + $env:Path
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to http://127.0.0.1:8000
npm run build      # type-checks, then writes frontend/dist
npm test           # vitest unit tests
```
After `npm run build`, the backend serves the UI itself at `http://localhost:8000/` (no separate dev server needed).
Override the location with `FRONTEND_DIST`. Unknown `/api/...` paths still return a JSON 404, never the HTML shell.

## 3. Hindsight (dedicated instance)
This project has its **own** Hindsight deployment (Docker Compose: `pra-hindsight` + `pra-hindsight-db`, separate network and volumes), independent of
any other project's. The app reaches it through `HINDSIGHT_BASE_URL` (default `http://127.0.0.1:8890`). It creates one memory bank per workspace,
named `<HINDSIGHT_BANK_PREFIX><workspace-slug>` (default prefix `pra-`), and never touches other banks.
Hindsight's own LLM (used server-side for fact extraction) has its **own key in `.env.hindsight`**, not in the app's `.env`.

```powershell
copy .env.hindsight.example .env.hindsight     # then set HINDSIGHT_API_LLM_API_KEY (and the DB password)
.\scripts\hindsight.ps1 up                     # start (refuses to start without an LLM key; see docs/hindsight-deployment.md)
.\scripts\hindsight.ps1 status                 # containers + API version
.\scripts\hindsight.ps1 smoke                  # real retain -> recall on a disposable bank
.\scripts\hindsight.ps1 stop                   # stop, keeping all data
```
Full architecture, ports, volumes, backup/restore and troubleshooting: [hindsight-deployment.md](hindsight-deployment.md).

## 4. Generation modes
| `LLM_PROVIDER` | Behaviour |
|---|---|
| `none` (default) | Deterministic **extractive** mode: answers are verbatim sentences from cited passages. Every response says so. |
| `openai_compatible` | Any OpenAI-compatible `/chat/completions` endpoint. Output is still validated by the harness (verbatim quotes, support checks). Set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. |

How LLM mode is verified: the HTTP client is unit-tested through `httpx.MockTransport` (`tests/test_llm_client.py`, including the
parameter rejections real GPT-5-family models return), workflow logic is tested with an in-process `ScriptedLLM`, and the real
OpenAI API was exercised by hand on 2026-10-21. Values in `.env` are whitespace-trimmed, but keep model names clean.
Newer OpenAI models need `max_completion_tokens` and the default temperature; the client adapts automatically on the first rejection.

## 5. Tests and evaluation
```
cd backend
..\.venv\Scripts\python -m pytest -q -m "not hindsight"   # offline subset (fast)
..\.venv\Scripts\python -m pytest -q                      # everything; live Hindsight tests take ~10 minutes total
..\.venv\Scripts\python -m evaldata.run                   # deterministic evaluation suite
```
Live tests create and delete their own banks (prefix `pra-test-`). If Hindsight is unreachable they skip with a loud reason.
`evaldata.run` exit codes: 0 all gates passed, 1 a gate failed, 2 inconclusive (some gates skipped).

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| UI banner: "Hindsight unreachable" | Start the dedicated instance (`.\scripts\hindsight.ps1 up`) or fix `HINDSIGHT_BASE_URL` (default `http://127.0.0.1:8890`). Ingestion stops at `RETAIN_USEFUL_MEMORY` and the source becomes `partial`; use **Retry from the failed stage** once it is back. Completed stages and memory writes are not repeated. |
| Hindsight is "healthy" but sources end `partial` with `retain_batch: Hindsight server error 500` | Hindsight runs its own LLM for fact extraction; its key is configured on the Hindsight server, not in this app. Check `.\scripts\hindsight.ps1 logs` (or `docker logs <hindsight-container>`): `401 … token_invalidated` means that server's API key was revoked. Recall and answers keep working; fix the key on the Hindsight side, then use **Retry from the failed stage**. |
| Source stays `partial` | Evidence is searchable but memory retention failed. Not marked complete on purpose. Set `HINDSIGHT_REQUIRED=false` only if you accept that. |
| PDF rejected as "no text layer" | Scanned/image-only PDFs are not supported (no OCR). |
| URL rejected | Private, loopback and link-local targets (including via redirects) are blocked by design (SSRF protection). Paywalled/login pages are never bypassed. |
| Port 8000 already in use | Another service is using it. Pick another with `--port 8010` and, for `npm run dev`, change the proxy target in `frontend/vite.config.ts`. |
| `npm` not found | Node is not on PATH. Use the `$env:Path = ...` line above or install Node.js. |
| API returns 401 | `APP_API_TOKEN` is set. Enter the token in the UI's top-right field, or send `Authorization: Bearer <token>`. |
| A run failed or was interrupted | Open **Harness inspector**, select the run, and use **Resume from the failed stage**. A crash is detected on startup and interrupted runs are re-queued (bounded by `WORKER_AUTO_RESUME_MAX`). |
