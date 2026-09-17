import json
import tempfile
import unittest
from pathlib import Path

import requests

from scripts.collect_meta_watch_lists import (
    collect_and_import,
    parse_article_observations,
)


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url, timeout=20):
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return _FakeResponse(value[0], status_code=value[1])
        return _FakeResponse(value)


def _article_html(main_count=40, ash_count=3, filler_count_override=None):
    filler_count = filler_count_override if filler_count_override is not None else (main_count - ash_count)
    return f"""
    <html>
      <head>
        <title>YCS Test City Top 32 Deck Lists - Yu-Gi-Oh! TCG Event Coverage</title>
        <meta property="article:published_time" content="2026-09-10T12:00:00Z" />
      </head>
      <body>
        <h3>1st Place: Alice Example - Kashtira</h3>
        <p>Main Deck: {main_count}</p>
        <ul>
          <li>{ash_count} Ash Blossom &amp; Joyous Spring</li>
          <li>{filler_count} Filler Card</li>
        </ul>
        <p>Side Deck: 15</p>
        <ul><li>15 Side Card</li></ul>
        <p>Extra Deck: 15</p>
        <ul><li>15 Extra Card</li></ul>
      </body>
    </html>
    """


class ParsingTests(unittest.TestCase):
    def test_parse_valid_article_preserves_metadata_and_counts(self):
        observations, rejected = parse_article_observations(
            _article_html(),
            "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/",
            {"format": "TCG_ADVANCED", "region": "NA"},
        )
        self.assertEqual(rejected, [])
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertEqual(obs["player"], "Alice Example")
        self.assertEqual(obs["placement"], "1st Place")
        self.assertEqual(obs["source_url"], "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/")
        self.assertEqual(obs["published_at"], "2026-09-10T12:00:00Z")
        self.assertEqual(sum(c["count"] for c in obs["main_deck"]), 40)
        self.assertEqual(sum(c["count"] for c in obs["side_deck"]), 15)
        self.assertEqual(sum(c["count"] for c in obs["extra_deck"]), 15)

    def test_malformed_article_is_rejected(self):
        observations, rejected = parse_article_observations(
            _article_html(main_count=41, filler_count_override=37),
            "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/",
            {"format": "TCG_ADVANCED", "region": "NA"},
        )
        self.assertEqual(observations, [])
        self.assertTrue(any("count mismatch" in r["reason"] for r in rejected))


class CollectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.report_path = str(Path(self.tmp.name) / "meta_watch_collection_report.json")
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": []}, f)
        self.sources = (
            {"name": "test", "url": "https://yugiohblog.konami.com/category/ycs/", "format": "TCG_ADVANCED", "region": "NA"},
        )
        self.index_html = """
        <html><body>
          <a href="https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/">Article</a>
        </body></html>
        """
        self.article_url = "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/"

    def test_network_failures_are_reported_without_erasing_dataset(self):
        session = _FakeSession({
            "https://yugiohblog.konami.com/category/ycs/": requests.ConnectionError("network down"),
        })
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=session,
        )
        self.assertEqual(report["import"]["added"], 0)
        self.assertEqual(len(report["source_failures"]), 1)
        with open(self.dataset_path) as f:
            dataset = json.load(f)
        self.assertEqual(dataset["observations"], [])

    def test_repeat_run_dedupes_and_changed_source_lists_are_flagged(self):
        session = _FakeSession({
            "https://yugiohblog.konami.com/category/ycs/": self.index_html,
            self.article_url: _article_html(),
        })
        first = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=session,
        )
        self.assertEqual(first["import"]["added"], 1)

        second = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=session,
        )
        self.assertEqual(second["import"]["added"], 0)
        self.assertEqual(second["import"]["duplicate_existing_skipped"], 0)
        self.assertEqual(second["duplicate_existing_precheck_skipped"], 1)

        changed_session = _FakeSession({
            "https://yugiohblog.konami.com/category/ycs/": self.index_html,
            self.article_url: _article_html(main_count=40, ash_count=2),
        })
        third = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=changed_session,
        )
        self.assertEqual(third["import"]["added"], 0)
        self.assertEqual(third["import"]["duplicate_existing_skipped"], 0)
        self.assertEqual(len(third["changed_source_lists"]), 1)


class WorkflowTests(unittest.TestCase):
    def test_meta_watch_workflow_uses_pr_updates_not_direct_push(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "meta_watch_daily.yml"
        ).read_text()
        self.assertIn("peter-evans/create-pull-request@v7", workflow)
        self.assertNotIn("git push", workflow)
        self.assertIn("scripts.collect_meta_watch_lists", workflow)
        # YGOPRODeck backfill is wired into the same automated PR and its
        # report is included in add-paths.
        self.assertIn("scripts.collect_ygoprodeck_lists", workflow)
        self.assertIn("data/meta_watch_ygoprodeck_report.json", workflow)


if __name__ == "__main__":
    unittest.main()
