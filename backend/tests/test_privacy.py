import unittest
from app import privacy


class PrivacyTests(unittest.TestCase):
    def test_scrub_phone_and_email(self):
        t = privacy.scrub_text("Call me at +886 900 000 678 or ana@example.com please")
        self.assertNotIn("912", t)
        self.assertNotIn("ana@example.com", t)
        self.assertIn("[PHONE]", t)
        self.assertIn("[EMAIL]", t)

    def test_scrub_us_phone_formats(self):
        for p in ("757-555-0142", "(757) 555-0142", "757.555.0142"):
            self.assertIn("[PHONE]", privacy.scrub_text(f"my number {p} thanks"))

    def test_does_not_scrub_small_quantities(self):
        t = privacy.scrub_text("We are 4 people and need 12 bottles at Zone 3")
        self.assertEqual(t, "We are 4 people and need 12 bottles at Zone 3")

    def test_scrub_known_name_only(self):
        t = privacy.scrub_text("This is Francis Jones, we need water", known_names=["Francis Jones"])
        self.assertNotIn("Francis", t)
        self.assertNotIn("Jones", t)
        # Documented limit: an unknown name is NOT removed
        self.assertIn("Maria", privacy.scrub_text("Maria next door needs insulin"))

    def test_masks(self):
        self.assertEqual(privacy.mask_phone("+886 900 000 678"), "***0678")
        self.assertEqual(privacy.mask_email("ana.lopez@example.com"), "a***@example.com")
        self.assertEqual(privacy.mask_name("Francis Jones"), "F. J.")

    def test_household_token_stable_and_distinct(self):
        a = privacy.household_token(phone="+1 757 555 0142")
        b = privacy.household_token(phone="1-757-555-0142")      # same phone, other format
        c = privacy.household_token(phone="757 555 0199")        # different phone
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("HH-"))
        self.assertNotIn("5550142", a)

    def test_same_address_different_households(self):
        a = privacy.household_token(name="Lee", address="12 Elm St Apt 1")
        b = privacy.household_token(name="Park", address="12 Elm St Apt 1")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
