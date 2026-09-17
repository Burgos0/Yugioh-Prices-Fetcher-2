"""
Focused tests for the YGOPRODeck card-identity bridge.

Covers, per the PR problem statement:

- Successful passcode -> canonical name resolution.
- Copy-count preservation through resolution.
- Exactly one catalogue fetch per collector run.
- Local cache reuse across runs (no extra HTTP calls).
- Unresolved passcodes are reported and NEVER substituted.
- Endpoint failure leaves the dataset byte-identical.
- Backfill CLI is idempotent (second run is a no-op).
- Legacy / non-YGOPRODeck observations preserved byte-identical.
- Read-only prices.db coverage validation with one-card-to-many-printings
  behaviour; missing DB reported honestly, not replaced with fixtures.

Network is stubbed. No real HTTP call is made from this file.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.backfill_ygoprodeck_card_names import backfill, check_prices_db_coverage
from scripts.collect_ygoprodeck_lists import collect_and_import
from scripts.ygoprodeck_card_catalogue import (
    CatalogueError,
    YGOPRODECK_CARDINFO_URL,
    build_passcode_map,
    fetch_catalogue,
    load_passcode_map,
    observation_has_numeric_passcodes,
    resolve_observation_cards,
    resolve_zone,
)


NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

# Realistic-ish cardinfo payload the tests fetch through the stubs. The
# 14558128 -> "Ash Blossom & Joyous Spring" mapping is the exact example
# in the problem statement.
CARDINFO_PAYLOAD = {
    "data": [
        {"id": 14558128, "name": "Ash Blossom & Joyous Spring"},
        {"id": 23434538, "name": "Maxx \"C\""},
        {"id": 10045474, "name": "PSY-Framegear Gamma"},
        {"id": 89631139, "name": "Blue-Eyes White Dragon"},
        {"id": 46986414, "name": "Dark Magician"},
        {"id": 82301904, "name": "Junk Speeder"},
    ]
}


class _CardinfoResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _CardinfoSession:
    def __init__(self, payload=CARDINFO_PAYLOAD, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def get(self, url, timeout=None, verify=None, params=None):
        self.calls.append({"url": url, "timeout": timeout, "verify": verify, "params": params})
        if self.error is not None:
            raise self.error
        return _CardinfoResponse(self.payload)


class BuildPasscodeMapTests(unittest.TestCase):
    def test_extracts_id_to_name_map(self):
        m = build_passcode_map(CARDINFO_PAYLOAD["data"])
        self.assertEqual(m["14558128"], "Ash Blossom & Joyous Spring")
        self.assertEqual(m["89631139"], "Blue-Eyes White Dragon")
        self.assertEqual(len(m), 6)

    def test_ignores_missing_or_malformed_entries(self):
        m = build_passcode_map(
            [
                {"id": 1, "name": "Ok"},
                {"id": "not-int", "name": "Skip Me"},
                {"id": 2, "name": ""},
                {"id": 3},
                {"name": "no id"},
                "totally invalid",
                {"id": 4, "name": "   trimmed   "},
            ]
        )
        self.assertEqual(m, {"1": "Ok", "4": "trimmed"})

    def test_handles_non_list_gracefully(self):
        self.assertEqual(build_passcode_map(None), {})
        self.assertEqual(build_passcode_map({"data": "nope"}), {})


class ResolveZoneTests(unittest.TestCase):
    MAP = {"14558128": "Ash Blossom & Joyous Spring", "89631139": "Blue-Eyes White Dragon"}

    def test_resolves_passcodes_and_preserves_counts(self):
        resolved, unresolved = resolve_zone(
            [{"name": "14558128", "count": 3}, {"name": "89631139", "count": 2}], self.MAP
        )
        self.assertEqual(
            resolved,
            [
                {"name": "Ash Blossom & Joyous Spring", "count": 3},
                {"name": "Blue-Eyes White Dragon", "count": 2},
            ],
        )
        self.assertEqual(unresolved, [])

    def test_passes_through_canonical_names_unchanged(self):
        resolved, unresolved = resolve_zone(
            [{"name": "Effect Veiler", "count": 2}], self.MAP
        )
        self.assertEqual(resolved, [{"name": "Effect Veiler", "count": 2}])
        self.assertEqual(unresolved, [])

    def test_reports_unresolved_and_does_not_substitute(self):
        resolved, unresolved = resolve_zone(
            [{"name": "14558128", "count": 3}, {"name": "99999999", "count": 1}], self.MAP
        )
        # Only the resolved one is returned. The unresolved passcode is
        # reported. We never invent a substitute card.
        self.assertEqual(resolved, [{"name": "Ash Blossom & Joyous Spring", "count": 3}])
        self.assertEqual(unresolved, [{"passcode": "99999999", "count": 1}])

    def test_combines_duplicate_canonical_targets(self):
        # Defensive: two different passcodes that map to the same
        # canonical name (should never happen in practice, but must not
        # silently drop copies).
        m = {"1": "Foo", "2": "Foo"}
        resolved, unresolved = resolve_zone(
            [{"name": "1", "count": 2}, {"name": "2", "count": 1}], m
        )
        self.assertEqual(resolved, [{"name": "Foo", "count": 3}])
        self.assertEqual(unresolved, [])


class ResolveObservationTests(unittest.TestCase):
    MAP = {"14558128": "Ash Blossom & Joyous Spring", "23434538": "Maxx \"C\""}

    def _obs(self):
        return {
            "event_id": "e",
            "main_deck": [
                {"name": "14558128", "count": 3},
                {"name": "23434538", "count": 3},
            ],
            "side_deck": [{"name": "14558128", "count": 2}],
            "extra_deck": [],
        }

    def test_fully_resolvable_observation(self):
        resolved, unresolved = resolve_observation_cards(self._obs(), self.MAP)
        self.assertEqual(unresolved, {})
        # Copy counts preserved zone-by-zone.
        self.assertEqual(
            resolved["main_deck"],
            [
                {"name": "Ash Blossom & Joyous Spring", "count": 3},
                {"name": "Maxx \"C\"", "count": 3},
            ],
        )
        self.assertEqual(
            resolved["side_deck"], [{"name": "Ash Blossom & Joyous Spring", "count": 2}]
        )
        self.assertEqual(resolved["extra_deck"], [])
        # Other fields preserved (this is a shallow copy update).
        self.assertEqual(resolved["event_id"], "e")

    def test_unresolved_observation_yields_none_and_report(self):
        obs = self._obs()
        obs["main_deck"].append({"name": "99999999", "count": 1})
        resolved, unresolved = resolve_observation_cards(obs, self.MAP)
        self.assertIsNone(resolved)
        self.assertIn("main_deck", unresolved)
        self.assertEqual(
            unresolved["main_deck"], [{"passcode": "99999999", "count": 1}]
        )


class HasNumericPasscodesTests(unittest.TestCase):
    def test_true_when_any_zone_has_digit_name(self):
        obs = {"main_deck": [{"name": "Ash Blossom & Joyous Spring", "count": 3}],
               "side_deck": [{"name": "14558128", "count": 1}],
               "extra_deck": []}
        self.assertTrue(observation_has_numeric_passcodes(obs))

    def test_false_when_all_canonical(self):
        obs = {"main_deck": [{"name": "Ash Blossom & Joyous Spring", "count": 3}],
               "side_deck": [], "extra_deck": []}
        self.assertFalse(observation_has_numeric_passcodes(obs))


class FetchCatalogueTests(unittest.TestCase):
    def test_uses_https_endpoint_and_verifies_tls(self):
        session = _CardinfoSession()
        payload = fetch_catalogue(session=session, retry_pacing_seconds=0)
        self.assertEqual(payload, CARDINFO_PAYLOAD)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], YGOPRODECK_CARDINFO_URL)
        self.assertTrue(call["verify"])
        self.assertIsNotNone(call["timeout"])

    def test_retries_on_transient_errors_then_succeeds(self):
        class _FlakySession:
            def __init__(self):
                self.calls = 0

            def get(self, url, timeout=None, verify=None, params=None):
                self.calls += 1
                if self.calls < 3:
                    raise requests.ConnectionError("blip")
                return _CardinfoResponse(CARDINFO_PAYLOAD)

        session = _FlakySession()
        sleeps = []
        payload = fetch_catalogue(
            session=session,
            retry_pacing_seconds=0.1,
            sleep=lambda s: sleeps.append(s),
            max_attempts=3,
        )
        self.assertEqual(payload, CARDINFO_PAYLOAD)
        self.assertEqual(session.calls, 3)
        # Two retries -> two paces.
        self.assertEqual(sleeps, [0.1, 0.1])

    def test_gives_up_and_raises_catalogue_error(self):
        session = _CardinfoSession(error=requests.ConnectionError("down"))
        with self.assertRaises(CatalogueError):
            fetch_catalogue(session=session, retry_pacing_seconds=0, max_attempts=2)

    def test_invalid_json_raises_immediately(self):
        class _BadJsonSession:
            def get(self, url, timeout=None, verify=None, params=None):
                class R:
                    def raise_for_status(self):
                        pass

                    def json(self):
                        raise ValueError("bad json")

                return R()

        with self.assertRaises(CatalogueError):
            fetch_catalogue(session=_BadJsonSession(), retry_pacing_seconds=0)


class LoadPasscodeMapCacheTests(unittest.TestCase):
    def test_cache_reuse_avoids_second_fetch(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            session = _CardinfoSession()
            m1, src1 = load_passcode_map(cache_dir=cache_dir, session=session)
            m2, src2 = load_passcode_map(cache_dir=cache_dir, session=session)
            self.assertEqual(m1, m2)
            self.assertEqual(src1, "api")
            self.assertEqual(src2, "cache")
            # Exactly one HTTP call across both loads.
            self.assertEqual(len(session.calls), 1)
            # Cache file is present under the supplied cache_dir.
            self.assertTrue(
                (Path(cache_dir) / "ygoprodeck_cardinfo.json").exists()
            )

    def test_missing_cache_and_api_failure_raises(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            session = _CardinfoSession(error=requests.ConnectionError("down"))
            with self.assertRaises(CatalogueError):
                load_passcode_map(cache_dir=cache_dir, session=session)


# --- Collector integration --------------------------------------------------


def _deck_record(deck_num, main_ids, extra_ids, side_ids, player="P"):
    return {
        "deckNum": deck_num,
        "deck_name": "Snake-Eye Fire King",
        "format": "Tournament Meta Decks",
        "tournamentName": "YCS Atlanta 2026",
        "tournamentPlacement": "1st",
        "tournamentPlayerName": player,
        "tournamentPlayerCount": 512,
        "submit_date": "2026-09-10",
        "main_deck": main_ids,
        "extra_deck": extra_ids,
        "side_deck": side_ids,
        "deck_description": "",
        "deck_excerpt": "",
        "pretty_url": f"deck-{deck_num}",
    }


class _DeckPageSession:
    """Only responds to getDecks.php, not to cardinfo.php. Used to be
    explicit that the collector must not fetch the catalogue via this
    session (the tests wire the catalogue in separately)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, url, params=None, timeout=None, verify=None):
        self.calls.append({"url": url, "params": dict(params or {}), "verify": verify})
        offset = int((params or {}).get("offset", 0))
        rows = self.rows if offset == 0 else []

        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return rows

        return _R()


class CollectorResolvesPasscodesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.report_path = str(Path(self.tmp.name) / "report.json")
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": []}, f)

    def test_resolves_and_preserves_counts_and_fetches_catalogue_once(self):
        rows = [
            _deck_record(
                1,
                main_ids="14558128,14558128,14558128,23434538",
                extra_ids="89631139,89631139",
                side_ids="46986414",
            )
        ]
        deck_session = _DeckPageSession(rows)
        catalogue_session = _CardinfoSession()
        with tempfile.TemporaryDirectory() as cache_dir:
            report = collect_and_import(
                dataset_path=self.dataset_path,
                report_path=self.report_path,
                session=deck_session,
                pacing_seconds=0,
                sleep=lambda s: None,
                now=NOW,
                cache_dir=cache_dir,
                catalogue_session=catalogue_session,
            )
        self.assertIsNone(report["endpoint_failure"])
        self.assertEqual(report["import"]["added"], 1)
        # Exactly one cardinfo HTTP call regardless of how many decks/cards
        # were processed.
        self.assertEqual(len(catalogue_session.calls), 1)
        self.assertEqual(report["card_catalogue"]["source"], "api")

        with open(self.dataset_path) as f:
            dataset = json.load(f)
        obs = dataset["observations"][0]
        # Names are canonical; copy counts preserved.
        self.assertEqual(
            sorted(obs["main_deck"], key=lambda c: c["name"]),
            [
                {"name": "Ash Blossom & Joyous Spring", "count": 3},
                {"name": "Maxx \"C\"", "count": 1},
            ],
        )
        self.assertEqual(
            obs["extra_deck"], [{"name": "Blue-Eyes White Dragon", "count": 2}]
        )
        self.assertEqual(obs["side_deck"], [{"name": "Dark Magician", "count": 1}])

    def test_unresolved_passcode_rejects_row_and_never_substitutes(self):
        rows = [
            _deck_record(
                1,
                main_ids="14558128,99999999",  # 99999999 unknown
                extra_ids="89631139",
                side_ids="46986414",
            )
        ]
        deck_session = _DeckPageSession(rows)
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=deck_session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=NOW,
            passcode_map=build_passcode_map(CARDINFO_PAYLOAD["data"]),
        )
        # Nothing imported; unresolved passcode reported honestly.
        self.assertEqual(report["import"]["added"], 0)
        self.assertEqual(len(report["unresolved_passcode_records"]), 1)
        unresolved = report["unresolved_passcode_records"][0]["unresolved"]
        self.assertIn("main_deck", unresolved)
        self.assertEqual(
            unresolved["main_deck"], [{"passcode": "99999999", "count": 1}]
        )
        with open(self.dataset_path) as f:
            dataset = json.load(f)
        self.assertEqual(dataset["observations"], [])

    def test_catalogue_endpoint_failure_leaves_dataset_untouched(self):
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
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": pre_existing}, f)
        with open(self.dataset_path, "rb") as f:
            original_bytes = f.read()

        rows = [_deck_record(1, "14558128", "89631139", "46986414")]
        deck_session = _DeckPageSession(rows)
        catalogue_session = _CardinfoSession(error=requests.ConnectionError("down"))
        with tempfile.TemporaryDirectory() as cache_dir:
            report = collect_and_import(
                dataset_path=self.dataset_path,
                report_path=self.report_path,
                session=deck_session,
                pacing_seconds=0,
                sleep=lambda s: None,
                now=NOW,
                cache_dir=cache_dir,
                catalogue_session=catalogue_session,
            )
        self.assertIsNotNone(report["endpoint_failure"])
        self.assertEqual(report["endpoint_failure"]["endpoint"], "ygoprodeck cardinfo")
        self.assertEqual(report["import"]["added"], 0)
        # Byte-identical dataset preservation on catalogue failure.
        with open(self.dataset_path, "rb") as f:
            self.assertEqual(f.read(), original_bytes)


# --- Backfill CLI -----------------------------------------------------------


def _legacy_obs():
    """A non-YGOPRODeck (Konami-sourced) observation whose names are
    already canonical. The backfill must leave it byte-identical."""
    return {
        "event_id": "konami-2026-08-ycs-montreal-p1",
        "event_name": "YCS Montreal",
        "event_date": "2026-08-16",
        "region": "NA",
        "format": "TCG_ADVANCED",
        "banlist_id": "2026-08",
        "player": "Konami Player",
        "placement": "1st",
        "archetype": "Snake-Eye",
        "source_url": "https://yugiohblog.konami.com/foo",
        "source_type": "tournament",
        "published_at": "2026-08-17T00:00:00Z",
        "first_seen_at": "2026-08-17T00:00:00Z",
        "main_deck": [{"name": "Ash Blossom & Joyous Spring", "count": 3}],
        "side_deck": [],
        "extra_deck": [],
    }


def _ygoprodeck_obs(deck_id, main_ids, side_ids=None, extra_ids=None):
    return {
        "event_id": f"ygoprodeck-e-{deck_id}",
        "event_name": "e",
        "event_date": "2026-09-13",
        "region": "UNKNOWN",
        "format": "TCG_ADVANCED",
        "banlist_id": "UNKNOWN-2026-09",
        "player": "P",
        "placement": "1st",
        "archetype": "A",
        "source_url": f"https://ygoprodeck.com/deck/x-{deck_id}",
        "source_type": "tournament",
        "published_at": None,
        "first_seen_at": "2026-09-14T00:00:00Z",
        "main_deck": [{"name": p, "count": c} for p, c in main_ids],
        "side_deck": [{"name": p, "count": c} for p, c in (side_ids or [])],
        "extra_deck": [{"name": p, "count": c} for p, c in (extra_ids or [])],
        "source_provider": "ygoprodeck",
        "source_deck_id": str(deck_id),
    }


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.report_path = str(Path(self.tmp.name) / "report.json")
        self.cache_dir = str(Path(self.tmp.name) / "cache")
        self.passcode_map = build_passcode_map(CARDINFO_PAYLOAD["data"])

    def _write(self, observations):
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": observations}, f, indent=2, sort_keys=True)

    def _read(self):
        with open(self.dataset_path) as f:
            return json.load(f)

    def test_resolves_passcodes_and_preserves_legacy_observations(self):
        legacy = _legacy_obs()
        yg = _ygoprodeck_obs(
            42,
            main_ids=[("14558128", 3), ("23434538", 2)],
            extra_ids=[("89631139", 1)],
        )
        self._write([legacy, yg])
        report = backfill(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            cache_dir=self.cache_dir,
            passcode_map=self.passcode_map,
        )
        self.assertEqual(report["observations_resolved"], 1)
        self.assertEqual(report["observations_ygoprodeck"], 1)
        self.assertEqual(report["observations_unresolved"], [])
        dataset = self._read()
        # Legacy observation is byte-identical (dict-equal).
        self.assertEqual(dataset["observations"][0], legacy)
        rewritten = dataset["observations"][1]
        self.assertEqual(
            rewritten["main_deck"],
            [
                {"name": "Ash Blossom & Joyous Spring", "count": 3},
                {"name": "Maxx \"C\"", "count": 2},
            ],
        )
        self.assertEqual(
            rewritten["extra_deck"], [{"name": "Blue-Eyes White Dragon", "count": 1}]
        )

    def test_second_run_is_a_no_op_idempotent(self):
        yg = _ygoprodeck_obs(42, main_ids=[("14558128", 3)])
        self._write([yg])
        backfill(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            cache_dir=self.cache_dir,
            passcode_map=self.passcode_map,
        )
        with open(self.dataset_path, "rb") as f:
            after_first = f.read()
        report2 = backfill(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            cache_dir=self.cache_dir,
            passcode_map=self.passcode_map,
        )
        with open(self.dataset_path, "rb") as f:
            after_second = f.read()
        self.assertEqual(after_first, after_second)
        # No numeric passcodes remain to scan.
        self.assertEqual(report2["observations_scanned"], 0)
        self.assertEqual(report2["observations_already_canonical_skipped"], 1)

    def test_dry_run_reports_but_writes_nothing(self):
        yg = _ygoprodeck_obs(42, main_ids=[("14558128", 3)])
        self._write([yg])
        with open(self.dataset_path, "rb") as f:
            before = f.read()
        report = backfill(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            cache_dir=self.cache_dir,
            passcode_map=self.passcode_map,
            dry_run=True,
        )
        with open(self.dataset_path, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertEqual(report["observations_resolved"], 1)

    def test_unresolved_passcode_leaves_observation_untouched(self):
        yg = _ygoprodeck_obs(42, main_ids=[("14558128", 3), ("99999999", 1)])
        self._write([yg])
        with open(self.dataset_path, "rb") as f:
            before = f.read()
        report = backfill(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            cache_dir=self.cache_dir,
            passcode_map=self.passcode_map,
        )
        with open(self.dataset_path, "rb") as f:
            after = f.read()
        # Nothing resolved -> nothing written; dataset byte-identical.
        self.assertEqual(before, after)
        self.assertEqual(report["observations_resolved"], 0)
        self.assertEqual(len(report["observations_unresolved"]), 1)
        self.assertEqual(
            report["observations_unresolved"][0]["unresolved"]["main_deck"],
            [{"passcode": "99999999", "count": 1}],
        )

    def test_catalogue_failure_leaves_dataset_byte_identical(self):
        yg = _ygoprodeck_obs(42, main_ids=[("14558128", 3)])
        self._write([yg])
        with open(self.dataset_path, "rb") as f:
            before = f.read()
        session = _CardinfoSession(error=requests.ConnectionError("down"))
        report = backfill(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            cache_dir=self.cache_dir,  # no cache present -> must fail
            session=session,
        )
        with open(self.dataset_path, "rb") as f:
            after = f.read()
        self.assertEqual(before, after)
        self.assertIsNotNone(report["endpoint_failure"])
        self.assertEqual(report["endpoint_failure"]["endpoint"], "ygoprodeck cardinfo")


# --- prices.db coverage validation -----------------------------------------


class PricesDbCoverageTests(unittest.TestCase):
    def _make_db(self, path, rows):
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE prices (product_id INTEGER, card_name TEXT, set_name TEXT, "
                "price REAL, date TEXT)"
            )
            conn.executemany(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?)", rows
            )
            conn.commit()
        finally:
            conn.close()

    def test_reports_missing_db_honestly(self):
        result = check_prices_db_coverage("/nonexistent/prices.db", ["Ash Blossom & Joyous Spring"])
        self.assertFalse(result["present"])
        self.assertIn("not found", result["reason"])

    def test_matches_case_insensitively_and_counts_printings(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "prices.db")
            # 16 tracked printings for Ash Blossom, mirroring the problem
            # statement's live-behavior confirmation.
            ash_rows = [
                (100 + i, "Ash Blossom & Joyous Spring", f"Set-{i}", 1.0, "2026-09-15")
                for i in range(16)
            ]
            # Two printings for Maxx "C".
            maxx_rows = [
                (200, "Maxx \"C\"", "Set-A", 1.0, "2026-09-15"),
                (201, "Maxx \"C\"", "Set-B", 1.0, "2026-09-15"),
            ]
            self._make_db(db_path, ash_rows + maxx_rows)
            result = check_prices_db_coverage(
                db_path,
                [
                    "ASH BLOSSOM & JOYOUS SPRING",  # exercise case-insensitivity
                    "Maxx \"C\"",
                    "Nonexistent Card",
                ],
            )
        self.assertTrue(result["present"])
        matched = {m["card_name"]: m["product_id_printings"] for m in result["matched"]}
        self.assertEqual(matched["ASH BLOSSOM & JOYOUS SPRING"], 16)
        self.assertEqual(matched["Maxx \"C\""], 2)
        self.assertEqual(result["unmatched"], ["Nonexistent Card"])
        # We must NEVER select a specific printing: the coverage payload
        # only exposes counts, not a chosen product_id.
        for entry in result["matched"]:
            self.assertNotIn("product_id", entry)
            self.assertNotIn("selected_product_id", entry)

    def test_backfill_with_prices_db_reports_coverage_but_never_picks_a_printing(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "prices.db")
            self._make_db(
                db_path,
                [
                    (1, "Ash Blossom & Joyous Spring", "Set-A", 1.0, "2026-09-15"),
                    (2, "Ash Blossom & Joyous Spring", "Set-B", 1.0, "2026-09-15"),
                ],
            )
            dataset_path = str(Path(d) / "meta_watch_lists.json")
            report_path = str(Path(d) / "report.json")
            with open(dataset_path, "w") as f:
                json.dump(
                    {"schema_version": 1, "observations": [
                        _ygoprodeck_obs(42, main_ids=[("14558128", 3)])
                    ]}, f)
            report = backfill(
                dataset_path=dataset_path,
                report_path=report_path,
                cache_dir=str(Path(d) / "cache"),
                passcode_map=build_passcode_map(CARDINFO_PAYLOAD["data"]),
                prices_db_path=db_path,
            )
        coverage = report["prices_db_coverage"]
        self.assertTrue(coverage["present"])
        matched = {m["card_name"]: m["product_id_printings"] for m in coverage["matched"]}
        # One canonical name mapped to multiple product_id printings; the
        # bridge reports the count, it does not pick one.
        self.assertEqual(matched["Ash Blossom & Joyous Spring"], 2)
        self.assertEqual(coverage["unmatched"], [])


if __name__ == "__main__":
    unittest.main()
