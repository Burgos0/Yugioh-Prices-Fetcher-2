"""
Regression tests for the YGOPRODeck TCG Advanced secondary source
collector (``scripts/collect_ygoprodeck_lists.py``).

These tests deliberately stub the network layer so no real HTTP calls are
made. They enforce the invariants called out in the PR #8 audit:

- Only ``getDecks.php`` is used; nothing else.
- Only TCG Advanced (Tournament Meta Decks) records survive filtering; OCG,
  Master Duel, Rush Duel, Genesys, online/casual markers are dropped.
- YGOPRODeck's ``deckNum`` is the stable source id used for deduplication.
- Relative ``submit_date`` values are honestly reported, never invented.
- Comma-separated deck arrays are parsed with duplicates counted as
  copies.
- Missing metadata is reported, not silently backfilled.
- Endpoint failures leave the existing dataset untouched.
- Formats are separated: no non-TCG-Advanced format leaks in as
  ``TCG_ADVANCED``.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.collect_ygoprodeck_lists import (
    YGOPRODECK_API_URL,
    _classify_submit_date,
    _is_tcg_advanced_record,
    _parse_deck_array,
    collect_and_import,
    fetch_tcg_decks,
    record_to_observation,
)


NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def _record(**overrides):
    base = {
        "deckNum": 1001,
        "deck_name": "Snake-Eye Fire King",
        "format": "Tournament Meta Decks",
        "tournamentName": "YCS Atlanta 2026",
        "tournamentPlacement": "1st",
        "tournamentPlayerName": "Alice Example",
        "tournamentPlayerCount": 512,
        "submit_date": "2026-09-10",
        "main_deck": "11,11,11,22,22,33",
        "extra_deck": "44,44,55",
        "side_deck": "66,66,77",
        "deck_description": "",
        "deck_excerpt": "",
        "pretty_url": "snake-eye-fire-king-1001",
    }
    base.update(overrides)
    return base


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Session:
    def __init__(self, pages=None, error=None):
        self.pages = pages or {}
        self.error = error
        self.calls = []

    def get(self, url, params=None, timeout=None, verify=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "timeout": timeout, "verify": verify}
        )
        if self.error is not None:
            raise self.error
        offset = int((params or {}).get("offset", 0))
        return _Response(self.pages.get(offset, []))


class DeckArrayParsingTests(unittest.TestCase):
    def test_comma_separated_string_parses_with_duplicates(self):
        self.assertEqual(_parse_deck_array("11,11,22"), ["11", "11", "22"])

    def test_json_array_string_parses_and_preserves_duplicates(self):
        self.assertEqual(_parse_deck_array("[11, 11, 22]"), ["11", "11", "22"])

    def test_list_input_is_normalised(self):
        self.assertEqual(_parse_deck_array([11, 22, 22]), ["11", "22", "22"])

    def test_empty_and_none_inputs(self):
        self.assertEqual(_parse_deck_array(""), [])
        self.assertEqual(_parse_deck_array(None), [])
        self.assertEqual(_parse_deck_array("   "), [])


class TCGAdvancedFilterTests(unittest.TestCase):
    def test_accepts_pure_tcg_advanced_row(self):
        self.assertTrue(_is_tcg_advanced_record(_record()))

    def test_rejects_ocg_format(self):
        self.assertFalse(_is_tcg_advanced_record(_record(format="Tournament Meta Decks OCG")))

    def test_rejects_master_duel_tournament(self):
        self.assertFalse(_is_tcg_advanced_record(_record(tournamentName="Master Duel Championship")))

    def test_rejects_rush_duel_deck_name(self):
        self.assertFalse(_is_tcg_advanced_record(_record(deck_name="Rush Duel Fun Deck")))

    def test_rejects_genesys_and_casual_hints_in_excerpt(self):
        self.assertFalse(
            _is_tcg_advanced_record(_record(deck_excerpt="Great Genesys format list"))
        )
        self.assertFalse(
            _is_tcg_advanced_record(_record(deck_description="casual online build"))
        )

    def test_rejects_non_tournament_format(self):
        self.assertFalse(_is_tcg_advanced_record(_record(format="Meta Decks")))


class SubmitDateClassificationTests(unittest.TestCase):
    def test_date_only_value_fixes_event_date_but_not_published_at(self):
        event_date, published_at, quality = _classify_submit_date("2026-09-14", NOW)
        self.assertEqual(event_date, "2026-09-14")
        self.assertIsNone(published_at)
        self.assertEqual(quality, "date_only")

    def test_iso_timestamp_fixes_both(self):
        event_date, published_at, quality = _classify_submit_date("2026-09-14T10:00:00Z", NOW)
        self.assertEqual(event_date, "2026-09-14")
        self.assertEqual(published_at, "2026-09-14T10:00:00Z")
        self.assertEqual(quality, "timestamp")

    def test_relative_value_is_refused_not_invented(self):
        for raw in ("3 days ago", "1 week ago", "2 hours ago"):
            event_date, published_at, quality = _classify_submit_date(raw, NOW)
            self.assertIsNone(event_date, msg=raw)
            self.assertIsNone(published_at, msg=raw)
            self.assertEqual(quality, "relative", msg=raw)

    def test_missing_and_unparseable_values(self):
        self.assertEqual(_classify_submit_date("", NOW), (None, None, "missing"))
        self.assertEqual(_classify_submit_date("banana", NOW), (None, None, "unparseable"))


class RecordToObservationTests(unittest.TestCase):
    def test_valid_record_maps_all_expected_fields(self):
        observation, reason = record_to_observation(_record(), now=NOW)
        self.assertIsNone(reason)
        self.assertIsNotNone(observation)
        self.assertEqual(observation["format"], "TCG_ADVANCED")
        self.assertEqual(observation["source_deck_id"], "1001")
        self.assertEqual(observation["source_provider"], "ygoprodeck")
        self.assertEqual(observation["source_url"], "https://ygoprodeck.com/deck/snake-eye-fire-king-1001")
        self.assertEqual(observation["event_name"], "YCS Atlanta 2026")
        self.assertEqual(observation["event_date"], "2026-09-10")
        self.assertEqual(observation["placement"], "1st")
        self.assertEqual(observation["tournament_player_count"], 512)
        self.assertEqual(observation["player"], "Alice Example")

        # comma-separated arrays => copies aggregated
        self.assertEqual(
            sorted(observation["main_deck"], key=lambda c: c["name"]),
            [{"name": "11", "count": 3}, {"name": "22", "count": 2}, {"name": "33", "count": 1}],
        )
        self.assertEqual(
            sorted(observation["extra_deck"], key=lambda c: c["name"]),
            [{"name": "44", "count": 2}, {"name": "55", "count": 1}],
        )
        self.assertEqual(
            sorted(observation["side_deck"], key=lambda c: c["name"]),
            [{"name": "66", "count": 2}, {"name": "77", "count": 1}],
        )

    def test_rejects_missing_deck_num(self):
        observation, reason = record_to_observation(_record(deckNum=None), now=NOW)
        self.assertIsNone(observation)
        self.assertIn("deckNum", reason)

    def test_rejects_non_tcg_advanced_record(self):
        observation, reason = record_to_observation(_record(tournamentName="Master Duel Cup"), now=NOW)
        self.assertIsNone(observation)
        self.assertEqual(reason, "not TCG Advanced")

    def test_rejects_missing_tournament_name(self):
        observation, reason = record_to_observation(_record(tournamentName=""), now=NOW)
        self.assertIsNone(observation)
        self.assertIn("tournamentName", reason)

    def test_rejects_missing_pretty_url(self):
        observation, reason = record_to_observation(_record(pretty_url=""), now=NOW)
        self.assertIsNone(observation)
        self.assertIn("pretty_url", reason)

    def test_rejects_missing_player_name(self):
        observation, reason = record_to_observation(_record(tournamentPlayerName=""), now=NOW)
        self.assertIsNone(observation)
        self.assertIn("tournamentPlayerName", reason)

    def test_rejects_relative_submit_date(self):
        observation, reason = record_to_observation(_record(submit_date="3 days ago"), now=NOW)
        self.assertIsNone(observation)
        self.assertIn("relative", reason)

    def test_rejects_empty_deck_arrays(self):
        observation, reason = record_to_observation(
            _record(main_deck="", extra_deck="", side_deck=""), now=NOW
        )
        self.assertIsNone(observation)
        self.assertIn("empty", reason)


class FetchTcgDecksTests(unittest.TestCase):
    def test_uses_only_getdecks_endpoint_with_tcg_category_and_verifies_tls(self):
        session = _Session(pages={0: []})
        rows = fetch_tcg_decks(from_date="2026-08-01", session=session, pacing_seconds=0)
        self.assertEqual(rows, [])
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], YGOPRODECK_API_URL)
        self.assertEqual(call["params"]["_sft_category"], "Tournament Meta Decks")
        self.assertTrue(call["verify"])

    def test_pagination_and_pacing(self):
        pages = {0: [_record() for _ in range(20)], 20: [_record(deckNum=2001)]}
        session = _Session(pages=pages)
        sleeps = []
        rows = fetch_tcg_decks(
            from_date="2026-08-01",
            session=session,
            pacing_seconds=0.25,
            sleep=lambda s: sleeps.append(s),
        )
        self.assertEqual(len(rows), 21)
        self.assertEqual(len(session.calls), 2)
        # One pacing sleep between the two page requests, none before the first.
        self.assertEqual(sleeps, [0.25])

    def test_cache_reuses_prior_pages_without_new_http_calls(self):
        pages = {0: [_record()]}
        session = _Session(pages=pages)
        cache = {}
        fetch_tcg_decks(from_date="2026-08-01", session=session, pacing_seconds=0, cache=cache)
        first_call_count = len(session.calls)
        fetch_tcg_decks(from_date="2026-08-01", session=session, pacing_seconds=0, cache=cache)
        self.assertEqual(len(session.calls), first_call_count)


class CollectAndImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.report_path = str(Path(self.tmp.name) / "ygoprodeck_report.json")

    def _write_dataset(self, observations):
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": observations}, f)

    def _read_dataset(self):
        with open(self.dataset_path) as f:
            return json.load(f)

    def test_happy_path_filters_out_non_tcg_and_imports_remaining(self):
        self._write_dataset([])
        rows = [
            _record(deckNum=1),
            _record(deckNum=2, format="Tournament Meta Decks OCG", tournamentName="Tokyo OCG"),
            _record(
                deckNum=3,
                tournamentName="Master Duel Cup",
                tournamentPlayerName="Bob",
                pretty_url="md-3",
            ),
            _record(deckNum=4, submit_date="3 days ago", tournamentPlayerName="Cara", pretty_url="c-4"),
            _record(deckNum=5, tournamentPlayerName="Dan", pretty_url="d-5"),
        ]
        session = _Session(pages={0: rows})
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=NOW,
        )

        self.assertIsNone(report["endpoint_failure"])
        self.assertEqual(report["records_fetched"], 5)
        self.assertEqual(report["records_excluded_non_tcg_advanced"], 2)
        # Deck 4 rejected for relative submit_date -- honest reporting.
        rejections = {r["deckNum"] for r in report["rejected_records"]}
        self.assertIn("4", rejections)
        self.assertEqual(report["import"]["added"], 2)

        dataset = self._read_dataset()
        formats = {o["format"] for o in dataset["observations"]}
        # Only TCG_ADVANCED reached the dataset -- format separation upheld.
        self.assertEqual(formats, {"TCG_ADVANCED"})
        providers = {o.get("source_provider") for o in dataset["observations"]}
        self.assertEqual(providers, {"ygoprodeck"})
        stored_deck_ids = {o["source_deck_id"] for o in dataset["observations"]}
        self.assertEqual(stored_deck_ids, {"1", "5"})

    def test_deduplication_by_deck_num_against_existing_dataset(self):
        # Pre-existing YGOPRODeck observation with deckNum=42 must not be re-imported.
        existing = {
            "event_id": "ygoprodeck-prior-2026-09-01",
            "event_name": "Prior Event",
            "event_date": "2026-09-01",
            "region": "UNKNOWN",
            "format": "TCG_ADVANCED",
            "banlist_id": "UNKNOWN-2026-09",
            "player": "Prior Player",
            "placement": "1st",
            "archetype": "Prior",
            "source_url": "https://ygoprodeck.com/deck/prior-42",
            "source_type": "tournament",
            "published_at": None,
            "first_seen_at": "2026-09-02T00:00:00Z",
            "main_deck": [{"name": "11", "count": 1}],
            "side_deck": [],
            "extra_deck": [],
            "source_provider": "ygoprodeck",
            "source_deck_id": "42",
        }
        self._write_dataset([existing])

        rows = [
            _record(deckNum=42, tournamentPlayerName="Someone Else", pretty_url="rehash-42"),
            _record(deckNum=43, tournamentPlayerName="Fresh Player", pretty_url="fresh-43"),
        ]
        session = _Session(pages={0: rows})
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=NOW,
        )
        self.assertEqual(report["duplicate_existing_precheck_skipped"], 1)
        self.assertEqual(report["import"]["added"], 1)
        dataset = self._read_dataset()
        deck_ids = {o.get("source_deck_id") for o in dataset["observations"]}
        self.assertEqual(deck_ids, {"42", "43"})

    def test_deduplication_within_same_batch(self):
        self._write_dataset([])
        rows = [
            _record(deckNum=7, tournamentPlayerName="First"),
            _record(deckNum=7, tournamentPlayerName="Second", pretty_url="dup-7"),
        ]
        session = _Session(pages={0: rows})
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=NOW,
        )
        self.assertEqual(report["duplicate_in_batch_precheck_skipped"], 1)
        self.assertEqual(report["import"]["added"], 1)

    def test_endpoint_failure_leaves_existing_data_intact_and_reports(self):
        pre_existing = [
            {
                "event_id": "keep-me",
                "event_name": "Keep Me",
                "event_date": "2026-05-01",
                "region": "NA",
                "format": "TCG_ADVANCED",
                "banlist_id": "2026-04",
                "player": "Keeper",
                "placement": "1st",
                "archetype": "Keep",
                "source_url": "https://yugiohblog.konami.com/keeper/",
                "source_type": "tournament",
                "published_at": "2026-05-01T00:00:00Z",
                "first_seen_at": "2026-05-02T00:00:00Z",
                "main_deck": [{"name": "Ash Blossom & Joyous Spring", "count": 3}],
                "side_deck": [],
                "extra_deck": [],
            }
        ]
        self._write_dataset(pre_existing)

        session = _Session(error=requests.ConnectionError("network down"))
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=NOW,
        )
        self.assertIsNotNone(report["endpoint_failure"])
        self.assertIn("network down", report["endpoint_failure"]["error"])
        self.assertEqual(report["import"]["added"], 0)
        dataset = self._read_dataset()
        self.assertEqual(dataset["observations"], pre_existing)

    def test_missing_metadata_is_reported_not_silently_backfilled(self):
        self._write_dataset([])
        rows = [
            _record(deckNum=100, tournamentPlayerName=""),  # missing player
            _record(deckNum=101, pretty_url=""),  # missing pretty_url
            _record(deckNum=102, tournamentName=""),  # missing tournament
            _record(deckNum=None),  # missing deckNum
        ]
        session = _Session(pages={0: rows})
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=NOW,
        )
        self.assertEqual(report["import"]["added"], 0)
        reasons = {r["reason"] for r in report["rejected_records"]}
        self.assertTrue(any("tournamentPlayerName" in r for r in reasons))
        self.assertTrue(any("pretty_url" in r for r in reasons))
        self.assertTrue(any("tournamentName" in r for r in reasons))
        self.assertTrue(any("deckNum" in r for r in reasons))
        dataset = self._read_dataset()
        self.assertEqual(dataset["observations"], [])

    def test_format_separation_never_writes_non_tcg_advanced(self):
        self._write_dataset([])
        rows = [
            _record(deckNum=200, format="Tournament Meta Decks OCG"),
            _record(deckNum=201, tournamentName="Master Duel Grand Prix"),
            _record(deckNum=202, deck_name="Rush Duel Special"),
            _record(deckNum=203, deck_excerpt="Genesys demonstration list"),
        ]
        session = _Session(pages={0: rows})
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=NOW,
        )
        self.assertEqual(report["records_excluded_non_tcg_advanced"], 4)
        self.assertEqual(report["import"]["added"], 0)
        dataset = self._read_dataset()
        self.assertEqual(dataset["observations"], [])


if __name__ == "__main__":
    unittest.main()
