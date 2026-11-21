"""Load the dashboard's synthetic demo scenario into the backend database.

    cd backend && python -m scripts.seed_demo --reset   # delete the db file, load reports, stock, roads
    REGION=petersburg python -m scripts.seed_demo --reset   # the real-map scenario (PowerShell: $env:REGION="petersburg")
    python -m scripts.run_extraction --live            # then the real model reads each report
    python -m scripts.seed_demo --reset --hand-written  # offline fallback: hand-typed readings

The reports are synthetic INPUTS, which is allowed. By default no extraction fields are written,
so every reading on the dashboard comes from the real model. --hand-written adds HAND-WRITTEN
stand-ins (model='synthetic-seed'); never present those as AI output.
"""
import os
import sys

from app import db, region

# The scenario comes from the active region (REGION env; default the fictional demo area):
# backend/data/regions/<REGION>.json holds its sample reports, stock and planner roads.
_R = region.load()
SEED = _R["reports"]   # zone, language, text, translation, need, qty, urgency, window, conf, missing, access, last4
STOCK = _R["stock"]
ROADS = region.roads()
AREA_CODE = "757" if _R["id"] == "demo" else "804"     # fictional 555 numbers either way


def seed(conn, extractions=True) -> None:
    """extractions=False loads only reports, stock and roads, so the REAL extractor fills the
    fields (python -m scripts.run_extraction --live). That is the path to present."""
    if conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]:
        raise SystemExit("Database already has reports. Use --reset to start over.")
    for item, n in STOCK.items():
        db.add_resource(conn, "supply", f"{item.title()} stock", n, item=item, zone="depot")
    db.add_resource(conn, "shelter", "Shelter beds", 10, item="shelter", zone="depot", accessible=True)
    for name, a, b, m in ROADS:
        db.add_road(conn, name, a, b, m)
    for s in SEED:
        r = db.add_report(conn, s["text"], channel="sms", language=s["lang"],
                          contact_phone=f"{AREA_CODE}-555-{s['last4']}", contact_address=s["addr"],
                          actor="seed")            # fictional 555 numbers; synthetic
        if not extractions:
            continue
        db.save_extraction(conn, r["id"], {
            "need_type": s["need"], "quantity": s["qty"], "zone": s["zone"], "urgency": s["urg"],
            "window_minutes": s["win"], "confidence": s["conf"], "missing_fields": s["miss"],
            "accessibility": s.get("access"), "translation_en": s["en"]},
            model="synthetic-seed", actor="seed")
    if not extractions:
        return
    db.link_duplicate(conn, 6, 2, score=0.93, reason="same household, same need", actor="seed")
    same = "Same building" if _R["id"] == "demo" else "Same address"
    db.link_duplicate(conn, 5, 4, status="separate_household", actor="seed",
                      reason=f"{same} as R-4, but a separate household, so it stays its own request.")


if __name__ == "__main__":
    path = os.environ.get("DB_PATH", "data/relief.db")
    if "--reset" in sys.argv and os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    db.init_db(conn)
    # Default: reports, stock and roads only, so the real model fills the fields.
    # --hand-written restores the old hand-typed readings (offline fallback only).
    # --no-extractions is still accepted; it is now the default.
    real = "--hand-written" not in sys.argv
    seed(conn, extractions=not real)
    print(f"Region: {_R['name']} (REGION={_R['id']})")
    print(f"Seeded {len(SEED)} synthetic reports into {path}"
          + (" (no extractions yet: now run python -m scripts.run_extraction --live)" if real
             else " WITH HAND-WRITTEN extractions (model='synthetic-seed'). Do not present these as AI output."))
