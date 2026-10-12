import itertools
import math
import unittest

import numpy as np
import pandas as pd

from app.analysis import calculate_candidate_gainers
from scripts.compare_candidate_gainers_followup import (
    DOLLAR_GRID,
    LOOSE_CONFIG,
    PCT_GRID,
    PRICE_GRID,
    bootstrap_ci,
    classify_jump_outcome,
    filter_cell,
    first_appearance,
    forward_stats,
    median_diff,
)

DATES = [f"2026-09-0{d}" for d in range(1, 10)]


def synthetic_prices():
    """Cards spread across the grid's floors, with confirmed and one-step moves."""
    rows = []

    def add(pid, markets, printing="Unlimited"):
        rows.extend({"product_id": pid, "printing": printing, "card_name": f"C{pid}",
                     "set_name": "Test Set", "low_price": None, "mid_price": None,
                     "market_price": m, "date": d} for d, m in zip(DATES, markets))

    add(900, [30.0] * 9)                                   # makes the set relevant
    pid = 1
    for base, cur in [(0.8, 1.3), (1.5, 2.1), (2.0, 2.6), (2.0, 3.5), (3.0, 3.7), (3.0, 4.6),
                      (4.0, 5.1), (4.0, 7.5), (8.0, 9.4), (8.0, 10.0), (10.0, 13.0), (12.0, 18.0),
                      (2.0, 20.0), (50.0, 80.0)]:
        add(pid, [base] * 6 + [cur] * 3)                   # one-step: UNCONFIRMED
        step = (cur - base) / 6
        add(pid, [base] * 3 + [round(base + step * k, 2) for k in range(1, 7)], "1st Edition")
        pid += 1
    add(pid, [10.0] * 8 + [16.0])                          # fresh-only jump
    return pd.DataFrame(rows)


class FilterCellEquivalenceTests(unittest.TestCase):
    def test_filtering_loose_list_equals_direct_run_for_every_cell(self):
        df = synthetic_prices()
        loose = calculate_candidate_gainers(None, df=df, config=LOOSE_CONFIG)
        self.assertIn("CONFIRMED", set(loose["weekly"]["status"]))
        cols = ["product_id", "printing", "status", "percent_gain"]
        for price, dollar, pct in itertools.product(PRICE_GRID, DOLLAR_GRID, PCT_GRID):
            config = {"min_current_price": price, "min_dollar_gain": dollar, "min_percent_gain": pct}
            direct = calculate_candidate_gainers(None, df=df, config=config)
            got = filter_cell(loose["weekly"], price, dollar, pct).reset_index(drop=True)
            pd.testing.assert_frame_equal(got[cols], direct["weekly"][cols], check_dtype=False)
            got_fresh = filter_cell(loose["fresh"], price, dollar, pct).reset_index(drop=True)
            pd.testing.assert_frame_equal(got_fresh[cols[:2] + ["percent_gain"]],
                                          direct["fresh"][cols[:2] + ["percent_gain"]], check_dtype=False)
            confirmed = filter_cell(loose["weekly"], price, dollar, pct, confirmed_only=True)
            self.assertTrue((confirmed["status"] == "CONFIRMED").all())
            self.assertEqual(len(confirmed), int((direct["weekly"]["status"] == "CONFIRMED").sum()))


class ForwardStatsTests(unittest.TestCase):
    def test_flat_rose_fell_and_conditional_median(self):
        chg = pd.Series([0.0, 0.004, -0.004, 1.0, -2.0, 3.0, np.nan])
        ret = pd.Series([0.0, 0.04, -0.04, 10.0, -20.0, 30.0, np.nan])
        s = forward_stats(ret, chg)
        self.assertEqual(s["n"], 6)                       # NaN exit dropped
        self.assertEqual((s["pct_rose"], s["pct_flat"], s["pct_fell"]), (33.3, 50.0, 16.7))
        self.assertEqual(s["n_changed"], 3)
        self.assertEqual(s["median_if_changed"], 10.0)    # median of 10, -20, 30
        self.assertEqual(s["median"], 0.02)

    def test_empty_and_all_flat(self):
        self.assertEqual(forward_stats(pd.Series([], dtype=float), pd.Series([], dtype=float))["n"], 0)
        s = forward_stats(pd.Series([0.0, 0.0]), pd.Series([0.0, 0.0]))
        self.assertEqual((s["pct_flat"], s["median_if_changed"]), (100.0, None))


class JumpOutcomeTests(unittest.TestCase):
    def test_outcome_boundaries(self):
        # baseline 10, jump to 50 (jump of 40).
        self.assertEqual(classify_jump_outcome(10, 50, 50.01), "CONTINUED")
        self.assertEqual(classify_jump_outcome(10, 50, 50.0), "HELD")       # unchanged = held
        self.assertEqual(classify_jump_outcome(10, 50, 30.0), "HELD")       # exactly 50% kept
        self.assertEqual(classify_jump_outcome(10, 50, 29.99), "PARTIAL")
        self.assertEqual(classify_jump_outcome(10, 50, 11.0), "REVERTED")   # within 10% of baseline
        self.assertEqual(classify_jump_outcome(10, 50, 11.01), "PARTIAL")
        self.assertEqual(classify_jump_outcome(10, 50, 6.0), "REVERTED")    # below baseline
        self.assertEqual(classify_jump_outcome(10, 50, math.nan), "MISSING")
        self.assertEqual(classify_jump_outcome(10, 50, None), "MISSING")


class RepeatAndBootstrapTests(unittest.TestCase):
    def test_first_appearance_keeps_earliest_date_per_printing(self):
        rows = pd.DataFrame({"product_id": [1, 1, 1, 2], "printing": ["U", "U", "1st", "U"],
                             "date": ["2026-10-02", "2026-09-30", "2026-10-01", "2026-10-02"],
                             "ret7": [5.0, 1.0, 2.0, 3.0]})
        kept = first_appearance(rows).sort_values(["product_id", "printing"])
        self.assertEqual(list(kept["ret7"]), [2.0, 1.0, 3.0])

    def test_bootstrap_is_deterministic_and_brackets_constant(self):
        per_date = [np.array([1.0, 2.0, 3.0]), np.array([2.0, 2.0]), np.array([10.0])]
        stat = lambda idx, _r: float(np.median(np.concatenate([per_date[i] for i in idx])))
        first = bootstrap_ci(stat, 3, n_boot=200, seed=1)
        self.assertEqual(first, bootstrap_ci(stat, 3, n_boot=200, seed=1))
        self.assertLessEqual(first[0], first[1])
        same = [np.array([4.0])] * 3
        self.assertEqual(bootstrap_ci(lambda idx, _r: median_diff(same, same, idx), 3, n_boot=50), [0.0, 0.0])

    def test_median_diff_handles_empty(self):
        self.assertIsNone(median_diff([np.array([])], [np.array([1.0])], [0]))
        self.assertIsNone(bootstrap_ci(lambda idx, _r: None, 2, n_boot=10))


if __name__ == "__main__":
    unittest.main()
