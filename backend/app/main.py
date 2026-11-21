"""Thin FastAPI wrapper over app/db.py. Run from the backend/ folder:

    python -m scripts.seed_demo --reset        # load the synthetic scenario (first time)
    uvicorn app.main:app --reload --port 8000  # then open http://localhost:8000/dashboard

Auth (demo level): if API_KEY is set, requests must send header  X-API-Key: <key>.
Coordinator decisions must also send  X-Coordinator: <name>  so every approval is attributable.
"""
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import ai_bridge, audit, dashboard_adapter as dash, db, map_layers

DB_PATH = lambda: os.environ.get("DB_PATH", "data/relief.db")  # noqa: E731


class UTF8JSONResponse(JSONResponse):
    """Say the charset out loud: Windows PowerShell 5.1 otherwise decodes JSON as Latin-1 and
    shows Vietnamese or Chinese report text garbled."""
    media_type = "application/json; charset=utf-8"


@asynccontextmanager
async def lifespan(app):
    conn = db.connect(DB_PATH())
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(title="Disaster Relief Coordination API", version="0.1.0", lifespan=lifespan,
              default_response_class=UTF8JSONResponse)
STATIC = Path(__file__).resolve().parent / "static"

# Only needed if the dashboard HTML is opened from a file or another port during development.
if os.environ.get("CORS_ALLOW_ALL") == "1":
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(db.RuleError)
async def rule_error_handler(request: Request, exc: db.RuleError):
    return UTF8JSONResponse(status_code=400, content={"detail": str(exc)})


def get_conn():
    conn = db.connect(DB_PATH())
    try:
        yield conn
    finally:
        conn.close()


def require_key(x_api_key: Optional[str] = Header(default=None)):
    expected = os.environ.get("API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


Auth = [Depends(require_key)]


# ---------------------------------------------------------------- models
class ReportIn(BaseModel):
    text: str = Field(min_length=1)
    channel: str = "sms"
    language: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_address: Optional[str] = None
    is_synthetic: bool = True
    corrects_id: Optional[int] = None


class ExtractionIn(BaseModel):
    need_type: Optional[str] = None
    quantity: Optional[int] = None
    people: Optional[int] = None
    location_text: Optional[str] = None
    zone: Optional[str] = None
    urgency: Optional[str] = None
    accessibility: Optional[str] = None
    missing_fields: list[str] = []
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    model: str = "unknown"
    from_cache: bool = False


class DuplicateIn(BaseModel):
    report_id: int
    linked_report_id: int
    score: Optional[float] = None
    reason: str = ""
    status: str = "suspected"


class ResourceIn(BaseModel):
    kind: str
    name: str
    capacity: int = Field(ge=0)
    item: Optional[str] = None
    zone: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    accessible: bool = False


class RoadIn(BaseModel):
    from_zone: str
    to_zone: str
    is_open: bool


class ProposalIn(BaseModel):
    report_id: int
    resource_id: int
    quantity: Optional[int] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    evidence: dict = {}


class DecisionIn(BaseModel):
    decision: str            # approve | reject | override
    reason: Optional[str] = None
    new_resource_id: Optional[int] = None


class RevealIn(BaseModel):
    reason: str


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------- live intake
# New reports are read by the local model in the background, one at a time, as soon as they
# arrive. Set AUTO_EXTRACT=0 to turn this off (then run scripts.run_extraction by hand).
log = logging.getLogger("relief")
_extract_lock = threading.Lock()
_extractor = {"fn": None, "model": None, "error": None}


def get_extractor():
    """Load Aminul's extractor once. Tests can set _extractor["fn"] and ["model"] directly."""
    if _extractor["fn"] is None and _extractor["error"] is None:
        try:
            _extractor["fn"], _extractor["model"] = ai_bridge.load_extractor()
        except Exception as e:          # ollama not installed, model missing, etc.
            _extractor["error"] = f"{type(e).__name__}: {e}"
            log.warning("live extraction unavailable: %s", _extractor["error"])
    return _extractor["fn"], _extractor["model"]


def extract_in_background(report_id: int):
    fn, model = get_extractor()
    if fn is None:
        return
    with _extract_lock:                 # one model, one report at a time
        conn = db.connect(DB_PATH())
        try:
            ai_bridge.extract_one(conn, report_id, fn, model, log=log.info)
        except Exception as e:          # the report stays 'new' and shows as held back
            log.warning("extraction failed for report %s: %s", report_id, e)
        finally:
            conn.close()


@app.post("/reports", dependencies=Auth)
def create_report(body: ReportIn, tasks: BackgroundTasks, conn=Depends(get_conn)):
    r = db.add_report(conn, body.text, body.channel, body.language, body.contact_name,
                      body.contact_phone, body.contact_address, body.is_synthetic,
                      body.corrects_id, actor="api")
    if os.environ.get("AUTO_EXTRACT", "1") != "0":
        tasks.add_task(extract_in_background, r["id"])
        r = {**r, "extraction": "queued"}
    else:
        r = {**r, "extraction": "off"}
    return r


@app.get("/reports", dependencies=Auth)
def list_reports(status: Optional[str] = None, conn=Depends(get_conn)):
    return db.list_reports(conn, status)


@app.get("/reports/{report_id}", dependencies=Auth)
def get_report(report_id: int, conn=Depends(get_conn)):
    r = db.get_report(conn, report_id)
    if r is None:
        raise HTTPException(404, "report not found")
    return {**r, "extractions": db.get_extractions(conn, report_id),
            "duplicate_links": db.get_duplicate_links(conn, report_id)}


@app.post("/reports/{report_id}/extraction", dependencies=Auth)
def save_extraction(report_id: int, body: ExtractionIn, conn=Depends(get_conn)):
    data = body.model_dump()
    model, cached = data.pop("model"), data.pop("from_cache")
    eid = db.save_extraction(conn, report_id, data, model=model, from_cache=cached)
    return {"extraction_id": eid}


@app.post("/duplicates", dependencies=Auth)
def link_duplicate(body: DuplicateIn, conn=Depends(get_conn)):
    db.link_duplicate(conn, body.report_id, body.linked_report_id, body.score, body.reason,
                      body.status)
    return {"ok": True}


@app.post("/resources", dependencies=Auth)
def add_resource(body: ResourceIn, conn=Depends(get_conn)):
    return {"resource_id": db.add_resource(conn, **body.model_dump())}


@app.get("/resources", dependencies=Auth)
def list_resources(conn=Depends(get_conn)):
    return db.list_open_resources(conn)


@app.post("/roads", dependencies=Auth)
def set_road(body: RoadIn, x_coordinator: Optional[str] = Header(default="coordinator"),
             conn=Depends(get_conn)):
    db.set_road_status(conn, body.from_zone, body.to_zone, body.is_open, actor=x_coordinator)
    return {"ok": True}


@app.post("/assignments", dependencies=Auth)
def propose(body: ProposalIn, conn=Depends(get_conn)):
    return {"assignment_id": db.propose_assignment(
        conn, body.report_id, body.resource_id, body.quantity, body.window_start,
        body.window_end, body.evidence)}


@app.get("/assignments", dependencies=Auth)
def list_assignments(status: Optional[str] = None, conn=Depends(get_conn)):
    return db.list_assignments(conn, status)


@app.post("/assignments/{assignment_id}/decision", dependencies=Auth)
def decide(assignment_id: int, body: DecisionIn,
           x_coordinator: Optional[str] = Header(default=None), conn=Depends(get_conn)):
    return db.decide_assignment(conn, assignment_id, body.decision, x_coordinator or "",
                                body.reason, body.new_resource_id)


@app.get("/dispatchable", dependencies=Auth)
def dispatchable(conn=Depends(get_conn)):
    return db.dispatchable(conn)


@app.post("/households/{token}/reveal", dependencies=Auth)
def reveal(token: str, body: RevealIn, x_coordinator: Optional[str] = Header(default=None),
           conn=Depends(get_conn)):
    return db.reveal_contact(conn, token, x_coordinator or "", body.reason)


@app.get("/audit", dependencies=Auth)
def get_audit(limit: int = 100, conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/audit/verify", dependencies=Auth)
def verify_audit(conn=Depends(get_conn)):
    ok, bad_id = audit.verify_chain(conn)
    return {"intact": ok, "first_bad_row": bad_id}


# ------------------------------------------------- coordinator dashboard
class ActIn(BaseModel):
    status: str                      # approved | rejected | pending (reopen)
    quantity: Optional[int] = None
    reason: Optional[str] = None
    evidence: dict = {}


class QuantityIn(BaseModel):
    quantity: int = Field(ge=0, le=100000)


class BlockIn(BaseModel):
    name: str = ""                   # road to close; "" reopens everything


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard")


# Two dashboards side by side. /dashboard serves the one named by DASHBOARD_UI (v1 by default);
# /dashboard/v1 and /dashboard/v2 always serve that version. Switching back = change one setting.
DASHBOARDS = {"v1": "dashboard.html", "v2": "dashboard_v2.html"}


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC / DASHBOARDS.get(os.environ.get("DASHBOARD_UI", "v1"), "dashboard.html"))


@app.get("/dashboard/v1", include_in_schema=False)
def dashboard_v1():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/dashboard/v2", include_in_schema=False)
def dashboard_v2():
    return FileResponse(STATIC / "dashboard_v2.html")


@app.get("/dashboard/state", dependencies=Auth)
def dashboard_state(conn=Depends(get_conn)):
    return dash.build_state(conn)


@app.get("/dashboard/map", dependencies=Auth)
def dashboard_map():
    """GeoJSON layers for the v2 dashboard map (see app/map_layers.py)."""
    return map_layers.load_map()


@app.post("/dashboard/reports/{report_id}/act", dependencies=Auth)
def dashboard_act(report_id: int, body: ActIn, x_coordinator: Optional[str] = Header(default=None),
                  conn=Depends(get_conn)):
    dash.act_on_report(conn, report_id, body.status, x_coordinator or "", body.quantity,
                       body.reason, body.evidence)
    return dash.build_state(conn)


@app.post("/dashboard/reports/{report_id}/quantity", dependencies=Auth)
def dashboard_quantity(report_id: int, body: QuantityIn,
                       x_coordinator: Optional[str] = Header(default=None), conn=Depends(get_conn)):
    dash.set_quantity(conn, report_id, body.quantity, x_coordinator or "")
    return dash.build_state(conn)


class ZoneIn(BaseModel):
    zone: str


@app.post("/dashboard/reports/{report_id}/zone", dependencies=Auth)
def dashboard_zone(report_id: int, body: ZoneIn,
                   x_coordinator: Optional[str] = Header(default=None), conn=Depends(get_conn)):
    dash.set_zone(conn, report_id, body.zone, x_coordinator or "")
    return dash.build_state(conn)


class NeedIn(BaseModel):
    need: str


@app.post("/dashboard/reports/{report_id}/need", dependencies=Auth)
def dashboard_need(report_id: int, body: NeedIn,
                   x_coordinator: Optional[str] = Header(default=None), conn=Depends(get_conn)):
    dash.set_need(conn, report_id, body.need, x_coordinator or "")
    return dash.build_state(conn)


@app.post("/dashboard/road", dependencies=Auth)
def dashboard_road(body: BlockIn, x_coordinator: Optional[str] = Header(default=None),
                   conn=Depends(get_conn)):
    dash.set_blocked_road(conn, body.name, x_coordinator or "")
    return dash.build_state(conn)
