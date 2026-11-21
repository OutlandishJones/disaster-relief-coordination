import heapq
import importlib
import json
import os
import unittest
from unittest import mock

from app import ai_bridge, dashboard_adapter as ad, db, map_layers, region


def reachable(name, closed=None):
    """Shortest minutes from the depot to every node, like the dashboard's planner."""
    graph = {}
    for n, a, b, m in region.roads(name):
        if n == closed:
            continue
        graph.setdefault(a, []).append((b, m)); graph.setdefault(b, []).append((a, m))
    best, todo = {"depot": 0}, [(0, "depot")]
    while todo:
        d, u = heapq.heappop(todo)
        for v, m in graph.get(u, []):
            if d + m < best.get(v, 1e9):
                best[v] = d + m; heapq.heappush(todo, (d + m, v))
    return best


class RegionFileTests(unittest.TestCase):
    def test_default_is_the_fictional_demo(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REGION", None)
            self.assertEqual(region.zone_ids(), {"riverside", "oak", "mill"})
            self.assertEqual(region.map_dir().name, "map")

    def test_demo_keeps_the_old_address_matching(self):
        a = region.zone_aliases("demo")
        for text, zone in [("riverside apts", "riverside"), ("quay rd", "riverside"), ("oak hollow", "oak"),
                           ("birch ct", "oak"), ("elm way", "oak"), ("millbrook", "mill"), ("weir ln", "mill"),
                           ("church st", "mill")]:
            self.assertEqual(a[text], zone, text)

    def test_unknown_region_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            region.load("atlantis")

    def test_every_region_is_consistent(self):
        for f in region.REGIONS.glob("*.json"):
            name = f.stem
            with self.subTest(region=name):
                r = region.load(name)
                zones = region.zone_ids(name)
                nodes = set(r["nodes"])
                self.assertIn("depot", nodes)
                self.assertTrue(zones <= nodes, "every zone needs a planner node")
                for n, a, b, m in r["roads"]:
                    self.assertTrue({a, b} <= nodes, n)
                    self.assertGreater(m, 0)
                self.assertTrue(zones <= set(reachable(name)), "every zone reachable from the depot")
                for s in r["reports"]:
                    self.assertIn(s["zone"], zones)
                    self.assertIn(s["need"], ad.KNOWN_NEEDS)
                    # the intake address alone places the report in its zone
                    with mock.patch.dict(os.environ, {"REGION": name}):
                        self.assertEqual(ai_bridge.zone_for(s["addr"]), s["zone"], s["addr"])
                layers = map_layers.load_map(region.map_dir(name))["features"]
                drawn = {f["properties"]["zone"] for f in layers if f["properties"]["layer"] == "zone"}
                self.assertEqual(drawn, zones)
                road_names = {f["properties"]["name"] for f in layers if f["properties"]["layer"] == "road"}
                self.assertEqual(road_names, {n for n, *_ in r["roads"]})


class PetersburgTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"REGION": "petersburg"})
        p.start(); self.addCleanup(p.stop)

    def test_addresses_land_in_real_neighborhoods(self):
        for text, zone in [("Witten St, Pocahontas Island", "pocahontas"), ("51 Gibbons St", "lakemont"),
                           ("we are near VSU", "ettrick"), ("E Bank St is underwater", "oldtowne"),
                           ("Upper Appomattox St", "battersea"), ("Slagle Ave and Hare St", "lakemont")]:
            self.assertEqual(ai_bridge.zone_for(text), zone, text)
        self.assertIsNone(ai_bridge.zone_for("12 Denton Ct, Millbrook"))   # demo names don't leak in

    def test_closing_campbells_bridge_reroutes_ettrick(self):
        self.assertEqual(reachable("petersburg")["ettrick"], 12)
        self.assertEqual(reachable("petersburg", closed="Campbell's Bridge")["ettrick"], 24)
        self.assertNotIn("pocahontas", reachable("petersburg", closed="Pocahontas St bridge"))

    def test_map_has_street_tiles_and_lonlat_data(self):
        m = map_layers.load_map()
        self.assertIn("{z}/{x}/{y}", m["basemap"]["url"])
        self.assertIn("OpenStreetMap", m["basemap"]["attribution"])
        lon, lat = next(f for f in m["features"] if f["properties"]["layer"] == "depot")["geometry"]["coordinates"]
        self.assertAlmostEqual(lat, 37.22, delta=0.05); self.assertAlmostEqual(lon, -77.40, delta=0.05)

    def test_seed_extract_and_link_in_petersburg(self):
        from scripts import seed_demo
        seed_demo = importlib.reload(seed_demo)
        self.addCleanup(lambda: importlib.reload(seed_demo))
        conn = db.connect(); db.init_db(conn)
        seed_demo.seed(conn, extractions=False)

        def fake(text, rid):      # reads need and zone from the text like the real extractor would
            s = seed_demo.SEED[int(rid) - 1]
            return {"report_id": rid, "need_type": s["need"], "quantity": s["qty"], "urgency": s["urg"],
                    "location": None, "confidence": 0.9}
        ai_bridge.run_pending(conn, fake, "ollama:fake", log=lambda *_: None)
        st = ad.build_state(conn)
        self.assertEqual(st["held_back"], 0, st["held"])
        self.assertEqual({r["hood"] for r in st["reports"]}, region.zone_ids())
        self.assertEqual(st["region"]["id"], "petersburg")
        self.assertEqual(st["region"]["zones"]["pocahontas"], "Pocahontas Island")
        self.assertEqual(set(st["region"]["nodes"]), set(region.load()["nodes"]))
        by = {r["id"]: r for r in st["reports"]}
        self.assertEqual(by["R-6"].get("dupOf"), "R-2")             # same phone, same need
        self.assertIn("separate household", by["R-5"].get("same", ""))   # same street, other phone
        self.assertTrue(all(r["phone"].startswith("•••-") for r in st["reports"]))

    def test_set_area_uses_region_zones(self):
        conn = db.connect(); db.init_db(conn)
        r = db.add_report(conn, "Need water", channel="sms", actor="t")
        db.save_extraction(conn, r["id"], {"need_type": "water", "quantity": 2}, model="t", actor="t")
        with self.assertRaises(db.RuleError):
            ad.set_zone(conn, r["id"], "mill", "Pat")            # a demo zone isn't valid here
        ad.set_zone(conn, r["id"], "lakemont", "Pat")
        self.assertEqual(ad.build_state(conn)["reports"][0]["hood"], "lakemont")


if __name__ == "__main__":
    unittest.main()
