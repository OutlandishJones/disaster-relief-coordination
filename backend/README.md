# Backend: API, database, privacy, audit log

Owner: Francis. Part of the Hack-on-the-Hill disaster relief project (Problem 2).
**All data is synthetic.** Nothing here is production-ready, and it isn't meant to be.

## Quick start

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # then edit the keys
export $(grep -v '^#' .env | xargs)                     # or set them in your shell
python -m unittest discover -s tests -t . -v            # should pass
python -m scripts.seed_demo --reset                     # load the synthetic scenario (no readings yet)
uvicorn app.main:app --reload --port 8000               # dashboard: http://localhost:8000/dashboard
                                                        # API docs:  http://localhost:8000/docs
```

If you set `API_KEY`, open the dashboard once as `/dashboard?key=<your key>`.
If you change `schema.sql`, delete `data/relief.db` and re-seed.

## Real extraction (do this before the demo)

`app/ai_bridge.py` connects Aminul's extractor (`ai/src/extraction.py`, Ollama + qwen2.5:7b) to
`db.save_extraction()`. Neither side renames fields; the mapping is at the top of `ai_bridge.py`.

```bash
# on a laptop with Ollama running and `ollama pull qwen2.5:7b` done
pip install ollama pydantic
python -m scripts.seed_demo --reset                     # 8 synthetic reports, stock, roads; NO hand-written fields
python -m scripts.run_extraction --live                # the real model reads each report's scrubbed text
uvicorn app.main:app --port 8000
```

* The extractor only receives `db.get_report_text()` (phones/emails/known names already scrubbed).
* `zone`: taken from the neighborhood the model extracts if it is a dashboard zone; otherwise from the
  intake form's address by a fixed lookup (not AI). The audit log records which (`zone_source`).
* `confidence` is the minimum of the model's need/location/urgency scores; the full dict is in the audit log.
* Failed extractions are kept and sent to review, never dropped.
* Duplicate links in this path are rule-based (same household token + same need = suspected duplicate;
  same address + different household = separate). Aminul's embedding dedupe is used on `--import`.
* `--redo` re-extracts everything as new rows; earlier extractions stay as history.
* `python -m scripts.run_extraction --import ../ai/data/extracted.json` loads saved real model output
  as new reports, marked `from_cache=1`. Those reports use Rampur/Green Park/Barrio Sur, which are not on
  the dashboard map, so they go to review and are counted as held back.

## Live intake

With the server running and Ollama up, a report posted to `POST /reports` is read by the model in
the background, linked against **all** earlier reports (same household + same need = likely
duplicate; same address + different household = separate), and appears on the dashboard within
about 5 seconds; the page checks for changes every 5 s, but not while you're typing.

* Reports are read one at a time. If the model isn't available, the report waits and the dashboard
  lists it under "not on the map yet" with the reason.
* `AUTO_EXTRACT=0` turns background reading off (the tests set this); then use
  `python -m scripts.run_extraction --live`.
* Report text is HTML-escaped on the dashboard, so a posted report can't inject script.

## Dashboard v2 (new layout, map layers) and how to switch back

| Address | Shows |
|---|---|
| `/dashboard` | The version named by `DASHBOARD_UI`: `v1` (default) or `v2` |
| `/dashboard/v1` | Always the original dashboard (`app/static/dashboard.html`, unchanged) |
| `/dashboard/v2` | Always the new one (`app/static/dashboard_v2.html`) |

v2 uses the same planning rules and backend calls as v1, and adds: a header with live status, a
connectivity alert, tabs (All / Low confidence / Not on map), **Set area** for held-back reports,
a map drawn from GeoJSON layers (roads, closures, no-signal areas, social vulnerability; each can
be switched off and the choice is remembered), and a "Classic view" link back to v1.

Map layers come from `data/map/*.geojson` (`app/map_layers.py`). The demo area is fictional.

## Real area: Petersburg / Ettrick (REGION)

`REGION` picks the area the whole system covers. One file per area in `data/regions/` holds its
zones and the street names that place an address in each zone, the planner's roads and travel
times, the map folder, street-map tiles and the sample reports `seed_demo` loads.

| `REGION` | Area |
|---|---|
| `demo` (default) | Fictional Riverside / Oak Hollow / Millbrook. Unchanged. |
| `petersburg` | Petersburg and Ettrick, VA, over an OpenStreetMap street map in v2 |

```powershell
$env:REGION="petersburg"
python -m scripts.seed_demo --reset          # Petersburg sample reports, stock and roads
python -m scripts.run_extraction --live      # or let the server read them
python -m uvicorn app.main:app --port 8000   # same window, so it sees REGION
```
Then open `/dashboard/v2?key=...`. Switch back: `Remove-Item Env:REGION`, reseed, restart. Always
reseed when switching: saved reports keep the area ids of the region they were read in
(`python -m scripts.check` warns if they don't match).

Petersburg zones: Ettrick (VSU), Battersea / West End, Old Towne, Pocahontas Island, Lakemont.
What is real and what isn't:
* **Real:** the street map (OpenStreetMap tiles, needs internet; offline the zones still draw),
  neighborhood and street names, which bridges connect which areas.
* **Approximate:** zone outlines (dashed) are hand-placed from landmark coordinates, not official
  boundaries. Planner routes are straight lines with estimated minutes. Social vulnerability is the
  city-wide ADAPT Virginia rating ("Very High") applied to each Petersburg zone; Ettrick has none.
* **Synthetic:** every report. Addresses use street names only, never house numbers.
* Demo tip: close **Campbell's Bridge** and Ettrick re-routes through Colonial Heights (12 to 24
  min); close the **Pocahontas St bridge** and Pocahontas Island has no open route.
* Tiles come from tile.openstreetmap.org, which is fine for a prototype but not for production
  use (their usage policy); a real deployment uses a tile provider or its own tile server.

**Reverting, from smallest to largest step:**
1. Use v1: `DASHBOARD_UI=v1` (the default), or open `/dashboard/v1`.
2. Turn the map layers off: `MAP_DIR=` an empty folder. v2 falls back to the simple v1 map.
3. Undo a whole change: each lands as its own commit (map layers, Set area, dashboard v2,
   OSM import, regions), so `git revert <commit>` removes exactly that one.
4. Back to the fictional area: unset `REGION` (the default is `demo`), reseed, restart. Untick
   **Street map** on the v2 map to hide the tiles without switching regions.

## The coordinator dashboard

`app/static/dashboard.html` is the existing dashboard with its UI and in-browser planning
code unchanged. Only its data source changed: it now loads from `GET /dashboard/state`
and saves every click through the API, so decisions persist and are audited.

| Dashboard action | Backend effect |
|---|---|
| Approve | Creates a proposal with its evidence, then a coordinator approves it. Stock is deducted. Audited. |
| Reject | Requires a reason (a prompt appears). Audited. |
| Reopen | Withdraws the decision and restores stock. Audited. |
| Quantity box | Adds a corrected extraction row. History is kept. Audited. |
| Close a road | Saved in `roads`, audited, and survives reload. One closure at a time. |
| Name field (new) | Sent as `X-Coordinator`. Required before any change, so every decision is attributable. |

Field mapping (dashboard to backend) is documented at the top of `app/dashboard_adapter.py`.
If the backend is unreachable, the page falls back to the built-in demo data with a red
"Offline demo mode" banner. Nothing is saved in that mode, so never present it as the live system.

Reports that can't be shown (no extraction yet, or an unknown neighborhood) are counted
in a line under the header instead of silently disappearing.

Streamlit users can skip the HTTP API and import the data layer directly:

```python
from backend.app import db
conn = db.connect("backend/data/relief.db"); db.init_db(conn)
```

## Layout

| File | What it does |
|---|---|
| `schema.sql` | Tables: contacts, reports, extractions, duplicate_links, resources, roads, assignments, audit_log |
| `app/privacy.py` | Scrubs phones, emails and known names; masks values; makes keyed household tokens |
| `app/audit.py` | Append-only, hash-chained audit log, with `verify_chain()` |
| `app/db.py` | All business rules (approval gate, stock checks, duplicate links). Tested. |
| `app/dashboard_adapter.py` | Shapes backend data for the dashboard and turns its clicks into audited actions |
| `app/static/dashboard.html` | The coordinator dashboard, wired to the backend |
| `app/main.py` | Thin FastAPI wrapper over `db.py` and the adapter |
| `app/ai_bridge.py` | Maps the extractor's output to `save_extraction()`; live and import modes; rule-based duplicate links |
| `scripts/seed_demo.py` | Loads the dashboard's 8 synthetic reports, stock, and roads (`--hand-written` adds hand-typed readings for offline use) |
| `scripts/run_extraction.py` | Runs the real extractor over the database (`--live`) or loads saved output (`--import`) |
| `scripts/check.py` | One-command health check: packages, settings, tests, Ollama, database, audit chain, server. Prints PASS / WARN / FAIL |
| `tests/` | `test_privacy.py`, `test_db.py`, `test_dashboard_adapter.py` (stdlib only), `test_api.py` (needs fastapi + httpx) |

## How each teammate plugs in

| Who | Reads | Writes |
|---|---|---|
| Ingest / data (Kameron) | | `db.add_report(...)` or `POST /reports` with synthetic text and contact fields |
| AI lead (Aminul) | `db.get_report_text(id)` (scrubbed text only) | `db.save_extraction(id, fields, model=..., from_cache=...)` |
| Dedup | `db.list_reports()` | `db.link_duplicate(a, b, score, reason)`. Never deletes. |
| Matching engineer | `db.list_open_resources()`, `roads` table | `db.propose_assignment(...)`. Proposals only. |
| Dashboard | `db.list_reports()`, `db.list_assignments()` | `db.decide_assignment(id, "approve"/"reject"/"override", coordinator, reason)` |

Extraction `fields` keys: `need_type, quantity, people, location_text, zone, urgency
(low|medium|high|critical), accessibility, missing_fields (list), confidence (0 to 1)`.
**Please do not rename these without telling Francis.** They are the shared schema.

## Rules the code enforces

1. **LLM only sees scrubbed text.** `get_report_text()` returns `text_masked`; raw contact details live only in `contacts`.
2. **Nothing is dispatchable until a coordinator approves.** `db.dispatchable()` returns approved assignments only.
3. **Decisions are attributable.** A coordinator name is required; reject and override also need a reason.
4. **Stock can't go below zero**, and approving deducts it.
5. **Duplicates are linked, never deleted**, and two households at one address get different tokens.
6. **Corrections supersede, never erase.** The original report stays.
7. **Every action is audited** in a hash-chained log with no raw PII. SQL triggers block UPDATE/DELETE, and `GET /audit/verify` detects tampering.
8. **Viewing raw contact details** requires a reason and is logged (`reveal_contact`).

## Before the demo: what is real and what is simulated

- The 8 reports are synthetic **inputs** (allowed). `seed_demo.py` writes no readings by default, so every reading
  comes from `run_extraction --live`. Only `--hand-written` adds hand-typed stand-ins (`model='synthetic-seed'`);
  `python -m scripts.check` fails if any are present.
- Real extraction leaves `window_minutes` empty unless the text states a time, so the dashboard shows the
  default 60 minutes and lists "Time window" as missing. That is accurate, not a bug.
- Suggestions are computed **in the browser** by the dashboard's JavaScript (`prioritized()` in dashboard.html):
  requests with no open route get nothing; requests that can arrive within their time window are served before late ones;
  critical > high > medium > low, then tightest window; a blank quantity never counts as met. The first-come-first-served
  baseline uses the same roads and stock. It is a priority rule, not an optimizer. The backend records, gates, and audits the decisions. If the matching engineer builds a server-side solver,
  have it call `db.propose_assignment()` and keep the dashboard as the review screen.
- Urgency is stored as low/medium/high/critical but the dashboard has 3 levels, so `critical` shows as "Urgent".
- The dashboard map layout (3 neighborhoods) is fixed. Zones other than riverside, oak, and mill are held back.

## Honest limits (use these in the presentation)

- Name scrubbing only works for names we already know (from the form's name field). A name written by a third party in free text will not be caught. Phones and emails are caught by regex.
- Auth is a single shared API key plus a self-declared `X-Coordinator` header. Fine for a demo, not for real use.
- No encryption at rest, no rate limiting, no real SMS/phone integration.
- SQLite with one process: no concurrency guarantees beyond SQLite's.
- `tests/test_api.py` needs `fastapi` and `httpx` installed; the data-layer tests do not.

## Git workflow

```bash
git checkout -b backend/api-db-privacy
git add backend/
git commit -m "Add backend: schema, privacy scrubbing, audit log, approval-gated API"
git push -u origin backend/api-db-privacy      # then open a Pull Request into main
```

Review checklist for the PR: tests pass, `.env` and `*.db` are not committed, no real personal data anywhere.
