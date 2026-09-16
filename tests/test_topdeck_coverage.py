import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.check_topdeck_coverage import (
    TARGET_FORMAT,
    TARGET_GAME,
    build_coverage_report,
    classify_player_deck_source,
    fetch_tournaments,
    write_report,
)


def _unix(dt_str):
    return int(datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


class DeckClassificationTests(unittest.TestCase):
    def test_complete_structured_requires_main_side_extra(self):
        player = {
            "deckObj": {
                "Mainboard": {"A": {"count": 1, "id": "x"}},
                "Sideboard": {"B": {"count": 1, "id": "y"}},
                "Extra Deck": {"C": {"count": 1, "id": "z"}},
            }
        }
        self.assertEqual(classify_player_deck_source(player), "complete_structured")

    def test_external_link_when_structured_incomplete(self):
        player = {
            "deckObj": {"Mainboard": {"A": {"count": 1, "id": "x"}}},
            "decklist": "https://example.com/deck",
        }
        self.assertEqual(classify_player_deck_source(player), "external_link")

    def test_missing_when_no_structured_and_no_link(self):
        self.assertEqual(classify_player_deck_source({"name": "No List"}), "missing_or_incomplete")


class CoverageReportTests(unittest.TestCase):
    def test_coverage_rollups_and_publication_timestamps(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        tournaments = [
            {
                "TID": "E1",
                "tournamentName": "Regional Paper Event",
                "game": TARGET_GAME,
                "format": TARGET_FORMAT,
                "startDate": _unix("2026-09-12"),
                "standings": [
                    {
                        "name": "Alice",
                        "deckObj": {
                            "Mainboard": {"A": {"count": 1, "id": "x"}},
                            "Sideboard": {"B": {"count": 1, "id": "y"}},
                            "Extra": {"C": {"count": 1, "id": "z"}},
                        },
                    },
                    {"name": "Bob", "decklist": "https://example.com/bob"},
                ],
            },
            {
                "TID": "E2",
                "tournamentName": "Older Regional",
                "game": TARGET_GAME,
                "format": TARGET_FORMAT,
                "startDate": _unix("2026-08-20"),
                "standings": [{"name": "Charlie"}],
            },
        ]
        report = build_coverage_report(tournaments, lookback_days=90, now=now)
        self.assertEqual(report["events_found"], 2)
        self.assertEqual(report["coverage_windows"]["last_14_days"]["events"], 1)
        self.assertEqual(report["coverage_windows"]["last_30_days"]["events"], 2)
        self.assertEqual(report["coverage_windows"]["last_90_days"]["events"], 2)
        self.assertEqual(report["overall_players"]["complete_structured"], 1)
        self.assertEqual(report["overall_players"]["external_link"], 1)
        self.assertEqual(report["overall_players"]["missing_or_incomplete"], 1)
        # No explicit publication fields in payload -> no substituted timestamps.
        self.assertIsNone(report["events"][0]["publication_timestamp"])
        self.assertIsNone(report["events"][1]["publication_timestamp"])
        self.assertTrue(report["paper_tcg_advanced_distinction"]["reliably_distinguished_in_sample"])
        self.assertEqual(report["paper_tcg_advanced_distinction"]["sample_size"], 2)

    def test_distinction_uncertain_when_digital_name_marker_present(self):
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        tournaments = [
            {
                "TID": "E1",
                "tournamentName": "Yu-Gi-Oh Master Duel Cup",
                "game": TARGET_GAME,
                "format": TARGET_FORMAT,
                "startDate": _unix("2026-09-12"),
                "standings": [],
            }
        ]
        report = build_coverage_report(tournaments, lookback_days=90, now=now)
        self.assertFalse(report["paper_tcg_advanced_distinction"]["reliably_distinguished_in_sample"])
        self.assertEqual(report["paper_tcg_advanced_distinction"]["assessment"], "uncertain_in_sample")


class ReportWriteTests(unittest.TestCase):
    def test_write_report_creates_parent_directory(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        output = Path(tmp.name) / "nested" / "report.json"
        payload = {"ok": True}
        write_report(str(output), payload)
        with open(output) as f:
            self.assertEqual(json.load(f), payload)


class WorkflowWiringTests(unittest.TestCase):
    def test_workflow_is_manual_and_uses_secret_artifact(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "topdeck_coverage_check.yml"
        ).read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("TOPDECK_API_KEY", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("python -m scripts.check_topdeck_coverage", workflow)


class FetchRequestTests(unittest.TestCase):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class _Session:
        def __init__(self):
            self.request = None

        def post(self, url, headers=None, json=None, timeout=None):
            self.request = {"url": url, "headers": headers, "json": json, "timeout": timeout}
            return FetchRequestTests._Response()

    def test_fetch_uses_v2_tournaments_post_and_expected_filters(self):
        session = self._Session()
        fetch_tournaments(
            api_key="redacted",
            lookback_days=90,
            timeout=15,
            api_base_url="https://topdeck.gg/api",
            session=session,
        )
        self.assertEqual(session.request["url"], "https://topdeck.gg/api/v2/tournaments")
        self.assertEqual(session.request["headers"]["Authorization"], "redacted")
        self.assertEqual(session.request["json"]["game"], TARGET_GAME)
        self.assertEqual(session.request["json"]["format"], TARGET_FORMAT)
        self.assertEqual(session.request["json"]["last"], 90)
        self.assertEqual(session.request["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
