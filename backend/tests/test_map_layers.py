import json
import os
import tempfile
import unittest
from pathlib import Path

from app import map_layers
from scripts import seed_demo


class MapLayerTests(unittest.TestCase):
    def test_demo_map_matches_dashboard_zones_and_roads(self):
        m = map_layers.load_map()
        layers = {}
        for f in m["features"]:
            layers.setdefault(f["properties"]["layer"], []).append(f["properties"])
        self.assertEqual({z["zone"] for z in layers["zone"]}, {"riverside", "oak", "mill"})
        self.assertEqual({r["name"] for r in layers["road"]}, {n for n, *_ in seed_demo.ROADS})
        self.assertIn("no_signal", layers)
        self.assertIn("fictional", m["attribution"].lower())

    def test_unknown_layers_and_broken_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.geojson").write_text("{not json")
            Path(d, "ok.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"layer": "road", "name": "A"},
                 "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}},
                {"type": "Feature", "properties": {"layer": "billboard"},
                 "geometry": {"type": "Point", "coordinates": [0, 0]}}]}))
            m = map_layers.load_map(d)
            self.assertEqual([f["properties"]["name"] for f in m["features"]], ["A"])

    def test_empty_folder_turns_map_off(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["MAP_DIR"] = d
            try:
                self.assertEqual(map_layers.load_map()["features"], [])
            finally:
                del os.environ["MAP_DIR"]


if __name__ == "__main__":
    unittest.main()
