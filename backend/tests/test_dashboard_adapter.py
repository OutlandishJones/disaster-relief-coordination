import json
import unittest

from app import audit, dashboard_adapter as ad, db
from scripts.seed_demo import seed


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        seed(self.conn)

    def state(self):
        return ad.build_state(self.conn)

    def rep(self, rid):
        return next(r for r in self.state()["reports"] if r["id"] == rid)

    # ---- shape the dashboard expects
    def test_state_matches_dashboard_demo_data(self):
        s = self.state()
        self.assertEqual(len(s["reports"]), 8)
        r1 = self.rep("R-1")
        self.assertEqual((r1["hood"], r1["lang"], r1["need"], r1["qty"], r1["u"], r1["win"], r1["conf"]),
                         ("riverside", "English", "water", 8, 3, 30, 94))
        self.assertEqual(r1["phone"], "•••-0117")
        self.assertIn("mobility", r1["access"])
        self.assertEqual(self.rep("R-2")["miss"], "Exact street address")
        self.assertEqual(s["stock"]["water"], 20)
        self.assertEqual(len(s["roads"]), 6)
        self.assertEqual(s["blocked"], "")

    def test_duplicate_and_same_building_notes(self):
        self.assertEqual(self.rep("R-6")["dupOf"], "R-2")
        self.assertIn("separate household", self.rep("R-5")["same"])
        self.assertEqual(len(db.list_reports(self.conn)), 8)       # nothing deleted

    def test_same_household_same_phone_different_households_differ(self):
        toks = {r["id"]: r["household_token"] for r in db.list_reports(self.conn)}
        self.assertEqual(toks[2], toks[6])         # R-2 and R-6: same phone, same household
        self.assertNotEqual(toks[4], toks[5])      # R-4 and R-5: same building, different households

    def test_no_raw_pii_leaves_the_server(self):
        blob = json.dumps(self.state(), ensure_ascii=False)
        self.assertNotIn("757-555", blob)
        self.assertNotIn("Quay Rd", blob)

    # ---- decisions
    def test_approve_updates_status_stock_dispatch_and_audit(self):
        ad.act_on_report(self.conn, 1, "approved", "Francis", quantity=8,
                         evidence={"eta_min": 8, "window_min": 30, "evil": "<script>"})
        r = self.rep("R-1")
        self.assertEqual((r["status"], r["alloc"]), ("approved", 8))
        self.assertEqual(len(db.dispatchable(self.conn)), 1)
        water = [x for x in db.list_open_resources(self.conn) if x["item"] == "water"][0]
        self.assertEqual(water["remaining"], 12)
        ev = json.loads(db.list_assignments(self.conn)[0]["evidence"])
        self.assertEqual(ev, {"eta_min": 8, "window_min": 30})      # unknown keys dropped
        self.assertTrue(any("approved by Francis" in m for m in self.state()["audit"]))
        self.assertTrue(audit.verify_chain(self.conn)[0])

    def test_requires_coordinator_name(self):
        for who in ("", "   ", None):
            with self.assertRaises(db.RuleError):
                ad.act_on_report(self.conn, 1, "approved", who, quantity=8)
        self.assertEqual(self.rep("R-1")["status"], "pending")
        self.assertEqual(db.dispatchable(self.conn), [])

    def test_reject_needs_reason_then_records_it(self):
        with self.assertRaises(db.RuleError):
            ad.act_on_report(self.conn, 3, "rejected", "Francis", quantity=6)
        ad.act_on_report(self.conn, 3, "rejected", "Francis", quantity=6, reason="duplicate of walk-in")
        self.assertEqual(self.rep("R-3")["status"], "rejected")
        self.assertEqual(db.dispatchable(self.conn), [])
        self.assertTrue(any("rejected by Francis: duplicate of walk-in" in m for m in self.state()["audit"]))

    def test_reopen_restores_stock_and_status(self):
        ad.act_on_report(self.conn, 1, "approved", "Francis", quantity=8)
        ad.act_on_report(self.conn, 1, "pending", "Francis")
        r = self.rep("R-1")
        self.assertEqual((r["status"], r["alloc"]), ("pending", 0))
        water = [x for x in db.list_open_resources(self.conn) if x["item"] == "water"][0]
        self.assertEqual(water["remaining"], 20)
        self.assertEqual(db.dispatchable(self.conn), [])
        # and it can be decided again
        ad.act_on_report(self.conn, 1, "approved", "Kameron", quantity=8)
        self.assertEqual(self.rep("R-1")["status"], "approved")

    def test_cannot_approve_more_than_stock_and_leaves_no_dangling_proposal(self):
        with self.assertRaises(db.RuleError):
            ad.act_on_report(self.conn, 7, "approved", "Francis", quantity=99)   # food stock is 12
        self.assertEqual(self.rep("R-7")["status"], "pending")
        self.assertEqual([a for a in db.list_assignments(self.conn) if a["status"] == "proposed"], [])

    def test_cannot_approve_zero(self):
        with self.assertRaises(db.RuleError):
            ad.act_on_report(self.conn, 1, "approved", "Francis", quantity=0)

    def test_reopen_with_nothing_decided_errors(self):
        with self.assertRaises(db.RuleError):
            ad.act_on_report(self.conn, 1, "pending", "Francis")

    # ---- quantity correction
    def test_quantity_correction_keeps_history_and_audits(self):
        ad.set_quantity(self.conn, 8, 6, "Francis")
        self.assertEqual(self.rep("R-8")["qty"], 6)
        exts = db.get_extractions(self.conn, 8)
        self.assertEqual([e["quantity"] for e in exts], [4, 6])
        self.assertEqual(exts[-1]["model"], "coordinator-correction")
        self.assertEqual(exts[-1]["window_minutes"], 90)            # other fields preserved
        self.assertTrue(any("corrected by Francis" in m for m in self.state()["audit"]))

    def test_quantity_blocked_after_decision_and_without_name(self):
        with self.assertRaises(db.RuleError):
            ad.set_quantity(self.conn, 8, 6, "")
        ad.act_on_report(self.conn, 8, "approved", "Francis", quantity=4)
        with self.assertRaises(db.RuleError):
            ad.set_quantity(self.conn, 8, 6, "Francis")

    # ---- roads
    def test_road_closure_one_at_a_time_and_audited(self):
        ad.set_blocked_road(self.conn, "Bridge St", "Francis")
        self.assertEqual(self.state()["blocked"], "Bridge St")
        ad.set_blocked_road(self.conn, "Main St", "Francis")        # switching reopens Bridge St
        self.assertEqual(self.state()["blocked"], "Main St")
        closed = self.conn.execute("SELECT name FROM roads WHERE is_open=0").fetchall()
        self.assertEqual([r["name"] for r in closed], ["Main St"])
        ad.set_blocked_road(self.conn, "", "Francis")
        self.assertEqual(self.state()["blocked"], "")
        with self.assertRaises(db.RuleError):
            ad.set_blocked_road(self.conn, "Nowhere Rd", "Francis")
        with self.assertRaises(db.RuleError):
            ad.set_blocked_road(self.conn, "Bridge St", "")

    # ---- reports the dashboard can't show yet are counted, not hidden silently
    def test_unextracted_or_unplaced_reports_are_counted(self):
        db.add_report(self.conn, "need help", language="en")                       # no extraction
        r = db.add_report(self.conn, "unclear place", language="en")
        db.save_extraction(self.conn, r["id"], {"need_type": "water", "quantity": 1, "zone": None})
        s = self.state()
        self.assertEqual(len(s["reports"]), 8)
        self.assertEqual(s["held_back"], 2)

    def test_corrected_report_replaces_original_on_dashboard(self):
        new = db.add_report(self.conn, "Correction: we need 6 blankets", language="en", corrects_id=8)
        db.save_extraction(self.conn, new["id"], {"need_type": "blankets", "quantity": 6, "zone": "oak",
                                                  "urgency": "low", "window_minutes": 90, "confidence": .9})
        ids = [r["id"] for r in self.state()["reports"]]
        self.assertNotIn("R-8", ids)
        self.assertIn(f"R-{new['id']}", ids)


if __name__ == "__main__":
    unittest.main()


class StockLimitTests(unittest.TestCase):
    """Approving can never hand out more than the depot has."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        seed(self.conn)

    def medicine_left(self, conn=None):
        return (conn or self.conn).execute(
            "SELECT SUM(remaining) FROM resources WHERE item='medicine'").fetchone()[0]

    def approved(self, conn, rid):
        return conn.execute("SELECT COUNT(*) FROM assignments WHERE report_id=? AND status='approved'",
                            (rid,)).fetchone()[0]

    def extra_medicine_request(self, conn, qty=2):
        r = db.add_report(conn, "Need insulin", channel="sms", actor="t")
        db.save_extraction(conn, r["id"], {"need_type": "medicine", "quantity": qty, "zone": "oak",
                                           "urgency": "high"}, model="t", actor="t")
        return r["id"]

    def test_approving_twice_is_refused_and_counts_once(self):
        ad.act_on_report(self.conn, 2, "approved", "Pat", quantity=2)
        with self.assertRaises(db.RuleError) as e:
            ad.act_on_report(self.conn, 2, "approved", "Pat", quantity=2)
        self.assertIn("already approved", str(e.exception))
        self.assertEqual(self.approved(self.conn, 2), 1)
        self.assertEqual(self.medicine_left(), 0)

    def test_cannot_approve_past_the_stock(self):
        ad.act_on_report(self.conn, 2, "approved", "Pat", quantity=2)
        other = self.extra_medicine_request(self.conn)
        with self.assertRaises(db.RuleError):
            ad.act_on_report(self.conn, other, "approved", "Pat", quantity=2)
        self.assertEqual(self.medicine_left(), 0)
        self.assertEqual(self.approved(self.conn, other), 0)

    def test_reopen_then_approve_again_works(self):
        ad.act_on_report(self.conn, 2, "approved", "Pat", quantity=2)
        ad.act_on_report(self.conn, 2, "pending", "Pat")
        self.assertEqual(self.medicine_left(), 2)
        ad.act_on_report(self.conn, 2, "approved", "Pat", quantity=1)
        self.assertEqual(self.medicine_left(), 1)

    def test_simultaneous_clicks_never_overspend(self):
        import os
        import tempfile
        import threading
        path = os.path.join(tempfile.mkdtemp(), "race.db")
        c = db.connect(path); db.init_db(c); seed(c)
        others = [self.extra_medicine_request(c, qty=1) for _ in range(4)]
        c.close()
        jobs = [2] * 4 + others                     # a double-clicked R-2 plus four 1-kit requests
        start, results = threading.Barrier(len(jobs)), []

        def click(rid):
            conn = db.connect(path)
            conn.execute("PRAGMA busy_timeout = 5000")
            start.wait()
            try:
                ad.act_on_report(conn, rid, "approved", "Pat", quantity=2 if rid == 2 else 1)
                results.append(rid)
            except db.RuleError:
                pass
            finally:
                conn.close()

        threads = [threading.Thread(target=click, args=(rid,)) for rid in jobs]
        for t in threads: t.start()
        for t in threads: t.join()
        c = db.connect(path)
        self.assertGreaterEqual(self.medicine_left(c), 0)
        handed_out = c.execute("SELECT COALESCE(SUM(a.quantity),0) FROM assignments a JOIN resources r"
                               " ON r.id=a.resource_id WHERE a.status='approved' AND r.item='medicine'"
                               ).fetchone()[0]
        self.assertLessEqual(handed_out, 2)
        self.assertLessEqual(self.approved(c, 2), 1)
        self.assertEqual(handed_out + self.medicine_left(c), 2)   # stock and approvals agree
        c.close()
