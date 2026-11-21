"""Data layer. All business rules live here so Streamlit, FastAPI and tests share them.

Teammates call these functions (or the HTTP API that wraps them):
  Aminul (extraction)   -> get_report_text(), save_extraction()
  Dedup owner           -> link_duplicate()
  Matching engineer     -> list_open_resources(), propose_assignment()
  Dashboard             -> list_reports(), list_assignments(), decide_assignment()
"""
import json
import sqlite3
from pathlib import Path

from . import audit, privacy

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

# Allowed assignment transitions. Nothing is dispatchable unless 'approved'.
DECISIONS = {"approve": "approved", "reject": "rejected", "override": "overridden"}


class RuleError(ValueError):
    """A business rule was violated (bad transition, missing reason, ...)."""


def connect(path=":memory:") -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _row(r):
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- reports
def add_report(conn, text, channel="sms", language=None, contact_name=None,
               contact_phone=None, contact_address=None, is_synthetic=True,
               corrects_id=None, actor="system") -> dict:
    """Ingest a report. Raw PII goes to `contacts`; only scrubbed text is stored in `reports`."""
    token = None
    if contact_name or contact_phone or contact_address:
        token = privacy.household_token(contact_name, contact_phone, contact_address)
        conn.execute(
            "INSERT OR IGNORE INTO contacts (household_token, name, phone, address, created_at)"
            " VALUES (?,?,?,?,?)",
            (token, contact_name, contact_phone, contact_address, audit.now()))
    names = [n for n in (contact_name,) if n]
    masked = privacy.scrub_text(text, known_names=names)
    cur = conn.execute(
        "INSERT INTO reports (household_token, channel, language, text_masked, is_synthetic,"
        " received_at, corrects_id) VALUES (?,?,?,?,?,?,?)",
        (token, channel, language, masked, int(is_synthetic), audit.now(), corrects_id))
    rid = cur.lastrowid
    if corrects_id:
        conn.execute("UPDATE reports SET status='superseded' WHERE id=?", (corrects_id,))
    audit.append(conn, actor, "report.ingested", "report", rid,
                 {"channel": channel, "language": language, "household": token,
                  "corrects": corrects_id, "synthetic": bool(is_synthetic)})
    conn.commit()
    return get_report(conn, rid)


def get_report(conn, report_id):
    return _row(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())


def get_report_text(conn, report_id) -> str:
    """The ONLY text the LLM should receive (already scrubbed)."""
    r = get_report(conn, report_id)
    if r is None:
        raise RuleError(f"report {report_id} not found")
    return r["text_masked"]


def list_reports(conn, status=None):
    q, args = "SELECT * FROM reports", ()
    if status:
        q, args = q + " WHERE status=?", (status,)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


# ------------------------------------------------------------ extractions
def save_extraction(conn, report_id, fields: dict, model="unknown", from_cache=False,
                    actor="extractor", action="extraction.saved", extra=None,
                    needs_review=None) -> int:
    """Store structured fields. `fields` keys: need_type, quantity, people, location_text,
    zone, urgency, accessibility, missing_fields (list), confidence.

    needs_review: the extractor's own review flag. If given, it decides the status (low
    confidence still forces review). If None, any missing field or confidence < 0.6 does."""
    if get_report(conn, report_id) is None:
        raise RuleError(f"report {report_id} not found")
    missing = fields.get("missing_fields") or []
    cur = conn.execute(
        "INSERT INTO extractions (report_id, need_type, quantity, people, location_text, zone,"
        " urgency, accessibility, window_minutes, translation_en, missing_fields, confidence,"
        " model, from_cache, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (report_id, fields.get("need_type"), fields.get("quantity"), fields.get("people"),
         fields.get("location_text"), fields.get("zone"), fields.get("urgency"),
         fields.get("accessibility"), fields.get("window_minutes"), fields.get("translation_en"),
         json.dumps(missing), fields.get("confidence"), model, int(from_cache), audit.now()))
    conf = fields.get("confidence")
    low = conf is not None and conf < 0.6
    if needs_review is None:
        status = "needs_review" if (missing or low) else "extracted"
    else:
        status = "needs_review" if (needs_review or low) else "extracted"
    conn.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))
    audit.append(conn, actor, action, "report", report_id,
                 {"model": model, "cached": bool(from_cache),
                  "confidence": fields.get("confidence"), "status": status, **(extra or {})})
    conn.commit()
    return cur.lastrowid


def get_extractions(conn, report_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM extractions WHERE report_id=? ORDER BY id", (report_id,))]


# ------------------------------------------------------------- duplicates
def link_duplicate(conn, report_id, linked_report_id, score=None, reason="",
                   status="suspected", actor="dedup") -> None:
    """Record a possible duplicate. Reports are NEVER deleted or merged."""
    conn.execute(
        "INSERT OR REPLACE INTO duplicate_links (report_id, linked_report_id, score, reason, status)"
        " VALUES (?,?,?,?,?)", (report_id, linked_report_id, score, reason, status))
    audit.append(conn, actor, "duplicate.linked", "report", report_id,
                 {"linked": linked_report_id, "score": score, "status": status})
    conn.commit()


def get_duplicate_links(conn, report_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM duplicate_links WHERE report_id=? OR linked_report_id=?",
        (report_id, report_id))]


# -------------------------------------------------------------- resources
def add_resource(conn, kind, name, capacity, item=None, zone=None, window_start=None,
                 window_end=None, accessible=False) -> int:
    cur = conn.execute(
        "INSERT INTO resources (kind,name,item,capacity,remaining,zone,window_start,window_end,"
        "accessible) VALUES (?,?,?,?,?,?,?,?,?)",
        (kind, name, item, capacity, capacity, zone, window_start, window_end, int(accessible)))
    conn.commit()
    return cur.lastrowid


def list_open_resources(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM resources WHERE remaining > 0")]


def set_road_status(conn, from_zone, to_zone, is_open, actor="coordinator") -> None:
    conn.execute(
        "INSERT INTO roads (from_zone,to_zone,is_open) VALUES (?,?,?)"
        " ON CONFLICT(from_zone,to_zone) DO UPDATE SET is_open=excluded.is_open",
        (from_zone, to_zone, int(is_open)))
    audit.append(conn, actor, "road.updated", "road", f"{from_zone}->{to_zone}",
                 {"is_open": bool(is_open)})
    conn.commit()


# ------------------------------------------------------------ assignments
def propose_assignment(conn, report_id, resource_id, quantity=None, window_start=None,
                       window_end=None, evidence=None, actor="optimizer") -> int:
    """The solver proposes; nothing happens until a coordinator approves."""
    cur = conn.execute(
        "INSERT INTO assignments (report_id,resource_id,quantity,window_start,window_end,"
        "status,origin,evidence,proposed_at) VALUES (?,?,?,?,?,'proposed','optimizer',?,?)",
        (report_id, resource_id, quantity, window_start, window_end,
         json.dumps(evidence or {}, ensure_ascii=False), audit.now()))
    audit.append(conn, actor, "assignment.proposed", "assignment", cur.lastrowid,
                 {"report": report_id, "resource": resource_id, "quantity": quantity})
    conn.commit()
    return cur.lastrowid


def get_assignment(conn, assignment_id):
    return _row(conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone())


def list_assignments(conn, status=None):
    q, args = "SELECT * FROM assignments", ()
    if status:
        q, args = q + " WHERE status=?", (status,)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]


def decide_assignment(conn, assignment_id, decision, coordinator, reason=None,
                      new_resource_id=None) -> dict:
    """Human decision on a proposed assignment: approve | reject | override.

    * Only 'proposed' assignments can be decided (no re-deciding).
    * A coordinator name is required, and reject/override require a reason.
    * Approving deducts stock; it cannot go below zero.
    * Override marks the original 'overridden' and, if new_resource_id is given,
      creates a coordinator-origin assignment that is approved immediately.
    """
    if decision not in DECISIONS:
        raise RuleError(f"decision must be one of {sorted(DECISIONS)}")
    if not coordinator or not coordinator.strip():
        raise RuleError("a coordinator name is required")
    a = get_assignment(conn, assignment_id)
    if a is None:
        raise RuleError(f"assignment {assignment_id} not found")
    if a["status"] != "proposed":
        raise RuleError(f"assignment {assignment_id} is already {a['status']}")
    if decision in ("reject", "override") and not (reason and reason.strip()):
        raise RuleError(f"a reason is required to {decision}")

    ts = audit.now()
    if decision == "approve":
        _reserve_stock(conn, a["resource_id"], a["quantity"])
    conn.execute(
        "UPDATE assignments SET status=?, decided_by=?, decided_at=?, decision_reason=? WHERE id=?",
        (DECISIONS[decision], coordinator, ts, reason, assignment_id))
    audit.append(conn, coordinator, f"assignment.{DECISIONS[decision]}", "assignment",
                 assignment_id, {"reason": reason, "report": a["report_id"]})

    if decision == "override" and new_resource_id:
        _reserve_stock(conn, new_resource_id, a["quantity"])
        cur = conn.execute(
            "INSERT INTO assignments (report_id,resource_id,quantity,window_start,window_end,status,"
            "origin,evidence,proposed_at,decided_by,decided_at,decision_reason)"
            " VALUES (?,?,?,?,?,'approved','coordinator',?,?,?,?,?)",
            (a["report_id"], new_resource_id, a["quantity"], a["window_start"], a["window_end"],
             json.dumps({"overrides": assignment_id}), ts, coordinator, ts, reason))
        audit.append(conn, coordinator, "assignment.approved", "assignment", cur.lastrowid,
                     {"origin": "coordinator", "overrides": assignment_id})
    conn.commit()
    return get_assignment(conn, assignment_id)


def _reserve_stock(conn, resource_id, quantity) -> None:
    qty = quantity or 0
    r = conn.execute("SELECT remaining FROM resources WHERE id=?", (resource_id,)).fetchone()
    if r is None:
        raise RuleError(f"resource {resource_id} not found")
    if r["remaining"] < qty:
        raise RuleError(f"resource {resource_id} has only {r['remaining']} left (need {qty})")
    # Check and subtract in ONE statement, so two approvals arriving together can't both pass the
    # check above and push stock below zero.
    cur = conn.execute("UPDATE resources SET remaining = remaining - ? WHERE id=? AND remaining >= ?",
                       (qty, resource_id, qty))
    if cur.rowcount != 1:
        left = conn.execute("SELECT remaining FROM resources WHERE id=?", (resource_id,)).fetchone()["remaining"]
        raise RuleError(f"resource {resource_id} has only {left} left (need {qty})")


def dispatchable(conn):
    """What may actually be acted on: approved assignments only."""
    return list_assignments(conn, status="approved")


# ----------------------------------------------------------------- contacts
def reveal_contact(conn, household_token, actor, reason) -> dict:
    """Raw contact details, e.g. for a driver to call a household. Always audited."""
    if not (reason and reason.strip()):
        raise RuleError("a reason is required to view contact details")
    row = conn.execute("SELECT name, phone, address FROM contacts WHERE household_token=?",
                       (household_token,)).fetchone()
    if row is None:
        raise RuleError("unknown household")
    audit.append(conn, actor, "contact.revealed", "household", household_token,
                 {"reason": reason})
    conn.commit()
    return dict(row)


# ----------------------------------------------------- coordinator corrections
CORRECTABLE = {"need_type", "quantity", "people", "location_text", "zone", "urgency",
               "accessibility", "window_minutes", "translation_en", "confidence"}


def correct_extraction(conn, report_id, changes: dict, coordinator) -> int:
    """A coordinator fixes extracted fields. Adds a NEW extraction row (history is kept)."""
    if not (coordinator and coordinator.strip()):
        raise RuleError("a coordinator name is required")
    bad = set(changes) - CORRECTABLE
    if bad:
        raise RuleError(f"cannot correct fields: {sorted(bad)}")
    exts = get_extractions(conn, report_id)
    if not exts:
        raise RuleError(f"report {report_id} has no extraction to correct")
    base = exts[-1]
    merged = {k: base[k] for k in CORRECTABLE}
    merged["missing_fields"] = json.loads(base["missing_fields"] or "[]")
    merged.update(changes)
    numeric = {k: [base[k], changes[k]] for k in changes if k in ("quantity", "people", "window_minutes")}
    return save_extraction(conn, report_id, merged, model="coordinator-correction",
                           actor=coordinator, action="extraction.corrected",
                           extra={"fields": sorted(changes), "numeric_changes": numeric})


# --------------------------------------------------------- withdraw / reopen
def withdraw_assignment(conn, assignment_id, actor, reason) -> dict:
    """Undo a proposal or a decision (e.g. coordinator clicks Reopen). Restores stock if it was approved."""
    a = get_assignment(conn, assignment_id)
    if a is None:
        raise RuleError(f"assignment {assignment_id} not found")
    if a["status"] not in ("proposed", "approved", "rejected"):
        raise RuleError(f"assignment {assignment_id} is already {a['status']}")
    if a["status"] == "approved":
        conn.execute("UPDATE resources SET remaining = MIN(capacity, remaining + ?) WHERE id=?",
                     (a["quantity"] or 0, a["resource_id"]))
    conn.execute("UPDATE assignments SET status='withdrawn', decided_by=?, decided_at=?,"
                 " decision_reason=? WHERE id=?", (actor, audit.now(), reason, assignment_id))
    audit.append(conn, actor, "assignment.withdrawn", "assignment", assignment_id,
                 {"report": a["report_id"], "was": a["status"], "reason": reason})
    conn.commit()
    return get_assignment(conn, assignment_id)


# -------------------------------------------------------------------- roads
def add_road(conn, name, from_zone, to_zone, minutes, is_open=True) -> int:
    cur = conn.execute("INSERT INTO roads (name,from_zone,to_zone,minutes,is_open) VALUES (?,?,?,?,?)",
                       (name, from_zone, to_zone, minutes, int(is_open)))
    conn.commit()
    return cur.lastrowid


def set_road_by_name(conn, name, is_open, actor="coordinator") -> None:
    row = conn.execute("SELECT id FROM roads WHERE name=?", (name,)).fetchone()
    if row is None:
        raise RuleError(f"unknown road '{name}'")
    conn.execute("UPDATE roads SET is_open=? WHERE id=?", (int(is_open), row["id"]))
    audit.append(conn, actor, "road.updated", "road", name, {"is_open": bool(is_open)})
    conn.commit()
