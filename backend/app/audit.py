"""Append-only, hash-chained audit log.

Each row's hash covers its own fields plus the previous row's hash, so editing or
deleting a past row breaks the chain and verify_chain() reports where.
SQLite triggers in schema.sql also block UPDATE/DELETE on the table.
Never put raw PII in `details`.
"""
import hashlib
import json
from datetime import datetime, timezone

GENESIS = "0" * 64


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(prev_hash, ts, actor, action, entity_type, entity_id, details_json) -> str:
    payload = json.dumps([prev_hash, ts, actor, action, entity_type, entity_id, details_json],
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def append(conn, actor, action, entity_type, entity_id=None, details=None) -> int:
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    ts = now()
    details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    entity_id = None if entity_id is None else str(entity_id)
    h = _digest(prev, ts, actor, action, entity_type, entity_id, details_json)
    cur = conn.execute(
        "INSERT INTO audit_log (ts, actor, action, entity_type, entity_id, details, prev_hash, hash)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (ts, actor, action, entity_type, entity_id, details_json, prev, h))
    return cur.lastrowid


def verify_chain(conn):
    """Return (True, None) if intact, else (False, id_of_first_bad_row)."""
    prev = GENESIS
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        expected = _digest(prev, r["ts"], r["actor"], r["action"], r["entity_type"],
                           r["entity_id"], r["details"])
        if r["prev_hash"] != prev or r["hash"] != expected:
            return False, r["id"]
        prev = r["hash"]
    return True, None
