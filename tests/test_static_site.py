"""Public-site branding, legal footer, analytics hook, and static card URLs."""
import os
import unittest
from unittest import mock

from app import create_app, printing_slug, tcgplayer_url


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

    def test_tcgplayer_links(self):
        self.assertEqual(tcgplayer_url(610840), "https://www.tcgplayer.com/product/610840")
        self.assertEqual(
            tcgplayer_url(610840, "https://tcgplayer.pxf.io/c/1/2/3"),
            "https://tcgplayer.pxf.io/c/1/2/3?u=https%3A%2F%2Fwww.tcgplayer.com%2Fproduct%2F610840",
        )

    def test_impact_meta_tag_only_when_valid(self):
        tag = '<meta name="impact-site-verification" value="abc-123">'
        with mock.patch.dict(os.environ, {"IMPACT_VERIFICATION_TAG": tag}):
            self.assertIn(tag, create_app().test_client().get("/").data.decode())
        with mock.patch.dict(os.environ, {"IMPACT_VERIFICATION_TAG": "<script>x</script>"}):
            self.assertNotIn("<script>x", create_app().test_client().get("/").data.decode())


if __name__ == "__main__":
    unittest.main()
