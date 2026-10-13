"""Stricter CONFIRMED rules for weekly gainers (reversion, single sale, listing floor)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.analysis import calculate_penny_movers, detect_drop, detect_spike

DATES = pd.to_datetime([f"2026-09-{d:02d}" for d in range(20, 30)])
BASELINE, RECENT = list(DATES[1:4]), list(DATES[-3:])


def history(markets, lows):
    return pd.DataFrame({"product_id": 1, "printing": "Unlimited", "date": DATES,
                         "market_price": markets, "low_price": lows})


def status(markets, lows, baseline=10.0, current=None):
    df = history(markets, lows)
    current = current if current is not None else pd.Series(markets[-3:]).median()
    return detect_spike(1, baseline, current, df, DATES[-1], RECENT, "Unlimited", BASELINE)


class GainerConfirmationRules(unittest.TestCase):
    def test_real_trend_is_confirmed(self):
        markets = [10, 10, 10, 10, 11, 13, 15, 17, 18, 18.5]
        lows = [9, 9, 9, 9, 10, 12, 14, 16, 17, 17]
        self.assertEqual(status(markets, lows), "CONFIRMED")

    def test_single_sale_step_is_unconfirmed(self):
        markets = [10] * 7 + [30, 30, 30]
        lows = [9] * 4 + [12, 15, 20, 26, 27, 28]
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_reverted_price_is_unconfirmed(self):
        markets = [10, 10, 10, 10, 11, 12, 13, 40, 40, 10.5]
        lows = [9, 9, 9, 9, 12, 20, 30, 35, 36, 36]
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_flat_listing_floor_is_unconfirmed(self):
        markets = [10, 10, 10, 10, 15, 20, 25, 30, 31, 32]
        lows = [9] * 10  # cheapest copy still ~old price
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_thin_market_baseline_is_unconfirmed(self):
        # Cheapest listing was ~4x the market price a week ago: stale market price.
        markets = [10, 10, 10, 10, 15, 20, 25, 30, 31, 32]
        lows = [40] * 10
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_thin_market_now_is_unconfirmed(self):
        markets = [10, 10, 10, 10, 11, 13, 15, 17, 18, 18.5]
        lows = [9, 9, 9, 9, 10, 20, 40, 45, 45, 45]  # listings jump way past sales
        self.assertEqual(status(markets, lows), "UNCONFIRMED")

    def test_missing_low_prices_skip_floor_rule(self):
        markets = [10, 10, 10, 10, 15, 20, 25, 30, 31, 32]
        self.assertEqual(status(markets, [None] * 10), "CONFIRMED")

    def test_legacy_callers_without_baseline_dates_unchanged(self):
        df = history([10] * 7 + [30, 30, 30], [9] * 10)
        self.assertEqual(detect_spike(1, 10.0, 30.0, df, DATES[-1], RECENT, "Unlimited"),
                         "CONFIRMED")


def drop_status(markets, lows, baseline=20.0, current=None):
    df = history(markets, lows)
    current = current if current is not None else pd.Series(markets[-3:]).median()
    return detect_drop(1, baseline, current, df, DATES[-1], RECENT, "Unlimited", BASELINE)


class LoserConfirmationRules(unittest.TestCase):
    def test_real_decline_is_confirmed(self):
        markets = [20, 20, 20, 20, 18, 16, 14, 12, 11, 10.5]
        lows = [19, 19, 19, 19, 17, 15, 13, 11, 10, 10]
        self.assertEqual(drop_status(markets, lows), "CONFIRMED")

    def test_single_sale_drop_is_unconfirmed(self):
        markets = [20] * 7 + [8, 8, 8]
        lows = [19] * 4 + [15, 12, 9, 8, 8, 8]
        self.assertEqual(drop_status(markets, lows), "UNCONFIRMED")

    def test_listings_still_high_is_unconfirmed(self):
        markets = [20, 20, 20, 20, 18, 16, 14, 12, 11, 10.5]
        lows = [19] * 10  # sellers haven't lowered prices
        self.assertEqual(drop_status(markets, lows), "UNCONFIRMED")

    def test_recovered_price_is_unconfirmed(self):
        markets = [20, 20, 20, 20, 18, 12, 11, 10, 10, 19.5]
        lows = [19, 19, 19, 19, 15, 11, 10, 9, 9, 9]
        self.assertEqual(drop_status(markets, lows, current=10.0), "UNCONFIRMED")


PENNY_DATES = [f"2026-09-{d:02d}" for d in range(1, 10)]
PENNY_TREND = [1.0, 1.0, 1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.7]


class PennyMoverConfirmationRules(unittest.TestCase):
    """Penny movers go through the same stricter rules as the main gainers list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE printing_prices (product_id INTEGER, printing TEXT, "
                "card_name TEXT, set_name TEXT, date TEXT, market_price REAL, low_price REAL)"
            )

    def insert(self, product_id, markets, lows):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO printing_prices VALUES (?, 'Unlimited', ?, 'Test Set', ?, ?, ?)",
                [(product_id, f"Card {product_id}", date, market, low)
                 for date, market, low in zip(PENNY_DATES, markets, lows)],
            )

    def test_penny_movers_failing_strict_rules_are_unconfirmed(self):
        self.insert(1, PENNY_TREND, [0.9, 0.9, 0.9, 1.0, 1.1, 1.2, 1.4, 1.5, 1.6])  # real trend
        self.insert(2, [1.0] * 6 + [1.6] * 3, [0.9, 0.9, 0.9, 1.0, 1.1, 1.2, 1.4, 1.5, 1.5])  # one sale
        self.insert(3, PENNY_TREND, [0.9] * 9)  # cheapest listing never moved
        self.insert(4, PENNY_TREND, [3.0] * 9)  # listings ~3x market: thin market

        movers = calculate_penny_movers(self.db_path)
        statuses = dict(zip(movers["product_id"], movers["status"]))
        self.assertEqual(statuses, {1: "CONFIRMED", 2: "UNCONFIRMED",
                                    3: "UNCONFIRMED", 4: "UNCONFIRMED"})

        # Without baseline_dates (the old penny call) rules 3-6 were skipped,
        # so every one of these passed.
        df = pd.read_sql("SELECT * FROM printing_prices", sqlite3.connect(self.db_path))
        df["date"] = pd.to_datetime(df["date"])
        dates = sorted(df["date"].unique())
        for _, row in movers.iterrows():
            pair_rows = df[df["product_id"] == row["product_id"]]
            self.assertEqual(
                detect_spike(row["product_id"], row["baseline_value"], row["current_value"],
                             pair_rows, dates[-1], dates[-3:], "Unlimited"),
                "CONFIRMED",
            )


if __name__ == "__main__":
    unittest.main()
