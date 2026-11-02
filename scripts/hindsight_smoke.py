"""Minimal real retain -> recall smoke test of the dedicated Hindsight, through the application's own gateway.

Uses a disposable bank and deletes it afterwards. Run from the repo root (see scripts/hindsight.ps1 smoke):
    .venv\\Scripts\\python scripts\\hindsight_smoke.py [base_url]
Exit code 0 = retained text was recalled; 1 = failure (the reason is printed).
"""
from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings  # noqa: E402
from app.services.hindsight_gateway import HindsightError, MemoryItemIn, RealHindsightGateway  # noqa: E402


def bank_ids(base_url: str) -> list[str]:
    raw = json.load(urllib.request.urlopen(base_url.rstrip("/") + "/v1/default/banks", timeout=15))
    return sorted(b.get("bank_id") or b.get("id") for b in (raw["banks"] if isinstance(raw, dict) else raw))


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else Settings().hindsight_base_url
    bank, token = "pra-smoke-" + uuid.uuid4().hex[:8], "quokka-" + uuid.uuid4().hex[:10]
    gw = RealHindsightGateway(base, timeout_s=120)
    print(f"target: {base}   disposable bank: {bank}")
    try:
        h = gw.health()
        if not h.ok:
            print(f"FAIL: Hindsight not healthy: {h.detail}")
            return 1
        print(f"health: ok, API version {h.version}")
        gw.ensure_bank(bank, "PRA smoke test", "Disposable bank used to verify the dedicated Hindsight deployment.")
        out = gw.retain_document(bank, "smoke-doc-1", [MemoryItemIn(
            content=f"The {token} experiment found that adaptive chunking raised citation precision by 17% over fixed-size chunking.",
            context="smoke test", tags=["source:smoke"], metadata={"source_id": "smoke"})])
        print(f"retain: {out.items_count} item(s) in {out.latency_ms} ms, LLM tokens in/out {out.input_tokens}/{out.output_tokens}")
        hits = gw.recall(bank, f"What did the {token} experiment find about chunking?", max_tokens=800, budget="low")
        found = any(token in m.text for m in hits)
        print(f"recall: {len(hits)} result(s); contains the unique token: {found}")
        if not found:
            print("FAIL: retained text was not recalled")
            return 1
        print("PASS: real retain -> recall works against", base)
        return 0
    except HindsightError as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        try:
            gw.delete_bank(bank)
            print("cleanup: disposable bank deleted")
        except HindsightError as e:  # never mask the real result with a cleanup error
            print(f"cleanup warning: could not delete {bank}: {e}")
        gw.close()


if __name__ == "__main__":
    sys.exit(main())
