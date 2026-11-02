from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.services.hindsight_gateway import HindsightError, build_gateway
from app.services.llm import ModelRuntime, NullEmbedder
from app.services.workspaces import create_workspace
from app.wiring import build_services

from tests.doubles import InMemoryHindsightDouble


def make_settings(tmp_path, **kw) -> Settings:
    return Settings(
        _env_file=None, data_dir=tmp_path, hindsight_bank_prefix="pra-test-", retry_backoff_base_s=0.0,
        worker_enabled=False, llm_provider="none", embedding_provider="none", auto_synthesize=kw.pop("auto_synthesize", False),
        **kw,
    )


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def double():
    return InMemoryHindsightDouble()


@pytest.fixture
def services(settings, double):
    """Offline services: real SQLite + migrations + real harness, Hindsight replaced by a clearly-named test double."""
    svc = build_services(settings, gateway=double, models=ModelRuntime(None, NullEmbedder()))
    svc.sleep = lambda s: None
    yield svc
    svc.session_factory.kw["bind"].dispose()


@pytest.fixture
def ws(services):
    with services.session() as s:
        w = create_workspace(s, services.settings, "Agentic AI", "test workspace")
        s.flush()
        return w.id


@pytest.fixture
def live_services(tmp_path):
    """Services wired to the REAL Hindsight server (skips loudly if unreachable). Cleans up created banks."""
    settings = make_settings(tmp_path)
    gw = build_gateway(settings)
    h = gw.health()
    if not h.ok:
        pytest.skip(f"LIVE Hindsight NOT reachable at {settings.hindsight_base_url}: {h.detail}")
    svc = build_services(settings, gateway=gw, models=ModelRuntime(None, NullEmbedder()))
    svc.sleep = lambda s: None
    created: list[str] = []
    svc.extras["created_banks"] = created
    yield svc
    for b in created:
        try:
            gw.delete_bank(b)
        except HindsightError:
            pass
    gw.close()
    svc.session_factory.kw["bind"].dispose()


def new_live_workspace(live_services, name="Live Test Workspace") -> str:
    suffix = uuid.uuid4().hex[:6]
    with live_services.session() as s:
        w = create_workspace(s, live_services.settings, f"{name} {suffix}")
        s.flush()
        live_services.extras["created_banks"].append(w.bank_id)
        return w.id
