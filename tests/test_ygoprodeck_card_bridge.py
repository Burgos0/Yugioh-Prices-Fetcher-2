"""
Offline tests for the YGOPRODeck card-identity bridge and the
idempotent backfill CLI.

Every test stubs out the network layer (either via ``requests``-mocked
sessions or the ``fetch=`` / ``resolve_card_ids_fn=`` dependency-injection
hooks) so no real HTTP call is ever made. The tests specifically enforce
the invariants called out in the task:

- ``resolve_card_ids`` maps numeric card_ids to canonical card names.
- The on-disk cache is reused across runs; a hit-only workload never
  calls the endpoint again.
- Unresolved card_ids are reported honestly and cause observations to
  be rejected -- never silently backfilled.
- An endpoint failure leaves the cache file untouched.
- The backfill CLI is idempotent (a second run makes zero changes).
- One canonical card name may bridge to multiple prices.db printings
  downstream; the bridge itself never over-specifies.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import requests

from scripts import backfill_ygoprodeck_card_names as backfill_cli
from scripts.collect_ygoprodeck_lists import collect_and_import
from scripts.ygoprodeck_card_bridge import (
    CACHE_SCHEMA_VERSION,
    CARDINFO_API_URL,
    CardBridgeError,
    _extract_card_id_map,
    fetch_cardinfo_map,
    load_cache,
    rebuild_cards_with_canonical_names,
    resolve_card_ids,
    save_cache,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload=None, status_error=None):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error
        return None

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _CardInfoSession:
    """Records calls to db.ygoprodeck.com/api/v7/cardinfo.php."""

    def __init__(self, payload=None, error=None, status_error=None):
        self.payload = payload
        self.error = error
        self.status_error = status_error
        self.calls = []

    def get(self, url, params=None, timeout=None, verify=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "timeout": timeout, "verify": verify}
        )
        if self.error is not None:
            raise self.error
        return _Response(payload=self.payload, status_error=self.status_error)


def _cardinfo_payload(entries):
    """Build a cardinfo.php-shaped payload from ``[(card_id, name), ...]``."""
    return {"data": [{"id": pid, "name": name} for pid, name in entries]}


# ---------------------------------------------------------------------------
# fetch_cardinfo_map / resolve_card_ids
# ---------------------------------------------------------------------------


class FetchCardInfoTests(unittest.TestCase):
    def test_uses_https_endpoint_with_tls_verification(self):
        session = _CardInfoSession(
            payload=_cardinfo_payload([(89631139, "Blue-Eyes White Dragon")])
        )
        mapping = fetch_cardinfo_map(session=session)
        self.assertEqual(mapping, {"89631139": "Blue-Eyes White Dragon"})
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], CARDINFO_API_URL)
        self.assertTrue(call["url"].startswith("https://"))
        self.assertTrue(call["verify"])

    def test_extracts_id_to_name_map_and_skips_malformed_entries(self):
        session = _CardInfoSession(
            payload={
                "data": [
                    {"id": 1, "name": "Ash Blossom & Joyous Spring"},
                    {"id": 2, "name": ""},  # empty name -> skip
                    {"id": None, "name": "Nameless"},  # missing id -> skip
                    "not-a-dict",  # skip
                    {"id": 3, "name": "Maxx \"C\""},
                ]
            }
        )
        mapping = fetch_cardinfo_map(session=session)
        self.assertEqual(
            mapping,
            {"1": "Ash Blossom & Joyous Spring", "3": "Maxx \"C\""},
        )

    def test_endpoint_error_raises_card_bridge_error(self):
        session = _CardInfoSession(error=requests.ConnectionError("network down"))
        with self.assertRaises(CardBridgeError) as ctx:
            fetch_cardinfo_map(session=session)
        self.assertIn("network down", str(ctx.exception))

    def test_http_error_raises_card_bridge_error(self):
        session = _CardInfoSession(status_error=requests.HTTPError("500 Server Error"))
        with self.assertRaises(CardBridgeError):
            fetch_cardinfo_map(session=session)

    def test_malformed_json_raises_card_bridge_error(self):
        session = _CardInfoSession(payload=ValueError("bad json"))
        with self.assertRaises(CardBridgeError):
            fetch_cardinfo_map(session=session)

    def test_empty_payload_raises_card_bridge_error(self):
        session = _CardInfoSession(payload={"data": []})
        with self.assertRaises(CardBridgeError):
            fetch_cardinfo_map(session=session)


class AltArtCardIdExtractionTests(unittest.TestCase):
    """
    Regression tests for the live dry-run bug where card id ``14558128``
    (an alt-art id for "Ash Blossom & Joyous Spring", canonical id
    ``14558127``) was reported as unresolved. YGOPRODeck's real
    ``cardinfo.php`` response lists alt-art ids only inside
    ``card_images[i].id``, never as a top-level ``id`` and never as a
    separate ``data`` entry -- the parser must fold both into the map.
    """

    def _ash_blossom_entry(self):
        # Exact shape of a real cardinfo.php entry for Ash Blossom &
        # Joyous Spring, trimmed to the fields the bridge inspects.
        return {
            "id": 14558127,
            "name": "Ash Blossom & Joyous Spring",
            "type": "Effect Monster",
            "card_images": [
                {"id": 14558127, "image_url": "https://.../14558127.jpg"},
                {"id": 14558128, "image_url": "https://.../14558128.jpg"},
                {"id": 14558129, "image_url": "https://.../14558129.jpg"},
            ],
        }

    def test_alt_art_ids_from_card_images_are_included(self):
        mapping = _extract_card_id_map({"data": [self._ash_blossom_entry()]})
        # All three ids resolve to the same canonical card name.
        self.assertEqual(mapping.get("14558127"), "Ash Blossom & Joyous Spring")
        self.assertEqual(mapping.get("14558128"), "Ash Blossom & Joyous Spring")
        self.assertEqual(mapping.get("14558129"), "Ash Blossom & Joyous Spring")

    def test_fetch_map_via_full_session_includes_alt_art_ids(self):
        session = _CardInfoSession(payload={"data": [self._ash_blossom_entry()]})
        mapping = fetch_cardinfo_map(session=session)
        self.assertEqual(mapping.get("14558128"), "Ash Blossom & Joyous Spring")

    def test_resolve_card_ids_resolves_alt_art_id_end_to_end(self):
        # Simulate the exact live-dry-run failure: the deck array
        # references 14558128 (alt art). With the fix, this must resolve
        # end-to-end via the bridge without landing in the unresolved set.
        session = _CardInfoSession(payload={"data": [self._ash_blossom_entry()]})
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = str(Path(tmp) / "cache.json")
            resolved, unresolved, fetched = resolve_card_ids(
                ["14558128"],
                cache_path=cache_path,
                fetch=lambda: fetch_cardinfo_map(session=session),
            )
        self.assertEqual(unresolved, [])
        self.assertEqual(resolved, {"14558128": "Ash Blossom & Joyous Spring"})
        self.assertTrue(fetched)

    def test_entries_without_top_level_id_still_map_alt_art_ids(self):
        # Some records may omit the top-level id (extremely rare) but
        # still ship a valid card_images list. We must still learn the
        # alt-art ids for those cards from card_images.
        entry = {
            "name": "Tearlaments Kitkallos",
            "card_images": [
                {"id": 82633143},
                {"id": 82633144},
            ],
        }
        mapping = _extract_card_id_map({"data": [entry]})
        self.assertEqual(mapping.get("82633143"), "Tearlaments Kitkallos")
        self.assertEqual(mapping.get("82633144"), "Tearlaments Kitkallos")

    def test_malformed_card_images_entries_are_skipped_not_fatal(self):
        entry = {
            "id": 89631139,
            "name": "Blue-Eyes White Dragon",
            "card_images": [
                {"id": 89631139},
                "not-a-dict",
                {"id": None},
                {"id": "not-numeric"},
                {"id": 89631140},
            ],
        }
        mapping = _extract_card_id_map({"data": [entry]})
        self.assertEqual(mapping.get("89631139"), "Blue-Eyes White Dragon")
        self.assertEqual(mapping.get("89631140"), "Blue-Eyes White Dragon")

    def test_missing_card_images_is_tolerated(self):
        # Entries without a card_images list must still contribute their
        # top-level id.
        entry = {"id": 42, "name": "The Answer"}
        mapping = _extract_card_id_map({"data": [entry]})
        self.assertEqual(mapping, {"42": "The Answer"})


class CacheSchemaVersionTests(unittest.TestCase):
    """
    A cache file written by an older/buggy version of the parser (for
    example, one that did not extract alt-art ids from ``card_images``)
    could otherwise silently short-circuit :func:`resolve_card_ids` with
    an incomplete map. To prevent that, the cache file carries a
    ``schema_version`` that must match :data:`CACHE_SCHEMA_VERSION`;
    any mismatch causes the loader to treat the file as absent so the
    next resolve triggers a fresh fetch instead of returning stale data.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = str(Path(self.tmp.name) / "cache.json")

    def test_cache_missing_schema_version_is_ignored(self):
        with open(self.cache_path, "w") as f:
            json.dump(
                {
                    "generated_at": "2026-09-16T00:00:00Z",
                    "card_id_to_name": {"14558127": "Ash Blossom & Joyous Spring"},
                },
                f,
            )
        self.assertEqual(load_cache(self.cache_path), {})

    def test_cache_with_mismatched_schema_version_is_ignored(self):
        with open(self.cache_path, "w") as f:
            json.dump(
                {
                    "schema_version": CACHE_SCHEMA_VERSION - 1,
                    "generated_at": "2026-09-16T00:00:00Z",
                    "card_id_to_name": {"14558127": "Ash Blossom & Joyous Spring"},
                },
                f,
            )
        self.assertEqual(load_cache(self.cache_path), {})

    def test_cache_with_matching_schema_version_is_loaded(self):
        with open(self.cache_path, "w") as f:
            json.dump(
                {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "generated_at": "2026-09-16T00:00:00Z",
                    "card_id_to_name": {"14558127": "Ash Blossom & Joyous Spring"},
                },
                f,
            )
        self.assertEqual(
            load_cache(self.cache_path),
            {"14558127": "Ash Blossom & Joyous Spring"},
        )

    def test_save_cache_writes_current_schema_version(self):
        save_cache({"14558127": "Ash Blossom & Joyous Spring"}, self.cache_path)
        with open(self.cache_path) as f:
            payload = json.load(f)
        self.assertEqual(payload["schema_version"], CACHE_SCHEMA_VERSION)

    def test_stale_cache_triggers_fresh_fetch_not_silent_incomplete_map(self):
        # A stale cache (from an older schema) that "resolves" an id
        # must NOT satisfy resolve_card_ids on its own -- the loader
        # discards it and resolve_card_ids fetches fresh data.
        with open(self.cache_path, "w") as f:
            json.dump(
                {
                    "schema_version": CACHE_SCHEMA_VERSION - 1,
                    "card_id_to_name": {"14558128": "wrong-name-from-buggy-parser"},
                },
                f,
            )

        fetch_calls = []

        def _fetch():
            fetch_calls.append(True)
            return {"14558128": "Ash Blossom & Joyous Spring"}

        resolved, unresolved, fetched = resolve_card_ids(
            ["14558128"], cache_path=self.cache_path, fetch=_fetch
        )
        self.assertEqual(len(fetch_calls), 1)
        self.assertTrue(fetched)
        self.assertEqual(resolved, {"14558128": "Ash Blossom & Joyous Spring"})
        self.assertEqual(unresolved, [])


class ResolveCardIdsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = str(Path(self.tmp.name) / "cache.json")

    def test_resolves_via_fetch_when_cache_is_empty(self):
        fetch_calls = []

        def _fetch():
            fetch_calls.append(True)
            return {"11": "Ash Blossom & Joyous Spring", "22": "Maxx \"C\""}

        resolved, unresolved, fetched = resolve_card_ids(
            ["11", "22"], cache_path=self.cache_path, fetch=_fetch
        )
        self.assertEqual(
            resolved, {"11": "Ash Blossom & Joyous Spring", "22": "Maxx \"C\""}
        )
        self.assertEqual(unresolved, [])
        self.assertTrue(fetched)
        self.assertEqual(len(fetch_calls), 1)
        # Cache persisted for reuse.
        cache_on_disk = load_cache(self.cache_path)
        self.assertEqual(
            cache_on_disk, {"11": "Ash Blossom & Joyous Spring", "22": "Maxx \"C\""}
        )

    def test_cache_reuse_avoids_second_endpoint_call(self):
        save_cache({"11": "Ash Blossom & Joyous Spring"}, self.cache_path)

        def _fetch():
            self.fail("resolve_card_ids must not fetch when cache already resolves")

        resolved, unresolved, fetched = resolve_card_ids(
            ["11"], cache_path=self.cache_path, fetch=_fetch
        )
        self.assertEqual(resolved, {"11": "Ash Blossom & Joyous Spring"})
        self.assertEqual(unresolved, [])
        self.assertFalse(fetched)

    def test_partial_cache_hit_still_fetches_only_once(self):
        save_cache({"11": "Ash Blossom & Joyous Spring"}, self.cache_path)
        fetch_count = {"n": 0}

        def _fetch():
            fetch_count["n"] += 1
            return {"22": "Maxx \"C\"", "33": "Called by the Grave"}

        resolved, unresolved, fetched = resolve_card_ids(
            ["11", "22", "33"], cache_path=self.cache_path, fetch=_fetch
        )
        self.assertEqual(
            resolved,
            {
                "11": "Ash Blossom & Joyous Spring",
                "22": "Maxx \"C\"",
                "33": "Called by the Grave",
            },
        )
        self.assertEqual(unresolved, [])
        self.assertTrue(fetched)
        self.assertEqual(fetch_count["n"], 1)

    def test_unresolved_ids_are_reported_not_invented(self):
        def _fetch():
            return {"11": "Ash Blossom & Joyous Spring"}

        resolved, unresolved, fetched = resolve_card_ids(
            ["11", "99999999"], cache_path=self.cache_path, fetch=_fetch
        )
        self.assertEqual(resolved, {"11": "Ash Blossom & Joyous Spring"})
        self.assertEqual(unresolved, ["99999999"])
        self.assertTrue(fetched)

    def test_endpoint_failure_leaves_cache_untouched(self):
        save_cache({"11": "Ash Blossom & Joyous Spring"}, self.cache_path)
        pre_bytes = Path(self.cache_path).read_bytes()

        def _fetch():
            raise CardBridgeError("network down")

        with self.assertRaises(CardBridgeError):
            resolve_card_ids(
                ["11", "22"], cache_path=self.cache_path, fetch=_fetch
            )
        # Cache file must not have been rewritten.
        self.assertEqual(Path(self.cache_path).read_bytes(), pre_bytes)

    def test_no_fetch_when_no_card_ids_requested(self):
        def _fetch():
            self.fail("must not fetch when nothing is requested")

        resolved, unresolved, fetched = resolve_card_ids(
            [], cache_path=self.cache_path, fetch=_fetch
        )
        self.assertEqual(resolved, {})
        self.assertEqual(unresolved, [])
        self.assertFalse(fetched)

    def test_non_numeric_card_ids_are_skipped_never_fetched(self):
        def _fetch():
            self.fail("must not fetch for non-numeric names")

        resolved, unresolved, fetched = resolve_card_ids(
            ["Ash Blossom & Joyous Spring", "", None],
            cache_path=self.cache_path,
            fetch=_fetch,
        )
        self.assertEqual(resolved, {})
        self.assertEqual(unresolved, [])
        self.assertFalse(fetched)


# ---------------------------------------------------------------------------
# rebuild_cards_with_canonical_names
# ---------------------------------------------------------------------------


class RebuildCardsTests(unittest.TestCase):
    def test_replaces_numeric_ids_with_canonical_names_preserving_counts(self):
        cards = [
            {"name": "11", "count": 3},
            {"name": "22", "count": 2},
            {"name": "33", "count": 1},
        ]
        resolved = {
            "11": "Ash Blossom & Joyous Spring",
            "22": "Maxx \"C\"",
            "33": "Called by the Grave",
        }
        rebuilt, unresolved = rebuild_cards_with_canonical_names(cards, resolved)
        self.assertEqual(unresolved, [])
        self.assertEqual(
            rebuilt,
            [
                {"name": "Ash Blossom & Joyous Spring", "count": 3},
                {"name": "Maxx \"C\"", "count": 2},
                {"name": "Called by the Grave", "count": 1},
            ],
        )

    def test_leaves_canonical_names_unchanged(self):
        cards = [{"name": "Ash Blossom & Joyous Spring", "count": 3}]
        rebuilt, unresolved = rebuild_cards_with_canonical_names(cards, {})
        self.assertEqual(unresolved, [])
        self.assertEqual(
            rebuilt, [{"name": "Ash Blossom & Joyous Spring", "count": 3}]
        )

    def test_reports_unresolved_ids(self):
        cards = [{"name": "11", "count": 3}, {"name": "99", "count": 2}]
        rebuilt, unresolved = rebuild_cards_with_canonical_names(
            cards, {"11": "Ash Blossom & Joyous Spring"}
        )
        self.assertEqual(unresolved, ["99"])
        # The resolved entry is still returned so callers can report the
        # partial mapping if they want; the collector treats *any*
        # unresolved id as a whole-observation rejection.
        self.assertEqual(rebuilt, [{"name": "Ash Blossom & Joyous Spring", "count": 3}])

    def test_two_card_ids_mapping_to_same_name_sum_counts(self):
        cards = [{"name": "11", "count": 2}, {"name": "12", "count": 1}]
        rebuilt, unresolved = rebuild_cards_with_canonical_names(
            cards,
            {"11": "Ash Blossom & Joyous Spring", "12": "Ash Blossom & Joyous Spring"},
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(
            rebuilt, [{"name": "Ash Blossom & Joyous Spring", "count": 3}]
        )


# ---------------------------------------------------------------------------
# Downstream: one canonical name -> multiple prices.db printings
# ---------------------------------------------------------------------------


class OneNameManyPrintingsTests(unittest.TestCase):
    """
    The bridge only maps card_id -> canonical *card name*. Any given
    canonical name can (and often does) correspond to multiple product_ids
    in prices.db (alt-arts, reprints, secret rares, etc). This test wires
    the bridge output through ``app.meta_watch.resolve_card_printings`` on
    an in-memory prices DB to confirm the one-name-to-many-printings
    downstream behaviour is preserved.
    """

    def test_single_canonical_name_bridges_to_multiple_printings(self):
        from app.meta_watch import resolve_card_printings

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE prices ("
            "product_id INTEGER, card_name TEXT, set_name TEXT,"
            " PRIMARY KEY (product_id))"
        )
        conn.executemany(
            "INSERT INTO prices (product_id, card_name, set_name) VALUES (?, ?, ?)",
            [
                (1001, "Ash Blossom & Joyous Spring", "Maximum Crisis"),
                (1002, "Ash Blossom & Joyous Spring (Ghost Rare)", "Maximum Crisis"),
                (1003, "Ash Blossom & Joyous Spring (Secret Rare)", "OTS Tournament Pack"),
                (2001, "Maxx \"C\"", "Structure Deck: Cyberse Link"),
            ],
        )
        conn.commit()

        # A single bridge entry (card_id 14558127 -> "Ash Blossom & Joyous
        # Spring") is expected to fan out to three tracked printings.
        canonical = "Ash Blossom & Joyous Spring"
        printings = resolve_card_printings(conn, canonical)
        product_ids = sorted(p["product_id"] for p in printings)
        self.assertEqual(product_ids, [1001, 1002, 1003])
        # Separate canonical name must NOT be conflated.
        maxx = resolve_card_printings(conn, "Maxx \"C\"")
        self.assertEqual([p["product_id"] for p in maxx], [2001])


# ---------------------------------------------------------------------------
# Collector integration: unresolved ids reject observations honestly
# ---------------------------------------------------------------------------


NOW_STR = "2026-09-16T12:00:00Z"


def _row(deckNum, main="11,22,33", extra="44", side="", **overrides):
    base = {
        "deckNum": deckNum,
        "deck_name": "Snake-Eye Fire King",
        "format": "Tournament Meta Decks",
        "tournamentName": "YCS Atlanta 2026",
        "tournamentPlacement": "1st",
        "tournamentPlayerName": f"Player {deckNum}",
        "tournamentPlayerCount": 100,
        "submit_date": "2026-09-10",
        "main_deck": main,
        "extra_deck": extra,
        "side_deck": side,
        "deck_description": "",
        "deck_excerpt": "",
        "pretty_url": f"deck-{deckNum}",
    }
    base.update(overrides)
    return base


class _CollectorSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, timeout=None, verify=None):
        self.calls.append({"url": url, "params": dict(params or {}), "verify": verify})
        offset = int((params or {}).get("offset", 0))
        return _Response(payload=self.pages.get(offset, []))


class CollectorBridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.report_path = str(Path(self.tmp.name) / "report.json")
        self.cache_path = str(Path(self.tmp.name) / "cache.json")
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": []}, f)

    def _read_dataset(self):
        with open(self.dataset_path) as f:
            return json.load(f)

    def _bridge(self, mapping, calls=None):
        def _fn(card_ids, cache_path=None, session=None, timeout=None):
            if calls is not None:
                calls.append(list(card_ids))
            resolved = {p: mapping[p] for p in card_ids if p in mapping}
            unresolved = sorted(p for p in card_ids if p not in mapping)
            return resolved, unresolved, False

        return _fn

    def test_new_imports_store_canonical_names_and_preserve_counts(self):
        rows = [_row(deckNum=1, main="11,11,22,22,22", extra="33", side="44")]
        session = _CollectorSession(pages={0: rows})
        mapping = {
            "11": "Ash Blossom & Joyous Spring",
            "22": "Maxx \"C\"",
            "33": "Accesscode Talker",
            "44": "Called by the Grave",
        }
        from datetime import datetime, timezone

        now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=now,
            card_cache_path=self.cache_path,
            resolve_card_ids_fn=self._bridge(mapping),
        )
        self.assertIsNone(report["endpoint_failure"])
        self.assertIsNone(report["card_bridge_failure"])
        self.assertEqual(report["import"]["added"], 1)
        obs = self._read_dataset()["observations"][0]
        self.assertEqual(
            sorted(obs["main_deck"], key=lambda c: c["name"]),
            [
                {"name": "Ash Blossom & Joyous Spring", "count": 2},
                {"name": "Maxx \"C\"", "count": 3},
            ],
        )
        self.assertEqual(obs["extra_deck"], [{"name": "Accesscode Talker", "count": 1}])
        self.assertEqual(obs["side_deck"], [{"name": "Called by the Grave", "count": 1}])
        # Legacy invariant: no numeric-only names left in the stored obs.
        for field in ("main_deck", "side_deck", "extra_deck"):
            for entry in obs[field]:
                self.assertFalse(
                    entry["name"].isdigit(),
                    msg=f"numeric card_id leaked into stored observation: {entry}",
                )

    def test_unresolved_ids_reject_observation_and_leave_dataset_unchanged(self):
        rows = [
            _row(deckNum=1, main="11,22", extra="33", side=""),
            _row(deckNum=2, main="11,99999", extra="33", side=""),  # 99999 unresolvable
        ]
        session = _CollectorSession(pages={0: rows})
        mapping = {
            "11": "Ash Blossom & Joyous Spring",
            "22": "Maxx \"C\"",
            "33": "Accesscode Talker",
        }
        from datetime import datetime, timezone

        now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=now,
            card_cache_path=self.cache_path,
            resolve_card_ids_fn=self._bridge(mapping),
        )
        # Deck 1 imported; deck 2 rejected honestly with a clear reason.
        self.assertEqual(report["import"]["added"], 1)
        reasons = [r["reason"] for r in report["rejected_records"] if r["deckNum"] == "2"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("unresolved card ids", reasons[0])
        self.assertIn("99999", reasons[0])
        stored = self._read_dataset()["observations"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source_deck_id"], "1")

    def test_card_bridge_failure_leaves_dataset_and_cache_untouched(self):
        rows = [_row(deckNum=1)]
        session = _CollectorSession(pages={0: rows})

        def _failing_bridge(card_ids, cache_path=None, session=None, timeout=None):
            raise CardBridgeError("cardinfo unreachable")

        from datetime import datetime, timezone

        now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            session=session,
            pacing_seconds=0,
            sleep=lambda s: None,
            now=now,
            card_cache_path=self.cache_path,
            resolve_card_ids_fn=_failing_bridge,
        )
        self.assertIsNotNone(report["card_bridge_failure"])
        self.assertIn("cardinfo unreachable", report["card_bridge_failure"]["error"])
        self.assertEqual(report["import"]["added"], 0)
        # Dataset stays empty; the cache file must not have been created.
        self.assertEqual(self._read_dataset()["observations"], [])
        self.assertFalse(Path(self.cache_path).exists())


# ---------------------------------------------------------------------------
# Backfill CLI: idempotence, YGOPRODeck-only scope, honest rejection
# ---------------------------------------------------------------------------


class BackfillCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.cache_path = str(Path(self.tmp.name) / "cache.json")

    def _write_dataset(self, observations):
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": observations}, f)

    def _read_dataset(self):
        with open(self.dataset_path) as f:
            return json.load(f)

    def _legacy_konami_obs(self):
        # A non-YGOPRODeck (legacy) observation. Its deck entries are
        # already canonical (as Konami article names are). This must be
        # returned byte-for-byte on every backfill run.
        return {
            "event_id": "2026-ycs-atlanta",
            "event_name": "YCS Atlanta 2026",
            "event_date": "2026-08-30",
            "region": "NA",
            "format": "TCG_ADVANCED",
            "banlist_id": "2026-04",
            "player": "Jane Doe",
            "placement": "1st",
            "archetype": "Kashtira",
            "source_url": "https://yugiohblog.konami.com/atlanta/",
            "source_type": "tournament",
            "published_at": "2026-08-31T12:00:00Z",
            "first_seen_at": "2026-09-01T00:00:00Z",
            "main_deck": [{"name": "Ash Blossom & Joyous Spring", "count": 3}],
            "side_deck": [],
            "extra_deck": [],
        }

    def _ygoprodeck_obs_numeric(self, deck_id="42"):
        return {
            "event_id": f"ygoprodeck-cup-{deck_id}",
            "event_name": "YGOPRODeck Cup",
            "event_date": "2026-09-10",
            "region": "UNKNOWN",
            "format": "TCG_ADVANCED",
            "banlist_id": "UNKNOWN-2026-09",
            "player": "Alice",
            "placement": "1st",
            "archetype": "Test",
            "source_url": f"https://ygoprodeck.com/deck/test-{deck_id}",
            "source_type": "tournament",
            "published_at": None,
            "first_seen_at": "2026-09-11T00:00:00Z",
            "main_deck": [
                {"name": "11", "count": 3},
                {"name": "22", "count": 2},
            ],
            "side_deck": [{"name": "33", "count": 1}],
            "extra_deck": [{"name": "44", "count": 1}],
            "source_provider": "ygoprodeck",
            "source_deck_id": deck_id,
        }

    def _bridge(self, mapping):
        def _fn(card_ids, cache_path=None, session=None, timeout=None):
            resolved = {p: mapping[p] for p in card_ids if p in mapping}
            unresolved = sorted(p for p in card_ids if p not in mapping)
            return resolved, unresolved, False

        return _fn

    def test_dry_run_by_default_leaves_dataset_untouched(self):
        legacy = self._legacy_konami_obs()
        ygo = self._ygoprodeck_obs_numeric("42")
        self._write_dataset([legacy, ygo])
        pre_bytes = Path(self.dataset_path).read_bytes()

        report = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=False,
            resolve_card_ids_fn=self._bridge(
                {
                    "11": "Ash Blossom & Joyous Spring",
                    "22": "Maxx \"C\"",
                    "33": "Called by the Grave",
                    "44": "Accesscode Talker",
                }
            ),
        )
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["observations_rewritten"], 1)
        self.assertEqual(Path(self.dataset_path).read_bytes(), pre_bytes)

    def test_apply_rewrites_only_ygoprodeck_observations(self):
        legacy = self._legacy_konami_obs()
        legacy_snapshot = json.dumps(legacy, sort_keys=True)
        ygo = self._ygoprodeck_obs_numeric("42")
        self._write_dataset([legacy, ygo])

        report = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=True,
            resolve_card_ids_fn=self._bridge(
                {
                    "11": "Ash Blossom & Joyous Spring",
                    "22": "Maxx \"C\"",
                    "33": "Called by the Grave",
                    "44": "Accesscode Talker",
                }
            ),
        )
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["observations_rewritten"], 1)
        self.assertEqual(report["ygoprodeck_observations"], 1)

        stored = self._read_dataset()["observations"]
        # Legacy observation is bit-for-bit identical.
        self.assertEqual(json.dumps(stored[0], sort_keys=True), legacy_snapshot)
        # YGOPRODeck row's names are canonical, counts preserved.
        self.assertEqual(
            sorted(stored[1]["main_deck"], key=lambda c: c["name"]),
            [
                {"name": "Ash Blossom & Joyous Spring", "count": 3},
                {"name": "Maxx \"C\"", "count": 2},
            ],
        )
        self.assertEqual(stored[1]["side_deck"], [{"name": "Called by the Grave", "count": 1}])
        self.assertEqual(stored[1]["extra_deck"], [{"name": "Accesscode Talker", "count": 1}])

    def test_backfill_is_idempotent(self):
        ygo = self._ygoprodeck_obs_numeric("42")
        self._write_dataset([ygo])
        mapping = {
            "11": "Ash Blossom & Joyous Spring",
            "22": "Maxx \"C\"",
            "33": "Called by the Grave",
            "44": "Accesscode Talker",
        }
        # First apply: does work.
        first = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=True,
            resolve_card_ids_fn=self._bridge(mapping),
        )
        self.assertEqual(first["observations_rewritten"], 1)
        after_first = Path(self.dataset_path).read_bytes()

        # Second apply with the SAME mapping: no changes and a bridge
        # that fails if it's asked to resolve anything (proving no
        # numeric card_ids remain).
        def _explode(card_ids, cache_path=None, session=None, timeout=None):
            if list(card_ids):
                raise AssertionError(
                    f"idempotence violated: bridge invoked for {list(card_ids)}"
                )
            return {}, [], False

        second = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=True,
            resolve_card_ids_fn=_explode,
        )
        self.assertEqual(second["observations_rewritten"], 0)
        self.assertEqual(second["observations_needing_backfill"], 0)
        self.assertEqual(Path(self.dataset_path).read_bytes(), after_first)

    def test_unresolved_ids_leave_that_observation_untouched(self):
        ygo_ok = self._ygoprodeck_obs_numeric("10")
        ygo_bad = self._ygoprodeck_obs_numeric("11")
        # Break one entry so card_id "99" is unresolvable.
        ygo_bad["main_deck"] = [
            {"name": "11", "count": 2},
            {"name": "99", "count": 1},
        ]
        bad_snapshot = json.dumps(ygo_bad, sort_keys=True)
        self._write_dataset([ygo_ok, ygo_bad])

        report = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=True,
            resolve_card_ids_fn=self._bridge(
                {
                    "11": "Ash Blossom & Joyous Spring",
                    "22": "Maxx \"C\"",
                    "33": "Called by the Grave",
                    "44": "Accesscode Talker",
                }
            ),
        )
        self.assertEqual(report["observations_rewritten"], 1)
        self.assertEqual(len(report["observations_with_unresolved_ids"]), 1)
        self.assertEqual(
            report["observations_with_unresolved_ids"][0]["unresolved_ids"], ["99"]
        )
        stored = self._read_dataset()["observations"]
        # The good row was rewritten.
        self.assertEqual(
            sorted(n["name"] for n in stored[0]["main_deck"]),
            ["Ash Blossom & Joyous Spring", "Maxx \"C\""],
        )
        # The bad row is unchanged.
        self.assertEqual(json.dumps(stored[1], sort_keys=True), bad_snapshot)

    def test_bridge_failure_leaves_dataset_and_cache_untouched(self):
        ygo = self._ygoprodeck_obs_numeric("42")
        self._write_dataset([ygo])
        pre_bytes = Path(self.dataset_path).read_bytes()

        def _failing(card_ids, cache_path=None, session=None, timeout=None):
            raise CardBridgeError("cardinfo down")

        report = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=True,
            resolve_card_ids_fn=_failing,
        )
        self.assertIsNotNone(report["card_bridge_failure"])
        self.assertEqual(report["observations_rewritten"], 0)
        self.assertEqual(Path(self.dataset_path).read_bytes(), pre_bytes)
        self.assertFalse(Path(self.cache_path).exists())

    def test_backfill_skips_non_ygoprodeck_observations_even_with_numeric_names(self):
        # A hypothetical non-YGOPRODeck row that happens to have a
        # numeric name must NOT be rewritten -- backfill scope is
        # source_provider == "ygoprodeck" only.
        legacy = self._legacy_konami_obs()
        legacy["main_deck"] = [{"name": "11", "count": 3}]  # deliberate
        legacy_snapshot = json.dumps(legacy, sort_keys=True)
        self._write_dataset([legacy])

        report = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=True,
            resolve_card_ids_fn=self._bridge({"11": "Ash Blossom & Joyous Spring"}),
        )
        self.assertEqual(report["ygoprodeck_observations"], 0)
        self.assertEqual(report["observations_needing_backfill"], 0)
        self.assertEqual(report["observations_rewritten"], 0)
        stored = self._read_dataset()["observations"]
        self.assertEqual(json.dumps(stored[0], sort_keys=True), legacy_snapshot)

    def test_no_card_ids_means_no_endpoint_call_at_all(self):
        # A dataset containing only already-canonical YGOPRODeck rows
        # must complete without ever invoking the bridge (proving no
        # network call on the fast path).
        ygo = self._ygoprodeck_obs_numeric("42")
        ygo["main_deck"] = [{"name": "Ash Blossom & Joyous Spring", "count": 3}]
        ygo["side_deck"] = []
        ygo["extra_deck"] = []
        self._write_dataset([ygo])

        def _explode(card_ids, cache_path=None, session=None, timeout=None):
            raise AssertionError("bridge must not be called when nothing needs backfill")

        report = backfill_cli.backfill(
            dataset_path=self.dataset_path,
            card_cache_path=self.cache_path,
            apply=True,
            resolve_card_ids_fn=_explode,
        )
        self.assertEqual(report["observations_needing_backfill"], 0)
        self.assertEqual(report["observations_rewritten"], 0)


if __name__ == "__main__":
    unittest.main()
