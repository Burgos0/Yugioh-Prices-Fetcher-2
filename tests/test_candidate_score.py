"""Focused offline tests for the explainable combined candidate score.

Covers the six required scenarios:

* ``as_of`` cutoff / no leakage (adoption AND price sides).
* Score ordering (higher blended evidence ranks first).
* Missing features are flagged and excluded from the average — never
  silently treated as zero.
* Identity-to-many-printings fan-out (one adopted card produces one
  ranked row per matching tracked ``product_id``, all sharing the same
  identity-level adoption metrics).
* Deterministic output (same inputs → byte-identical JSON).
* Empty data (no crash, well-formed empty report).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.candidate_score import (
    MOMENTUM_CAP_PCT,
    TREND_CAP,
    build_candidates,
    compute_row_score,
    normalize_momentum,
    normalize_penetration,
    normalize_placement_weighted,
    normalize_trend,
)
from app.meta_watch import FORMAT_TCG_ADVANCED
from app.tournament_adoption import SOURCE_PROVIDER_YGOPRODECK


def make_obs(
    event_id: str,
    player: str,
    main: Optional[List[Tuple[str, int]]] = None,
    *,
    placement: str = "Winner",
    event_date: str = "2026-08-30",
    fmt: str = FORMAT_TCG_ADVANCED,
    source_provider: str = SOURCE_PROVIDER_YGOPRODECK,
    source_type: str = "tournament",
    archetype: str = "TestDeck",
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "event_name": "Test Event",
        "event_date": event_date,
        "region": "NA",
        "format": fmt,
        "banlist_id": "2026-04",
        "player": player,
        "placement": placement,
        "archetype": archetype,
        "source_url": "https://example.com/coverage",
        "source_type": source_type,
        "source_provider": source_provider,
        "published_at": None,
        "first_seen_at": "2026-09-01T00:00:00Z",
        "main_deck": [{"name": n, "count": c} for n, c in (main or [])],
        "side_deck": [],
        "extra_deck": [],
    }


def _write_dataset(path: str, observations: Iterable[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"observations": list(observations)}, fh)


def _make_prices_db(path: str, rows: Iterable[Tuple[int, str, str, str, Optional[float]]]) -> None:
    """Rows: (product_id, card_name, set_name, date, market_price)."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE prices ("
            "product_id INTEGER, card_name TEXT, set_name TEXT, "
            "low_price REAL, mid_price REAL, high_price REAL, "
            "market_price REAL, direct_low_price REAL, date TEXT, "
            "PRIMARY KEY(product_id, date))"
        )
        conn.executemany(
            "INSERT INTO prices (product_id, card_name, set_name, date, market_price) "
            "VALUES (?, ?, ?, ?, ?)",
            list(rows),
        )
        conn.commit()


class _Fixture(unittest.TestCase):
    """Provides a temp dataset + prices.db pair per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = os.path.join(self.tmp.name, "meta_watch.json")
        self.db_path = os.path.join(self.tmp.name, "prices.db")

    def _price_series(
        self,
        product_id: int,
        card_name: str,
        start_date: str,
        prices: List[float],
    ) -> List[Tuple[int, str, str, str, Optional[float]]]:
        # Consecutive daily observations starting at start_date.
        from datetime import datetime, timedelta

        start = datetime.strptime(start_date, "%Y-%m-%d")
        return [
            (product_id, card_name, "Set", (start + timedelta(days=i)).strftime("%Y-%m-%d"), p)
            for i, p in enumerate(prices)
        ]


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


class NormalizationTests(unittest.TestCase):
    def test_penetration_and_placement_clamp_to_unit_interval(self) -> None:
        self.assertEqual(normalize_penetration(None), None)
        self.assertEqual(normalize_penetration(0.0), 0.0)
        self.assertEqual(normalize_penetration(0.5), 0.5)
        self.assertEqual(normalize_penetration(1.5), 1.0)
        self.assertEqual(normalize_penetration(-0.1), 0.0)
        self.assertEqual(normalize_placement_weighted(1.2), 1.0)

    def test_momentum_treats_drops_as_zero_evidence(self) -> None:
        self.assertEqual(normalize_momentum(None), None)
        self.assertEqual(normalize_momentum(0.0), 0.0)
        self.assertEqual(normalize_momentum(-15.0), 0.0)
        self.assertAlmostEqual(normalize_momentum(MOMENTUM_CAP_PCT / 2.0), 0.5)
        self.assertEqual(normalize_momentum(MOMENTUM_CAP_PCT * 2), 1.0)

    def test_trend_single_point_contributes_nothing(self) -> None:
        self.assertEqual(normalize_trend(None), None)
        self.assertEqual(normalize_trend(1), 0.0)
        self.assertAlmostEqual(normalize_trend(TREND_CAP), 1.0)
        self.assertEqual(normalize_trend(TREND_CAP + 10), 1.0)


# ---------------------------------------------------------------------------
# compute_row_score: missing components handling
# ---------------------------------------------------------------------------


class RowScoreMissingTests(unittest.TestCase):
    def test_missing_price_side_is_flagged_not_zeroed(self) -> None:
        adoption = {
            "card_name": "X",
            "unique_decks": 5,
            "total_copies": 10,
            "deck_penetration_rate": 1.0,
            "placement_weighted_adoption": 1.0,
            "latest_event_date": "2026-08-30",
        }
        components, missing, score, available = compute_row_score(adoption, None)
        self.assertIn("price_momentum", missing)
        self.assertIn("price_trend", missing)
        self.assertNotIn("deck_penetration", missing)
        self.assertEqual(available, 2)
        # Adoption-only row with perfect adoption should score 1.0,
        # NOT 0.6 (the adoption weight sum). That would be the "missing
        # == zero" bug.
        self.assertAlmostEqual(score, 1.0)

    def test_missing_adoption_side_is_flagged(self) -> None:
        price = {
            "product_id": 1,
            "card_name": "X",
            "percent_change": 50.0,
            "consecutive_rising": 5,
            "history_count": 10,
            "data_quality": {},
        }
        components, missing, score, available = compute_row_score(None, price)
        self.assertIn("deck_penetration", missing)
        self.assertIn("placement_weighted", missing)
        self.assertAlmostEqual(score, 1.0)  # perfect price side, renormalized

    def test_completely_missing_yields_zero_score_and_all_flags(self) -> None:
        components, missing, score, available = compute_row_score(None, None)
        self.assertEqual(available, 0)
        self.assertEqual(score, 0.0)
        self.assertEqual(
            set(missing),
            {"deck_penetration", "placement_weighted", "price_momentum", "price_trend"},
        )


# ---------------------------------------------------------------------------
# End-to-end: cutoff/no-leakage, ordering, fan-out, determinism, empty data
# ---------------------------------------------------------------------------


class CutoffNoLeakageTests(_Fixture):
    def test_future_tournament_is_dropped_from_adoption_side(self) -> None:
        past = make_obs("E1", "A", [("Card X", 3)], event_date="2026-06-01")
        future = make_obs("E2", "B", [("Card Y", 3)], event_date="2026-09-15")
        _write_dataset(self.dataset_path, [past, future])
        _make_prices_db(
            self.db_path,
            self._price_series(100, "Card X", "2026-05-25", [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
            + self._price_series(200, "Card Y", "2026-05-25", [1.0] * 9),
        )
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=self.db_path,
            as_of="2026-08-30",
        )
        # Card Y's tournament is in the future and must be excluded.
        card_names = {r["card_name"] for r in report["candidates"] if r["adoption"]["unique_decks"]}
        self.assertIn("Card X", card_names)
        self.assertNotIn("Card Y", card_names)

    def test_future_price_row_is_dropped_from_price_side(self) -> None:
        obs = make_obs("E1", "A", [("Card X", 3)], event_date="2026-06-01")
        _write_dataset(self.dataset_path, [obs])
        # A run of prices with a big spike AFTER the cutoff. If the
        # cutoff leaked, percent_change would jump.
        pre_cutoff = self._price_series(
            100, "Card X", "2026-08-22", [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        )
        post_cutoff = self._price_series(
            100, "Card X", "2026-09-01", [500.0, 500.0, 500.0]
        )
        _make_prices_db(self.db_path, pre_cutoff + post_cutoff)
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=self.db_path,
            as_of="2026-08-30",
        )
        row = next(r for r in report["candidates"] if r["product_id"] == 100)
        # Latest date used must be at or before the cutoff.
        self.assertLessEqual(row["price"]["latest_date"], "2026-08-30")
        # Recent median can't be inflated by the spike.
        self.assertLess(row["price"]["recent_median"], 10.0)


class ScoreOrderingTests(_Fixture):
    def test_strong_evidence_ranks_above_weak_evidence(self) -> None:
        # Card A: high adoption and strong price rise.
        # Card B: low adoption, flat price.
        obs = [
            make_obs("E1", "A", [("Card A", 3)], placement="Winner"),
            make_obs("E1", "B", [("Card A", 3), ("Card B", 1)], placement="Runner-Up"),
            make_obs("E2", "C", [("Card A", 3)], placement="Top 4"),
            make_obs("E2", "D", [("Card A", 3)], placement="Top 4"),
            make_obs("E3", "E", [("Card A", 3), ("Card B", 1)], placement="Top 8"),
        ]
        _write_dataset(self.dataset_path, obs)
        _make_prices_db(
            self.db_path,
            self._price_series(100, "Card A", "2026-08-15",
                                [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 2.5, 3.0, 3.5, 4.0])
            + self._price_series(200, "Card B", "2026-08-15",
                                 [1.0] * 12),
        )
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=self.db_path,
            as_of="2026-08-30",
        )
        pids_in_order = [r["product_id"] for r in report["candidates"] if r["product_id"] is not None]
        self.assertEqual(pids_in_order.index(100), 0)
        self.assertLess(
            next(r for r in report["candidates"] if r["product_id"] == 200)["final_score"],
            next(r for r in report["candidates"] if r["product_id"] == 100)["final_score"],
        )


class FanOutTests(_Fixture):
    def test_one_adopted_card_produces_one_row_per_matching_printing(self) -> None:
        obs = [
            make_obs("E1", "A", [("Ash Blossom & Joyous Spring", 3)], placement="Winner"),
            make_obs("E1", "B", [("Ash Blossom & Joyous Spring", 3)], placement="Top 8"),
        ]
        _write_dataset(self.dataset_path, obs)
        _make_prices_db(
            self.db_path,
            self._price_series(100, "Ash Blossom & Joyous Spring", "2026-08-15", [1.0] * 12)
            + self._price_series(101, "Ash Blossom & Joyous Spring (Ultra Rare)", "2026-08-15", [1.0] * 12)
            + self._price_series(102, "Ash Blossom & Joyous Spring (Ghost Rare)", "2026-08-15", [1.0] * 12),
        )
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=self.db_path,
            as_of="2026-08-30",
        )
        product_rows = [r for r in report["candidates"] if r["product_id"] is not None]
        self.assertEqual(len(product_rows), 3)
        # All three rows share the same identity-level adoption metrics.
        pens = {r["adoption"]["deck_penetration_rate"] for r in product_rows}
        pwas = {r["adoption"]["placement_weighted_adoption"] for r in product_rows}
        unique_decks = {r["adoption"]["unique_decks"] for r in product_rows}
        self.assertEqual(len(pens), 1)
        self.assertEqual(len(pwas), 1)
        self.assertEqual(unique_decks, {2})
        # printing_count_for_card is 3 on every fanned row; a reason
        # notes the fan-out so we don't overclaim identification.
        for r in product_rows:
            self.assertEqual(r["printing_count_for_card"], 3)
            self.assertTrue(
                any("fanned out to 3" in reason for reason in r["reasons"]),
                r["reasons"],
            )

    def test_adopted_card_with_no_matching_printing_still_surfaces_as_unresolved_row(self) -> None:
        obs = [make_obs("E1", "A", [("Untracked Card", 3)])]
        _write_dataset(self.dataset_path, obs)
        _make_prices_db(
            self.db_path,
            self._price_series(999, "Some Other Card", "2026-08-15", [1.0] * 12),
        )
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=self.db_path,
            as_of="2026-08-30",
        )
        unresolved = [r for r in report["candidates"] if r["product_id"] is None]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["card_name"], "Untracked Card")
        self.assertIn("price_momentum", unresolved[0]["missing_flags"])


class DeterminismTests(_Fixture):
    def test_repeated_build_is_byte_identical(self) -> None:
        obs = [
            make_obs("E1", "A", [("Card A", 3), ("Card B", 1)], placement="Winner"),
            make_obs("E1", "B", [("Card A", 2)], placement="Top 4"),
        ]
        _write_dataset(self.dataset_path, obs)
        _make_prices_db(
            self.db_path,
            self._price_series(100, "Card A", "2026-08-15", [1.0, 1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4, 1.45, 1.5])
            + self._price_series(200, "Card B", "2026-08-15", [2.0] * 10),
        )
        r1 = build_candidates(self.dataset_path, self.db_path, as_of="2026-08-30")
        r2 = build_candidates(self.dataset_path, self.db_path, as_of="2026-08-30")
        # Strip generated_at-like fields — we don't emit any, so this
        # should already be identical.
        self.assertEqual(
            json.dumps(r1, sort_keys=True),
            json.dumps(r2, sort_keys=True),
        )


class EmptyDataTests(_Fixture):
    def test_missing_dataset_and_missing_db(self) -> None:
        # Neither file exists.
        report = build_candidates(
            dataset_path=os.path.join(self.tmp.name, "nope.json"),
            prices_db_path=os.path.join(self.tmp.name, "nope.db"),
            as_of=None,
        )
        self.assertEqual(report["candidate_count"], 0)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["adoption_total_decks"], 0)
        self.assertEqual(report["price_product_count"], 0)

    def test_empty_dataset_with_populated_db_still_yields_price_only_rows(self) -> None:
        _write_dataset(self.dataset_path, [])
        _make_prices_db(
            self.db_path,
            self._price_series(100, "Card A", "2026-08-15", [1.0] * 10),
        )
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=self.db_path,
        )
        self.assertEqual(report["adoption_total_decks"], 0)
        product_rows = [r for r in report["candidates"] if r["product_id"] == 100]
        self.assertEqual(len(product_rows), 1)
        row = product_rows[0]
        # Adoption missing → flagged, confidence low.
        self.assertIn("deck_penetration", row["missing_flags"])
        self.assertIn("placement_weighted", row["missing_flags"])
        self.assertEqual(row["confidence"], "low")


class ConfidenceTests(_Fixture):
    def test_full_signals_and_thresholds_yield_high_confidence(self) -> None:
        # 5 qualifying decks, all containing the card, 12 price obs, no staleness.
        obs = [
            make_obs(f"E{i}", f"P{i}", [("Card A", 3)], placement="Winner",
                     event_date="2026-08-2%d" % (i % 10))
            for i in range(5)
        ]
        _write_dataset(self.dataset_path, obs)
        _make_prices_db(
            self.db_path,
            self._price_series(100, "Card A", "2026-08-19",
                                [1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4, 1.45, 1.5, 1.55]),
        )
        report = build_candidates(
            dataset_path=self.dataset_path,
            prices_db_path=self.db_path,
            as_of="2026-08-30",
        )
        row = next(r for r in report["candidates"] if r["product_id"] == 100)
        self.assertEqual(row["confidence"], "high")


class CLITests(_Fixture):
    def test_cli_writes_deterministic_json(self) -> None:
        obs = [make_obs("E1", "A", [("Card A", 3)])]
        _write_dataset(self.dataset_path, obs)
        _make_prices_db(
            self.db_path,
            self._price_series(100, "Card A", "2026-08-15", [1.0] * 10),
        )
        out = os.path.join(self.tmp.name, "out.json")
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
            "rank_candidates.py",
        )
        result = subprocess.run(
            [
                sys.executable,
                script,
                "--dataset",
                self.dataset_path,
                "--prices-db",
                self.db_path,
                "--as-of",
                "2026-08-30",
                "--top",
                "0",
                "--output",
                out,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["as_of"], "2026-08-30")
        self.assertGreaterEqual(payload["candidate_count"], 1)
        # Weights are documented in the emitted payload.
        self.assertAlmostEqual(sum(payload["weights"].values()), 1.0)


if __name__ == "__main__":
    unittest.main()
