"""Workflow registry. Imported lazily to avoid circular imports between workflows and the services they use."""
from __future__ import annotations

import importlib

from app.harness.engine import WorkflowDef

_MODULES = ["ingestion", "qa", "synthesis", "summary", "memory", "evaluation"]


def all_workflows() -> dict[str, WorkflowDef]:
    defs: dict[str, WorkflowDef] = {}
    for name in _MODULES:
        try:
            mod = importlib.import_module(f"app.harness.workflows.{name}")
        except ModuleNotFoundError as e:  # a workflow not implemented yet is simply absent (never faked)
            if e.name == f"app.harness.workflows.{name}":
                continue
            raise
        d = mod.build()
        defs[d.name] = d
    return defs
