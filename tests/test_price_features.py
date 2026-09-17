"""Offline tests for the read-only price-feature layer.

These tests build a small in-memory-style prices.db under a temp directory and
exercise :mod:`app.price_features` end-to-end, including the CLI.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

from app.price_features import (
    compute_features,
    compute_product_features,
    load_price_rows,
)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


class PriceFeatureTestBase(unittest.TestCase):
    """Shared fixtures: a temp prices.db with the production schema."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE prices ("
                "product_id INTEGER, card_name TEXT, set_name TEXT, "
                "low_price REAL, mid_price REAL, high_price REAL, "
                "market_price REAL, direct_low_price REAL, date TEXT, "
                "PRIMARY KEY(product_id, date))"
            )

    def insert(
        self,
        product_id: int,
        card_name: str,
        readings: Iterable[Tuple[str, Optional[float]]],
        set_name: str = "Test Set",
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO prices (product_id, card_name, set_name, "
                "market_price, date) VALUES (?, ?, ?, ?, ?)",
                [
                    (product_id, card_name, set_name, price, date)
                    for date, price in readings
                ],
            )


class LoadAndCutoffTests(PriceFeatureTestBase):
    def test_as_of_cutoff_excludes_future_rows(self) -> None:
        base = datetime(2026, 1, 10)
        readings = [(_iso(base + timedelta(days=i)), 10.0 + i) for i in range(6)]
        self.insert(1, "Card A", readings)

        rows = load_price_rows(self.db_path, as_of=_iso(base + timedelta(days=2)))
        dates = [r["date"] for r in rows]
        self.assertEqual(dates, ["2026-01-10", "2026-01-11", "2026-01-12"])
        for r in rows:
            self.assertLessEqual(r["date"], "2026-01-12")

    def test_no_cutoff_returns_all_rows(self) -> None:
        self.insert(1, "Card A", [("2026-01-01", 1.0), ("2026-01-02", 2.0)])
        rows = load_price_rows(self.db_path)
        self.assertEqual(len(rows), 2)

    def test_missing_db_file_returns_empty(self) -> None:
        rows = load_price_rows(str(Path(self.tmp.name) / "does_not_exist.db"))
        self.assertEqual(rows, [])

    def test_product_id_filter(self) -> None:
        self.insert(1, "A", [("2026-01-01", 1.0)])
        self.insert(2, "B", [("2026-01-01", 2.0)])
        rows = load_price_rows(self.db_path, product_ids=[2])
        self.assertEqual({r["product_id"] for r in rows}, {2})

    def test_invalid_as_of_raises(self) -> None:
        with self.assertRaises(ValueError):
            load_price_rows(self.db_path, as_of="not-a-date")


class FeatureComputationTests(PriceFeatureTestBase):
    def _daily_series(
        self, prices: Sequence[float], end_date: datetime
    ) -> Sequence[Tuple[str, float]]:
        n = len(prices)
        return [
            (_iso(end_date - timedelta(days=n - 1 - i)), float(p))
            for i, p in enumerate(prices)
        ]

    def test_recent_and_baseline_medians_and_percent_change(self) -> None:
        # 10 consecutive daily prices; latest = day 10.
        # Days 3, 4, 5 form the baseline window (latest - 8, -7, -6 days back
        # from a series where the earliest day is "latest - 9 days").
        as_of = datetime(2026, 2, 10)
        # Build prices such that:
        #   baseline_window (days at latest-8,-7,-6) -> [10, 20, 15] -> median 15
        #   recent 3 obs -> [40, 30, 50] -> median 40
        prices = [
            5,   # latest - 9
            10,  # latest - 8  (baseline)
            20,  # latest - 7  (baseline)
            15,  # latest - 6  (baseline)
            12,  # latest - 5
            18,  # latest - 4
            22,  # latest - 3
            40,  # latest - 2
            30,  # latest - 1
            50,  # latest
        ]
        self.insert(1, "Card", self._daily_series(prices, as_of))

        [feat] = compute_features(self.db_path, as_of=_iso(as_of))
        self.assertEqual(feat["product_id"], 1)
        self.assertEqual(feat["card_name"], "Card")
        self.assertEqual(feat["latest_price"], 50.0)
        self.assertEqual(feat["latest_date"], _iso(as_of))
        self.assertEqual(feat["recent_median"], 40.0)
        self.assertEqual(feat["baseline_median"], 15.0)
        self.assertAlmostEqual(feat["percent_change"], (40 - 15) / 15 * 100)
        self.assertEqual(feat["history_count"], 10)
        self.assertFalse(feat["data_quality"]["missing_baseline"])
        self.assertFalse(feat["data_quality"]["missing_recent"])
        self.assertFalse(feat["data_quality"]["stale_latest"])

    def test_no_future_leakage_in_features(self) -> None:
        as_of = datetime(2026, 2, 10)
        pre = self._daily_series([1.0, 2.0, 3.0, 4.0], as_of)
        # Future rows that must be ignored.
        future = [
            (_iso(as_of + timedelta(days=1)), 9999.0),
            (_iso(as_of + timedelta(days=5)), 12345.0),
        ]
        self.insert(1, "Card", list(pre) + future)

        [feat] = compute_features(self.db_path, as_of=_iso(as_of))
        # Latest observation must be the as_of date, not any future price.
        self.assertEqual(feat["latest_date"], _iso(as_of))
        self.assertEqual(feat["latest_price"], 4.0)
        self.assertNotIn(9999.0, [feat["recent_median"], feat["baseline_median"]])

    def test_consecutive_rising_persistence(self) -> None:
        as_of = datetime(2026, 3, 1)
        # Series: 10, 12, 11, 13, 14, 15 -> trailing rising run is 4 (11,13,14,15)
        self.insert(
            1,
            "Card",
            self._daily_series([10, 12, 11, 13, 14, 15], as_of),
        )
        [feat] = compute_features(self.db_path, as_of=_iso(as_of))
        self.assertEqual(feat["consecutive_rising"], 4)

        # Flat/decreasing tail resets to 1.
        self.insert(
            2,
            "Flat",
            self._daily_series([10, 11, 12, 12], as_of),
        )
        feats = {f["product_id"]: f for f in compute_features(
            self.db_path, as_of=_iso(as_of)
        )}
        self.assertEqual(feats[2]["consecutive_rising"], 1)

    def test_volatility_zero_for_constant_series_and_positive_for_swings(
        self,
    ) -> None:
        as_of = datetime(2026, 3, 1)
        self.insert(1, "Flat", self._daily_series([5.0] * 6, as_of))
        self.insert(2, "Swings", self._daily_series([10, 20, 10, 20, 10, 20], as_of))
        feats = {f["product_id"]: f for f in compute_features(
            self.db_path, as_of=_iso(as_of)
        )}
        self.assertEqual(feats[1]["volatility"], 0.0)
        self.assertGreater(feats[2]["volatility"], 0.0)

    def test_volatility_none_for_short_series(self) -> None:
        as_of = datetime(2026, 3, 1)
        self.insert(1, "Short", self._daily_series([10.0, 12.0], as_of))
        [feat] = compute_features(self.db_path, as_of=_iso(as_of))
        self.assertIsNone(feat["volatility"])

    def test_sparse_history_flags_and_partial_features(self) -> None:
        as_of = datetime(2026, 3, 1)
        # Only one observation, well outside the baseline window.
        self.insert(1, "Sparse", [(_iso(as_of), 42.0)])
        [feat] = compute_features(self.db_path, as_of=_iso(as_of))
        self.assertEqual(feat["history_count"], 1)
        self.assertTrue(feat["data_quality"]["sparse_history"])
        self.assertTrue(feat["data_quality"]["missing_baseline"])
        self.assertTrue(feat["data_quality"]["missing_recent"])  # < 3 obs
        self.assertIsNone(feat["percent_change"])
        self.assertIsNone(feat["volatility"])
        self.assertEqual(feat["latest_price"], 42.0)
        self.assertEqual(feat["recent_median"], 42.0)
        self.assertEqual(feat["consecutive_rising"], 1)

    def test_empty_database_returns_empty_list(self) -> None:
        self.assertEqual(compute_features(self.db_path), [])
        self.assertEqual(compute_features(self.db_path, as_of="2026-01-01"), [])

    def test_all_null_prices_flagged(self) -> None:
        # Rows exist but market_price is NULL -> no valid history.
        self.insert(
            1,
            "Nulls",
            [("2026-01-01", None), ("2026-01-02", None)],
        )
        [feat] = compute_features(self.db_path, as_of="2026-01-02")
        self.assertEqual(feat["history_count"], 0)
        self.assertTrue(feat["data_quality"]["no_valid_prices"])
        self.assertIsNone(feat["latest_price"])
        self.assertIsNone(feat["recent_median"])

    def test_stale_latest_flag(self) -> None:
        as_of = datetime(2026, 3, 10)
        latest_obs = as_of - timedelta(days=10)
        self.insert(
            1,
            "Stale",
            self._daily_series([1.0, 2.0, 3.0, 4.0], latest_obs),
        )
        [feat] = compute_features(self.db_path, as_of=_iso(as_of))
        self.assertTrue(feat["data_quality"]["stale_latest"])

    def test_baseline_missing_when_history_starts_after_window(self) -> None:
        as_of = datetime(2026, 3, 10)
        # Only recent 3 days of history -> nothing in the [-8, -6] window.
        self.insert(1, "Recent", self._daily_series([10, 11, 12], as_of))
        [feat] = compute_features(self.db_path, as_of=_iso(as_of))
        self.assertIsNone(feat["baseline_median"])
        self.assertIsNone(feat["percent_change"])
        self.assertTrue(feat["data_quality"]["missing_baseline"])

    def test_multiple_products_preserve_identity(self) -> None:
        as_of = datetime(2026, 3, 1)
        self.insert(11, "Alpha", self._daily_series([1, 2, 3], as_of))
        self.insert(22, "Beta", self._daily_series([4, 5, 6], as_of))
        feats = compute_features(self.db_path, as_of=_iso(as_of))
        self.assertEqual([f["product_id"] for f in feats], [11, 22])
        self.assertEqual([f["card_name"] for f in feats], ["Alpha", "Beta"])


class ProductLevelHelperTests(unittest.TestCase):
    def test_compute_product_features_empty_returns_none(self) -> None:
        self.assertIsNone(compute_product_features([]))


class CLITests(PriceFeatureTestBase):
    def test_cli_emits_valid_json_payload(self) -> None:
        as_of = datetime(2026, 4, 1)
        n = 10
        readings = [
            (_iso(as_of - timedelta(days=n - 1 - i)), float(10 + i))
            for i in range(n)
        ]
        self.insert(555, "CLI Card", readings)

        out_path = Path(self.tmp.name) / "features.json"
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "compute_price_features.py"),
                "--db",
                self.db_path,
                "--as-of",
                _iso(as_of),
                "--output",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["as_of"], _iso(as_of))
        self.assertEqual(payload["product_count"], 1)
        [feat] = payload["features"]
        self.assertEqual(feat["product_id"], 555)
        self.assertEqual(feat["card_name"], "CLI Card")
        self.assertEqual(feat["latest_date"], _iso(as_of))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
