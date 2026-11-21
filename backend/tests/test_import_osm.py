import json
import os
import tempfile
import unittest
from pathlib import Path

from app import map_layers
from scripts import import_osm

OVERPASS = {"elements": [
    {"type": "way", "id": 1, "tags": {"highway": "residential", "name": "Mill Rd"},
     "geometry": [{"lat": 37.21, "lon": -77.40}, {"lat": 37.22, "lon": -77.39}]},
    {"type": "way", "id": 2, "tags": {"highway": "service"}, "geometry": [{"lat": 37.21, "lon": -77.41}]},
    {"type": "node", "id": 3, "lat": 37.2, "lon": -77.4}]}


class ImportOsmTests(unittest.TestCase):
    def test_overpass_to_map_layer(self):
        fc = import_osm.to_geojson(OVERPASS)
        self.assertEqual(len(fc["features"]), 1)                      # 1-point way and node skipped
        f = fc["features"][0]
        self.assertEqual(f["properties"]["layer"], "road")
        self.assertEqual(f["geometry"]["coordinates"][0], [-77.40, 37.21])   # GeoJSON is [lon, lat]
        self.assertIn("OpenStreetMap", fc["attribution"])

    def test_cli_writes_a_folder_the_dashboard_reads(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d, "o.json"), Path(d, "osm", "roads.geojson")
            src.write_text(json.dumps(OVERPASS))
            import_osm.main(["--from-file", str(src), "--out", str(out)])
            m = map_layers.load_map(out.parent)
            self.assertEqual([f["properties"]["name"] for f in m["features"]], ["Mill Rd"])

    def test_query_rejects_bad_or_huge_boxes(self):
        self.assertIn("highway", import_osm.query("37.20,-77.42,37.24,-77.37"))
        with self.assertRaises(SystemExit):
            import_osm.query("37.24,-77.42,37.20,-77.37")             # south > north
        with self.assertRaises(SystemExit):
            import_osm.query("36,-80,38,-76")                          # far too big


if __name__ == "__main__":
    unittest.main()
