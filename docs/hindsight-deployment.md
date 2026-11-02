# Dedicated Hindsight deployment (Personal Research Assistant)

Personal Research Assistant has its **own** Hindsight server and database. It shares nothing with any other project's Hindsight
(for example SelfLearningAgent): own Compose project, network, containers, volumes, database and host ports.

> Status (2026-10-21): the deployment is built and verified (see [Verification record](#verification-record)). **It needs a valid OpenAI key
> in `.env.hindsight` before it can run in its intended mode**, because Hindsight 0.10.2 refuses to start without an LLM key.

## Architecture
```
 Windows host                                    Docker network  pra_hindsight_net (private bridge)
 ┌──────────────────────┐   127.0.0.1:8890    ┌──────────────────────────┐   5432 (not published)   ┌──────────────────────────┐
 │ PRA backend (uvicorn)│ ──────────────────► │ pra-hindsight            │ ───────────────────────► │ pra-hindsight-db         │
 │ HINDSIGHT_BASE_URL   │   REST API :8888    │ ghcr.io/vectorize-io/    │   postgresql://…         │ pgvector/pgvector:pg17   │
 └──────────────────────┘                     │ hindsight:0.10.2         │                          │ db "hindsight"           │
   browser ──────────────► 127.0.0.1:9990 ──► │  + control-plane UI :9999│                          └────────────┬─────────────┘
                                              └────────────┬─────────────┘                                       │
                                                  volume pra_hindsight_models                          volume pra_hindsight_pg_data
                                                  (model cache, re-downloadable)                       (ALL memories: back this up)
```
| Item | Value |
|---|---|
| Compose file / project | `docker-compose.hindsight.yml` / `pra-hindsight` |
| Containers | `pra-hindsight` (API + UI), `pra-hindsight-db` (PostgreSQL 17 + pgvector 0.8.x) |
| Images | `ghcr.io/vectorize-io/hindsight:0.10.2` (pinned; matches `hindsight-client==0.10.2`), `pgvector/pgvector:pg17` |
| Network | `pra_hindsight_net` |
| Volumes | `pra_hindsight_pg_data` (data), `pra_hindsight_models` (cache) |
| Host ports (loopback only) | API `127.0.0.1:8890` → container 8888; UI `127.0.0.1:9990` → container 9999. The database publishes **no** host port. |
| Why not 8888/9999/5432 | Those are in use by the other project's deployment. 5433 is used by an unrelated local Postgres. |
| Restart policy | `unless-stopped` |
| Health checks | DB: `pg_isready`; Hindsight: `GET /health/ready` (start period 120 s) |
| Env file | `.env.hindsight` (gitignored; template `.env.hindsight.example`) |

Every variable name above was read from the image itself (`/app/api/hindsight_api/config.py`, `/app/start-all.sh`), not assumed.

## Credentials
* `.env.hindsight` is **separate** from the app's `.env`. Always pass `--env-file .env.hindsight` (the helper script does), which also stops Compose from
  reading the app's `.env`.
* `HINDSIGHT_API_LLM_API_KEY` — an OpenAI key **for this Hindsight server**. It is not copied from any other deployment or from the app's `.env`; you must
  provide it. `HINDSIGHT_API_LLM_PROVIDER` (default `openai`) and `HINDSIGHT_API_LLM_MODEL` (default `gpt-5-mini`) are set next to it. Reflect, retain and
  consolidation LLMs fall back to these.
* `PRA_HINDSIGHT_DB_PASSWORD` — generated randomly for this install (40 URL-safe characters). Only reachable on the private network.
* `HINDSIGHT_API_SKIP_LLM_VERIFICATION` — Hindsight makes one real LLM call at startup unless this is `true`. Keep it `false` once a key is configured: a missing or
  revoked key then fails **loudly at start** instead of silently at the first retain (which is how the previous shared instance failed).
* `.gitignore` excludes `.env`, `.env.*` (except the two `*.example` templates), `data/`, `backups/`. Nothing in the repo contains a key; log output is masked by
  `scripts\hindsight.ps1 logs`.

## First-time setup (PowerShell, from the repo root)
```powershell
copy .env.hindsight.example .env.hindsight
notepad .env.hindsight                    # set HINDSIGHT_API_LLM_API_KEY=sk-...  and HINDSIGHT_API_SKIP_LLM_VERIFICATION=false
                                          # (PRA_HINDSIGHT_DB_PASSWORD: replace the placeholder with a long random value)
docker compose --env-file .env.hindsight -f docker-compose.hindsight.yml config -q   # validate (prints nothing when OK)
.\scripts\hindsight.ps1 up                # starts only pra-hindsight + pra-hindsight-db, waits for health
.\scripts\hindsight.ps1 smoke             # real retain -> recall on a disposable bank (deleted afterwards)
```
Then make sure the app's `.env` has `HINDSIGHT_BASE_URL=http://127.0.0.1:8890` (the default) and start the backend **from `backend\`**
(`..\.venv\Scripts\python -m uvicorn app.main:app --port 8000`). If the backend ever runs inside Docker on `pra_hindsight_net`, use `http://pra-hindsight:8888`.

## Everyday commands
| Task | Command |
|---|---|
| Start (needs an LLM key) | `.\scripts\hindsight.ps1 up` |
| Status (containers, API version, provider) | `.\scripts\hindsight.ps1 status` |
| Health (API, readiness, UI, DB) | `.\scripts\hindsight.ps1 health` |
| Logs, secrets masked | `.\scripts\hindsight.ps1 logs -Tail 100` |
| Stop, **keeping containers and all data** | `.\scripts\hindsight.ps1 stop` (then `start`) |
| Restart | `.\scripts\hindsight.ps1 restart` |
| Remove containers + network, **keeping volumes** | `.\scripts\hindsight.ps1 down` (never adds `-v`) |
| Verify connectivity end to end | `.\scripts\hindsight.ps1 smoke` |
| UI | http://127.0.0.1:9990 |

Equivalent raw commands: `docker compose --env-file .env.hindsight -f docker-compose.hindsight.yml <up -d|stop|start|restart|down|ps>`.
**Never** run `down -v`, `docker volume rm pra_hindsight_pg_data`, `docker volume prune` or `docker system prune`: they delete the memories.

## Backup and restore
```powershell
.\scripts\hindsight.ps1 backup            # -> backups\hindsight\hindsight-<timestamp>.dump (pg_dump custom format, self-verified with pg_restore --list)

# Safe test of any dump: restore into a scratch database (the live one is untouched), then drop it
.\scripts\hindsight.ps1 restore -File backups\hindsight\hindsight-<timestamp>.dump -Database hindsight_restore_test
docker exec pra-hindsight-db psql -U hindsight -d postgres -c "drop database hindsight_restore_test;"

# Real restore: OVERWRITES the live database. Takes a safety backup first, stops pra-hindsight, restores, starts it again
.\scripts\hindsight.ps1 restore -File backups\hindsight\hindsight-<timestamp>.dump -Yes
```
The dump is written inside the container and copied out (`docker cp`) because PowerShell's `>` redirection corrupts binary output. `.env.hindsight`
(DB password, LLM key) is **not** part of the backup; store it separately. Models in `pra_hindsight_models` need no backup (they are re-downloaded).

## Temporary no-LLM mode (chunk-only)
`.\scripts\hindsight.ps1 up -ChunkOnly` runs with `HINDSIGHT_API_LLM_PROVIDER=none` (documented in the image: "Retain uses chunks mode (no fact extraction), and
reflect/consolidation are disabled"). It is useful to test infrastructure without a key, **not** for real use: memories are raw chunks, and the app's
consolidation / mental-model features fail with *"Mental model refresh requires an LLM provider"*. The app does not display that Hindsight is in this mode, so
do not leave it running for real work. Switching to the real configuration is `.\scripts\hindsight.ps1 up` after setting the key (Compose recreates only `pra-hindsight`; data is kept).

## Verification record
Run on 2026-10-21 (Docker 29.8.2, Compose 5.5.1, Windows 11). Everything below was executed and observed.

| Check | Result |
|---|---|
| `docker compose ... config -q` | valid; no name/port/volume/network clashes found beforehand |
| Start | DB healthy in ~6 s; Hindsight healthy in ~15 s; migrations ran from scratch and installed pgvector in the dedicated DB |
| API / UI | `GET /health` healthy, `/health/ready` 200, `/version` → `0.10.2`; control plane UI HTTP 200 |
| Network exposure | 8890/9990 bound to `127.0.0.1` only, unreachable on the machine's other address; DB not published |
| Real retain → recall via the app's gateway | passed (disposable bank; unique token recalled) — **in chunk-only mode** |
| Data really in the new DB | rows in `banks/documents/memory_units/chunks` of `pra-hindsight-db`, token present in stored text |
| Isolation | the smoke bank existed only on `:8890`; the other instance's bank list (`pra-ai-recourses`, `support-ops`) unchanged |
| Persistence | `restart`, `stop`/`start`, and `down` (no `-v`) + `up` (new container IDs) each preserved the data |
| Restart policy | crashing the API process → Docker restarted it automatically (restarts=1), healthy again. (`docker kill` is a *manual* stop and is, by design, not restarted.) Not tested: host reboot / Docker daemon restart |
| Backup / restore | backup → `pg_restore --list` → restore into a scratch DB (same row counts, text intact); live restore path via `-Yes` also run on this instance's own data |
| Other project untouched | before/after snapshot of its containers (IDs, start times, ports, mounts), volumes, network and its compose/.env file hashes: **no differences** |
| App databases | `backend\data\app.db` and `data\app.db` byte-identical (size and mtime) before and after |
| App tests against the new instance (chunk-only mode) | full suite: 172 passed, 1 failed (`test_live_multi_source_synthesis_memory_and_later_session_recall`: consolidation needs an LLM) |

**Not verified (blocked on a valid key):** any real LLM operation — fact extraction on retain, reflect, consolidation / mental models, startup LLM verification.
With an empty key and provider `openai` the container exits immediately with `ValueError: LLM API key is required` and (restart policy) keeps retrying; `up` therefore
refuses to start in that situation.

## Moving from the previous shared instance
The application previously used the other project's Hindsight on `:8888`. Memories already retained there (for example bank `pra-ai-recourses`) **stay in that
instance**; the new instance starts empty. The app's SQLite database still records those findings as retained, so it will not re-send them automatically.

* **What you see meanwhile:** a question is answered from the source documents only, and the trace shows `recall returned 0`. (Until 2026-10-21 this also showed the
  misleading warning *"Hindsight memory was unavailable … (AppError)"*: Hindsight answers a recall on a bank that does not exist with `404 Bank '…' not found`, which the app
  treated as an outage. A missing bank is now "no memories yet"; real outages and server errors still produce the warning. Restart the backend to pick up the fix.)
* **Re-populate the new instance without touching the app database:**
  ```powershell
  .\.venv\Scripts\python scripts\repopulate_hindsight_memory.py --db backend\data\app.db           # dry run: lists what would be sent
  .\.venv\Scripts\python scripts\repopulate_hindsight_memory.py --db backend\data\app.db --apply   # sends it (needs a working key AND OpenAI credits)
  ```
  It replays exactly what the app sent the first time (same items, `document_id` = source id, replace semantics → safe to run twice), opens the database read-only, and
  only ever writes to Hindsight. Verified as a dry run: 34 items (8 + 6 + 20) for the workspace "AI recourses". `--apply` was **not** run (see the OpenAI billing blocker below).
* To go back temporarily, set `HINDSIGHT_BASE_URL=http://localhost:8888` in the app's `.env` (the old instance still holds the old memories and can recall them, but cannot retain).

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `pra-hindsight` restarts in a loop; log says `LLM API key is required` | `HINDSIGHT_API_LLM_API_KEY` is empty in `.env.hindsight`. Set it, then `.\scripts\hindsight.ps1 up`. |
| Container starts but exits at startup with an OpenAI 401 | The key is invalid/revoked (startup verification is on). Create a new key. |
| `retain` fails; Hindsight log shows `RateLimitError … 429 … insufficient_quota` / "You have no credits remaining" | The key is valid but the OpenAI account has no credit left. Add credits at https://platform.openai.com/settings/organization/billing/ (a startup check still passes if it ran earlier). Recall keeps working; sources end `partial`; **Retry** after topping up. Observed on 2026-10-21. |
| Answer shows "Hindsight memory was unavailable" | Real outage or 5xx: check `.\scripts\hindsight.ps1 status` / `logs`. (A missing bank is *not* an error any more; it just means no memories yet.) |
| `retain` returns HTTP 500, API otherwise healthy | The server's LLM call failed. `.\scripts\hindsight.ps1 logs` → look for `AuthenticationError` / quota errors. App sources end `partial`; fix the key, then **Retry** in the UI. |
| `port is already allocated` on 8890/9990 | Change `PRA_HINDSIGHT_API_PORT` / `PRA_HINDSIGHT_UI_PORT` in `.env.hindsight` **and** `HINDSIGHT_BASE_URL` in the app's `.env`. |
| App banner "Hindsight unreachable" | `.\scripts\hindsight.ps1 status`; start it; confirm `HINDSIGHT_BASE_URL`. |
| `compose config` says `set PRA_HINDSIGHT_DB_PASSWORD` | You ran Compose without `--env-file .env.hindsight` (use the script). |
| Changed the DB password after first start | Postgres only reads it when initialising an empty volume. Change it inside Postgres (`ALTER USER`) or restore into a fresh volume. |
| Data seems gone after `down` | `down` keeps volumes. Check `docker volume ls --filter name=pra_` and that you start with the same compose project (`pra-hindsight`). |
| Slow first start | Hindsight loads embedding/reranker models (cached in `pra_hindsight_models` afterwards). |
| `docker kill`/`docker stop` container does not come back | Intended: `unless-stopped` honours manual stops. Use `start`. |
