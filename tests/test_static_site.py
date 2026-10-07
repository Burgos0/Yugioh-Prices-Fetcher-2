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

    def test_goatcounter_on_every_page(self):
        html = create_app().test_client().get("/").data.decode()
        self.assertIn('data-goatcounter="https://tcgtrending.goatcounter.com/count"', html)

    def test_tcgplayer_links(self):
        self.assertEqual(tcgplayer_url(610840), "https://www.tcgplayer.com/product/610840")
        self.assertEqual(
            tcgplayer_url(610840, "https://tcgplayer.pxf.io/c/1/2/3"),
            "https://tcgplayer.pxf.io/c/1/2/3?u=https%3A%2F%2Fwww.tcgplayer.com%2Fproduct%2F610840",
        )

    def test_impact_meta_tag_present(self):
        html = create_app().test_client().get("/").data.decode()
        self.assertIn('name="impact-site-verification" value="0793ee41-38a9-47e6-a71c-892b6e43c65f"', html)

if __name__ == "__main__":
    unittest.main()
