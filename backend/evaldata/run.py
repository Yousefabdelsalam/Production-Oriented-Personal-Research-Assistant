"""CLI: run the deterministic evaluation suite against the configured stack and record an EvaluationRun.

    python -m evaldata.run            # uses .env (real Hindsight if reachable; memory gates SKIPPED otherwise)

Exit code: 0 all gates passed · 1 a gate failed · 2 inconclusive (some gates skipped, none failed)
"""
from __future__ import annotations

import json
import sys
import uuid

from app.config import get_settings
from app.harness.logging import configure_logging
from app.harness.workflows.evaluation import EvalState
from app.models import EvaluationRun
from app.wiring import build_services


def main() -> int:
    settings = get_settings()
    configure_logging("WARNING")
    svc = build_services(settings)
    try:
        label = uuid.uuid4().hex[:6]
        ex = svc.runner.start_and_run("evaluation", state=EvalState(label=label), trigger={"type": "cli"}, idempotency_key=f"eval:{label}")
        if ex.status != "succeeded":
            print(f"EVALUATION EXECUTION FAILED at {ex.failed_stage}: {ex.error}")
            return 1
        with svc.session() as s:
            run = s.get(EvaluationRun, ex.result["run_id"])
            env, gates, metrics = run.environment, run.gates, run.metrics
            passed = run.passed
        print(f"Evaluation run {ex.result['run_id']}  mode={env['generation_mode']}  hindsight_live={env['hindsight_live']}")
        for n in env.get("notes", []):
            print("  note:", n)
        print(f"{'gate':32} {'value':>8}  {'need':>8}  result")
        for g in gates:
            res = "SKIPPED (" + g.get("reason", "") + ")" if g["skipped"] else ("PASS" if g["passed"] else "FAIL")
            val = "-" if g["value"] is None else f"{g['value']:.3f}"
            print(f"{g['name']:32} {val:>8}  {g['op']}{g['threshold']:<6}  {res}")
        print("\nmetrics:", json.dumps({k: {kk: vv for kk, vv in v.items() if kk not in ('probes', 'checks', 'injection_checks', 'isolation_checks')} for k, v in metrics.items()}, indent=1))
        print("\nOVERALL:", {True: "PASSED", False: "FAILED", None: "INCONCLUSIVE (gates skipped)"}[passed])
        return {True: 0, False: 1, None: 2}[passed]
    finally:
        svc.gateway.close()


if __name__ == "__main__":
    sys.exit(main())
