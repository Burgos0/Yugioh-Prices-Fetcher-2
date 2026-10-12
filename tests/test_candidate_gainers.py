import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.analysis import CANDIDATE_GAINERS_CONFIG, calculate_candidate_gainers

# Nine snapshots: baseline = positions 0-2, recent = positions 6-8.
DATES = [f"2026-09-0{d}" for d in range(1, 10)]
SUPPORT = (900, "Unlimited")


class CandidateGainersTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        # A flat $30 card makes "Test Set" relevant under the production rule.
        self.add(SUPPORT[0], [30.0] * 9, name="Support")

    def add(self, product_id, markets, printing="Unlimited", set_name="Test Set",
            name=None, lows=None, mids=None, dates=DATES):
        for i, (date, market) in enumerate(zip(dates, markets)):
            if market == "skip":
                continue
            self.rows.append({
                "product_id": product_id, "printing": printing,
                "card_name": name or f"Card {product_id}", "set_name": set_name,
                "low_price": lows[i] if lows else None,
                "mid_price": mids[i] if mids else None,
                "high_price": 9999.0,  # never used; would distort results if it were
                "market_price": market, "date": date,
            })

    def step(self, product_id, baseline, current, **kw):
        """Flat baseline then a one-step move held for the recent window."""
        self.add(product_id, [baseline] * 6 + [current] * 3, **kw)

    def run_calc(self, **kw):
        return calculate_candidate_gainers(None, df=pd.DataFrame(self.rows), **kw)

    def weekly_ids(self, result):
        return list(result["weekly"]["product_id"])

    def reason(self, result, product_id, kind="weekly", printing="Unlimited"):
        ev = result["evaluated"]
        row = ev[(ev["product_id"] == product_id) & (ev["printing"] == printing)]
        value = row[f"{kind}_reason"].iloc[0]
        return None if pd.isna(value) else value

    # --- eligibility floors -------------------------------------------------
    def test_exact_boundaries_pass(self):
        self.step(1, 12.0, 15.0)   # gain $3.00 and exactly 25%
        self.step(2, 2.0, 5.0)     # current exactly $5.00
        self.step(3, 8.0, 11.0)    # gain exactly $3.00
        self.step(4, 20.0, 25.0)   # exactly 25%
        result = self.run_calc()
        self.assertEqual(sorted(self.weekly_ids(result)), [1, 2, 3, 4])
        row = result["weekly"].set_index("product_id").loc[1]
        self.assertEqual(row["percent_gain"], 25.0)
        self.assertEqual(row["dollar_gain"], 3.0)

    def test_just_below_each_floor_fails_with_reason(self):
        self.step(1, 1.99, 4.99)   # below price floor
        self.step(2, 8.0, 10.99)   # below dollar floor
        self.step(3, 20.0, 24.99)  # below percent floor ($4.99 gain)
        result = self.run_calc()
        self.assertEqual(self.weekly_ids(result), [])
        self.assertEqual(self.reason(result, 1), "price_floor")
        self.assertEqual(self.reason(result, 2), "dollar_floor")
        self.assertEqual(self.reason(result, 3), "pct_floor")
        self.assertEqual(result["exclusions"]["weekly"]["price_floor"], 1)

    def test_floor_applies_to_current_not_baseline(self):
        self.step(1, 0.50, 4.00)   # 700% but current < $5
        self.step(2, 2.00, 20.00)  # cheap baseline is fine
        result = self.run_calc()
        self.assertEqual(self.weekly_ids(result), [2])
        self.assertEqual(self.reason(result, 1), "price_floor")
        self.assertEqual(self.reason(result, 1, "fresh"), "price_floor")

    def test_large_legitimate_spike_is_kept_uncapped(self):
        self.step(1, 10.0, 1000.0)
        result = self.run_calc()
        self.assertEqual(result["weekly"]["percent_gain"].iloc[0], 9900.0)
        self.assertEqual(result["fresh"]["percent_gain"].iloc[0], 9900.0)

    # --- invalid and incomplete data -----------------------------------------
    def test_invalid_and_missing_prices(self):
        self.add(1, [0.0, 10, 10, 10, 10, 10, 20, 20, 20])            # zero
        self.add(2, [10, 10, 10, 10, 10, 10, 20, -5.0, 20])           # negative
        self.add(3, [10, 10, 10, 10, 10, 10, 20, 20, math.inf])       # infinite
        self.add(4, [10, 10, 10, 10, 10, 10, 20, float("nan"), 20])   # NaN = missing
        self.add(5, [10, 10, 10, 10, 10, 10, 20, None, 20])           # NULL = missing
        self.step(6, 10.0, 20.0)                                      # control
        result = self.run_calc()
        self.assertEqual(self.weekly_ids(result), [6])
        self.assertEqual(list(result["fresh"]["product_id"]), [6])
        for pid in (1, 2, 3):
            self.assertEqual(self.reason(result, pid), "invalid_price")
        for pid in (4, 5):
            self.assertEqual(self.reason(result, pid), "incomplete_window")

    def test_incomplete_windows_excluded(self):
        self.add(1, [10, "skip", 10, 10, 10, 10, 20, 20, 20])   # missing baseline snapshot
        self.add(2, [10, 10, 10, 10, 10, 10, 20, "skip", 20])   # missing recent snapshot
        self.add(3, [20, 20, 20], dates=DATES[6:])              # no baseline at all
        result = self.run_calc()
        self.assertEqual(self.weekly_ids(result), [])
        for pid in (1, 2, 3):
            self.assertEqual(self.reason(result, pid), "incomplete_window")

    def test_insufficient_history_returns_empty(self):
        result = calculate_candidate_gainers(
            None, df=pd.DataFrame(self.rows).query("date < '2026-09-09'"))
        self.assertIsNone(result["dates"])
        self.assertTrue(result["weekly"].empty)
        self.assertTrue(result["fresh"].empty)

    # --- identity and scope --------------------------------------------------
    def test_separate_printings_stay_separate(self):
        self.step(1, 10.0, 20.0, printing="1st Edition")
        self.step(1, 10.0, 10.0, printing="Unlimited")
        self.step(1, 5.0, 40.0, printing="Limited")
        result = self.run_calc()
        weekly = result["weekly"]
        self.assertEqual(list(zip(weekly["product_id"], weekly["printing"])),
                         [(1, "Limited"), (1, "1st Edition")])
        self.assertEqual(list(weekly["baseline_value"]), [5.0, 10.0])

    def test_sealed_printing_excluded(self):
        self.step(1, 10.0, 20.0, printing="Normal")
        self.assertEqual(self.weekly_ids(self.run_calc()), [])

    def test_relevance_filter_preserved_and_reported(self):
        self.step(1, 4.0, 8.0, set_name="Obscure Set")        # set has no $25/$10 cards
        self.step(2, 10.0, 20.0, set_name="Tournament Pack 1")
        self.add(3, [30.0] * 9, set_name="Tournament Pack 1")  # relevant by value, still excluded
        self.step(4, 10.0, 20.0)
        result = self.run_calc()
        self.assertEqual(self.weekly_ids(result), [4])
        self.assertEqual(self.reason(result, 1), "relevance_filter")
        excluded = result["relevance_excluded"].set_index("product_id")["relevance_reason"]
        self.assertEqual(excluded.to_dict(), {1: "not_relevant_set", 2: "excluded_set"})

    def test_as_of_ignores_later_data(self):
        self.step(1, 10.0, 20.0)
        later = DATES + ["2026-09-10"]
        self.add(2, [10.0] * 9 + [99.0], dates=later)
        self.add(1, ["skip"] * 9 + [10.0], dates=later)
        self.add(SUPPORT[0], ["skip"] * 9 + [30.0], dates=later)
        result = self.run_calc(as_of="2026-09-09")
        self.assertEqual(result["dates"]["as_of"], "2026-09-09")
        self.assertEqual(result["dates"]["recent"], DATES[6:])
        self.assertEqual(result["dates"]["baseline"], DATES[:3])
        self.assertEqual(self.weekly_ids(result), [1])
        self.assertEqual(list(result["fresh"]["product_id"]), [1])

    def test_reads_printing_prices_from_db(self):
        self.step(1, 10.0, 20.0)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "prices.db")
            with sqlite3.connect(db_path) as conn:
                pd.DataFrame(self.rows).to_sql("printing_prices", conn, index=False)
            result = calculate_candidate_gainers(db_path)
        self.assertEqual(self.weekly_ids(result), [1])

    # --- bands ---------------------------------------------------------------
    def test_band_edges(self):
        self.step(1, 10.0, 19.99)
        self.step(2, 10.0, 20.0)
        self.step(3, 50.0, 99.99)
        self.step(4, 50.0, 100.0)
        self.step(5, 2.0, 5.0)
        result = self.run_calc()
        bands = result["weekly"].set_index("product_id")["band"].to_dict()
        self.assertEqual(bands, {1: "$5-20", 2: "$20-100", 3: "$20-100", 4: "$100+", 5: "$5-20"})

    def test_top_n_per_band_uses_same_ranking(self):
        self.step(1, 10.0, 15.0)   # 50%
        self.step(2, 5.0, 15.0)    # 200%
        self.step(3, 20.0, 40.0)   # 100%
        result = self.run_calc(config={"top_n_per_band": 1})
        by_band = {k: list(v["product_id"]) for k, v in result["weekly_by_band"].items()}
        self.assertEqual(by_band, {"$5-20": [2], "$20-100": [3], "$100+": []})

    # --- ranking -------------------------------------------------------------
    def test_confirmed_first_then_pct_dollar_identity(self):
        self.add(1, [10, 10, 10, 11, 12, 13, 14, 15, 16])  # steady trend: CONFIRMED, 50%
        self.step(2, 10.0, 20.0)                           # one sale step: UNCONFIRMED, 100%
        self.step(3, 5.0, 10.0)                            # 100%, $5 gain
        self.step(5, 10.0, 20.0, printing="Unlimited")     # tie with 2 and 4 -> identity order
        self.step(4, 10.0, 20.0, printing="Unlimited")
        self.step(4, 10.0, 20.0, printing="1st Edition")
        weekly = self.run_calc()["weekly"]
        self.assertEqual(list(weekly["status"]), ["CONFIRMED"] + ["UNCONFIRMED"] * 5)
        self.assertEqual(list(zip(weekly["product_id"], weekly["printing"])), [
            (1, "Unlimited"), (2, "Unlimited"), (4, "1st Edition"), (4, "Unlimited"),
            (5, "Unlimited"), (3, "Unlimited")])
        self.assertEqual(list(weekly["rank"]), [1, 2, 3, 4, 5, 6])

    def test_ranking_is_deterministic_under_input_order(self):
        for pid in (7, 3, 5):
            self.step(pid, 10.0, 20.0)
        first = self.run_calc()["weekly"]
        self.rows.reverse()
        second = self.run_calc()["weekly"]
        self.assertEqual(list(first["product_id"]), [3, 5, 7])
        self.assertEqual(list(second["product_id"]), [3, 5, 7])

    # --- fresh jump ----------------------------------------------------------
    def test_fresh_jump_sees_latest_observation(self):
        self.add(1, [10.0] * 8 + [50.0])
        result = self.run_calc()
        self.assertEqual(self.weekly_ids(result), [])
        self.assertEqual(self.reason(result, 1), "dollar_floor")
        fresh = result["fresh"].iloc[0]
        self.assertEqual((fresh["product_id"], fresh["current_value"]), (1, 50.0))
        self.assertEqual(fresh["percent_gain"], 400.0)
        self.assertEqual(fresh["recent_above_baseline_count"], 1)

    def test_fresh_supporting_count_is_not_a_filter(self):
        self.add(1, [10, 10, 10, 10, 10, 10, 10, 10, 40])   # 1 of 3 elevated
        self.add(2, [10, 10, 10, 10, 10, 10, 11, 10.5, 20])  # 1.10x boundary counts
        self.add(3, [10, 10, 10, 10, 10, 10, 30, 30, 30])   # 3 of 3
        fresh = self.run_calc()["fresh"].set_index("product_id")
        self.assertEqual(list(fresh.index), [1, 3, 2])
        self.assertEqual(fresh["recent_above_baseline_count"].to_dict(), {1: 1, 3: 3, 2: 2})

    def test_fresh_ranking_ignores_status(self):
        self.add(1, [10, 10, 10, 11, 12, 13, 14, 15, 16])  # confirmed weekly, 60% fresh
        self.step(2, 10.0, 20.0)                           # unconfirmed weekly, 100% fresh
        self.assertEqual(list(self.run_calc()["fresh"]["product_id"]), [2, 1])

    # --- badges --------------------------------------------------------------
    def badge_case(self, product_id, low_base, low_now, mid=25.0):
        self.step(product_id, 10.0, 20.0,
                  lows=[low_base] * 6 + [low_now] * 3,
                  mids=[mid] * 9 if mid is not None else None)

    def test_badges(self):
        self.badge_case(1, 8.0, 9.6)     # low +20% (still < 80% of market) -> STRONG
        self.badge_case(2, 16.0, 16.0)   # low flat but already >= 80% of market -> STRONG
        self.badge_case(3, 8.0, 8.0)     # low flat, far below market -> WEAK
        self.badge_case(4, 8.0, 7.0)     # low down -> WEAK
        self.badge_case(5, 30.0, 45.0)   # low rose but > 2x market -> WEAK (thin)
        self.badge_case(6, 8.0, 8.8)     # +10%: neither -> NEUTRAL
        self.badge_case(7, 8.0, None)    # low missing -> UNKNOWN
        self.badge_case(8, 8.0, 9.6, mid=None)  # mid missing -> UNKNOWN
        self.badge_case(9, 0.0, 9.6)     # zero baseline low is invalid -> UNKNOWN
        self.badge_case(10, 8.0, 9.2)    # exactly +15% -> STRONG
        result = self.run_calc()
        badges = result["weekly"].set_index("product_id")["badge"].to_dict()
        self.assertEqual(badges, {1: "STRONG", 2: "STRONG", 3: "WEAK", 4: "WEAK", 5: "WEAK",
                                  6: "NEUTRAL", 7: "UNKNOWN", 8: "UNKNOWN", 9: "UNKNOWN",
                                  10: "STRONG"})
        self.assertEqual(sorted(result["fresh"]["product_id"]), list(range(1, 11)))

    def test_badges_never_filter_or_reorder(self):
        for pid, low_now in ((1, 9.6), (2, 8.0), (3, None)):
            self.badge_case(pid, 8.0, low_now)
        base = self.run_calc()
        changed = self.run_calc(config={"badge_low_rise_min": 99.0, "badge_low_flat_max": -99.0,
                                        "badge_low_support_ratio": 99.0,
                                        "badge_max_low_to_market": 0.0})
        for kind in ("weekly", "fresh"):
            cols = ["product_id", "printing", "rank"]
            pd.testing.assert_frame_equal(base[kind][cols], changed[kind][cols])
        self.assertNotEqual(list(base["weekly"]["badge"]), list(changed["weekly"]["badge"]))

    def test_config_block_is_default(self):
        self.assertEqual(CANDIDATE_GAINERS_CONFIG["min_current_price"], 5.0)
        self.assertEqual(CANDIDATE_GAINERS_CONFIG["min_dollar_gain"], 3.0)
        self.assertEqual(CANDIDATE_GAINERS_CONFIG["min_percent_gain"], 25.0)
        self.assertEqual(CANDIDATE_GAINERS_CONFIG["price_band_edges"], (5.0, 20.0, 100.0))
        self.assertEqual(CANDIDATE_GAINERS_CONFIG["top_n_per_band"], 20)


if __name__ == "__main__":
    unittest.main()
