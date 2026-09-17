"""
Focused offline tests for the TCG_ADVANCED tournament-adoption feature.

These tests exercise the identity-level aggregation and the read-only
printing fan-out in isolation from Flask, network, or a real
``prices.db`` — a fresh clone / offline CI must still be able to run
them and get deterministic results.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.meta_watch import FORMAT_MASTER_DUEL, FORMAT_TCG_ADVANCED
from app.tournament_adoption import (
    SOURCE_PROVIDER_YGOPRODECK,
    aggregate_adoption,
    apply_as_of_cutoff,
    build_adoption_features,
    build_report,
    filter_source,
    join_printings,
    placement_weight,
)


def make_obs(event_id, player, main=None, side=None, extra=None, *,
             placement="Winner", event_date="2026-08-30",
             source_provider=SOURCE_PROVIDER_YGOPRODECK,
             fmt=FORMAT_TCG_ADVANCED, source_type="tournament",
             archetype="TestDeck", archived_at="2026-08-30T12:00:00Z"):
    """Build a decklist observation with sensible defaults for this suite."""
    def entries(items):
        if not items:
            return []
        return [{"name": n, "count": c} for n, c in items]
    return {
        "event_id": event_id, "event_name": "Test Event", "event_date": event_date,
        "region": "NA", "format": fmt, "banlist_id": "2026-04", "player": player,
        "placement": placement, "archetype": archetype,
        "source_url": "https://example.com/coverage", "source_type": source_type,
        "source_provider": source_provider,
        "published_at": None, "first_seen_at": archived_at, "archived_at": archived_at,
        "main_deck": entries(main), "side_deck": entries(side), "extra_deck": entries(extra),
    }


class PlacementWeightTests(unittest.TestCase):
    def test_known_placements_are_weighted(self):
        self.assertEqual(placement_weight("Winner"), 4.0)
        self.assertEqual(placement_weight("1"), 4.0)
        self.assertEqual(placement_weight("Runner-Up"), 3.0)
        self.assertEqual(placement_weight("2"), 3.0)
        self.assertEqual(placement_weight("Top 4"), 2.0)
        self.assertEqual(placement_weight("Top 8"), 1.0)

    def test_unknown_placement_is_zero_not_error(self):
        # A deck with an unknown placement still counts toward
        # unique_decks; it just doesn't inflate weighted adoption.
        self.assertEqual(placement_weight("mystery"), 0.0)
        self.assertEqual(placement_weight(None), 0.0)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(placement_weight("  wInNeR  "), 4.0)


class FilterSourceTests(unittest.TestCase):
    def test_keeps_only_ygoprodeck_tcg_advanced_tournament(self):
        obs_keep = make_obs("E1", "A", [("Card X", 3)])
        obs_wrong_provider = make_obs("E2", "B", [("Card X", 3)], source_provider="konami_blog")
        obs_wrong_provider_missing = make_obs("E3", "C", [("Card X", 3)], source_provider=None)
        obs_wrong_format = make_obs("E4", "D", [("Card X", 3)], fmt=FORMAT_MASTER_DUEL)
        obs_casual = make_obs("E5", "E", [("Card X", 3)], source_type="casual")
        kept = filter_source([obs_keep, obs_wrong_provider, obs_wrong_provider_missing,
                              obs_wrong_format, obs_casual])
        self.assertEqual([o["event_id"] for o in kept], ["E1"])


class CutoffTests(unittest.TestCase):
    def test_as_of_excludes_future_observations(self):
        past = make_obs("E1", "A", [("Card X", 3)], event_date="2026-06-01")
        on_cutoff = make_obs("E2", "B", [("Card X", 3)], event_date="2026-08-30")
        future = make_obs("E3", "C", [("Card X", 3)], event_date="2026-09-15")
        kept = apply_as_of_cutoff([past, on_cutoff, future], as_of="2026-08-30")
        self.assertEqual({o["event_id"] for o in kept}, {"E1", "E2"})

    def test_none_cutoff_keeps_everything(self):
        obs = [make_obs("E1", "A", [("Card X", 3)], event_date="2050-01-01")]
        self.assertEqual(len(apply_as_of_cutoff(obs, as_of=None)), 1)

    def test_invalid_cutoff_raises(self):
        with self.assertRaises(ValueError):
            apply_as_of_cutoff([], as_of="not-a-date")


class DuplicateCardHandlingTests(unittest.TestCase):
    def test_same_card_in_multiple_zones_counts_one_deck_and_combined_copies(self):
        # Ash Blossom in main (3) + side (2) of the same deck: one deck
        # for penetration, but all 5 copies for total_copies.
        obs = make_obs("E1", "A",
                       main=[("Ash Blossom & Joyous Spring", 3)],
                       side=[("Ash Blossom & Joyous Spring", 2)])
        agg = aggregate_adoption([obs])
        (card,) = agg["cards"]
        self.assertEqual(card["card_name"], "Ash Blossom & Joyous Spring")
        self.assertEqual(card["unique_decks"], 1)
        self.assertEqual(card["total_copies"], 5)

    def test_same_card_listed_twice_in_one_zone_is_combined(self):
        # Malformed but harmless: get_zone_entries combines these into 3 copies.
        obs = make_obs("E1", "A",
                       main=[("Card X", 2), ("Card X", 1)])
        agg = aggregate_adoption([obs])
        (card,) = agg["cards"]
        self.assertEqual(card["unique_decks"], 1)
        self.assertEqual(card["total_copies"], 3)


class PlacementWeightingTests(unittest.TestCase):
    def test_weighted_adoption_reflects_finish_quality(self):
        # Deck A (Winner, weight 4) plays Card X.
        # Deck B (Top 8, weight 1) does not.
        # Unweighted penetration = 1/2 = 0.5.
        # Weighted adoption = 4 / (4 + 1) = 0.8.
        winner = make_obs("E1", "A", [("Card X", 1)], placement="Winner")
        top8 = make_obs("E1", "B", [("Card Y", 1)], placement="Top 8")
        agg = aggregate_adoption([winner, top8])
        by_name = {c["card_name"]: c for c in agg["cards"]}
        self.assertAlmostEqual(by_name["Card X"]["deck_penetration_rate"], 0.5)
        self.assertAlmostEqual(by_name["Card X"]["placement_weighted_adoption"], 0.8)
        self.assertAlmostEqual(by_name["Card Y"]["deck_penetration_rate"], 0.5)
        self.assertAlmostEqual(by_name["Card Y"]["placement_weighted_adoption"], 0.2)

    def test_unknown_placements_do_not_break_weighting(self):
        # All decks have unknown placements -> total_placement_weight is 0,
        # so weighted adoption is defined as 0.0 rather than a ZeroDivisionError.
        a = make_obs("E1", "A", [("Card X", 1)], placement="mystery")
        b = make_obs("E1", "B", [("Card X", 1)], placement="mystery")
        agg = aggregate_adoption([a, b])
        (card,) = agg["cards"]
        self.assertEqual(card["placement_weighted_adoption"], 0.0)
        # But identity-level penetration still counts them.
        self.assertEqual(card["deck_penetration_rate"], 1.0)

    def test_latest_event_date_is_the_max(self):
        older = make_obs("E1", "A", [("Card X", 1)], event_date="2026-06-01")
        newer = make_obs("E2", "B", [("Card X", 1)], event_date="2026-08-15")
        agg = aggregate_adoption([older, newer])
        (card,) = agg["cards"]
        self.assertEqual(card["latest_event_date"], "2026-08-15")


class EmptyMissingTests(unittest.TestCase):
    def test_missing_dataset_file_yields_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "does_not_exist.json")
            report = build_adoption_features(dataset_path=path)
            self.assertEqual(report["total_decks"], 0)
            self.assertEqual(report["cards"], [])
            self.assertEqual(report["raw_observation_count"], 0)

    def test_empty_dataset_yields_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "empty.json")
            with open(path, "w") as f:
                json.dump({"observations": []}, f)
            report = build_adoption_features(dataset_path=path)
            self.assertEqual(report["total_decks"], 0)
            self.assertEqual(report["cards"], [])

    def test_dataset_with_no_matching_observations_yields_empty_cards(self):
        # All observations are Master Duel, so the TCG_ADVANCED filter drops them.
        obs = [make_obs("E1", "A", [("Card X", 1)], fmt=FORMAT_MASTER_DUEL)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mismatch.json")
            with open(path, "w") as f:
                json.dump({"observations": obs}, f)
            report = build_adoption_features(dataset_path=path)
            self.assertEqual(report["raw_observation_count"], 1)
            self.assertEqual(report["filtered_observation_count"], 0)
            self.assertEqual(report["total_decks"], 0)


class DeduplicationTests(unittest.TestCase):
    def test_same_event_same_player_counted_once(self):
        a = make_obs("E1", "Alice", [("Card X", 3)])
        b = make_obs("E1", "Alice", [("Card X", 3)])  # duplicate
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.json")
            with open(path, "w") as f:
                json.dump({"observations": [a, b]}, f)
            report = build_adoption_features(dataset_path=path)
        self.assertEqual(report["duplicate_dropped_count"], 1)
        self.assertEqual(report["total_decks"], 1)


class PrintingJoinTests(unittest.TestCase):
    """Read-only join of adoption features onto a prices-schema fixture."""

    def _make_conn(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE prices (product_id INTEGER, card_name TEXT, set_name TEXT, "
            "date TEXT, market_price REAL)"
        )
        conn.executemany(
            "INSERT INTO prices (product_id, card_name, set_name, date, market_price) "
            "VALUES (?, ?, ?, ?, ?)", rows,
        )
        conn.commit()
        return conn

    def test_exact_name_join(self):
        conn = self._make_conn([
            (100, "Ash Blossom & Joyous Spring", "Set A", "2026-08-01", 5.0),
            (200, "Effect Veiler", "Set B", "2026-08-01", 1.0),
        ])
        cards = [{"card_name": "Ash Blossom & Joyous Spring", "unique_decks": 1,
                   "total_copies": 3, "deck_penetration_rate": 1.0,
                   "placement_weighted_adoption": 1.0, "latest_event_date": "2026-08-01"}]
        joined = join_printings(conn, cards)
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0]["printing_count"], 1)
        self.assertEqual(joined[0]["printings"][0]["product_id"], 100)
        # Feature values are preserved unchanged.
        self.assertEqual(joined[0]["unique_decks"], 1)

    def test_one_card_fans_out_to_many_printings(self):
        # Same canonical card, three different tracked printings (base
        # name and two "<name> (…" printings). Adoption should fan out
        # to all three; identity is one confirmation, not three.
        conn = self._make_conn([
            (100, "Ash Blossom & Joyous Spring", "Secret Rare", "2026-08-01", 20.0),
            (101, "Ash Blossom & Joyous Spring (Ultra Rare)", "Reprint Set", "2026-08-01", 8.0),
            (102, "Ash Blossom & Joyous Spring (Ghost Rare)", "Premium Set", "2026-08-01", 150.0),
            (200, "Unrelated Card", "Other Set", "2026-08-01", 1.0),
        ])
        cards = [{"card_name": "Ash Blossom & Joyous Spring", "unique_decks": 5,
                   "total_copies": 15, "deck_penetration_rate": 0.5,
                   "placement_weighted_adoption": 0.6, "latest_event_date": "2026-08-01"}]
        joined = join_printings(conn, cards)
        self.assertEqual(joined[0]["printing_count"], 3)
        product_ids = sorted(p["product_id"] for p in joined[0]["printings"])
        self.assertEqual(product_ids, [100, 101, 102])
        # Adoption feature itself is unchanged despite fan-out.
        self.assertEqual(joined[0]["unique_decks"], 5)
        self.assertEqual(joined[0]["deck_penetration_rate"], 0.5)

    def test_card_with_no_matching_printing_is_kept_with_empty_list(self):
        conn = self._make_conn([
            (200, "Some Other Card", "Set X", "2026-08-01", 1.0),
        ])
        cards = [{"card_name": "Brand New Unreleased Card", "unique_decks": 4,
                   "total_copies": 12, "deck_penetration_rate": 0.4,
                   "placement_weighted_adoption": 0.5, "latest_event_date": "2026-08-15"}]
        joined = join_printings(conn, cards)
        self.assertEqual(joined[0]["printing_count"], 0)
        self.assertEqual(joined[0]["printings"], [])
        self.assertEqual(joined[0]["card_name"], "Brand New Unreleased Card")

    def test_prefix_join_does_not_merge_distinct_cards(self):
        # "Ash Blossom" (a fake shorter name) should NOT pick up
        # "Ash Blossom & Joyous Spring" — the boundary rule requires
        # a literal " (" after the exact name.
        conn = self._make_conn([
            (100, "Ash Blossom & Joyous Spring", "Secret Rare", "2026-08-01", 20.0),
        ])
        cards = [{"card_name": "Ash Blossom", "unique_decks": 1,
                   "total_copies": 1, "deck_penetration_rate": 1.0,
                   "placement_weighted_adoption": 1.0, "latest_event_date": "2026-08-01"}]
        joined = join_printings(conn, cards)
        self.assertEqual(joined[0]["printing_count"], 0)


class ReportTests(unittest.TestCase):
    def test_report_without_prices_db_still_yields_identity_features(self):
        obs = [make_obs("E1", "A", [("Card X", 3)]),
                make_obs("E1", "B", [("Card X", 1), ("Card Y", 2)], placement="Top 8")]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.json")
            with open(path, "w") as f:
                json.dump({"observations": obs}, f)
            report = build_report(dataset_path=path, prices_db_path=None)
        self.assertFalse(report["printings_joined"])
        self.assertEqual(report["total_decks"], 2)
        self.assertEqual(report["returned_card_count"], 2)
        for entry in report["cards"]:
            self.assertEqual(entry["printings"], [])
            self.assertEqual(entry["printing_count"], 0)

    def test_report_top_limits_returned_cards_but_reports_total(self):
        obs = [
            make_obs("E1", "A", [("A", 1), ("B", 1), ("C", 1)]),
            make_obs("E1", "B", [("A", 1), ("B", 1)]),
            make_obs("E2", "C", [("A", 1)]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d.json")
            with open(path, "w") as f:
                json.dump({"observations": obs}, f)
            report = build_report(dataset_path=path, top=2)
        self.assertEqual(report["returned_card_count"], 2)
        self.assertEqual(report["total_card_count"], 3)
        self.assertEqual([c["card_name"] for c in report["cards"]], ["A", "B"])

    def test_cli_writes_json_output(self):
        obs = [make_obs("E1", "A", [("Card X", 3)])]
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = os.path.join(tmp, "d.json")
            out_path = os.path.join(tmp, "out.json")
            with open(dataset_path, "w") as f:
                json.dump({"observations": obs}, f)
            from app.tournament_adoption import _main
            rc = _main(["--dataset", dataset_path, "--output", out_path, "--top", "0"])
            self.assertEqual(rc, 0)
            with open(out_path) as f:
                payload = json.load(f)
        self.assertEqual(payload["total_decks"], 1)
        self.assertEqual(payload["cards"][0]["card_name"], "Card X")


if __name__ == "__main__":
    unittest.main()
