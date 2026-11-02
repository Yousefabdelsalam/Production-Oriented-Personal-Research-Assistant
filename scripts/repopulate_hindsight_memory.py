"""Re-send already-retained findings to the CURRENT Hindsight (e.g. after moving to a new, empty Hindsight instance).

The application database says these findings are 'retained', so the app will never send them again by itself. This script
replays exactly what the app sent the first time: the same items (app.services.memory.finding_memory_item), the same
document_id (= source id) and replace semantics, so running it twice cannot create duplicates.

It NEVER writes to the application database (opened read-only); the only write is to Hindsight, and only with --apply.

    .venv\\Scripts\\python scripts\\repopulate_hindsight_memory.py --db backend\\data\\app.db            # dry run: shows what would be sent
    .venv\\Scripts\\python scripts\\repopulate_hindsight_memory.py --db backend\\data\\app.db --apply    # sends it (needs a Hindsight with a working LLM key and credits)
Optional: --workspace <id>   --base-url <hindsight url> (default: HINDSIGHT_BASE_URL from the app settings)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import Settings  # noqa: E402
from app.harness import policies  # noqa: E402
from app.models import ExtractedFinding, Source, Workspace  # noqa: E402
from app.services.hindsight_gateway import HindsightError, RealHindsightGateway  # noqa: E402
from app.services.memory import KIND_PRIORITY, bank_mission, finding_memory_item  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to the application's SQLite file (opened read-only)")
    ap.add_argument("--workspace", help="only this workspace id")
    ap.add_argument("--base-url", help="Hindsight URL (default: settings.hindsight_base_url)")
    ap.add_argument("--apply", action="store_true", help="actually send to Hindsight (default is a dry run)")
    a = ap.parse_args()

    db = Path(a.db).resolve()
    if not db.is_file():
        print(f"database not found: {db}")
        return 2
    settings = Settings()
    base = a.base_url or settings.hindsight_base_url
    cap = settings.memory_max_findings_per_source
    engine = create_engine(f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true")  # read-only: cannot modify the app database
    gw = RealHindsightGateway(base, settings.hindsight_api_key, settings.hindsight_timeout_s) if a.apply else None
    print(f"app database : {db} (read-only)\nHindsight    : {base}\nmode         : {'APPLY (writes to Hindsight)' if a.apply else 'dry run (nothing is sent)'}\n")

    failures = 0
    with Session(engine) as s:
        wss = s.query(Workspace).filter(*( [Workspace.id == a.workspace] if a.workspace else [] )).all()
        for ws in wss:
            print(f"workspace {ws.name!r} -> bank {ws.bank_id!r}")
            if a.apply:
                try:
                    gw.ensure_bank(ws.bank_id, ws.name, bank_mission(ws))
                except HindsightError as e:
                    print(f"   FAILED to create/ensure bank: {e}")
                    failures += 1
                    continue
            for src in s.query(Source).filter_by(workspace_id=ws.id).order_by(Source.created_at).all():
                fs = (s.query(ExtractedFinding)
                      .filter_by(source_id=src.id, validated=True, superseded=False, memory_state="retained").all())
                # same policy filter + ordering + cap the app applied when it first retained
                ok = [f for f in fs if policies.evaluate_retention(
                    kind=f.kind, statement=f.statement, epistemic=f.epistemic, has_provenance=bool(f.passage_id and f.source_id and f.quote),
                    validated=f.validated, quote=f.quote).allowed]
                ok.sort(key=lambda f: (KIND_PRIORITY.get(f.kind, 9), -f.confidence))
                ok = ok[:cap]
                if not ok:
                    print(f"   - {src.title[:60]!r}: nothing to send")
                    continue
                items = [finding_memory_item(f, src) for f in ok]
                if not a.apply:
                    print(f"   - {src.title[:60]!r}: would send {len(items)} item(s) as document {src.id}")
                    continue
                try:
                    out = gw.retain_document(ws.bank_id, src.id, items, replace=True)
                    print(f"   - {src.title[:60]!r}: sent {len(items)} item(s) -> {out.items_count} stored, {out.latency_ms} ms")
                except HindsightError as e:
                    failures += 1
                    print(f"   - {src.title[:60]!r}: FAILED: {e}")
    if gw:
        gw.close()
    print("\nFAILED" if failures else ("\ndone" if a.apply else "\ndry run complete; re-run with --apply to send"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
