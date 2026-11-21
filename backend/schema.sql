-- Disaster relief coordination: database schema (SQLite).
-- All data is SYNTHETIC for the hackathon. Raw contact details live only in `contacts`.
-- The LLM and the dashboard read masked text from `reports`, never from `contacts`.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS contacts (          -- the only place raw PII is stored
  household_token TEXT PRIMARY KEY,            -- keyed hash, e.g. HH-7F3A21BC
  name            TEXT,
  phone           TEXT,
  address         TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  household_token TEXT REFERENCES contacts(household_token),
  channel         TEXT NOT NULL CHECK (channel IN ('sms','form','call','other')),
  language        TEXT,                          -- e.g. en, zh, hi, es
  text_masked     TEXT NOT NULL,                 -- phones/emails/names already scrubbed
  status          TEXT NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new','extracted','needs_review','superseded','closed')),
  is_synthetic    INTEGER NOT NULL DEFAULT 1,
  received_at     TEXT NOT NULL,
  corrects_id     INTEGER REFERENCES reports(id) -- set when this report corrects an earlier one
);

CREATE TABLE IF NOT EXISTS extractions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id     INTEGER NOT NULL REFERENCES reports(id),
  need_type     TEXT,                            -- water, food, medicine, blanket, shelter, ...
  quantity      INTEGER,
  people        INTEGER,
  location_text TEXT,
  zone          TEXT,
  urgency       TEXT CHECK (urgency IN ('low','medium','high','critical') OR urgency IS NULL),
  accessibility TEXT,
  window_minutes INTEGER,                        -- how long the household can wait
  translation_en TEXT,                           -- English translation shown to coordinators
  missing_fields TEXT,                           -- JSON list
  confidence    REAL CHECK (confidence BETWEEN 0 AND 1 OR confidence IS NULL),
  model         TEXT,                            -- model name or 'regex-baseline'
  from_cache    INTEGER NOT NULL DEFAULT 0,      -- 1 if a saved real model output was reused
  extracted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS duplicate_links (    -- links duplicates; NEVER deletes reports
  report_id        INTEGER NOT NULL REFERENCES reports(id),
  linked_report_id INTEGER NOT NULL REFERENCES reports(id),
  score            REAL,
  reason           TEXT,
  status           TEXT NOT NULL DEFAULT 'suspected'
                   CHECK (status IN ('suspected','confirmed_duplicate','separate_household')),
  PRIMARY KEY (report_id, linked_report_id),
  CHECK (report_id <> linked_report_id)
);

CREATE TABLE IF NOT EXISTS resources (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL CHECK (kind IN ('supply','vehicle','shelter')),
  name       TEXT NOT NULL,
  item       TEXT,                               -- water, food, ...
  capacity   INTEGER NOT NULL,
  remaining  INTEGER NOT NULL CHECK (remaining >= 0),
  zone       TEXT,
  window_start TEXT,
  window_end   TEXT,
  accessible INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS roads (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  name      TEXT UNIQUE,                         -- e.g. 'Bridge St'
  from_zone TEXT NOT NULL,
  to_zone   TEXT NOT NULL,
  minutes   INTEGER,                             -- travel time when open
  is_open   INTEGER NOT NULL DEFAULT 1,
  UNIQUE (from_zone, to_zone)
);

CREATE TABLE IF NOT EXISTS assignments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id    INTEGER NOT NULL REFERENCES reports(id),
  resource_id  INTEGER NOT NULL REFERENCES resources(id),
  quantity     INTEGER,
  window_start TEXT,
  window_end   TEXT,
  status       TEXT NOT NULL DEFAULT 'proposed'
               CHECK (status IN ('proposed','approved','rejected','overridden','withdrawn')),
  origin       TEXT NOT NULL DEFAULT 'optimizer' CHECK (origin IN ('optimizer','coordinator')),
  evidence     TEXT,                             -- JSON: why this was suggested
  proposed_at  TEXT NOT NULL,
  decided_by   TEXT,
  decided_at   TEXT,
  decision_reason TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (          -- append-only, hash-chained
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id   TEXT,
  details     TEXT,                              -- JSON, must contain no raw PII
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE INDEX IF NOT EXISTS idx_reports_household ON reports(household_token);
CREATE INDEX IF NOT EXISTS idx_extractions_report ON extractions(report_id);
CREATE INDEX IF NOT EXISTS idx_assignments_status ON assignments(status);
