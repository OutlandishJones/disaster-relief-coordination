import unittest
from app import audit, db


class DbTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)

    def _report(self, **kw):
        base = dict(text="Need water for 4 people, Elm St", channel="sms", language="en")
        base.update(kw)
        return db.add_report(self.conn, **base)

    # ---- privacy
    def test_raw_pii_not_in_reports_table(self):
        r = self._report(text="I'm Ana Ruiz, call 757-555-0142, need food",
                         contact_name="Ana Ruiz", contact_phone="757-555-0142")
        self.assertNotIn("757", r["text_masked"])
        self.assertNotIn("Ruiz", r["text_masked"])
        self.assertTrue(r["household_token"].startswith("HH-"))
        dump = " ".join(str(dict(x)) for x in self.conn.execute("SELECT * FROM reports"))
        self.assertNotIn("757-555-0142", dump)
        audit_dump = " ".join(str(dict(x)) for x in self.conn.execute("SELECT * FROM audit_log"))
        self.assertNotIn("757-555-0142", audit_dump)
        self.assertNotIn("Ana Ruiz", audit_dump)

    def test_two_households_same_address_both_kept(self):
        a = self._report(contact_name="Lee", contact_address="12 Elm St")
        b = self._report(contact_name="Park", contact_address="12 Elm St")
        self.assertNotEqual(a["household_token"], b["household_token"])
        self.assertEqual(len(db.list_reports(self.conn)), 2)

    def test_reveal_requires_reason_and_is_audited(self):
        r = self._report(contact_name="Ana", contact_phone="757-555-0142")
        with self.assertRaises(db.RuleError):
            db.reveal_contact(self.conn, r["household_token"], "kameron", "")
        c = db.reveal_contact(self.conn, r["household_token"], "kameron", "driver needs to call")
        self.assertEqual(c["phone"], "757-555-0142")
        actions = [x["action"] for x in self.conn.execute("SELECT action FROM audit_log")]
        self.assertIn("contact.revealed", actions)

    # ---- duplicates / corrections
    def test_duplicate_link_keeps_both_reports(self):
        a, b = self._report(), self._report()
        db.link_duplicate(self.conn, b["id"], a["id"], score=0.93, reason="same text, same zone")
        self.assertEqual(len(db.list_reports(self.conn)), 2)
        self.assertEqual(len(db.get_duplicate_links(self.conn, a["id"])), 1)

    def test_correction_supersedes_but_keeps_original(self):
        a = self._report()
        b = self._report(text="Correction: need 6 bottles", corrects_id=a["id"])
        self.assertEqual(db.get_report(self.conn, a["id"])["status"], "superseded")
        self.assertEqual(b["corrects_id"], a["id"])

    # ---- extraction
    def test_low_confidence_or_missing_goes_to_review(self):
        r = self._report()
        db.save_extraction(self.conn, r["id"], {"need_type": "water", "confidence": 0.4,
                                                "missing_fields": ["street"]}, model="m")
        self.assertEqual(db.get_report(self.conn, r["id"])["status"], "needs_review")
        r2 = self._report()
        db.save_extraction(self.conn, r2["id"], {"need_type": "water", "confidence": 0.95})
        self.assertEqual(db.get_report(self.conn, r2["id"])["status"], "extracted")

    # ---- human approval gate
    def _proposal(self, qty=10, stock=50):
        r = self._report()
        res = db.add_resource(self.conn, "supply", "Water pallet", stock, item="water")
        aid = db.propose_assignment(self.conn, r["id"], res, quantity=qty, evidence={"why": "fits"})
        return r, res, aid

    def test_nothing_dispatchable_until_approved(self):
        _, res, aid = self._proposal()
        self.assertEqual(db.dispatchable(self.conn), [])
        db.decide_assignment(self.conn, aid, "approve", "francis")
        self.assertEqual(len(db.dispatchable(self.conn)), 1)
        left = [r for r in db.list_open_resources(self.conn) if r["id"] == res][0]["remaining"]
        self.assertEqual(left, 40)

    def test_decision_rules(self):
        _, _, aid = self._proposal()
        with self.assertRaises(db.RuleError):
            db.decide_assignment(self.conn, aid, "approve", "")          # no coordinator
        with self.assertRaises(db.RuleError):
            db.decide_assignment(self.conn, aid, "reject", "francis")    # no reason
        with self.assertRaises(db.RuleError):
            db.decide_assignment(self.conn, aid, "maybe", "francis")     # bad decision
        db.decide_assignment(self.conn, aid, "reject", "francis", reason="road closed")
        with self.assertRaises(db.RuleError):
            db.decide_assignment(self.conn, aid, "approve", "francis")   # already decided

    def test_cannot_overdraw_stock(self):
        _, _, aid = self._proposal(qty=10, stock=5)
        with self.assertRaises(db.RuleError):
            db.decide_assignment(self.conn, aid, "approve", "francis")
        self.assertEqual(db.get_assignment(self.conn, aid)["status"], "proposed")

    def test_override_creates_coordinator_assignment(self):
        r, _, aid = self._proposal(qty=10)
        alt = db.add_resource(self.conn, "vehicle", "Truck 2", 100, zone="B")
        db.decide_assignment(self.conn, aid, "override", "kameron",
                             reason="truck 2 closer", new_resource_id=alt)
        self.assertEqual(db.get_assignment(self.conn, aid)["status"], "overridden")
        d = db.dispatchable(self.conn)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["origin"], "coordinator")
        self.assertEqual(d[0]["resource_id"], alt)

    # ---- audit
    def test_audit_chain_intact_then_detects_tampering(self):
        _, _, aid = self._proposal()
        db.decide_assignment(self.conn, aid, "approve", "francis")
        ok, bad = audit.verify_chain(self.conn)
        self.assertTrue(ok)
        self.assertIsNone(bad)
        # Tamper by bypassing the append-only trigger (drop it, edit, as an attacker with db access)
        self.conn.execute("DROP TRIGGER audit_no_update")
        self.conn.execute("UPDATE audit_log SET actor='mallory' WHERE id=2")
        ok, bad = audit.verify_chain(self.conn)
        self.assertFalse(ok)
        self.assertEqual(bad, 2)

    def test_audit_log_is_append_only(self):
        self._report()
        with self.assertRaises(Exception):
            self.conn.execute("DELETE FROM audit_log")
        with self.assertRaises(Exception):
            self.conn.execute("UPDATE audit_log SET actor='x'")

    def test_road_status_audited(self):
        db.set_road_status(self.conn, "A", "B", False, actor="francis")
        row = self.conn.execute("SELECT * FROM roads").fetchone()
        self.assertEqual(row["is_open"], 0)
        db.set_road_status(self.conn, "A", "B", True, actor="francis")
        self.assertEqual(self.conn.execute("SELECT is_open FROM roads").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
