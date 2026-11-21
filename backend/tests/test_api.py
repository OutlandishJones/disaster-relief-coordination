"""API smoke tests. Requires: pip install fastapi httpx. Skipped automatically if missing."""
import os
import tempfile
import unittest

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:  # pragma: no cover
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/httpx not installed")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        os.environ["API_KEY"] = "k"
        os.environ["AUTO_EXTRACT"] = "0"      # never call a real model from tests
        from app.main import app
        self._ctx = TestClient(app)
        self.c = self._ctx.__enter__()
        self.h = {"X-API-Key": "k"}

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_requires_api_key(self):
        self.assertEqual(self.c.get("/reports").status_code, 401)
        self.assertEqual(self.c.get("/reports", headers=self.h).status_code, 200)

    def test_report_is_scrubbed_over_http(self):
        r = self.c.post("/reports", headers=self.h, json={
            "text": "Ana here, 757-555-0142, need water", "contact_name": "Ana",
            "contact_phone": "757-555-0142"}).json()
        self.assertNotIn("757", r["text_masked"])
        self.assertNotIn("Ana", r["text_masked"])

    def test_full_approval_flow(self):
        rid = self.c.post("/reports", headers=self.h, json={"text": "need water"}).json()["id"]
        res = self.c.post("/resources", headers=self.h, json={
            "kind": "supply", "name": "Water", "capacity": 20}).json()["resource_id"]
        aid = self.c.post("/assignments", headers=self.h, json={
            "report_id": rid, "resource_id": res, "quantity": 5}).json()["assignment_id"]
        self.assertEqual(self.c.get("/dispatchable", headers=self.h).json(), [])
        # no coordinator header -> rejected
        bad = self.c.post(f"/assignments/{aid}/decision", headers=self.h, json={"decision": "approve"})
        self.assertEqual(bad.status_code, 400)
        ok = self.c.post(f"/assignments/{aid}/decision", json={"decision": "approve"},
                         headers={**self.h, "X-Coordinator": "francis"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(self.c.get("/dispatchable", headers=self.h).json()), 1)
        self.assertTrue(self.c.get("/audit/verify", headers=self.h).json()["intact"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/httpx not installed")
class LiveIntakeApiTests(unittest.TestCase):
    """POST /reports reads the report in the background (fake extractor here) and the
    dashboard shows it without any manual step."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = os.path.join(self.tmp.name, "test.db")
        os.environ["API_KEY"] = "k"
        os.environ["AUTO_EXTRACT"] = "1"
        from app import main
        from tests.test_ai_bridge import FakeExtractor
        self.main = main
        self.saved = dict(main._extractor)
        main._extractor.update(fn=FakeExtractor(), model="fake", error=None)
        self._ctx = TestClient(main.app)
        self.c = self._ctx.__enter__()
        self.h = {"X-API-Key": "k"}

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self.main._extractor.clear(); self.main._extractor.update(self.saved)
        os.environ["AUTO_EXTRACT"] = "0"
        self.tmp.cleanup()

    def test_posted_reports_appear_and_link(self):
        body = {"text": "We need water for 3 people", "contact_phone": "757-555-0199",
                "contact_address": "12 Birch Ct, Oak Hollow"}
        r1 = self.c.post("/reports", headers=self.h, json=body).json()
        r2 = self.c.post("/reports", headers=self.h, json={**body, "text": "Still no water, 3 of us"}).json()
        self.assertEqual(r1["extraction"], "queued")
        state = self.c.get("/dashboard/state", headers=self.h).json()
        ids = {r["id"]: r for r in state["reports"]}
        self.assertIn(f"R-{r1['id']}", ids)
        self.assertEqual(ids[f"R-{r2['id']}"].get("dupOf"), f"R-{r1['id']}")
        self.assertEqual(state["held"], [])
