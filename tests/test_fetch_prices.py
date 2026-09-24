import sqlite3
import tempfile
import unittest
import json
from datetime import datetime, timezone
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
}, {
    "productId": 100,
    "subTypeName": "Unlimited",
    "lowPrice": 0.8,
    "midPrice": 1.8,
    "highPrice": 2.8,
    "marketPrice": 2.3,
    "directLowPrice": None,
}]


class ArchiveFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "prices.db")

    def test_default_target_date_is_yesterday_utc(self):
        with patch.object(fetch_prices, "datetime") as date_mock:
            date_mock.now.return_value = datetime(2026, 9, 24, tzinfo=timezone.utc)
            self.assertEqual(fetch_prices.parse_target_date(), "2026-09-23")

    def test_explicit_target_date_remains_exact(self):
        self.assertEqual(fetch_prices.parse_target_date("2026-01-02"), "2026-01-02")

    def test_archive_prices_are_written_to_both_tables(self):
        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1), \
                patch.object(fetch_prices, "check_7z_available"), \
                patch.object(fetch_prices, "fetch_set_names", return_value={"10": "Test Set"}), \
                patch.object(fetch_prices, "download_archive"), \
                patch.object(fetch_prices, "extract_archive"), \
                patch.object(fetch_prices, "find_category_dir", return_value="archive/2"), \
                patch.object(fetch_prices, "parse_archive_group_prices", return_value={"10": PRICES}), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 0)

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT product_id, card_name, set_name, date FROM prices"
            ).fetchone()
            printings = conn.execute(
                "SELECT product_id, printing, market_price FROM printing_prices"
            ).fetchall()
            subtypes = conn.execute(
                "SELECT product_id, subtype FROM product_subtypes"
            ).fetchall()
        self.assertEqual(row, (100, None, "Test Set", "2026-09-23"))
        self.assertEqual(printings, [(100, "1st Edition", 2.5), (100, "Unlimited", 2.3)])
        self.assertEqual(subtypes, [(100, "1st Edition")])

    def test_http_error_is_not_retried_for_non_transient_status(self):
        response = Mock(status_code=403)
        with patch.object(fetch_prices.requests, "get", return_value=response) as get:
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                fetch_prices.fetch_json("https://example.invalid/groups")
        self.assertEqual(get.call_count, 1)

    def test_tcgcsv_requests_are_paced(self):
        responses = [Mock(status_code=200, json=lambda: GROUPS),
                     Mock(status_code=200, json=lambda: GROUPS)]
        with patch.object(fetch_prices, "_last_tcgcsv_request_at", None), \
                patch.object(fetch_prices.time, "monotonic", side_effect=[100.0, 100.1]), \
                patch.object(fetch_prices.time, "sleep") as sleep, \
                patch.object(fetch_prices.requests, "get", side_effect=responses):
            fetch_prices.fetch_json("https://tcgcsv.com/tcgplayer/2/groups")
            fetch_prices.fetch_json("https://tcgcsv.com/tcgplayer/2/groups")

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.4)

    def test_tcgcsv_401_is_terminal_without_cooldown(self):
        response = Mock(status_code=401)
        with patch.object(fetch_prices.requests, "get", return_value=response) as get, \
                patch.object(fetch_prices.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                fetch_prices.fetch_json("https://tcgcsv.com/tcgplayer/2/groups")
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_non_tcgcsv_401_is_terminal(self):
        response = Mock(status_code=401)
        with patch.object(fetch_prices.requests, "get", return_value=response) as get, \
                patch.object(fetch_prices.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                fetch_prices.fetch_json("https://example.invalid/groups")

        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_partial_archive_result_never_creates_database(self):
        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 2), \
                patch.object(fetch_prices, "check_7z_available"), \
                patch.object(fetch_prices, "fetch_set_names", return_value={"10": "Test Set"}), \
                patch.object(fetch_prices, "download_archive"), \
                patch.object(fetch_prices, "extract_archive"), \
                patch.object(fetch_prices, "find_category_dir", return_value="archive/2"), \
                patch.object(fetch_prices, "parse_archive_group_prices", return_value={"10": PRICES}), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 1)

        self.assertFalse(Path(self.db_path).exists())

    def test_archive_download_failure_stops_before_database_write(self):
        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "check_7z_available"), \
                patch.object(fetch_prices, "fetch_set_names", return_value={}), \
                patch.object(fetch_prices, "download_archive", side_effect=RuntimeError("HTTP 503")), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices"]):
            self.assertEqual(fetch_prices.main(), 1)

        self.assertFalse(Path(self.db_path).exists())

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
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM printing_prices").fetchone()[0], 0)
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

    def test_archive_parser_loads_group_prices_and_only_resolves_unknown_group(self):
        category_dir = Path(self.tmp.name) / "2"
        (category_dir / "10").mkdir(parents=True)
        (category_dir / "11").mkdir()
        with (category_dir / "10" / "prices").open("w") as price_file:
            json.dump({"results": PRICES}, price_file)

        known_cards = {}
        with patch.object(fetch_prices, "fetch_json", return_value=PRODUCTS) as fetch:
            parsed = fetch_prices.parse_archive_group_prices(str(category_dir), known_cards)

        self.assertEqual(parsed, {"10": PRICES})
        fetch.assert_called_once_with("https://tcgcsv.com/tcgplayer/2/10/products")
        self.assertEqual(known_cards[100], "Test Card")


if __name__ == "__main__":
    unittest.main()
