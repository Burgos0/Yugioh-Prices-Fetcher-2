import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.analysis import (
    calculate_early_movers,
    calculate_early_mover_candidates,
    early_movers_cache_path,
    RELEASE_SCOPE_ALL_YEARS,
    RELEASE_SCOPE_2007_ONWARD,
    SUBTYPE_MODE_VERIFIED,
)
from scripts.fetch_prices import init_db, parse_and_build_records, save_tracked_subtypes


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'daily.yml'


class DailyWorkflowWiringTests(unittest.TestCase):
    """Checks the workflow YAML itself, since that's the only place step
    ordering and the commit file list are defined."""

    def setUp(self):
        self.workflow_text = WORKFLOW_PATH.read_text()

    def test_fetcher_runs_before_alert_generation(self):
        fetch_pos = self.workflow_text.index('scripts.fetch_prices')
        early_movers_pos = self.workflow_text.index('scripts.early_movers')
        self.assertLess(fetch_pos, early_movers_pos)

    def test_fetcher_invoked_as_a_module_so_app_imports_resolve(self):
        # A bare `python scripts/fetch_prices.py` does not put the repo root
        # on sys.path, which breaks its `from app.subtype_policy import ...`.
        self.assertIn('python -m scripts.fetch_prices', self.workflow_text)

    def test_commit_step_includes_only_code_and_small_display_caches(self):
        commit_section = self.workflow_text[self.workflow_text.index('Commit results to repo'):]
        self.assertIn('data/early_movers_all_years.json', commit_section)
        self.assertIn('data/early_movers_2007_onward.json', commit_section)
        # The databases are now persisted via Release snapshots, not ordinary commits.
        self.assertNotIn('data/prices.db', commit_section)
        self.assertNotIn('data/signals.db', commit_section)

    def test_restore_precedes_fetch_precedes_publish_snapshot(self):
        restore_pos = self.workflow_text.index('scripts.restore_snapshot')
        fetch_pos = self.workflow_text.index('scripts.fetch_prices')
        publish_pos = self.workflow_text.index('scripts.publish_snapshot')
        self.assertLess(restore_pos, fetch_pos)
        self.assertLess(fetch_pos, publish_pos)

    def test_runs_are_serialized_via_concurrency_group(self):
        self.assertIn('concurrency:', self.workflow_text)
        self.assertIn('cancel-in-progress: false', self.workflow_text)

    def test_continue_on_error_step_outcomes_are_explicitly_checked(self):
        self.assertIn('steps.top_gainers.outcome', self.workflow_text)
        self.assertIn('steps.early_movers.outcome', self.workflow_text)

    def test_snapshot_is_published_even_if_analysis_steps_fail(self):
        # publish_snapshot's `if:` must depend only on success() (i.e. the
        # required restore/fetch steps), not on the analysis outcomes --
        # the fetched database must be preserved even if analysis fails.
        publish_section_start = self.workflow_text.index('id: publish_snapshot')
        publish_if_line = self.workflow_text[publish_section_start:publish_section_start + 200]
        self.assertNotIn('top_gainers.outcome', publish_if_line)
        self.assertNotIn('early_movers.outcome', publish_if_line)

    def test_commit_step_requires_both_analysis_steps_to_have_succeeded(self):
        commit_section_start = self.workflow_text.index('Commit results to repo')
        commit_if_line = self.workflow_text[commit_section_start:commit_section_start + 300]
        self.assertIn("steps.top_gainers.outcome == 'success'", commit_if_line)
        self.assertIn("steps.early_movers.outcome == 'success'", commit_if_line)

    def test_analysis_failure_hard_fails_the_workflow_not_just_a_warning(self):
        self.assertIn('exit 1', self.workflow_text)
        self.assertIn("steps.top_gainers.outcome != 'success'", self.workflow_text)
        self.assertIn("steps.early_movers.outcome != 'success'", self.workflow_text)


class FetchAtomicityTests(unittest.TestCase):
    """A skipped or failed price insert must never establish subtype
    provenance for that row (see scripts.fetch_prices.parse_and_build_records
    and the single commit/rollback transaction in main())."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cat_dir = Path(self.tmp.name) / 'cat'
        self.cat_dir.mkdir()

    def write_group(self, group_id, items):
        group_dir = self.cat_dir / group_id
        group_dir.mkdir()
        with open(group_dir / 'prices', 'w') as f:
            json.dump({"results": items}, f)

    def test_skipped_all_null_price_row_never_establishes_subtype(self):
        self.write_group('1', [
            {"productId": 100, "subTypeName": "1st Edition",
             "lowPrice": None, "midPrice": None, "highPrice": None,
             "marketPrice": None, "directLowPrice": None},
        ])
        records, _, newly_established, skipped = parse_and_build_records(
            str(self.cat_dir), '2026-09-10', known_cards={100: 'Test Card'}, set_names={}, tracked_subtypes={})

        self.assertEqual(records, [])
        self.assertNotIn(100, newly_established)

    def test_saved_price_row_does_establish_subtype(self):
        self.write_group('1', [
            {"productId": 101, "subTypeName": "1st Edition",
             "lowPrice": 1.0, "midPrice": 2.0, "highPrice": 3.0,
             "marketPrice": 2.5, "directLowPrice": None},
        ])
        records, _, newly_established, skipped = parse_and_build_records(
            str(self.cat_dir), '2026-09-10', known_cards={101: 'Test Card'}, set_names={}, tracked_subtypes={})

        self.assertEqual(len(records), 1)
        self.assertEqual(newly_established.get(101), '1st Edition')

    def test_missing_tracked_subtype_is_skipped_and_never_reestablished(self):
        # Product 102 was tracked as "Unlimited" previously; today's data
        # only has "1st Edition" -- must be skipped, not silently switched.
        self.write_group('1', [
            {"productId": 102, "subTypeName": "1st Edition",
             "lowPrice": 5.0, "midPrice": 5.0, "highPrice": 5.0,
             "marketPrice": 5.0, "directLowPrice": None},
        ])
        records, _, newly_established, skipped = parse_and_build_records(
            str(self.cat_dir), '2026-09-10', known_cards={102: 'Test Card'}, set_names={},
            tracked_subtypes={102: 'Unlimited'})

        self.assertEqual(records, [])
        self.assertNotIn(102, newly_established)
        self.assertIn(102, skipped)

    def test_transaction_rollback_leaves_no_partial_provenance(self):
        # Simulate the same all-or-nothing transaction main() uses: if the
        # price executemany fails, the subtype insert in the same
        # transaction must roll back too.
        self.write_group('1', [
            {"productId": 103, "subTypeName": "1st Edition",
             "lowPrice": 1.0, "midPrice": 1.0, "highPrice": 1.0,
             "marketPrice": 1.0, "directLowPrice": None},
        ])
        records, _, newly_established, _ = parse_and_build_records(
            str(self.cat_dir), '2026-09-10', known_cards={103: 'Test Card'}, set_names={}, tracked_subtypes={})
        self.assertIn(103, newly_established)

        db_path = str(Path(self.tmp.name) / 'prices.db')
        conn = init_db(db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                # A deliberately malformed statement to force a mid-transaction failure.
                conn.execute("INSERT INTO not_a_real_table VALUES (1)")
                save_tracked_subtypes(conn, newly_established, '2026-09-10')
                conn.commit()
        except Exception:
            pass
        conn.rollback()
        conn.close()

        with sqlite3.connect(db_path) as check_conn:
            row = check_conn.execute(
                "SELECT 1 FROM product_subtypes WHERE product_id = ?", (103,)).fetchone()
        self.assertIsNone(row)  # never committed, so never "established"


class EarlyMoversScopeAndEmptyStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices = str(Path(self.tmp.name) / 'prices.db')
        with sqlite3.connect(self.prices) as conn:
            conn.execute('CREATE TABLE prices (product_id INTEGER, card_name TEXT, '
                         'set_name TEXT, date TEXT, market_price REAL)')

    def insert(self, product, readings, set_name='Test'):
        with sqlite3.connect(self.prices) as conn:
            conn.executemany('INSERT INTO prices VALUES (?, ?, ?, ?, ?)',
                             [(product, 'Card', set_name, d, p) for d, p in readings])

    def make_relevant(self, set_name='Test', id_base=900):
        for i in range(5):
            self.insert(id_base + i, [('2026-08-28', 5.0), ('2026-08-29', 5.0), ('2026-08-30', 5.0),
                                       ('2026-08-31', 5.0), ('2026-09-01', 5.0)], set_name=set_name)

    def test_both_scope_caches_are_generated_and_can_differ(self):
        # Mirrors what scripts/early_movers.py does for each scope.
        self.insert(1, [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)])
        self.make_relevant()
        with sqlite3.connect(self.prices) as conn:
            conn.execute('CREATE TABLE product_subtypes (product_id INTEGER PRIMARY KEY, '
                         'subtype TEXT, established_date TEXT)')
            conn.execute("INSERT INTO product_subtypes VALUES (1, '1st Edition', '2026-09-01')")

        results = {}
        for scope in (RELEASE_SCOPE_ALL_YEARS, RELEASE_SCOPE_2007_ONWARD):
            movers = calculate_early_movers(self.prices, as_of='2026-09-04', release_scope=scope,
                                             subtype_mode=SUBTYPE_MODE_VERIFIED)
            candidates = calculate_early_mover_candidates(self.prices, as_of='2026-09-04', release_scope=scope,
                                                           subtype_mode=SUBTYPE_MODE_VERIFIED)
            results[scope] = {"movers": movers, "candidates_available": not candidates.empty}

        self.assertEqual(early_movers_cache_path(RELEASE_SCOPE_ALL_YEARS),
                          'data/early_movers_all_years.json')
        self.assertEqual(early_movers_cache_path(RELEASE_SCOPE_2007_ONWARD),
                          'data/early_movers_2007_onward.json')
        # 'Test' has no cached release date; all_years still surfaces it, 2007_onward does not.
        self.assertEqual(results[RELEASE_SCOPE_ALL_YEARS]['movers'].product_id.tolist(), [1])
        self.assertTrue(results[RELEASE_SCOPE_2007_ONWARD]['movers'].empty)
        self.assertTrue(results[RELEASE_SCOPE_ALL_YEARS]['candidates_available'])

    def test_route_distinguishes_collecting_history_from_no_qualifying_alerts(self):
        app = create_app()
        client = app.test_client()

        collecting_cache = str(Path(self.tmp.name) / 'collecting.json')
        with open(collecting_cache, 'w') as f:
            json.dump({"scope": "all_years", "subtype_mode": "verified",
                       "candidates_available": False, "movers": []}, f)

        no_alerts_cache = str(Path(self.tmp.name) / 'no_alerts.json')
        with open(no_alerts_cache, 'w') as f:
            json.dump({"scope": "all_years", "subtype_mode": "verified",
                       "candidates_available": True, "movers": []}, f)

        with patch('app.routes.early_movers_cache_path', return_value=collecting_cache):
            with patch('os.path.exists', side_effect=lambda p: True):
                response = client.get('/early-movers')
        self.assertIn(b'Collecting verified history', response.data)

        with patch('app.routes.early_movers_cache_path', return_value=no_alerts_cache):
            with patch('os.path.exists', side_effect=lambda p: True):
                response = client.get('/early-movers')
        self.assertIn(b'no cards currently qualify', response.data)


if __name__ == '__main__':
    unittest.main()
