"""Public-site branding, legal footer, analytics hook, and static card URLs."""
import os
import unittest
from unittest import mock

from app import create_app, printing_slug


class StaticSiteTests(unittest.TestCase):
    def test_printing_slug(self):
        self.assertEqual(printing_slug("1st Edition"), "1st-edition")
        self.assertEqual(printing_slug("Unlimited"), "unlimited")

    def test_branding_and_legal_footer(self):
        html = create_app().test_client().get("/").data.decode()
        self.assertIn("TCG Trending", html)
        self.assertIn("not affiliated with or endorsed by Konami or TCGplayer", html)
        self.assertIn("Not financial advice", html)
        self.assertIn("affiliate links", html)

    def test_analytics_only_when_configured(self):
        with mock.patch.dict(os.environ, {"GOATCOUNTER_CODE": ""}):
            self.assertNotIn("goatcounter", create_app().test_client().get("/").data.decode())
        with mock.patch.dict(os.environ, {"GOATCOUNTER_CODE": "tcgtrending"}):
            html = create_app().test_client().get("/").data.decode()
        self.assertIn("https://tcgtrending.goatcounter.com/count", html)


if __name__ == "__main__":
    unittest.main()
