"""Map layers for the v2 dashboard, read from GeoJSON files.

Every *.geojson file in MAP_DIR is merged into one FeatureCollection. Without MAP_DIR the folder
comes from the active region (REGION env, see app/region.py; the default region uses backend/data/map).
Each feature carries properties.layer, one of:
  zone       neighborhood polygon; properties.zone must match a zone id of the active region
             and may carry vulnerability = low | medium | high
  road       LineString; properties.name matches the roads table, so closures show on the map
  water      polygon, drawn only
  no_signal  polygon where people can't call or text (FCC coverage gaps or DIRS outages)
  depot      Point
Files produced by scripts/import_osm.py use the same shape, so real roads drop in file for file.
To switch the map off, point MAP_DIR at an empty folder: the dashboard falls back to its simple map.
"""
import json
import os
from pathlib import Path

from . import region

LAYERS = {"zone", "road", "water", "no_signal", "depot"}
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "map"


def map_dir() -> Path:
    return Path(os.environ.get("MAP_DIR") or region.map_dir())


def load_map(directory=None) -> dict:
    d = Path(directory) if directory else map_dir()
    features, names, attributions = [], [], []
    for f in sorted(d.glob("*.geojson")) if d.is_dir() else []:
        try:
            fc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                                   # a broken file is skipped, never fatal
        names.append(fc.get("name") or f.stem)
        if fc.get("attribution"):
            attributions.append(fc["attribution"])
        for feat in fc.get("features", []):
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            if props.get("layer") in LAYERS and geom.get("type") in ("Point", "LineString", "Polygon", "MultiPolygon"):
                features.append({"type": "Feature", "properties": props, "geometry": geom})
    out = {"type": "FeatureCollection", "sources": names, "attribution": " · ".join(attributions),
           "features": features}
    if directory is None:                 # the region's street-map tiles, if it has any
        out["basemap"] = region.load().get("basemap")
    return out
