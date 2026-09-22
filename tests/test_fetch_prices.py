import sqlite3
import tempfile
import unittest
import json
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

    def write_archive_tree(self, extract_dir):
        group_dir = Path(extract_dir) / "2026-09-17" / "2" / "10"
        group_dir.mkdir(parents=True)
        (group_dir / "prices").write_text(json.dumps({"results": PRICES}))

    def test_default_date_is_yesterday_utc(self):
        with patch.object(fetch_prices, "current_utc_date", return_value="2026-09-18"):
            self.assertEqual(fetch_prices.parse_target_date(), "2026-09-17")

    def test_explicit_date_remains_exact(self):
        with patch.object(fetch_prices, "current_utc_date", return_value="2026-09-18"):
            self.assertEqual(
                fetch_prices.parse_target_date("2024-02-29"), "2024-02-29")

    def test_successful_archive_is_written_with_printings_and_subtype(self):
        def extract(_archive_path, extract_dir):
            self.write_archive_tree(extract_dir)

        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1), \
                patch.object(fetch_prices, "check_7z_available"), \
                patch.object(fetch_prices, "download_archive") as download, \
                patch.object(fetch_prices, "extract_archive", side_effect=extract), \
                patch.object(fetch_prices, "fetch_set_names", return_value={"10": "Test Set"}), \
                patch.object(fetch_prices, "fetch_json", return_value=PRODUCTS) as fetch_json, \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices", "2026-09-17"]):
            self.assertEqual(fetch_prices.main(), 0)

        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.args[0], "2026-09-17")
        fetch_json.assert_called_once_with(
            "https://tcgcsv.com/tcgplayer/2/10/products")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT product_id, card_name, set_name, date FROM prices"
            ).fetchone()
            printings = conn.execute(
                "SELECT product_id, printing, market_price FROM printing_prices"
            ).fetchall()
            subtype = conn.execute(
                "SELECT product_id, subtype, established_date FROM product_subtypes"
            ).fetchone()
        self.assertEqual(row, (100, "Test Card", "Test Set", "2026-09-17"))
        self.assertEqual(printings, [(100, "1st Edition", 2.5), (100, "Unlimited", 2.3)])
        self.assertEqual(subtype, (100, "1st Edition", "2026-09-17"))

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

    def test_tcgcsv_401_is_terminal(self):
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

    def test_archive_below_minimum_cannot_modify_database(self):
        def extract(_archive_path, extract_dir):
            self.write_archive_tree(extract_dir)

        prior_row = (
            999, "Prior", "Prior Set", 1.0, 2.0, 3.0, 2.5, None, "2026-09-17"
        )
        with fetch_prices.init_db(self.db_path) as conn:
            conn.execute("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", prior_row)

        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 2), \
                patch.object(fetch_prices, "check_7z_available"), \
                patch.object(fetch_prices, "download_archive"), \
                patch.object(fetch_prices, "extract_archive", side_effect=extract), \
                patch.object(fetch_prices, "fetch_set_names", return_value={"10": "Test Set"}), \
                patch.object(fetch_prices, "fetch_json", return_value=PRODUCTS), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices", "2026-09-17"]):
            self.assertEqual(fetch_prices.main(), 1)

        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT * FROM prices WHERE date = ?", ("2026-09-17",)
                ).fetchall(),
                [prior_row],
            )

    def test_metadata_failure_does_not_discard_archive(self):
        def extract(_archive_path, extract_dir):
            self.write_archive_tree(extract_dir)

        with fetch_prices.init_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (100, "Test Card", "Known Set", 1.0, 2.0, 3.0, 2.5, None,
                 "2026-09-16"),
            )

        with patch.object(fetch_prices, "DB_PATH", self.db_path), \
                patch.object(fetch_prices, "MIN_EXPECTED_DAILY_ROWS", 1), \
                patch.object(fetch_prices, "check_7z_available"), \
                patch.object(fetch_prices, "download_archive"), \
                patch.object(fetch_prices, "extract_archive", side_effect=extract), \
                patch.object(fetch_prices, "fetch_json", side_effect=RuntimeError("HTTP 401")), \
                patch.object(fetch_prices.sys, "argv", ["fetch_prices", "2026-09-17"]):
            self.assertEqual(fetch_prices.main(), 0)

        with sqlite3.connect(self.db_path) as conn:
            archived = conn.execute(
                "SELECT card_name, set_name FROM prices WHERE date = '2026-09-17'"
            ).fetchone()
            self.assertEqual(archived, ("Test Card", "Known Set"))
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM printing_prices WHERE date = '2026-09-17'"
                ).fetchone()[0],
                2,
            )

    def test_malformed_group_file_rejects_archive(self):
        group_dir = Path(self.tmp.name) / "category" / "10"
        group_dir.mkdir(parents=True)
        (group_dir / "prices").write_text("not json")

        with self.assertRaisesRegex(RuntimeError, "group 10"):
            fetch_prices.parse_archive_prices(str(group_dir.parent))

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

if __name__ == "__main__":
    unittest.main()
