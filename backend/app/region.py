"""Which area the system covers: zones, address matching, planner roads, map folder and sample reports.

    REGION=demo         (default) the fictional Riverside / Oak Hollow / Millbrook area
    REGION=petersburg   Petersburg and Ettrick, Virginia, on a real street map

Each region is one JSON file in backend/data/regions/. Switching back is unsetting REGION and
restarting; nothing else changes. Start from a fresh database when you switch (seed_demo --reset),
because saved reports keep the zone ids of the region they were read in.

Region file fields:
  name            shown in the dashboard header
  map_dir         folder (under backend/data) with the region's GeoJSON layers
  basemap         optional {"url": tile URL with {z}/{x}/{y}, "attribution": "..."}: street map tiles
  zones           [{"id", "name", "aliases": [text that places a report in this zone]}]
  nodes           {id: {"label", "x", "y"}} for the simple fallback diagram, or {"label", "lon", "lat"}
  roads           [[name, from_node, to_node, minutes]] the planner's road graph ("depot" is the start)
  stock           {item: count} supplies loaded by seed_demo
  reports         sample synthetic reports for seed_demo (same fields as scripts/seed_demo.py)
"""
import json
import os
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
REGIONS = DATA / "regions"
DEFAULT = "demo"


def region_name() -> str:
    return (os.environ.get("REGION") or DEFAULT).strip().lower()


@lru_cache(maxsize=8)
def _load(name: str) -> dict:
    path = REGIONS / f"{name}.json"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in REGIONS.glob("*.json")))
        raise SystemExit(f"REGION={name!r} not found. Available: {known}")
    r = json.loads(path.read_text(encoding="utf-8"))
    r["id"] = name
    return r


def load(name=None) -> dict:
    return _load((name or region_name()).strip().lower())


def zone_ids(name=None) -> set:
    return {z["id"] for z in load(name)["zones"]}


def zone_names(name=None) -> dict:
    return {z["id"]: z["name"] for z in load(name)["zones"]}


def zone_aliases(name=None) -> dict:
    """alias text (lower case) -> zone id. The zone id and its name always count as aliases."""
    out = {}
    for z in load(name)["zones"]:
        for a in [z["id"], z["name"], *z.get("aliases", [])]:
            out[a.casefold()] = z["id"]
    return out


def roads(name=None) -> list:
    return [tuple(r) for r in load(name)["roads"]]


def map_dir(name=None) -> Path:
    return DATA / load(name).get("map_dir", "map")


def nodes_px(name=None, w=360, h=260, pad=36) -> dict:
    """Node positions for the simple fallback diagram: {id: [x, y, label]} in a w x h box."""
    nodes = load(name).get("nodes", {})
    if all("x" in n for n in nodes.values()):
        return {k: [n["x"], n["y"], n["label"]] for k, n in nodes.items()}
    lons = [n["lon"] for n in nodes.values()]
    lats = [n["lat"] for n in nodes.values()]
    sx = (w - 2 * pad) / ((max(lons) - min(lons)) or 1)
    sy = (h - 2 * pad) / ((max(lats) - min(lats)) or 1)
    return {k: [round(pad + (n["lon"] - min(lons)) * sx), round(pad + (max(lats) - n["lat"]) * sy), n["label"]]
            for k, n in nodes.items()}


def public(name=None) -> dict:
    """What the dashboard needs to know about the region (no sample data)."""
    r = load(name)
    return {"id": r["id"], "name": r["name"], "zones": zone_names(name), "nodes": nodes_px(name),
            "basemap": r.get("basemap")}
