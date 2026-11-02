"""Builds the service container (real Hindsight, configured models) and runs migrations."""
from __future__ import annotations

from app.config import Settings, get_settings
from app.container import Services
from app.db import make_engine, make_session_factory, run_migrations
from app.harness.engine import WorkflowRunner
from app.registry import all_workflows
from app.services.hindsight_gateway import HindsightGateway, build_gateway
from app.services.llm import ModelRuntime, build_runtime


def build_services(settings: Settings | None = None, *, gateway: HindsightGateway | None = None,
                   models: ModelRuntime | None = None, migrate: bool = True, **overrides) -> Services:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.originals_dir.mkdir(parents=True, exist_ok=True)
    if migrate:
        run_migrations(settings.db_url)
    engine = make_engine(settings.db_url)
    services = Services(
        settings=settings, session_factory=make_session_factory(engine),
        gateway=gateway or build_gateway(settings), models=models or build_runtime(settings), **overrides,
    )
    WorkflowRunner(services, all_workflows())
    return services
