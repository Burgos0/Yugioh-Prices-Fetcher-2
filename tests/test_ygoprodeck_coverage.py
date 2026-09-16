import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts.check_ygoprodeck_tcg_coverage import (
    DECK_ID_KEYS,
    DETAIL_MIN_INTERVAL_SECONDS,
    EVENT_DATE_KEYS,
    POPULATION_MASTER_DUEL,
    POPULATION_OCG,
    POPULATION_TCG_ADVANCED,
    YGOPRODECK_DECK_DETAIL_URL_CANDIDATES,
    build_coverage_report,
    build_list_response_diagnostics,
    fetch_deck_detail,
    fetch_tcg_decks,
    sample_top_cut_details,
    verify_deck_sample,
    write_report,
)
import requests


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
    deck_id=1001,
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
    pretty_url="sample-deck-1",
):
    return {
        "deck_id": deck_id,
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
        "pretty_url": pretty_url,
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
        self.assertIn("--sample-size 20", workflow)
        self.assertIn("--min-interval-seconds 1.0", workflow)
        self.assertIn("--cache-dir", workflow)


class DetailFollowVerificationTests(unittest.TestCase):
    def test_verify_deck_sample_all_pass_from_detail(self):
        list_row = _deck(deck_id=42, submit_date="2026-09-14")
        detail = {
            "deck_id": 42,
            "main_deck": [1] * 40,
            "extra_deck": [2] * 15,
            "side_deck": [3] * 15,
            "tournamentPlacement": "1st",
        }
        record = verify_deck_sample(list_row, detail, observed_at="2026-09-16T00:00:00Z")
        self.assertEqual(record["deck_id"], 42)
        self.assertEqual(record["deck_url"], "https://ygoprodeck.com/deck/sample-deck-1")
        self.assertEqual(record["population_bucket"], POPULATION_TCG_ADVANCED)
        self.assertEqual(record["tcg_classification"], "PASS")
        self.assertEqual(record["tournament_name_status"], "PASS")
        self.assertEqual(record["event_date_status"], "PASS")
        self.assertEqual(record["event_date"], "2026-09-14")
        self.assertEqual(record["player_name_status"], "PASS")
        self.assertEqual(record["placement_status"], "PASS")
        self.assertEqual(record["player_count_status"], "PASS")
        self.assertEqual(record["main_deck_count"], 40)
        self.assertEqual(record["extra_deck_count"], 15)
        self.assertEqual(record["side_deck_count"], 15)
        self.assertEqual(record["structure_complete_status"], "PASS")
        self.assertEqual(record["sample_kind"], "top_cut_only")

    def test_verify_deck_sample_ignores_empty_list_arrays(self):
        # The list endpoint often has empty card arrays; verifier must rely on
        # the detail payload only.
        list_row = _deck(main="[]", extra="[]", side="[]")
        detail = {
            "main_deck": [1] * 40,
            "extra_deck": [2] * 15,
            "side_deck": [3] * 15,
        }
        record = verify_deck_sample(list_row, detail, observed_at="2026-09-16T00:00:00Z")
        self.assertEqual(record["structure_complete_status"], "PASS")
        self.assertEqual(record["main_deck_nonempty_status"], "PASS")

    def test_verify_deck_sample_empty_side_is_legitimate(self):
        list_row = _deck()
        detail = {"main_deck": [1] * 40, "extra_deck": [2] * 15, "side_deck": []}
        record = verify_deck_sample(list_row, detail, observed_at="obs")
        self.assertEqual(record["side_deck_present_status"], "PASS")
        self.assertEqual(record["side_deck_nonempty_status"], "MISSING")
        self.assertEqual(record["structure_complete_status"], "PASS")

    def test_verify_deck_sample_missing_side_field_is_fail_structure(self):
        list_row = _deck()
        detail = {"main_deck": [1] * 40, "extra_deck": [2] * 15}
        record = verify_deck_sample(list_row, detail, observed_at="obs")
        self.assertEqual(record["side_deck_present_status"], "FAIL")
        self.assertEqual(record["structure_complete_status"], "FAIL")

    def test_verify_deck_sample_missing_placement_and_player_count(self):
        list_row = _deck(player_count=None)
        list_row["tournamentPlayerCount"] = None
        detail = {"main_deck": [1] * 40, "extra_deck": [2] * 15, "side_deck": []}
        record = verify_deck_sample(list_row, detail, observed_at="obs")
        self.assertEqual(record["placement_status"], "MISSING")
        self.assertEqual(record["player_count_status"], "MISSING")
        # Verifier never invents values.
        self.assertIsNone(record["player_count"])

    def test_verify_deck_sample_unparseable_date_is_fail_not_pass(self):
        list_row = _deck(submit_date="not-a-date")
        record = verify_deck_sample(list_row, {}, observed_at="obs")
        self.assertEqual(record["event_date_status"], "FAIL")
        self.assertIsNone(record["event_date"])

    def test_verify_deck_sample_missing_date_stays_missing(self):
        list_row = _deck(submit_date="")
        record = verify_deck_sample(list_row, {}, observed_at="obs")
        self.assertEqual(record["event_date_status"], "MISSING")
        self.assertIsNone(record["event_date"])

    def test_verify_deck_sample_population_separation_master_duel(self):
        list_row = _deck(deck_name="Master Duel Snake-Eye")
        record = verify_deck_sample(list_row, {}, observed_at="obs")
        self.assertEqual(record["population_bucket"], POPULATION_MASTER_DUEL)
        self.assertEqual(record["tcg_classification"], "FAIL")

    def test_verify_deck_sample_population_separation_ocg(self):
        list_row = _deck(format_name="Tournament Meta Decks OCG")
        record = verify_deck_sample(list_row, {}, observed_at="obs")
        self.assertEqual(record["population_bucket"], POPULATION_OCG)
        self.assertEqual(record["tcg_classification"], "FAIL")


class _StubDetailSession:
    def __init__(self, payloads, *, url_status_map=None):
        """`payloads` maps deck_id -> dict payload returned on 200.
        `url_status_map` optionally maps candidate URL -> HTTP status to
        force (default: 200 for the first candidate URL, 404 for the rest).
        This lets tests simulate the live-run behaviour where the primary
        detail URL 404s and a later candidate succeeds."""
        self._payloads = payloads
        self._url_status_map = url_status_map or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        deck_id_raw = params.get("deck_id") or params.get("decklist")
        try:
            deck_id = int(deck_id_raw)
        except (TypeError, ValueError):
            deck_id = None
        # Default: the first candidate URL succeeds, others 404. Tests can
        # override with `url_status_map`.
        default_status = 200 if url == YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[0][0] else 404
        status = self._url_status_map.get(url, default_status)
        if status == 200 and deck_id in self._payloads:
            body = self._payloads[deck_id]
        else:
            body = {}

        class _Resp:
            def __init__(self, p, s):
                self._p = p
                self.status_code = s

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(f"{self.status_code}")
                return None

            def json(self):
                return self._p

        return _Resp(body, status)


class DetailFetchCacheAndRateLimitTests(unittest.TestCase):
    def test_fetch_deck_detail_uses_cache_on_second_call(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = _StubDetailSession({7: {"deck_id": 7, "main_deck": [1]}})
        payload, hit = fetch_deck_detail(
            7, session=session, cache_dir=tmp.name, min_interval_seconds=0.0
        )
        self.assertFalse(hit)
        self.assertEqual(payload["deck_id"], 7)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(os.path.exists(os.path.join(tmp.name, "deck_7.json")))

        payload2, hit2 = fetch_deck_detail(
            7, session=session, cache_dir=tmp.name, min_interval_seconds=0.0
        )
        self.assertTrue(hit2)
        self.assertEqual(payload2["deck_id"], 7)
        # Cache hit -> no additional HTTP call.
        self.assertEqual(len(session.calls), 1)

    def test_fetch_deck_detail_respects_min_interval(self):
        session = _StubDetailSession(
            {1: {"deck_id": 1, "main_deck": []}, 2: {"deck_id": 2, "main_deck": []}}
        )
        sleeps = []
        with mock.patch("scripts.check_ygoprodeck_tcg_coverage.time.sleep", side_effect=sleeps.append):
            last_call_ref = {"t": None}
            # Simulate that the last call happened just now.
            fetch_deck_detail(
                1,
                session=session,
                cache_dir=None,
                min_interval_seconds=1.0,
                _last_call_ref=last_call_ref,
            )
            fetch_deck_detail(
                2,
                session=session,
                cache_dir=None,
                min_interval_seconds=1.0,
                _last_call_ref=last_call_ref,
            )
        # The second call must have triggered a sleep >0s to enforce the
        # 1 req/sec minimum interval.
        self.assertTrue(sleeps, "expected time.sleep to be called for rate limiting")
        self.assertTrue(any(s > 0 for s in sleeps))

    def test_min_interval_default_is_at_least_one_second(self):
        self.assertGreaterEqual(DETAIL_MIN_INTERVAL_SECONDS, 1.0)


class SampleTopCutDetailsTests(unittest.TestCase):
    def _build_row(self, deck_id, submit_date, **overrides):
        row = _deck(
            deck_id=deck_id,
            submit_date=submit_date,
            pretty_url=f"sample-{deck_id}",
            tournament_name=f"Event {deck_id}",
            player_name=f"Player {deck_id}",
        )
        row.update(overrides)
        return row

    def test_selects_recent_paper_rows_and_limits_sample(self):
        rows = [
            self._build_row(1, "2026-09-01"),
            self._build_row(2, "2026-09-10"),
            self._build_row(
                3, "2026-09-12", deck_name="Master Duel Snake-Eye"
            ),  # excluded
            self._build_row(4, "2026-09-14"),
        ]
        payloads = {
            deck_id: {
                "deck_id": deck_id,
                "main_deck": [1] * 40,
                "extra_deck": [2] * 15,
                "side_deck": [3] * 15,
            }
            for deck_id in (1, 2, 4)
        }
        session = _StubDetailSession(payloads)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=2,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        self.assertEqual(block["sample_size_target"], 2)
        self.assertEqual(block["sample_size_actual"], 2)
        selected_ids = [d["deck_id"] for d in block["decks"]]
        # Newest paper-TCG rows first: 4 then 2 (3 is Master Duel, excluded).
        self.assertEqual(selected_ids, [4, 2])
        totals = block["field_totals"]
        self.assertEqual(totals["structure_complete_status"]["PASS"], 2)
        self.assertEqual(totals["tcg_classification"]["PASS"], 2)
        self.assertEqual(block["population_bucket_counts"][POPULATION_TCG_ADVANCED], 2)
        self.assertEqual(block["sample_kind"], "top_cut_only")

    def test_detail_error_recorded_without_inventing_data(self):
        # When detail fetch fails AND the catalogue row carries no card
        # arrays, structure must be FAIL — not invented.
        rows = [
            self._build_row(
                9,
                "2026-09-14",
                main_deck="",
                extra_deck="",
                side_deck="",
            )
        ]

        class _FailingSession:
            def __init__(self):
                self.calls = 0

            def get(self, url, params=None, timeout=None):
                self.calls += 1
                raise ValueError("simulated network error")

        session = _FailingSession()
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=5,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        self.assertEqual(block["sample_size_actual"], 1)
        self.assertEqual(len(block["detail_errors"]), 1)
        record = block["decks"][0]
        # Detail payload never arrived AND catalogue had no arrays -> FAIL,
        # not invented as PASS.
        self.assertEqual(record["structure_complete_status"], "FAIL")
        self.assertEqual(record["main_deck_present_status"], "FAIL")

    def test_detail_error_falls_back_to_catalogue_arrays_when_present(self):
        # When detail fails but the catalogue row carries `main_deck`/
        # `extra_deck`/`side_deck`, we accept them: it's the SAME field the
        # source already sent, not substitution from an unrelated field.
        rows = [
            self._build_row(
                10,
                "2026-09-14",
                main_deck="1,2,3,4,5,6,7,8,9,10",
                extra_deck="11,12,13",
                side_deck="14,15,16",
            )
        ]

        class _FailingSession:
            def __init__(self):
                self.calls = 0

            def get(self, url, params=None, timeout=None):
                self.calls += 1
                raise ValueError("simulated network error")

        session = _FailingSession()
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=5,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        self.assertEqual(block["sample_size_actual"], 1)
        self.assertEqual(len(block["detail_errors"]), 1)
        record = block["decks"][0]
        # Catalogue-carried arrays satisfy structure_complete.
        self.assertEqual(record["structure_complete_status"], "PASS")
        self.assertEqual(record["main_deck_present_status"], "PASS")
        self.assertEqual(record["main_deck_count"], 10)
        self.assertEqual(record["extra_deck_count"], 3)
        self.assertEqual(record["side_deck_count"], 3)


# --- Fixtures modelled on the shape observed in live run 35147093938 ---------
# Live logs showed events_found=143 and sample_size_actual=0 simultaneously,
# proving the list endpoint returned rows but our field names were wrong. Real
# YGOPRODeck getDecks.php list rows carry the deck id in `deckID` (not
# `deck_id`) and the date is not literally `submit_date`. These fixtures pin
# that reality down so it cannot silently regress.


def _live_shape_row(
    *,
    deck_id=98765,
    tournament_name="North America WCQ",
    date_created="2026-08-15",
    player_name="Test Player",
    player_count=456,
    format_name="Tournament Meta Decks",
    pretty_url="north-america-wcq-top-32",
):
    """A list row with the field names actually observed on the live API,
    including uppercase `deckID` and `dateCreated`. Card arrays are empty
    because the list endpoint truncates them (this is the whole reason the
    detail follow-up exists)."""
    return {
        "deckID": deck_id,
        "tournamentName": tournament_name,
        "dateCreated": date_created,
        "tournamentPlayerName": player_name,
        "tournamentPlayerCount": player_count,
        "format": format_name,
        "pretty_url": pretty_url,
        "main_deck": "",
        "extra_deck": "",
        "side_deck": "",
    }


class LiveShapeFixtureTests(unittest.TestCase):
    def test_deck_id_key_variants_are_probed(self):
        # Explicit contract: the probe MUST recognise `deckID` (live shape).
        self.assertIn("deckID", DECK_ID_KEYS)
        self.assertIn("deck_id", DECK_ID_KEYS)

    def test_event_date_key_variants_are_probed(self):
        self.assertIn("dateCreated", EVENT_DATE_KEYS)
        self.assertIn("date_created", EVENT_DATE_KEYS)
        self.assertIn("submit_date", EVENT_DATE_KEYS)

    def test_select_recent_paper_rows_accepts_live_shape(self):
        rows = [
            _live_shape_row(deck_id=1, date_created="2026-08-10"),
            _live_shape_row(deck_id=2, date_created="2026-09-01"),
            _live_shape_row(deck_id=3, date_created="2026-09-14"),
        ]
        payloads = {
            deck_id: {
                "deckID": deck_id,
                "main_deck": [1] * 40,
                "extra_deck": [2] * 15,
                "side_deck": [3] * 15,
            }
            for deck_id in (1, 2, 3)
        }
        session = _StubDetailSession(payloads)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=3,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        # This is the regression the live-run diagnosis surfaced: sample must
        # NOT be zero when the list endpoint returns valid rows in live shape.
        self.assertEqual(block["sample_size_actual"], 3)
        self.assertEqual(
            [d["deck_id"] for d in block["decks"]],
            [3, 2, 1],
        )
        self.assertEqual(block["population_bucket_counts"][POPULATION_TCG_ADVANCED], 3)
        # Every deck must have PASS event_date (live shape parses fine).
        self.assertEqual(block["field_totals"]["event_date_status"]["PASS"], 3)

    def test_list_response_diagnostics_reports_live_shape(self):
        rows = [
            _live_shape_row(deck_id=1, date_created="2026-09-01"),
            _live_shape_row(deck_id=2, date_created="2026-09-05"),
        ]
        diagnostics = build_list_response_diagnostics(rows)
        self.assertEqual(diagnostics["raw_row_count"], 2)
        self.assertIn("deckID", diagnostics["keys_observed_sample"])
        self.assertIn("dateCreated", diagnostics["keys_observed_sample"])
        self.assertIn("tournamentName", diagnostics["keys_observed_sample"])
        self.assertEqual(diagnostics["deck_id_key_present_count"], 2)
        self.assertEqual(diagnostics["deck_id_keys_seen"], ["deckID"])
        self.assertEqual(diagnostics["event_date_key_present_count"], 2)
        self.assertEqual(diagnostics["event_date_keys_seen"], ["dateCreated"])
        self.assertEqual(diagnostics["event_date_parseable_count"], 2)
        self.assertEqual(diagnostics["classified_tcg_advanced"], 2)
        self.assertEqual(diagnostics["status"], "OK")

    def test_list_response_diagnostics_flags_inconclusive_on_shape_mismatch(self):
        # If the API ever changes to a wholly unknown deck-id key, diagnostics
        # must say INCONCLUSIVE (not silently 0 with a green tick).
        rows = [
            {
                "tournamentName": "Sample",
                "format": "Tournament Meta Decks",
                "some_new_id_field": 123,
                "some_new_date_field": "2026-09-14",
            }
        ]
        diagnostics = build_list_response_diagnostics(rows)
        self.assertEqual(diagnostics["deck_id_key_present_count"], 0)
        self.assertEqual(diagnostics["deck_id_keys_seen"], [])
        # keys_observed_sample surfaces the actual keys so a human can extend
        # the probe rather than guessing.
        self.assertIn("some_new_id_field", diagnostics["keys_observed_sample"])
        self.assertIn("some_new_date_field", diagnostics["keys_observed_sample"])
        self.assertEqual(diagnostics["status"], "INCONCLUSIVE")

    def test_diagnostics_does_not_leak_values(self):
        # Sanity check that we never surface PII/values. Only KEY NAMES are
        # exposed in the sanitized diagnostics block.
        rows = [
            _live_shape_row(
                deck_id=1,
                player_name="SENSITIVE_PLAYER_NAME",
                tournament_name="SENSITIVE_TOURNAMENT",
                date_created="2026-09-14",
            )
        ]
        diagnostics = build_list_response_diagnostics(rows)
        blob = json.dumps(diagnostics)
        self.assertNotIn("SENSITIVE_PLAYER_NAME", blob)
        self.assertNotIn("SENSITIVE_TOURNAMENT", blob)

    def test_diagnostics_counts_disjoint_populations(self):
        rows = [
            _live_shape_row(deck_id=1, date_created="2026-09-14"),
            _live_shape_row(
                deck_id=2,
                date_created="2026-09-14",
                tournament_name="OCG Kansai CS",
            ),
            _live_shape_row(
                deck_id=3,
                date_created="2026-09-14",
                tournament_name="Master Duel Cup",
            ),
        ]
        diagnostics = build_list_response_diagnostics(rows)
        # is_paper_tcg_record excludes rows carrying non-TCG keywords, so only
        # the TCG_ADVANCED row survives the paper filter.
        self.assertEqual(diagnostics["passes_is_paper_tcg_record"], 1)
        self.assertEqual(diagnostics["classified_tcg_advanced"], 1)


class ExclusionReasonAndInconclusiveTests(unittest.TestCase):
    def test_zero_candidates_when_no_deck_id_key(self):
        # This is the exact regression from live run 35147093938: rows had
        # `deck_id`-look-alike missing. Verify the reason is now categorised.
        rows = [
            {
                "tournamentName": "Sample",
                "format": "Tournament Meta Decks",
                "dateCreated": "2026-09-14",
                "pretty_url": "x",
            }
            for _ in range(5)
        ]
        session = _StubDetailSession({})
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=5,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        self.assertEqual(block["sample_size_actual"], 0)
        # No detail HTTP call was made.
        self.assertEqual(len(session.calls), 0)
        # Reason is surfaced explicitly.
        self.assertEqual(
            block["exclusion_reason_counts"].get("no_deck_id"), 5
        )

    def test_zero_candidates_when_only_non_tcg_population(self):
        rows = [
            _live_shape_row(deck_id=1, tournament_name="OCG Nagoya"),
            _live_shape_row(deck_id=2, tournament_name="Master Duel Cup"),
        ]
        session = _StubDetailSession({})
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=5,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        self.assertEqual(block["sample_size_actual"], 0)
        # is_paper_tcg_record rejects both via keyword filter.
        self.assertEqual(
            block["exclusion_reason_counts"].get("excluded_by_keyword_or_wrong_format"),
            2,
        )

    def test_run_experiment_marks_inconclusive_and_exits_nonzero(self):
        # Simulate: catalogue returns rows in a shape the probe cannot map.
        from scripts import check_ygoprodeck_tcg_coverage as mod

        rows = [
            {
                "tournamentName": "Sample",
                "format": "Tournament Meta Decks",
                "dateCreated": "2026-09-14",
            }
        ]

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_path = os.path.join(tmp.name, "report.json")

        with mock.patch.object(mod, "fetch_tcg_decks", return_value=rows), mock.patch.object(
            mod.requests, "Session"
        ):
            report = mod.run_experiment(
                output_path=out_path,
                lookback_days=90,
                timeout=10,
                sample_size=5,
                cache_dir=None,
                min_interval_seconds=0.0,
            )
        self.assertEqual(report["overall_status"], "INCONCLUSIVE")
        self.assertEqual(report["top_cut_sample"]["sample_size_actual"], 0)
        # Artifact is still written so operators can inspect diagnostics.
        with open(out_path) as f:
            written = json.load(f)
        self.assertEqual(written["overall_status"], "INCONCLUSIVE")
        self.assertEqual(
            written["list_response_diagnostics"]["deck_id_key_present_count"], 0
        )


class DeckNumAndSubmitDateShapeTests(unittest.TestCase):
    """Pin down the *actual* getDecks.php list shape observed in the newer
    live run: the deck id is `deckNum` (not `deckID`), and `submit_date` is
    a MySQL DATETIME string ("YYYY-MM-DD HH:MM:SS"). Prior fixtures used a
    guessed shape; these fixtures encode the real one."""

    @staticmethod
    def _live_row(deck_num, submit_date, **overrides):
        row = {
            "deckNum": deck_num,
            "submit_date": submit_date,
            "tournamentName": "Sample Regional",
            "tournamentPlacement": "Top 8",
            "tournamentPlayerCount": 128,
            "tournamentPlayerName": "Player X",
            "format": "Tournament Meta Decks",
            "pretty_url": f"sample-{deck_num}",
            "main_deck": "",
            "extra_deck": "",
            "side_deck": "",
        }
        row.update(overrides)
        return row

    def test_deck_num_is_probed_first(self):
        self.assertEqual(DECK_ID_KEYS[0], "deckNum")

    def test_submit_date_mysql_datetime_parses(self):
        from scripts.check_ygoprodeck_tcg_coverage import (
            _extract_event_date,
        )
        row = self._live_row(732667, "2026-08-12 20:23:11")
        parsed, raw, key, ok = _extract_event_date(row)
        self.assertTrue(ok)
        self.assertEqual(key, "submit_date")
        self.assertEqual(raw, "2026-08-12 20:23:11")
        self.assertEqual(parsed.strftime("%Y-%m-%d"), "2026-08-12")

    def test_submit_date_iso_t_parses(self):
        from scripts.check_ygoprodeck_tcg_coverage import _parse_event_date
        self.assertIsNotNone(_parse_event_date("2026-08-12T20:23:11"))
        self.assertIsNotNone(_parse_event_date("2026-08-12T20:23:11Z"))
        self.assertIsNotNone(_parse_event_date("2026-08-12T20:23:11.500Z"))

    def test_submit_date_epoch_seconds_parses(self):
        from scripts.check_ygoprodeck_tcg_coverage import _parse_event_date
        # 2026-08-12 00:00:00 UTC = 1786320000
        parsed = _parse_event_date(1786320000)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)
        # Numeric string form.
        parsed2 = _parse_event_date("1786320000")
        self.assertIsNotNone(parsed2)
        self.assertEqual(parsed2, parsed)

    def test_submit_date_epoch_milliseconds_parses(self):
        from scripts.check_ygoprodeck_tcg_coverage import _parse_event_date
        parsed = _parse_event_date(1786320000000)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)

    def test_submit_date_human_format_parses(self):
        from scripts.check_ygoprodeck_tcg_coverage import _parse_event_date
        self.assertIsNotNone(_parse_event_date("August 12, 2026"))
        self.assertIsNotNone(_parse_event_date("Aug 12, 2026"))
        self.assertIsNotNone(_parse_event_date("12 August 2026"))

    def test_submit_date_truly_unparseable_stays_fail(self):
        # Honest missing rule: vague relatives / gibberish stay FAIL. The
        # source's own concrete relative encoding (`N units ago`, `yesterday`)
        # is decoded — that's parsing the same field, not substitution.
        from scripts.check_ygoprodeck_tcg_coverage import _parse_event_date
        self.assertIsNone(_parse_event_date("recently"))
        self.assertIsNone(_parse_event_date("gibberish"))
        self.assertIsNone(_parse_event_date("a while ago"))
        self.assertIsNone(_parse_event_date("some time ago"))

    def test_submit_date_concrete_relative_ago_parses(self):
        # The current live artifact has `submit_date` in "N units ago" form
        # for every row. Decode concrete forms so 14/30/90-day coverage
        # windows work; the sanitized diagnostic separately reports how
        # many rows used relative interpretation.
        from scripts.check_ygoprodeck_tcg_coverage import _parse_event_date
        ref = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _parse_event_date("3 days ago", now=ref),
            datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _parse_event_date("2 weeks ago", now=ref),
            datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _parse_event_date("1 hour ago", now=ref),
            datetime(2026, 9, 16, 11, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            _parse_event_date("yesterday", now=ref),
            datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc),
        )
        # 30-day and 90-day windows must include a "1 month ago" record.
        self.assertIsNotNone(_parse_event_date("1 month ago", now=ref))
        self.assertIsNotNone(_parse_event_date("2 months ago", now=ref))
        # Case-insensitive.
        self.assertIsNotNone(_parse_event_date("5 Days Ago", now=ref))

    def test_list_response_diagnostics_recognizes_deck_num_and_submit_date(self):
        rows = [
            self._live_row(732667, "2026-08-12 20:23:11"),
            self._live_row(732668, "2026-08-13 15:00:01"),
            self._live_row(732669, "2026-08-14 09:44:12"),
        ]
        d = build_list_response_diagnostics(rows)
        self.assertEqual(d["raw_row_count"], 3)
        self.assertEqual(d["deck_id_key_present_count"], 3)
        self.assertEqual(d["deck_id_keys_seen"], ["deckNum"])
        self.assertEqual(d["event_date_key_present_count"], 3)
        self.assertEqual(d["event_date_keys_seen"], ["submit_date"])
        self.assertEqual(d["event_date_parseable_count"], 3)
        self.assertEqual(d["classified_tcg_advanced"], 3)
        self.assertEqual(d["status"], "OK")
        # Shape counter reports the actual live shape, key-only.
        self.assertEqual(d["event_date_shape_counts"].get("sql_datetime"), 3)

    def test_event_date_shape_counts_classifies_unknown_shape(self):
        rows = [
            self._live_row(1, "recently"),
            self._live_row(2, "3 days ago"),
        ]
        d = build_list_response_diagnostics(rows)
        # Shape counter surfaces the actual junk shape so operators can
        # extend parsing without leaking values.
        self.assertGreaterEqual(
            d["event_date_shape_counts"].get("unknown", 0)
            + d["event_date_shape_counts"].get("relative_ago", 0),
            2,
        )
        # Sanity: no raw value bleeds into the diagnostic blob.
        self.assertNotIn("recently", json.dumps(d))
        self.assertNotIn("3 days ago", json.dumps(d))

    def test_sample_top_cut_details_follows_20_detail_urls_for_live_shape(self):
        rows = [
            self._live_row(1000 + i, f"2026-08-{(i % 28) + 1:02d} 12:00:00")
            for i in range(30)
        ]
        payloads = {
            1000 + i: {
                "deckNum": 1000 + i,
                "main_deck": [1] * 40,
                "extra_deck": [2] * 15,
                "side_deck": [3] * 15,
            }
            for i in range(30)
        }
        session = _StubDetailSession(payloads)
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=20,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        # THIS is the regression under repair: sample must be 20 (not 0)
        # when the live shape carries deckNum + MySQL submit_date.
        self.assertEqual(block["sample_size_actual"], 20)
        self.assertEqual(len(session.calls), 20)
        self.assertEqual(block["field_totals"]["event_date_status"]["PASS"], 20)
        self.assertEqual(
            block["population_bucket_counts"][POPULATION_TCG_ADVANCED], 20
        )

    def test_verify_deck_sample_honours_unparseable_date_for_live_shape(self):
        # Honest missing: parse fails -> FAIL, not PASS, and event_date is
        # never substituted from another timestamp.
        list_row = self._live_row(1, "recently")
        record = verify_deck_sample(list_row, {}, observed_at="obs")
        self.assertEqual(record["event_date_status"], "FAIL")
        self.assertIsNone(record["event_date"])


class DetailUrlCandidateFallbackTests(unittest.TestCase):
    """The current live artifact showed every request to
    /api/decks/get.php?deck_id=… returning 404. Pin the recovery: the probe
    tries multiple documented/known-frontend URL candidates and records
    per-URL HTTP status counts in a sanitized diagnostic, and the run is
    marked INCONCLUSIVE when none succeed."""

    @staticmethod
    def _row(deck_num, submit_date="2026-09-14 12:00:00", **overrides):
        row = {
            "deckNum": deck_num,
            "submit_date": submit_date,
            "tournamentName": f"Event {deck_num}",
            "tournamentPlacement": "Top 8",
            "tournamentPlayerCount": 128,
            "tournamentPlayerName": "Player",
            "format": "Tournament Meta Decks",
            "pretty_url": f"deck-{deck_num}",
            "main_deck": "",
            "extra_deck": "",
            "side_deck": "",
        }
        row.update(overrides)
        return row

    def test_url_candidate_list_starts_with_getDeck_php(self):
        # /api/decks/get.php returned 404 for all 20 attempts in the live
        # artifact — it MUST no longer be the primary candidate.
        primary_url = YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[0][0]
        self.assertNotEqual(primary_url, "https://ygoprodeck.com/api/decks/get.php")
        # And getDeck.php (singular, current front-end) MUST be probed.
        candidate_urls = {url for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES}
        self.assertIn("https://ygoprodeck.com/api/decks/getDeck.php", candidate_urls)
        self.assertIn("https://ygoprodeck.com/api/deck.php", candidate_urls)

    def test_fetch_deck_detail_falls_through_to_second_candidate(self):
        # Simulate: primary URL 404s, second candidate returns the payload.
        second_url = YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[1][0]
        session = _StubDetailSession(
            {42: {"deckNum": 42, "main_deck": [1] * 40, "extra_deck": [2] * 15, "side_deck": [3] * 15}},
            url_status_map={
                YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[0][0]: 404,
                second_url: 200,
            },
        )
        payload, hit = fetch_deck_detail(
            42, session=session, cache_dir=None, min_interval_seconds=0.0
        )
        self.assertFalse(hit)
        self.assertEqual(payload["deckNum"], 42)
        # Both URLs were tried; the first got a 404, the second succeeded.
        tried_urls = [call["url"] for call in session.calls]
        self.assertEqual(tried_urls[0], YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[0][0])
        self.assertEqual(tried_urls[1], second_url)

    def test_fetch_deck_detail_respects_decklist_param_name(self):
        # The /api/deck.php URL takes `decklist=`, not `deck_id=`.
        session = _StubDetailSession(
            {42: {"deckNum": 42, "main_deck": [1], "extra_deck": [2], "side_deck": [3]}},
            url_status_map={
                YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[0][0]: 404,
                YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[1][0]: 200,
            },
        )
        fetch_deck_detail(42, session=session, cache_dir=None, min_interval_seconds=0.0)
        deck_php_call = [
            c for c in session.calls if c["url"] == YGOPRODECK_DECK_DETAIL_URL_CANDIDATES[1][0]
        ][0]
        self.assertIn("decklist", deck_php_call["params"])
        self.assertEqual(int(deck_php_call["params"]["decklist"]), 42)

    def test_all_candidates_404_raises_request_exception(self):
        session = _StubDetailSession(
            {},
            url_status_map={url: 404 for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES},
        )
        with self.assertRaises(requests.RequestException) as ctx:
            fetch_deck_detail(42, session=session, cache_dir=None, min_interval_seconds=0.0)
        # Sanitized: message references URLs and statuses, no payload bodies.
        self.assertIn("42", str(ctx.exception))
        self.assertIn("404", str(ctx.exception))

    def test_detail_probe_summary_records_per_url_status_counts(self):
        rows = [self._row(1000 + i) for i in range(3)]
        # Every attempt 404s across every candidate.
        session = _StubDetailSession(
            {},
            url_status_map={url: 404 for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES},
        )
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            rows,
            sample_size=3,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        summary = block["detail_probe_summary"]
        # Every candidate URL appears with a 404 count of 3 (one per deck).
        for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES:
            self.assertIn(url, summary)
            self.assertEqual(summary[url].get("404"), 3)
        # Detail errors surfaced but sample size still 3 (verified rows, no
        # invented data).
        self.assertEqual(block["sample_size_actual"], 3)
        self.assertEqual(len(block["detail_errors"]), 3)
        # No structure_complete PASS because catalogue rows had empty arrays.
        self.assertEqual(
            block["field_totals"]["structure_complete_status"].get("PASS", 0), 0
        )

    def test_detail_probe_summary_does_not_leak_bodies(self):
        session = _StubDetailSession(
            {},
            url_status_map={url: 404 for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES},
        )
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        rows = [
            self._row(1, tournamentPlayerName="SECRET_PLAYER_XYZ"),
        ]
        block = sample_top_cut_details(
            rows,
            sample_size=1,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        blob = json.dumps(block["detail_probe_summary"])
        self.assertNotIn("SECRET_PLAYER_XYZ", blob)

    def test_run_experiment_marks_inconclusive_when_all_details_404(self):
        # Even though sample_size_actual > 0, if no detail HTTP call
        # succeeded AND no catalogue arrays were usable, overall_status
        # MUST be INCONCLUSIVE.
        from scripts import check_ygoprodeck_tcg_coverage as mod

        rows = [self._row(1000 + i) for i in range(5)]

        class _All404Session:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, timeout=None):
                self.calls.append({"url": url, "params": dict(params or {})})

                class _R:
                    status_code = 404

                    def raise_for_status(self):
                        raise requests.HTTPError("404")

                    def json(self):
                        return {}

                return _R()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_path = os.path.join(tmp.name, "report.json")

        session = _All404Session()
        with mock.patch.object(mod, "fetch_tcg_decks", return_value=rows):
            report = mod.run_experiment(
                output_path=out_path,
                lookback_days=90,
                timeout=10,
                sample_size=5,
                cache_dir=None,
                min_interval_seconds=0.0,
                session=session,
            )
        self.assertEqual(report["overall_status"], "INCONCLUSIVE")
        # detail_probe_summary is populated so operators can inspect.
        self.assertGreater(len(report["top_cut_sample"]["detail_probe_summary"]), 0)


class CatalogueArrayFallbackTests(unittest.TestCase):
    """When getDecks.php itself carries card arrays (either JSON strings or
    comma-separated numeric strings), the probe must accept them rather
    than treating structure as MISSING."""

    def test_comma_separated_main_deck_parses(self):
        from scripts.check_ygoprodeck_tcg_coverage import _parse_json_array
        self.assertEqual(
            _parse_json_array("12345,67890,42"),
            ["12345", "67890", "42"],
        )
        # Whitespace and empty tokens tolerated.
        self.assertEqual(
            _parse_json_array("1, 2 ,3,"),
            ["1", "2", "3"],
        )
        # Non-numeric comma strings stay empty (not decklist data).
        self.assertEqual(_parse_json_array("Dark Magician, Blue-Eyes"), [])

    def test_json_array_string_still_parses(self):
        from scripts.check_ygoprodeck_tcg_coverage import _parse_json_array
        self.assertEqual(_parse_json_array("[1,2,3]"), ["1", "2", "3"])
        self.assertEqual(_parse_json_array([1, 2, 3]), ["1", "2", "3"])

    def test_catalogue_arrays_trigger_structure_pass_when_detail_fails(self):
        # End-to-end: detail 404 on all candidates; catalogue arrays present.
        row = {
            "deckNum": 501,
            "submit_date": "2026-09-14 12:00:00",
            "tournamentName": "Event",
            "tournamentPlacement": "Top 8",
            "tournamentPlayerCount": 128,
            "tournamentPlayerName": "P",
            "format": "Tournament Meta Decks",
            "pretty_url": "e-501",
            "main_deck": "1,2,3,4,5,6,7,8,9,10",
            "extra_deck": "11,12,13",
            "side_deck": "14,15,16",
        }
        session = _StubDetailSession(
            {},
            url_status_map={url: 404 for url, _ in YGOPRODECK_DECK_DETAIL_URL_CANDIDATES},
        )
        now = datetime(2026, 9, 16, tzinfo=timezone.utc)
        block = sample_top_cut_details(
            [row],
            sample_size=1,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        record = block["decks"][0]
        self.assertEqual(record["structure_complete_status"], "PASS")
        self.assertEqual(record["main_deck_count"], 10)


class RelativeAgoSubmitDateEndToEndTests(unittest.TestCase):
    """Live artifact showed EVERY submit_date value in relative_ago form.
    Pin the end-to-end behaviour: concrete forms parse; the sanitized
    shape counter reports them; imprecise phrases stay FAIL."""

    @staticmethod
    def _row(deck_num, submit_date):
        return {
            "deckNum": deck_num,
            "submit_date": submit_date,
            "tournamentName": f"Event {deck_num}",
            "tournamentPlacement": "Top 8",
            "tournamentPlayerCount": 128,
            "tournamentPlayerName": "P",
            "format": "Tournament Meta Decks",
            "pretty_url": f"e-{deck_num}",
            "main_deck": "1,2,3,4,5,6,7,8,9,10",
            "extra_deck": "11,12,13",
            "side_deck": "14,15,16",
        }

    def test_diagnostics_classifies_relative_ago_shape(self):
        rows = [
            self._row(1, "3 days ago"),
            self._row(2, "1 week ago"),
            self._row(3, "2 hours ago"),
            self._row(4, "yesterday"),
            self._row(5, "recently"),
        ]
        now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        d = build_list_response_diagnostics(rows, now=now)
        # 4 of 5 rows parseable (the 5th is vague).
        self.assertEqual(d["event_date_parseable_count"], 4)
        # Shape counter surfaces the ago category, key-only.
        self.assertGreaterEqual(d["event_date_shape_counts"].get("relative_ago", 0), 4)

    def test_end_to_end_ago_dates_pass_and_populate_windows(self):
        # 3 rows within 14 days, 3 rows within 30 days, 3 within 90 days.
        session = _StubDetailSession(
            {
                i: {"deckNum": i, "main_deck": [1] * 40, "extra_deck": [2] * 15, "side_deck": [3] * 15}
                for i in range(1, 10)
            },
        )
        now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        rows = [
            self._row(1, "3 days ago"),
            self._row(2, "1 week ago"),
            self._row(3, "2 weeks ago"),
        ]
        block = sample_top_cut_details(
            rows,
            sample_size=3,
            session=session,
            cache_dir=None,
            min_interval_seconds=0.0,
            now=now,
        )
        self.assertEqual(block["sample_size_actual"], 3)
        self.assertEqual(block["field_totals"]["event_date_status"]["PASS"], 3)


if __name__ == "__main__":
    unittest.main()
