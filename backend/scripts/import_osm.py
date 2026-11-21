"""Download real roads from OpenStreetMap into a map layer the v2 dashboard can draw.

    cd backend
    python -m scripts.import_osm --bbox 37.20,-77.42,37.24,-77.37          # south,west,north,east
    MAP_DIR=data/map_osm  (PowerShell: $env:MAP_DIR="data/map_osm")  then restart the server

Writes data/map_osm/roads.geojson; the fictional demo map in data/map is left alone, so switching
back is just unsetting MAP_DIR. --from-file reads a saved Overpass JSON instead of downloading.

Roads come from the public Overpass API and are OpenStreetMap data (ODbL licence): the dashboard
shows "© OpenStreetMap contributors". Notes:
* Keep the box small (a town or a few neighborhoods); Overpass refuses very large requests.
* Real road names only show as closed on the map if they match a name in the roads table.
  Planning still uses the roads table, not this geometry (see the improvements backlog).
* Zone polygons, no-signal areas and vulnerability are separate layers you add as their own
  .geojson files in the same folder (same shape as data/map/demo.geojson).
"""
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = "https://overpass-api.de/api/interpreter"
KINDS = "motorway|trunk|primary|secondary|tertiary|unclassified|residential|service"


def query(bbox: str) -> str:
    s, w, n, e = (float(v) for v in bbox.split(","))
    if not (s < n and w < e) or (n - s) * (e - w) > 0.25:
        raise SystemExit("bbox must be south,west,north,east and small (about a town)")
    return f'[out:json][timeout:60];way["highway"~"^({KINDS})$"]({s},{w},{n},{e});out geom;'


def fetch(bbox: str) -> dict:
    data = urllib.parse.urlencode({"data": query(bbox)}).encode()
    req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": "relief-coordination-hackathon/0.1"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def to_geojson(overpass: dict) -> dict:
    feats = []
    for el in overpass.get("elements", []):
        geom = el.get("geometry") or []
        if el.get("type") != "way" or len(geom) < 2:
            continue
        tags = el.get("tags", {})
        feats.append({"type": "Feature",
                      "properties": {"layer": "road", "name": tags.get("name", ""),
                                     "highway": tags.get("highway", ""), "osm_id": el.get("id")},
                      "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in geom]}})
    return {"type": "FeatureCollection", "name": "OpenStreetMap roads",
            "attribution": "© OpenStreetMap contributors (ODbL)", "features": feats}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--bbox", help="south,west,north,east in degrees")
    p.add_argument("--from-file", help="a saved Overpass JSON response")
    p.add_argument("--out", default="data/map_osm/roads.geojson")
    a = p.parse_args(argv)
    if not (a.bbox or a.from_file):
        p.error("give --bbox or --from-file")
    raw = json.loads(Path(a.from_file).read_text(encoding="utf-8")) if a.from_file else fetch(a.bbox)
    fc = to_geojson(raw)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc), encoding="utf-8")
    named = sum(1 for f in fc["features"] if f["properties"]["name"])
    print(f"{len(fc['features'])} roads ({named} named) written to {out}")
    print(f"Use them: set MAP_DIR={out.parent} and restart the server.")


if __name__ == "__main__":
    main()
