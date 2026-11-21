"""Adapter between the backend and the existing coordinator dashboard.

The dashboard (app/static/dashboard.html) keeps its own UI and its own planning code.
This module only (a) shapes backend data the way the dashboard already expects it and
(b) turns the dashboard's button clicks into audited backend actions.

Dashboard fields -> backend:
  id "R-12"     report id 12              hood        extraction.zone
  lang "Spanish" reports.language ("es")  need/qty    extraction.need_type / quantity
  text          reports.text_masked      u (1-3)     extraction.urgency (low/medium/high)
  en            extraction.translation_en win (min)   extraction.window_minutes
  conf (0-100)  extraction.confidence*100 miss        extraction.missing_fields
  phone "•••-0117"  last 4 digits only    dupOf/same  duplicate_links
  status/alloc  latest decided assignment
Raw phones and names never leave the server: the dashboard only gets the last 4 digits.
"""
import json
import threading

from . import audit, db, privacy, region

def known_zones():
    """Zone ids of the active region (REGION env; default the fictional demo area)."""
    return region.zone_ids()


KNOWN_NEEDS = {"water", "food", "medicine", "blankets", "shelter"}
LANG_NAMES = {"en": "English", "es": "Spanish", "vi": "Vietnamese", "zh": "Mandarin",
              "hi": "Hindi", "ar": "Arabic", "fr": "French"}
URGENCY_TO_LEVEL = {"low": 1, "medium": 2, "high": 3, "critical": 3}
EVIDENCE_KEYS = {"eta_min", "window_min", "confidence", "left_after", "note"}   # what the UI may attach


def _rid(report_id: int) -> str:
    return f"R-{report_id}"


def _latest_extraction(conn, report_id):
    return conn.execute("SELECT * FROM extractions WHERE report_id=? ORDER BY id DESC LIMIT 1",
                        (report_id,)).fetchone()


def _phone_hint(conn, token):
    if not token:
        return "no phone"
    row = conn.execute("SELECT phone FROM contacts WHERE household_token=?", (token,)).fetchone()
    if not row or not row["phone"]:
        return "no phone"
    return "•••-" + privacy.mask_phone(row["phone"])[3:]


# ------------------------------------------------------------------- state
def held_reason(e):
    """Why a report can't be placed on the dashboard yet, or None if it can."""
    if e is None:
        return "Waiting for the AI to read it."
    if not e["need_type"]:
        return "The AI couldn't read it. A person needs to check it."
    if e["need_type"] not in KNOWN_NEEDS:
        return f"Need \"{e['need_type']}\" isn't one the planner handles yet."
    if e["zone"] not in known_zones():
        return "Area unclear: no known neighborhood in the text or the intake address."
    return None


def build_state(conn) -> dict:
    reports, held = [], []
    for r in conn.execute("SELECT * FROM reports WHERE status != 'superseded' ORDER BY id").fetchall():
        e = _latest_extraction(conn, r["id"])
        why = held_reason(e)
        if why:                # not placed on the map; listed with the reason so a person can act
            held.append({"id": _rid(r["id"]), "dbid": r["id"], "reason": why,
                         "text": (r["text_masked"] or "")[:140]})
            continue
        miss = [m.replace("_", " ").capitalize() for m in json.loads(e["missing_fields"] or "[]")]
        if e["window_minutes"] is None and "Time window" not in miss:
            miss.append("Time window")
        dup = conn.execute(
            "SELECT linked_report_id FROM duplicate_links WHERE report_id=? AND status IN"
            " ('suspected','confirmed_duplicate') ORDER BY linked_report_id LIMIT 1", (r["id"],)).fetchone()
        sep = conn.execute(
            "SELECT reason FROM duplicate_links WHERE report_id=? AND status='separate_household' LIMIT 1",
            (r["id"],)).fetchone()
        approved = conn.execute("SELECT quantity FROM assignments WHERE report_id=? AND status='approved'"
                                " ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        decided = conn.execute("SELECT status FROM assignments WHERE report_id=? AND status IN"
                               " ('approved','rejected') ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        item = {
            "id": _rid(r["id"]), "dbid": r["id"], "hood": e["zone"],
            "lang": LANG_NAMES.get(r["language"], r["language"] or "Unknown"),
            "text": r["text_masked"], "en": e["translation_en"] or "",
            "need": e["need_type"], "qty": e["quantity"] or 0,
            "u": URGENCY_TO_LEVEL.get(e["urgency"], 2), "win": e["window_minutes"] or 60,
            "conf": round((e["confidence"] or 0) * 100), "miss": ", ".join(miss),
            "phone": _phone_hint(conn, r["household_token"]),
            "crit": e["urgency"] == "critical",              # ranked above "high" by the planner
            "status": decided["status"] if decided else "pending",
            "alloc": approved["quantity"] if approved else 0,
        }
        if e["accessibility"]:
            item["access"] = e["accessibility"]
        if sep:
            item["same"] = sep["reason"]
        if dup:
            item["dupOf"] = _rid(dup["linked_report_id"])
        reports.append(item)

    stock = {row["item"]: row["cap"] for row in conn.execute(
        "SELECT item, SUM(capacity) AS cap FROM resources WHERE item IS NOT NULL GROUP BY item")}
    roads = conn.execute("SELECT name, from_zone, to_zone, minutes, is_open FROM roads"
                         " WHERE name IS NOT NULL ORDER BY id").fetchall()
    closed = [r["name"] for r in roads if not r["is_open"]]
    return {"reports": reports, "stock": stock,
            "roads": [[r["name"], r["from_zone"], r["to_zone"], r["minutes"]] for r in roads],
            "blocked": closed[0] if closed else "", "held_back": len(held), "held": held,
            "audit": audit_messages(conn), "region": region.public()}


# ------------------------------------------------------------------- audit
def audit_messages(conn, limit=7) -> list:
    """Plain-language audit lines, newest first. Raw PII is never in the log, so none appears here."""
    out = []
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)):
        d = json.loads(r["details"] or "{}")
        who, act = r["actor"], r["action"]
        rep = _rid(d["report"]) if d.get("report") else None
        if act == "report.ingested":
            m = f"{_rid(int(r['entity_id']))} received ({d.get('channel')}, {LANG_NAMES.get(d.get('language'), d.get('language'))})."
        elif act == "extraction.saved":
            conf = d.get("confidence")
            m = f"{_rid(int(r['entity_id']))} read by {d.get('model')}" + (f", confidence {round(conf*100)}%." if conf is not None else ".")
        elif act == "extraction.corrected":
            m = f"{_rid(int(r['entity_id']))} corrected by {who} ({', '.join(d.get('fields', []))})."
        elif act == "duplicate.linked":
            m = f"{_rid(int(r['entity_id']))} linked to {_rid(d['linked'])} ({d.get('status')}). Nothing deleted."
        elif act == "assignment.proposed":
            m = f"Plan suggested {d.get('quantity')} units for {rep}."
        elif act == "assignment.approved":
            m = f"{rep or 'Assignment ' + r['entity_id']} approved by {who}."
        elif act == "assignment.rejected":
            m = f"{rep} rejected by {who}: {d.get('reason')}"
        elif act == "assignment.overridden":
            m = f"{rep} overridden by {who}: {d.get('reason')}"
        elif act == "assignment.withdrawn":
            m = f"{rep} reopened by {who}."
        elif act == "road.updated":
            m = f"{r['entity_id']} {'reopened' if d.get('is_open') else 'closed'} by {who}. Plan recomputed."
        elif act == "contact.revealed":
            m = f"Contact details viewed by {who}."
        else:
            m = f"{act} by {who}."
        out.append(m)
    return out


# ----------------------------------------------------------------- actions
def _need_of(conn, report_id):
    e = _latest_extraction(conn, report_id)
    if e is None:
        raise db.RuleError(f"report {report_id} has no extraction yet")
    return e["need_type"]


def _resource_for(conn, need, require_stock):
    q = "SELECT id FROM resources WHERE item=?" + (" AND remaining>0" if require_stock else "")
    row = conn.execute(q + " ORDER BY id LIMIT 1", (need,)).fetchone()
    return row["id"] if row else None


def _clean_evidence(evidence) -> dict:
    ev = {}
    for k, v in (evidence or {}).items():
        if k in EVIDENCE_KEYS and isinstance(v, (int, float, str)) and len(str(v)) <= 200:
            ev[k] = v
    return ev


def _need_name(conn, name):
    if not (name and name.strip()):
        raise db.RuleError("Enter your name at the top before approving, rejecting or changing anything.")


# One decision at a time: a double-click or two coordinators clicking together are handled in
# turn, so the "already decided" and stock checks always see the result of the previous click.
_decide_lock = threading.Lock()


def act_on_report(conn, report_id, status, coordinator, quantity=None, reason=None, evidence=None) -> None:
    """Dashboard buttons: status = approved | rejected | pending (Reopen)."""
    with _decide_lock:
        _act(conn, report_id, status, coordinator, quantity, reason, evidence)


def _act(conn, report_id, status, coordinator, quantity, reason, evidence) -> None:
    _need_name(conn, coordinator)
    if db.get_report(conn, report_id) is None:
        raise db.RuleError(f"report {report_id} not found")

    if status == "pending":
        last = conn.execute("SELECT id FROM assignments WHERE report_id=? AND status IN"
                            " ('approved','rejected') ORDER BY id DESC LIMIT 1", (report_id,)).fetchone()
        if last is None:
            raise db.RuleError("Nothing to reopen for this request.")
        db.withdraw_assignment(conn, last["id"], coordinator, "reopened by coordinator")
        return
    if status not in ("approved", "rejected"):
        raise db.RuleError("status must be approved, rejected or pending")

    decided = conn.execute("SELECT status FROM assignments WHERE report_id=? AND status IN"
                           " ('approved','rejected') ORDER BY id DESC LIMIT 1", (report_id,)).fetchone()
    if decided:
        raise db.RuleError(f"{_rid(report_id)} is already {decided['status']}. Reopen it first to change it.")
    need = _need_of(conn, report_id)
    qty = int(quantity or 0)
    if status == "approved" and qty <= 0:
        raise db.RuleError(f"Nothing to send: no {need} is available in the plan for this request.")
    if status == "rejected" and not (reason and reason.strip()):
        raise db.RuleError("A reason is required to reject a request.")
    resource_id = _resource_for(conn, need, require_stock=(status == "approved")) or _resource_for(conn, need, False)
    if resource_id is None:
        raise db.RuleError(f"No supply of {need} is registered.")

    for old in conn.execute("SELECT id FROM assignments WHERE report_id=? AND status='proposed'",
                            (report_id,)).fetchall():
        db.withdraw_assignment(conn, old["id"], "system", "superseded by a new decision")
    aid = db.propose_assignment(conn, report_id, resource_id, quantity=qty,
                                evidence=_clean_evidence(evidence), actor="dashboard-plan")
    try:
        db.decide_assignment(conn, aid, "approve" if status == "approved" else "reject",
                             coordinator, reason)
    except db.RuleError:
        db.withdraw_assignment(conn, aid, "system", "decision failed")
        raise


def set_quantity(conn, report_id, quantity, coordinator) -> None:
    _need_name(conn, coordinator)
    if conn.execute("SELECT 1 FROM assignments WHERE report_id=? AND status IN ('approved','rejected')",
                    (report_id,)).fetchone():
        raise db.RuleError("Reopen this request before changing its quantity.")
    db.correct_extraction(conn, report_id, {"quantity": max(0, int(quantity))}, coordinator)


def set_zone(conn, report_id, zone, coordinator) -> None:
    """A coordinator places a held-back report on the map ("Set area"). Saved as a correction:
    a new extraction row by the coordinator, so the AI's reading and the change are both kept."""
    if zone not in known_zones():
        raise db.RuleError(f"unknown area: {zone}")
    db.correct_extraction(conn, report_id, {"zone": zone}, coordinator)


def set_need(conn, report_id, need, coordinator) -> None:
    """A coordinator gives a held-back report a need the planner handles ("Set need"), e.g. when
    the AI filed it as "other". Saved as a correction, like set_zone."""
    if need not in KNOWN_NEEDS:
        raise db.RuleError(f"unknown need: {need}")
    db.correct_extraction(conn, report_id, {"need_type": need}, coordinator)


def set_blocked_road(conn, name, coordinator) -> None:
    """The dashboard closes one road at a time ('' reopens everything)."""
    _need_name(conn, coordinator)
    if name and conn.execute("SELECT 1 FROM roads WHERE name=?", (name,)).fetchone() is None:
        raise db.RuleError(f"unknown road '{name}'")
    for r in conn.execute("SELECT name FROM roads WHERE is_open=0 AND name IS NOT NULL").fetchall():
        if r["name"] != name:
            db.set_road_by_name(conn, r["name"], True, actor=coordinator)
    if name:
        row = conn.execute("SELECT is_open FROM roads WHERE name=?", (name,)).fetchone()
        if row["is_open"]:
            db.set_road_by_name(conn, name, False, actor=coordinator)
