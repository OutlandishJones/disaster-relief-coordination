import json
import unittest
from pathlib import Path

from app import ai_bridge, audit, dashboard_adapter, db
from scripts import seed_demo

AI_DATA = Path(__file__).resolve().parents[2] / "ai" / "data"


def ext(**kw):
    """Shaped exactly like ai/src/extraction.py output."""
    base = {"language": "en", "need_type": "water", "quantity": 4, "neighborhood": None,
            "address": None, "landmark": None, "household_size": 4, "household_note": None,
            "urgency": "high", "urgency_cues": [], "accessibility_needs": [], "time_window": None,
            "missing_fields": ["landmark", "time_window"],
            "confidence": {"need_type": 0.95, "quantity": 0.9, "location": 0.8, "urgency": 0.85},
            "translation_en": "", "report_id": "1", "raw_text": "", "needs_review": False}
    base.update(kw)
    return base


class FakeExtractor:
    """Stands in for Ollama in tests only. Records what text it was given."""
    NEEDS = [("insulin", "medicine"), ("medicin", "medicine"), ("water", "water"), ("chăn", "blanket"),
             ("blanket", "blanket"), ("避难", "shelter"), ("food", "food")]

    def __init__(self):
        self.seen = []

    def __call__(self, text, report_id=""):
        self.seen.append(text)
        need = next((n for k, n in self.NEEDS if k in text.lower()), "other")
        return ext(need_type=need, report_id=report_id, raw_text=text)


class MappingTests(unittest.TestCase):
    def test_field_mapping(self):
        f, review, extra = ai_bridge.to_fields(ext(
            need_type="blanket", neighborhood="Oak Hollow", address="Birch Ct", household_size=3,
            accessibility_needs=["wheelchair", "elderly"], time_window="within 2 hours",
            translation_en="call 757-555-0142"))
        self.assertEqual(f["need_type"], "blankets")
        self.assertEqual(f["zone"], "oak")
        self.assertEqual(f["people"], 3)
        self.assertEqual(f["location_text"], "Oak Hollow, Birch Ct")
        self.assertEqual(f["accessibility"], "wheelchair; elderly")
        self.assertEqual(f["window_minutes"], 120)
        self.assertEqual(f["confidence"], 0.8)          # min of need/location/urgency
        self.assertNotIn("757", f["translation_en"])
        self.assertFalse(review)
        self.assertEqual(extra["zone_source"], "extractor")

    def test_quantity_falls_back_to_people_for_per_person_needs(self):
        f, _, extra = ai_bridge.to_fields(ext(quantity=None, household_size=4))
        self.assertEqual((f["quantity"], extra["quantity_source"]), (4, "people_count"))
        f, _, _ = ai_bridge.to_fields(ext(need_type="medicine", quantity=None, household_size=2))
        self.assertIsNone(f["quantity"])

    def test_unknown_zone_uses_intake_then_review(self):
        f, review, extra = ai_bridge.to_fields(ext(neighborhood="Nowhere"), fallback_zone="mill")
        self.assertEqual((f["zone"], extra["zone_source"], review), ("mill", "intake_form", False))
        f, review, _ = ai_bridge.to_fields(ext(needs_review=True, confidence={
            "need_type": 0.9, "location": 0.1, "urgency": 0.3}), fallback_zone="mill")
        self.assertTrue(review)                          # low urgency confidence still reviewed
        f, review, _ = ai_bridge.to_fields(ext(needs_review=True, confidence={
            "need_type": 0.9, "location": 0.1, "urgency": 0.8}), fallback_zone="mill")
        self.assertEqual((review, f["confidence"]), (False, 0.8))   # no text location is fine
        f, review, _ = ai_bridge.to_fields(ext(neighborhood="Nowhere"))
        self.assertIsNone(f["zone"])
        self.assertTrue(review)

    def test_zone_alias_is_whole_word(self):
        self.assertIsNone(ai_bridge.zone_for("Oakland"))
        self.assertEqual(ai_bridge.zone_for("Riverside Apts, Bldg 12"), "riverside")

    def test_failed_extraction_is_kept_and_reviewed(self):
        conn = db.connect(); db.init_db(conn)
        r = db.add_report(conn, "help please water")
        ai_bridge.save(conn, r["id"], {"report_id": "1", "needs_review": True, "error": "bad json"},
                       "ollama:test")
        self.assertEqual(db.get_report(conn, r["id"])["status"], "needs_review")
        self.assertEqual(len(db.get_extractions(conn, r["id"])), 1)


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(); db.init_db(self.conn)
        seed_demo.seed(self.conn, extractions=False)
        self.fake = FakeExtractor()
        ai_bridge.run_pending(self.conn, self.fake, "ollama:fake", log=lambda *_: None)

    def test_extractor_only_sees_scrubbed_text(self):
        self.assertEqual(len(self.fake.seen), 8)
        self.assertFalse(any("757-555" in t for t in self.fake.seen))

    def test_no_synthetic_seed_rows(self):
        models = {r[0] for r in self.conn.execute("SELECT model FROM extractions")}
        self.assertEqual(models, {"ollama:fake"})

    def test_dashboard_shows_real_extractions(self):
        st = dashboard_adapter.build_state(self.conn)
        self.assertEqual(len(st["reports"]), 8)
        self.assertEqual(st["held_back"], 0)
        zones = [r["hood"] for r in st["reports"]]
        self.assertEqual(zones, ["riverside", "oak", "mill", "riverside", "riverside", "oak", "mill", "oak"])
        self.assertTrue(all(r["crit"] is False for r in st["reports"]))   # fake extractor says "high"

    def test_rule_based_duplicate_links(self):
        links = {(r["report_id"], r["linked_report_id"], r["status"])
                 for r in self.conn.execute("SELECT * FROM duplicate_links")}
        self.assertIn((6, 2, "suspected"), links)              # same phone, same need
        self.assertIn((5, 4, "separate_household"), links)     # same building, other phone
        self.assertEqual(len(db.list_reports(self.conn)), 8)    # nothing deleted

    def test_rerun_is_idempotent_and_redo_keeps_history(self):
        self.assertEqual(ai_bridge.run_pending(self.conn, self.fake, "x", log=lambda *_: None), [])
        ai_bridge.run_pending(self.conn, self.fake, "ollama:fake2", redo=True, log=lambda *_: None)
        self.assertEqual(len(db.get_extractions(self.conn, 1)), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM duplicate_links").fetchone()[0], 2)
        self.assertEqual(audit.verify_chain(self.conn), (True, None))


class LiveIntakeTests(unittest.TestCase):
    """A report that arrives after the others is read on its own and linked to ALL earlier reports."""
    def setUp(self):
        self.conn = db.connect(); db.init_db(self.conn)
        seed_demo.seed(self.conn, extractions=False)
        self.fake = FakeExtractor()
        ai_bridge.run_pending(self.conn, self.fake, "ollama:fake", log=lambda *_: None)

    def contact(self, rid):
        return self.conn.execute("SELECT c.phone, c.address FROM reports r JOIN contacts c USING"
                                 " (household_token) WHERE r.id=?", (rid,)).fetchone()

    def links_from(self, rid):
        return {(r["linked_report_id"], r["status"]) for r in
                self.conn.execute("SELECT * FROM duplicate_links WHERE report_id=?", (rid,))}

    def test_repeat_text_links_to_earlier_report(self):
        c = self.contact(2)                                   # R-2: insulin, Spanish
        r = db.add_report(self.conn, "Insulina, por favor, otra vez", contact_phone=c["phone"],
                          contact_address=c["address"])
        ai_bridge.extract_one(self.conn, r["id"], self.fake, "ollama:fake", log=lambda *_: None)
        self.assertIn((2, "suspected"), self.links_from(r["id"]))
        st = dashboard_adapter.build_state(self.conn)
        self.assertEqual(next(x for x in st["reports"] if x["dbid"] == r["id"])["dupOf"], "R-2")

    def test_neighbor_stays_separate(self):
        c = self.contact(4)
        r = db.add_report(self.conn, "Family of 4 needs food", contact_phone="757-555-0111",
                          contact_address=c["address"])
        ai_bridge.extract_one(self.conn, r["id"], self.fake, "ollama:fake", log=lambda *_: None)
        self.assertIn((4, "separate_household"), self.links_from(r["id"]))
        self.assertFalse(any(s == "suspected" for _, s in self.links_from(r["id"])))

    def test_rerun_adds_no_duplicate_links(self):
        before = self.conn.execute("SELECT COUNT(*) FROM duplicate_links").fetchone()[0]
        ai_bridge.link_by_household(self.conn, range(1, 9))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM duplicate_links").fetchone()[0], before)

    def test_coordinator_can_place_a_held_report(self):
        r = db.add_report(self.conn, "water please, near the old school")
        ai_bridge.save(self.conn, r["id"], ext(neighborhood="Atlantis"), "ollama:fake")
        self.assertEqual(dashboard_adapter.build_state(self.conn)["held_back"], 1)
        with self.assertRaises(db.RuleError):
            dashboard_adapter.set_zone(self.conn, r["id"], "atlantis", "Francis")
        with self.assertRaises(db.RuleError):
            dashboard_adapter.set_zone(self.conn, r["id"], "oak", "")      # name required
        dashboard_adapter.set_zone(self.conn, r["id"], "oak", "Francis")
        st = dashboard_adapter.build_state(self.conn)
        self.assertEqual(st["held_back"], 0)
        self.assertEqual(next(x for x in st["reports"] if x["dbid"] == r["id"])["hood"], "oak")
        rows = db.get_extractions(self.conn, r["id"])
        self.assertEqual([x["model"] for x in rows], ["ollama:fake", "coordinator-correction"])

    def test_coordinator_can_set_the_need_of_a_held_report(self):
        r = db.add_report(self.conn, "Gia đình chúng tôi có 5 người, trong đó có 3 trẻ em cần sơ cứu",
                          contact_address="12 Denton Ct, Millbrook")
        ai_bridge.save(self.conn, r["id"], ext(need_type="other"), "ollama:fake", fallback_zone="mill")
        st = dashboard_adapter.build_state(self.conn)
        self.assertIn("isn't one the planner handles", next(h for h in st["held"] if h["dbid"] == r["id"])["reason"])
        with self.assertRaises(db.RuleError):
            dashboard_adapter.set_need(self.conn, r["id"], "other", "Francis")
        dashboard_adapter.set_need(self.conn, r["id"], "medicine", "Francis")
        st = dashboard_adapter.build_state(self.conn)
        card = next(x for x in st["reports"] if x["dbid"] == r["id"])
        self.assertEqual((card["need"], card["hood"]), ("medicine", "mill"))
        self.assertEqual(db.get_extractions(self.conn, r["id"])[-1]["model"], "coordinator-correction")

    def test_held_reports_are_listed_with_a_reason(self):
        waiting = db.add_report(self.conn, "help")
        other = db.add_report(self.conn, "need a ride to the clinic",
                              contact_address="Birch Ct, Oak Hollow")
        ai_bridge.save(self.conn, other["id"], ext(need_type="transport"), "ollama:fake",
                       fallback_zone="oak")
        nowhere = db.add_report(self.conn, "water please")
        ai_bridge.save(self.conn, nowhere["id"], ext(neighborhood="Atlantis"), "ollama:fake")
        st = dashboard_adapter.build_state(self.conn)
        why = {h["dbid"]: h["reason"] for h in st["held"]}
        self.assertEqual(st["held_back"], 3)
        self.assertIn("Waiting for the AI", why[waiting["id"]])
        self.assertIn("transport", why[other["id"]])
        self.assertIn("Area unclear", why[nowhere["id"]])


@unittest.skipUnless((AI_DATA / "extracted.json").exists(), "ai/data not present")
class ImportRealOutputTests(unittest.TestCase):
    def test_import_aminul_outputs(self):
        conn = db.connect(); db.init_db(conn)
        rows = json.loads((AI_DATA / "extracted.json").read_text(encoding="utf-8"))
        ids = ai_bridge.import_outputs(conn, rows, "ollama:qwen2.5:7b", log=lambda *_: None)
        self.assertEqual(len(ids), len(rows))
        e = conn.execute("SELECT * FROM extractions WHERE report_id=?", (ids["R13"],)).fetchone()
        self.assertEqual(e["from_cache"], 1)
        self.assertEqual(db.get_report(conn, ids["R13"])["status"], "needs_review")
        # Rampur colony is not on the dashboard map, so it is held for review rather than guessed
        r01 = conn.execute("SELECT * FROM extractions WHERE report_id=?", (ids["R01"],)).fetchone()
        self.assertEqual((r01["need_type"], r01["urgency"], r01["zone"]), ("water", "high", None))
        self.assertEqual(db.get_report(conn, ids["R01"])["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
