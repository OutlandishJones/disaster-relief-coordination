"""Bridge: Aminul's extractor (ai/src/extraction.py) -> db.save_extraction().

Two ways in:
  * LIVE   run_pending(conn, extract_fn)  -- the extractor reads db.get_report_text() (scrubbed
           text only) and its real output is saved. This is the path to demo.
  * IMPORT import_outputs(conn, rows)     -- loads saved REAL model output (ai/data/extracted.json
           or processed.json) as new reports, marked from_cache=1. Honest reuse, not simulation.

Field mapping (extractor -> backend), so neither side has to rename anything:
  need_type          -> need_type   ('blanket' -> 'blankets' to match the dashboard)
  quantity           -> quantity (if empty and the need is per-person, household_size is used,
                                    as the extractor's prompt instructs; audit: quantity_source)
  household_size     -> people
  neighborhood/address/landmark -> location_text (joined)
  neighborhood/...   -> zone        via the region's zone aliases; if the text names no known zone, the zone is
                                    looked up from the intake form's address (deterministic, not AI)
  urgency            -> urgency
  accessibility_needs-> accessibility ('; '-joined)
  time_window        -> window_minutes (only if it states minutes/hours; else None)
  translation_en     -> translation_en (phones/emails scrubbed again, just in case)
  missing_fields     -> missing_fields
  confidence {dict}  -> confidence = min(need_type, location, urgency); full dict goes to the audit log
  needs_review       -> report status (also forced on low confidence or unknown zone)
"""
import json
import re
import sys
from pathlib import Path

from . import db, privacy, region

# Text the extractor may produce -> dashboard zone. Longest alias wins. The aliases come from the
# active region (backend/data/regions/<REGION>.json); add street or neighborhood names there.
def zone_aliases():
    return region.zone_aliases()


NEED_ALIASES = {"blanket": "blankets"}
PER_PERSON = {"water", "food", "blankets", "shelter"}   # not medicine: dose count isn't a head count
CHANNELS = {"sms", "form", "call"}
REVIEW_KEYS = ("need_type", "location", "urgency")
AI_SRC = Path(__file__).resolve().parents[2] / "ai" / "src"


# ------------------------------------------------------------------ mapping
def zone_for(*texts):
    for t in texts:
        t = (t or "").casefold()
        aliases = zone_aliases()
        for alias in sorted(aliases, key=len, reverse=True):
            if re.search(r"\b" + re.escape(alias) + r"\b", t):
                return aliases[alias]
    return None


def zone_from_intake(conn, report_id):
    """Zone from the address typed into the intake form. The address itself never leaves the server."""
    row = conn.execute("SELECT c.address FROM reports r JOIN contacts c USING (household_token)"
                       " WHERE r.id=?", (report_id,)).fetchone()
    return zone_for(row["address"]) if row else None


def parse_window(text):
    m = re.search(r"(\d+)\s*(minutes?|mins?|hours?|hrs?|h)\b", (text or "").casefold())
    if not m:
        return None
    n = int(m.group(1))
    return n * 60 if m.group(2).startswith("h") else n


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def overall_confidence(conf):
    if isinstance(conf, (int, float)):
        return max(0.0, min(1.0, float(conf)))
    vals = []
    for k in REVIEW_KEYS:
        try:
            vals.append(float((conf or {})[k]))
        except (KeyError, TypeError, ValueError):
            pass
    return max(0.0, min(1.0, min(vals))) if vals else None


def to_fields(ext: dict, fallback_zone=None):
    """Extractor dict -> (save_extraction fields, needs_review, audit extras)."""
    if ext.get("error") or not ext.get("need_type"):
        return ({"need_type": None, "confidence": 0.0,
                 "missing_fields": ["need_type", "location", "urgency"]},
                True, {"extract_error": str(ext.get("error") or "no need_type")[:200]})
    hood, addr, mark = ext.get("neighborhood"), ext.get("address"), ext.get("landmark")
    zone = zone_for(hood, addr, mark)
    source = "extractor" if zone else None
    if zone is None and fallback_zone:
        zone, source = fallback_zone, "intake_form"
    conf_d = ext.get("confidence") if isinstance(ext.get("confidence"), dict) else {}
    if source == "intake_form" and conf_d:
        # Location came from a structured form field, so text-location confidence doesn't apply.
        # Review is then driven by need/urgency confidence (extractor's own 0.5 threshold).
        conf = overall_confidence({k: v for k, v in conf_d.items() if k != "location"})
        review = any(_num(conf_d.get(k), 1) < 0.5 for k in ("need_type", "urgency"))
    else:
        conf = overall_confidence(ext.get("confidence"))
        review = bool(ext.get("needs_review")) or zone is None
    need = NEED_ALIASES.get(ext["need_type"], ext["need_type"])
    qty, qty_source = ext.get("quantity"), "extractor"
    if qty is None and ext.get("household_size") and need in PER_PERSON:
        # The extractor's own prompt rule ("no item count -> use the people count"), applied
        # when the model gave household_size but left quantity empty. Recorded in the audit log.
        qty, qty_source = ext["household_size"], "people_count"
    fields = {
        "need_type": need,
        "quantity": qty,
        "people": ext.get("household_size"),
        "location_text": ", ".join(x for x in (hood, addr, mark) if x) or None,
        "zone": zone,
        "urgency": ext.get("urgency"),
        "accessibility": "; ".join(ext.get("accessibility_needs") or []) or None,
        "window_minutes": parse_window(ext.get("time_window")),
        "translation_en": privacy.scrub_text(ext.get("translation_en") or "") or None,
        "missing_fields": list(ext.get("missing_fields") or []),
        "confidence": conf,
    }
    extra = {"zone_source": source, "quantity_source": qty_source if qty is not None else None,
             "field_confidence": ext.get("confidence") or {}}
    return fields, review, extra


def save(conn, report_id, ext, model, from_cache=False, fallback_zone=None) -> int:
    fields, review, extra = to_fields(ext, fallback_zone)
    return db.save_extraction(conn, report_id, fields, model=model, from_cache=from_cache,
                              needs_review=review, extra=extra)


# --------------------------------------------------------------------- live
def load_extractor():
    """Import Aminul's extract() from ai/src. Needs `pip install ollama` and a running Ollama."""
    if str(AI_SRC) not in sys.path:
        sys.path.insert(0, str(AI_SRC))
    import extraction  # noqa: E402  (ai/src/extraction.py)
    return extraction.extract, f"ollama:{extraction.MODEL}"


def extract_one(conn, rid, extract_fn, model, log=print) -> dict:
    """Read one report with the extractor (scrubbed text only), save it, and link it against
    every existing report. Used for live intake and by run_pending."""
    ext = extract_fn(db.get_report_text(conn, rid), str(rid))
    save(conn, rid, ext, model, fallback_zone=zone_from_intake(conn, rid))
    link_by_household(conn, [rid])
    log(f"R-{rid}: {ext.get('need_type')} / {ext.get('urgency')} review={ext.get('needs_review')}")
    return ext


def run_pending(conn, extract_fn, model, redo=False, log=print) -> list:
    """Extract every report that has no extraction yet (or all non-superseded ones with redo=True)."""
    q = ("SELECT id FROM reports WHERE status!='superseded'" if redo
         else "SELECT id FROM reports WHERE status='new'")
    done = []
    for (rid,) in conn.execute(q + " ORDER BY id").fetchall():
        extract_one(conn, rid, extract_fn, model, log=log)
        done.append(rid)
    return done


def _link_row(conn, rid):
    return conn.execute(
        "SELECT r.id, r.household_token AS tok, c.address AS addr, "
        "(SELECT need_type FROM extractions e WHERE e.report_id=r.id ORDER BY e.id DESC LIMIT 1) AS need "
        "FROM reports r LEFT JOIN contacts c USING (household_token) WHERE r.id=?", (rid,)).fetchone()


def link_by_household(conn, report_ids) -> int:
    """Rule-based linking (no AI), each given report against EVERY earlier report that isn't
    superseded: same household token + same need -> suspected duplicate; same intake address but a
    different household -> separate_household. Links point from the later report to the earlier
    one. Never deletes. Safe to run again: existing links are skipped."""
    earlier = [r[0] for r in conn.execute("SELECT id FROM reports WHERE status!='superseded' ORDER BY id")]
    n = 0
    for b in sorted(set(report_ids)):
        rb = _link_row(conn, b)
        if rb is None:
            continue
        for a in earlier:
            if a >= b:
                break
            if conn.execute("SELECT 1 FROM duplicate_links WHERE report_id=? AND linked_report_id=?",
                            (b, a)).fetchone():
                continue
            ra = _link_row(conn, a)
            if ra["tok"] and ra["tok"] == rb["tok"] and ra["need"] and ra["need"] == rb["need"]:
                db.link_duplicate(conn, b, a, reason="same household and same need",
                                  actor="dedup-rules")
                n += 1
            elif ra["addr"] and ra["tok"] != rb["tok"] and \
                    (ra["addr"] or "").casefold() == (rb["addr"] or "").casefold():
                db.link_duplicate(conn, b, a, status="separate_household", actor="dedup-rules",
                                  reason=f"Same address as R-{a}, but a separate household, so it stays its own request.")
                n += 1
    return n


# ------------------------------------------------------------------- import
def import_outputs(conn, rows, model, log=print) -> dict:
    """Load saved real extractor output (list of dicts with report_id + raw_text). Returns {ext_id: db_id}."""
    ids = {}
    for ext in rows:
        ch = ext.get("channel") if ext.get("channel") in CHANNELS else "other"
        r = db.add_report(conn, ext.get("raw_text") or "", channel=ch, language=ext.get("language"),
                          actor="import")
        save(conn, r["id"], ext, model, from_cache=True)
        ids[ext["report_id"]] = r["id"]
        log(f"{ext['report_id']} -> R-{r['id']}")
    for ext in rows:                       # duplicate links from Aminul's dedupe step, if present
        me = ids[ext["report_id"]]
        for rel in ext.get("related") or []:
            other = ids.get(rel.get("report_id"))
            v = rel.get("verdict")
            if other and v in ("likely_duplicate", "separate_household"):
                db.link_duplicate(conn, me, other, score=rel.get("similarity"),
                                  status="suspected" if v == "likely_duplicate" else v,
                                  reason=f"{v} (text similarity {rel.get('similarity')})",
                                  actor="dedup-embeddings")
    return ids


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
