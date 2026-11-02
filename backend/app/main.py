"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.api.routes import public, router
from app.config import Settings, get_settings
from app.container import Services
from app.errors import AppError, InvalidInput, NotFound, PolicyViolation, RetryableError
from app.harness.logging import configure_logging, redact
from app.harness.worker import JobWorker
from app.wiring import build_services

log = logging.getLogger("api")

_STATUS = {InvalidInput: 400, NotFound: 404, PolicyViolation: 403, RetryableError: 503}


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services(settings)
        app.state.services = svc
        worker = JobWorker(svc, settings.worker_poll_s)
        app.state.worker = worker
        if svc.settings.worker_enabled:
            worker.start()
        else:
            svc.runner.recover_interrupted()
        log.info("app.started", extra={"generation_mode": svc.models.mode, "hindsight": svc.settings.hindsight_base_url})
        yield
        worker.stop()
        svc.gateway.close()

    app = FastAPI(title="Personal Research Assistant", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        status = next((v for k, v in _STATUS.items() if isinstance(exc, k)), 500)
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "message": redact(str(exc))}})

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Unexpected server error (see logs)."}})

    app.include_router(public)
    app.include_router(router)
    _mount_frontend(app, settings)
    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built UI (single-page app) from the same origin as the API. No-op until `npm run build` has run."""
    dist = Path(settings.frontend_dist).resolve()
    index = dist / "index.html"
    if not index.is_file():
        return

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path == "api" or path.startswith("api/"):  # unknown API routes stay 404 JSON, never the HTML shell
            raise HTTPException(status_code=404, detail="not found")
        target = (dist / path).resolve()
        if path and target.is_file() and dist in target.parents:  # confined to dist: no path traversal
            return FileResponse(target)
        return FileResponse(index)


app = create_app()  # uvicorn app.main:app
