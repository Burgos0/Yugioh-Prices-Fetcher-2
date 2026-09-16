import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts.check_ygoprodeck_tcg_coverage import (
    DETAIL_MIN_INTERVAL_SECONDS,
    POPULATION_MASTER_DUEL,
    POPULATION_OCG,
    POPULATION_TCG_ADVANCED,
    build_coverage_report,
    fetch_deck_detail,
    fetch_tcg_decks,
    sample_top_cut_details,
    verify_deck_sample,
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
    def __init__(self, payloads):
        self._payloads = payloads
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        deck_id = int(params["deck_id"])
        payload = self._payloads[deck_id]

        class _Resp:
            def __init__(self, p):
                self._p = p

            def raise_for_status(self):
                return None

            def json(self):
                return self._p

        return _Resp(payload)


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
        rows = [self._build_row(9, "2026-09-14")]

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
        # Because the detail payload never arrived, card arrays are FAIL, not
        # invented as PASS.
        self.assertEqual(record["structure_complete_status"], "FAIL")
        self.assertEqual(record["main_deck_present_status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
