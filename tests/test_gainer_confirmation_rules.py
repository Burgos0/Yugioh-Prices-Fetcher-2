"""Stricter CONFIRMED rules for weekly gainers (reversion, single sale, listing floor)."""
import unittest
import pandas as pd

from app.analysis import detect_spike

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


if __name__ == "__main__":
    unittest.main()
