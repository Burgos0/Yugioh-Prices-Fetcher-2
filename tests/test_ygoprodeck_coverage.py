import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.check_ygoprodeck_tcg_coverage import (
    build_coverage_report,
    fetch_tcg_decks,
    write_report,
)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        offset = int((params or {}).get("offset", 0))
        return _Response(self.pages.get(offset, []))


def _deck(
    *,
    deck_name="Sample Deck",
    format_name="Tournament Meta Decks",
    tournament_name="Sample Regional",
    submit_date="2026-09-10",
    player_name="Player A",
    player_count=64,
    main="[1,2,3]",
    extra="[4,5]",
    side="[6,7]",
    excerpt="",
):
    return {
        "deck_name": deck_name,
        "format": format_name,
        "tournamentName": tournament_name,
        "submit_date": submit_date,
        "tournamentPlayerName": player_name,
        "tournamentPlayerCount": player_count,
        "main_deck": main,
        "extra_deck": extra,
        "side_deck": side,
        "deck_excerpt": excerpt,
        "pretty_url": "sample-deck-1",
    }


class FilteringAndCoverageTests(unittest.TestCase):
    def test_excludes_non_paper_markers(self):
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        rows = [
            _deck(),
            _deck(deck_name="Master Duel Sample", tournament_name="Master Duel Cup"),
            _deck(format_name="Tournament Meta Decks OCG", tournament_name="Tokyo OCG"),
            _deck(deck_name="Practice Test Deck", tournament_name="Practice Run"),
        ]
        report = build_coverage_report(rows, now=now)
        self.assertEqual(report["events_found"], 1)
        self.assertEqual(report["excluded_non_paper_records"], 3)

    def test_counts_complete_external_missing_and_windows(self):
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        rows = [
            _deck(submit_date="2026-09-14", tournament_name="Event A"),  # complete
            _deck(
                submit_date="2026-09-12",
                tournament_name="Event A",
                main="[1]",
                extra="[]",
                side="[]",
                excerpt="https://example.com/list",
            ),  # external link
            _deck(
                submit_date="2026-08-20",
                tournament_name="Event B",
                main="[1]",
                extra="[]",
                side="[]",
                excerpt="no links",
            ),  # missing/incomplete
        ]
        report = build_coverage_report(rows, now=now)
        self.assertEqual(report["overall_players"]["complete_structured"], 1)
        self.assertEqual(report["overall_players"]["external_link"], 1)
        self.assertEqual(report["overall_players"]["missing_or_incomplete"], 1)
        self.assertEqual(report["coverage_windows"]["last_14_days"]["events"], 2)
        self.assertEqual(report["coverage_windows"]["last_30_days"]["events"], 3)
        self.assertEqual(report["coverage_windows"]["last_90_days"]["events"], 3)

    def test_timestamp_quality_categories(self):
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        rows = [
            _deck(submit_date="2026-09-14"),  # date_only
            _deck(submit_date="2026-09-14T10:00:00Z", tournament_name="Event B"),
            _deck(submit_date="3 days ago", tournament_name="Event C"),
            _deck(submit_date="", tournament_name="Event D"),
        ]
        report = build_coverage_report(rows, now=now)
        self.assertEqual(report["publication_timestamp_quality"]["date_only"], 1)
        self.assertEqual(report["publication_timestamp_quality"]["timestamp"], 1)
        self.assertEqual(report["publication_timestamp_quality"]["relative_text"], 1)
        self.assertEqual(report["publication_timestamp_quality"]["missing"], 1)


class FetchTests(unittest.TestCase):
    def test_fetch_paginates_by_offset(self):
        pages = {
            0: [_deck() for _ in range(20)],
            20: [_deck(tournament_name="Event B")],
        }
        session = _Session(pages)
        rows = fetch_tcg_decks(from_date="2026-06-16", timeout=10, session=session)
        self.assertEqual(len(rows), 21)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["params"]["offset"], 0)
        self.assertEqual(session.calls[1]["params"]["offset"], 20)
        self.assertEqual(session.calls[0]["params"]["_sft_category"], "Tournament Meta Decks")


class OutputAndWorkflowTests(unittest.TestCase):
    def test_write_report(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        report_path = Path(tmp.name) / "nested" / "report.json"
        payload = {"ok": True}
        write_report(str(report_path), payload)
        with open(report_path) as f:
            self.assertEqual(json.load(f), payload)

    def test_workflow_manual_artifact_only(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "ygoprodeck_coverage_check.yml"
        ).read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("python -m scripts.check_ygoprodeck_tcg_coverage", workflow)


if __name__ == "__main__":
    unittest.main()
