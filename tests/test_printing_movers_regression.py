import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from app.analysis import (
    _calculate_printing_relevant_sets,
    _load_printing_prices,
    _select_weekly_mover_windows,
    calculate_penny_movers,
    calculate_top_gainers,
    calculate_top_losers,
    detect_drop,
    detect_spike,
)

SNAPSHOT_DATES = [
    "2026-09-01", "2026-09-02", "2026-09-04",
    "2026-09-05", "2026-09-08", "2026-09-09",
    "2026-09-10", "2026-09-12", "2026-09-15",
]


def reference_printing_movers(db_path, limit, mover_type):
    """Pre-optimization implementation that rescans the full DataFrame per pair."""
    df = _load_printing_prices(db_path)
    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])
    weekly_windows = _select_weekly_mover_windows(df)
    if weekly_windows is None:
        return pd.DataFrame()
    recent_dates, baseline_dates = weekly_windows

    relevant_sets = None
    if mover_type in ("gainer", "loser"):
        relevant_sets = _calculate_printing_relevant_sets(df)

    recent = df[(df["date"].isin(recent_dates)) & df["market_price"].notna()]
    baseline = df[(df["date"].isin(baseline_dates)) & df["market_price"].notna()]
    identity = ["product_id", "printing"]
    current_medians = recent.groupby(identity)["market_price"].median()
    baseline_medians = baseline.groupby(identity)["market_price"].median()
    complete_recent = recent.groupby(identity)["date"].nunique() == len(recent_dates)
    complete_baseline = baseline.groupby(identity)["date"].nunique() == len(baseline_dates)
    complete_pairs = complete_recent[complete_recent].index.intersection(
        complete_baseline[complete_baseline].index
    )
    latest_date = df["date"].max()
    results = []

    for product_id, printing in complete_pairs:
        pair = (product_id, printing)
        pair_rows = df[(df["product_id"] == product_id) & (df["printing"] == printing)]
        set_name = pair_rows["set_name"].iloc[0]
        if relevant_sets is not None and set_name not in relevant_sets:
            continue
        baseline_value = baseline_medians[pair]
        current_value = current_medians[pair]

        if mover_type == "gainer":
            if current_value < 2.0 or current_value <= baseline_value:
                continue
            dollar_change = current_value - baseline_value
            percent_change = dollar_change / baseline_value * 100
            status = detect_spike(product_id, baseline_value, current_value, df,
                                  latest_date, recent_dates, printing, baseline_dates)
            result = {"dollar_gain": dollar_change, "percent_gain": percent_change}
        elif mover_type == "loser":
            if baseline_value < 3.0 or current_value >= baseline_value:
                continue
            dollar_change = current_value - baseline_value
            percent_change = dollar_change / baseline_value * 100
            status = detect_drop(product_id, baseline_value, current_value, df,
                                 latest_date, recent_dates, printing)
            result = {"dollar_change": dollar_change, "percent_change": percent_change}
        else:
            if baseline_value < 0.25 or current_value >= 2.0 or current_value <= baseline_value:
                continue
            dollar_change = current_value - baseline_value
            percent_change = dollar_change / baseline_value * 100
            if dollar_change < 0.25 or percent_change < 20:
                continue
            status = detect_spike(product_id, baseline_value, current_value, df,
                                  latest_date, recent_dates, printing)
            result = {"dollar_gain": dollar_change, "percent_gain": percent_change}

        result.update({
            "product_id": product_id,
            "printing": printing,
            "card_name": pair_rows["card_name"].iloc[0],
            "set_name": set_name,
            "baseline_value": baseline_value,
            "current_value": current_value,
            "status": status,
        })
        results.append(result)

    results_df = pd.DataFrame(results)
    if results_df.empty:
        return results_df
    sort_column = "percent_change" if mover_type == "loser" else "percent_gain"
    if mover_type == "gainer":
        results_df["_confirmed"] = results_df["status"] == "CONFIRMED"
        results_df = results_df.sort_values(["_confirmed", sort_column], ascending=[False, False])
        results_df = results_df.drop(columns="_confirmed").head(limit)
    else:
        results_df = results_df.sort_values(sort_column, ascending=mover_type == "loser").head(limit)
    results_df.insert(0, "rank", range(1, len(results_df) + 1))
    return results_df


class PrintingMoverRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE printing_prices (product_id INTEGER, printing TEXT, "
                "card_name TEXT, set_name TEXT, date TEXT, market_price REAL, "
                "PRIMARY KEY (product_id, printing, date))"
            )

    def insert_rows(self, rows):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("INSERT INTO printing_prices VALUES (?, ?, ?, ?, ?, ?)", rows)

    def insert_series(self, product_id, card_name, prices, printing="Unlimited",
                      set_name="Relevant Set"):
        self.insert_rows([
            (product_id, printing, card_name, set_name, date, price)
            for date, price in zip(SNAPSHOT_DATES, prices)
        ])

    def add_relevant_set_support(self):
        for product_id in range(900, 905):
            self.insert_series(product_id, f"Support {product_id}", [30.0] * 9)

    def build_randomized_history(self):
        rng = random.Random(1234)
        printings = ["Unlimited", "1st Edition", "Limited"]
        sets = ["Relevant Set", "Other Relevant", "Bulk Set"]
        rows = []
        for product_id in range(1, 400):
            set_name = sets[product_id % len(sets)]
            for printing in rng.sample(printings, rng.randint(1, len(printings))):
                base = rng.choice([0.3, 0.8, 1.5, 3.0, 4.5, 8.0, 20.0, 60.0])
                for index, date in enumerate(SNAPSHOT_DATES):
                    if rng.random() < 0.05:
                        continue
                    price = None if rng.random() < 0.03 else round(
                        base * rng.uniform(0.4, 2.2) if index >= 6 else base * rng.uniform(0.9, 1.1), 2
                    )
                    # Occasional renames exercise first-row metadata selection.
                    card_name = f"Card {product_id}" if rng.random() < 0.9 else f"Card {product_id} alt"
                    rows.append((product_id, printing, card_name, set_name, date, price))
        rng.shuffle(rows)
        self.insert_rows(rows)
        for product_id in range(1000, 1005):
            self.insert_series(product_id, f"Other {product_id}", [30.0] * 9, set_name="Other Relevant")
        self.add_relevant_set_support()

    def assert_matches_reference(self, calculate, mover_type, limit):
        actual = calculate(self.db_path, limit=limit)
        expected = reference_printing_movers(self.db_path, limit, mover_type)
        self.assertFalse(expected.empty)
        pd.testing.assert_frame_equal(actual, expected)

    def test_gainers_match_reference(self):
        self.build_randomized_history()
        self.assert_matches_reference(calculate_top_gainers, "gainer", 50)
        self.assert_matches_reference(calculate_top_gainers, "gainer", 10_000)

    def test_losers_match_reference(self):
        self.build_randomized_history()
        self.assert_matches_reference(calculate_top_losers, "loser", 50)
        self.assert_matches_reference(calculate_top_losers, "loser", 10_000)

    def test_penny_movers_match_reference(self):
        self.build_randomized_history()
        self.assert_matches_reference(calculate_penny_movers, "penny", 50)
        self.assert_matches_reference(calculate_penny_movers, "penny", 10_000)

    def test_spike_status_and_printing_separation(self):
        self.add_relevant_set_support()
        base = [10.0, 10.0, 10.0, 99.0, 99.0, 99.0]
        self.insert_series(1, "Gain Card", base + [20.0, 20.0, 20.0])
        self.insert_series(1, "Gain Card", base + [12.0, 12.0, 40.0], "1st Edition")
        self.insert_series(1, "Gain Card", base + [10.5, 10.5, 13.0], "Limited")
        drop_base = [20.0, 20.0, 20.0, 1.0, 1.0, 1.0]
        self.insert_series(2, "Loss Card", drop_base + [10.0, 10.0, 10.0])
        self.insert_series(2, "Loss Card", drop_base + [15.0, 15.0, 5.0], "1st Edition")
        penny_base = [1.0, 1.0, 1.0, 99.0, 99.0, 99.0]
        self.insert_series(3, "Penny Card", penny_base + [1.5, 1.5, 1.5])
        self.insert_series(3, "Penny Card", penny_base + [1.5, 1.5, 4.0], "1st Edition")

        def status_by_printing(frame, product_id):
            rows = frame[frame["product_id"] == product_id]
            return dict(zip(rows["printing"], rows["status"]))

        gainers = calculate_top_gainers(self.db_path)
        losers = calculate_top_losers(self.db_path)
        penny_movers = calculate_penny_movers(self.db_path)

        self.assertEqual(status_by_printing(gainers, 1), {
            "Unlimited": "CONFIRMED",
            "1st Edition": "UNCONFIRMED",
            "Limited": "UNCONFIRMED",
        })
        self.assertEqual(status_by_printing(losers, 2), {
            "Unlimited": "CONFIRMED",
            "1st Edition": "UNCONFIRMED",
        })
        self.assertEqual(status_by_printing(penny_movers, 3), {
            "Unlimited": "CONFIRMED",
            "1st Edition": "UNCONFIRMED",
        })

        first_edition = gainers[(gainers["product_id"] == 1) & (gainers["printing"] == "1st Edition")].iloc[0]
        self.assertEqual((first_edition["baseline_value"], first_edition["current_value"]), (10.0, 12.0))
        self.assertEqual(list(gainers.columns), [
            "rank", "dollar_gain", "percent_gain", "product_id", "printing",
            "card_name", "set_name", "baseline_value", "current_value", "status",
        ])
        self.assertEqual(list(losers.columns), [
            "rank", "dollar_change", "percent_change", "product_id", "printing",
            "card_name", "set_name", "baseline_value", "current_value", "status",
        ])

        for calculate, mover_type in (
            (calculate_top_gainers, "gainer"),
            (calculate_top_losers, "loser"),
            (calculate_penny_movers, "penny"),
        ):
            pd.testing.assert_frame_equal(
                calculate(self.db_path, limit=50),
                reference_printing_movers(self.db_path, 50, mover_type),
            )


if __name__ == "__main__":
    unittest.main()
