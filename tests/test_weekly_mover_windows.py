import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.analysis import (
    calculate_penny_movers,
    calculate_top_gainers,
    calculate_top_losers,
)


class WeeklyMoverWindowTests(unittest.TestCase):
    SNAPSHOT_DATES = [
        "2026-09-01", "2026-09-02", "2026-09-04",
        "2026-09-05", "2026-09-08", "2026-09-09",
        "2026-09-10", "2026-09-12", "2026-09-15",
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE prices (product_id INTEGER, card_name TEXT, "
                "set_name TEXT, date TEXT, market_price REAL)"
            )

    def insert_series(self, product_id, card_name, prices):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?)",
                [
                    (product_id, card_name, "Test Set", date, price)
                    for date, price in zip(self.SNAPSHOT_DATES, prices)
                ],
            )

    def add_relevant_set_support(self):
        for product_id in range(100, 105):
            self.insert_series(product_id, f"Support {product_id}", [30.0] * 9)

    def test_missing_calendar_dates_use_actual_snapshot_windows(self):
        self.add_relevant_set_support()
        self.insert_series(1, "Gain Card", [10.0, 10.0, 10.0, 99.0, 99.0, 99.0, 20.0, 20.0, 20.0])
        self.insert_series(2, "Loss Card", [20.0, 20.0, 20.0, 99.0, 99.0, 99.0, 10.0, 10.0, 10.0])
        self.insert_series(3, "Penny Card", [1.0, 1.0, 1.0, 99.0, 99.0, 99.0, 2.0, 2.0, 2.0])

        gainers = calculate_top_gainers(self.db_path)
        losers = calculate_top_losers(self.db_path)
        penny_movers = calculate_penny_movers(self.db_path)

        gain = gainers[gainers["product_id"] == 1].iloc[0]
        loss = losers[losers["product_id"] == 2].iloc[0]
        penny = penny_movers[penny_movers["product_id"] == 3].iloc[0]
        self.assertEqual((gain["baseline_value"], gain["current_value"]), (10.0, 20.0))
        self.assertEqual((loss["baseline_value"], loss["current_value"]), (20.0, 10.0))
        self.assertEqual((penny["baseline_value"], penny["current_value"]), (1.0, 2.0))

    def test_card_missing_required_snapshot_is_excluded(self):
        self.add_relevant_set_support()
        self.insert_series(1, "Complete Gain", [10.0, 10.0, 10.0, 99.0, 99.0, 99.0, 20.0, 20.0, 20.0])
        self.insert_series(2, "Incomplete Gain", [10.0, 10.0, None, 99.0, 99.0, 99.0, 20.0, 20.0, 20.0])

        gainers = calculate_top_gainers(self.db_path)
        self.assertIn(1, gainers["product_id"].tolist())
        self.assertNotIn(2, gainers["product_id"].tolist())

        # The same completeness rule applies to loser and penny calculations.
        self.insert_series(3, "Complete Loss", [20.0, 20.0, 20.0, 99.0, 99.0, 99.0, 10.0, 10.0, 10.0])
        self.insert_series(4, "Incomplete Loss", [20.0, 20.0, None, 99.0, 99.0, 99.0, 10.0, 10.0, 10.0])
        self.insert_series(5, "Complete Penny", [1.0, 1.0, 1.0, 99.0, 99.0, 99.0, 2.0, 2.0, 2.0])
        self.insert_series(6, "Incomplete Penny", [1.0, 1.0, 1.0, 99.0, 99.0, 99.0, 2.0, None, 2.0])

        losers = calculate_top_losers(self.db_path)
        penny_movers = calculate_penny_movers(self.db_path)
        self.assertIn(3, losers["product_id"].tolist())
        self.assertNotIn(4, losers["product_id"].tolist())
        self.assertIn(5, penny_movers["product_id"].tolist())
        self.assertNotIn(6, penny_movers["product_id"].tolist())


if __name__ == "__main__":
    unittest.main()
