import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scripts.fetch_prices as fetch_prices


GROUPS = [{"groupId": 10, "name": "Test Set"}]
PRODUCTS = [{"productId": 100, "name": "Test Card"}]
PRICES = [{
    "productId": 100,
    "subTypeName": "1st Edition",
    "lowPrice": 1.0,
    "midPrice": 2.0,
    "highPrice": 3.0,
    "marketPrice": 2.5,
    "directLowPrice": None,
}]


class LiveFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")

    @staticmethod
    def api_response(url):
        if url.endswith("/groups"):
            return GROUPS
        if url.endswith("/products"):
            return PRODUCTS
        if url.endswith("/prices"):
            return PRICES
        raise AssertionError(url)

    def test_successful_live_responses_are_written(self):
        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1), \
                patch.object(fetch_prices, "current_utc_date", return_value="2026-09-17"), \
                patch.object(fetch_prices, "fetch_json", side_effect=self.api_response), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 0)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT product_id, card_name, set_name, date FROM prices"
            ).fetchone()
        self.assertEqual(row, (100, "Test Card", "Test Set", "2026-09-17"))

    def test_http_error_is_not_retried_for_non_transient_status(self):
        response = Mock(status_code=403)
        with patch.object(fetch_prices.requests, "get", return_value=response) as get:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                fetch_prices.fetch_json("https://example.invalid/groups")
        self.assertEqual(get.call_count, 1)

    def test_partial_live_result_never_creates_database(self):
        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 2), \
                patch.object(fetch_prices, "current_utc_date", return_value="2026-09-17"), \
                patch.object(fetch_prices, "fetch_json", side_effect=self.api_response), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 1)

        self.assertFalse(Path(self.db_path).exists())

    def test_failed_group_request_stops_before_database_write(self):
        def response(url):
            if url.endswith("/groups"):
                return GROUPS
            if url.endswith("/products"):
                return PRODUCTS
            raise RuntimeError("HTTP 503")

        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "current_utc_date", return_value="2026-09-17"), \
                patch.object(fetch_prices, "fetch_json", side_effect=response), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 1)

        self.assertFalse(Path(self.db_path).exists())

    def test_inaccessible_group_is_skipped(self):
        groups = [
            {"groupId": 10, "name": "Accessible Set"},
            {"groupId": 11, "name": "Restricted Set"},
        ]

        def response(url):
            if url.endswith("/groups"):
                return groups
            if "/10/products" in url:
                return PRODUCTS
            if "/10/prices" in url:
                return PRICES
            if "/11/products" in url:
                raise RuntimeError("HTTP 401")
            if "/11/prices" in url:
                raise AssertionError("prices should not be requested for inaccessible groups")
            raise AssertionError(url)

        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1), \
                patch.object(fetch_prices, "current_utc_date", return_value="2026-09-17"), \
                patch.object(fetch_prices, "fetch_json", side_effect=response), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 0)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT product_id, card_name, set_name, date FROM prices"
            ).fetchall()
        self.assertEqual(rows, [(100, "Test Card", "Accessible Set", "2026-09-17")])

    def test_transaction_failure_rolls_back_prices_and_subtypes(self):
        records = [(100, "Card", "Set", 1.0, 2.0, 3.0, 2.5, None, "2026-09-17")]
        with patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1), \
                patch.object(
                    fetch_prices, "save_tracked_subtypes",
                    side_effect=sqlite3.OperationalError("forced failure")):
            with self.assertRaises(sqlite3.OperationalError):
                fetch_prices.write_records_atomically(
                    self.db_path, "2026-09-17", records, {100: "1st Edition"})

        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM product_subtypes").fetchone()[0], 0)

    def test_same_day_rerun_removes_products_missing_from_new_snapshot(self):
        with fetch_prices.init_db(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (100, "Current", "Set", 1.0, 2.0, 3.0, 2.5, None, "2026-09-17"),
                    (101, "Stale", "Set", 1.0, 2.0, 3.0, 2.5, None, "2026-09-17"),
                ],
            )
        records = [
            (100, "Current", "Set", 2.0, 3.0, 4.0, 3.5, None, "2026-09-17")
        ]

        with patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1):
            rows_before, daily_rows = fetch_prices.write_records_atomically(
                self.db_path, "2026-09-17", records, {})

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT product_id, market_price FROM prices WHERE date = ?",
                ("2026-09-17",),
            ).fetchall()
        self.assertEqual((rows_before, daily_rows), (2, 1))
        self.assertEqual(rows, [(100, 3.5)])

    def test_exception_after_same_day_deletion_restores_prior_rows(self):
        prior_row = (
            101, "Prior", "Set", 1.0, 2.0, 3.0, 2.5, None, "2026-09-17"
        )
        with fetch_prices.init_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", prior_row
            )
        replacement = [
            (100, "New", "Set", 2.0, 3.0, 4.0, 3.5, None, "2026-09-17")
        ]

        with patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1), \
                patch.object(
                    fetch_prices, "save_tracked_subtypes",
                    side_effect=sqlite3.OperationalError("forced failure")):
            with self.assertRaises(sqlite3.OperationalError):
                fetch_prices.write_records_atomically(
                    self.db_path, "2026-09-17", replacement, {})

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM prices WHERE date = ?", ("2026-09-17",)
            ).fetchall()
        self.assertEqual(rows, [prior_row])

    def test_fetch_crossing_utc_date_boundary_is_rejected(self):
        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "fetch_json", side_effect=self.api_response), \
                patch.object(
                    fetch_prices, "current_utc_date",
                    side_effect=["2026-09-17", "2026-09-18"]), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 1)

        self.assertFalse(Path(self.db_path).exists())


if __name__ == "__main__":
    unittest.main()
