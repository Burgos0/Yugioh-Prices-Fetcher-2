import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.analysis import (
    apply_release_scope,
    calculate_early_movers,
    calculate_early_mover_candidates,
    early_movers_cache_path,
    RELEASE_SCOPE_ALL_YEARS,
    RELEASE_SCOPE_2007_ONWARD,
)
from scripts.replay_early_movers import build_control_groups


def cache_record(group_id, name, release_date):
    return {"group_id": group_id, "name": name, "release_date": release_date,
            "source": "test-fixture", "fetched_at": "2026-01-01T00:00:00Z"}


class ReleaseScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices = str(Path(self.tmp.name) / 'prices.db')
        self.cache_path = str(Path(self.tmp.name) / 'set_release_dates.json')
        with sqlite3.connect(self.prices) as conn:
            conn.execute('CREATE TABLE prices (product_id INTEGER, card_name TEXT, '
                         'set_name TEXT, date TEXT, market_price REAL)')

    def write_cache(self, records):
        with open(self.cache_path, 'w') as f:
            json.dump({r['group_id']: r for r in records}, f)

    def insert(self, product, set_name, readings, card_name=None):
        with sqlite3.connect(self.prices) as conn:
            conn.executemany('INSERT INTO prices VALUES (?, ?, ?, ?, ?)',
                             [(product, card_name or f'Card{product}', set_name, d, p)
                              for d, p in readings])

    def make_relevant(self, set_name, id_base):
        for i in range(5):
            self.insert(id_base + i, set_name,
                        [('2026-08-28', 5.0), ('2026-08-29', 5.0), ('2026-08-30', 5.0),
                         ('2026-08-31', 5.0), ('2026-09-01', 5.0)])

    def momentum_reading(self, product, set_name, card_name=None):
        self.insert(product, set_name,
                    [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)],
                    card_name=card_name)

    # --- scope default: all years ---

    def test_all_years_is_the_default(self):
        self.momentum_reading(1, 'OldSet2006')
        self.make_relevant('OldSet2006', 900)
        self.write_cache([cache_record('1', 'OldSet2006', '2006-12-31')])

        # No release_scope passed -- must default to "all_years" and include
        # the pre-2007 set rather than require an explicit opt-in.
        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=self.cache_path)
        self.assertEqual(movers.product_id.tolist(), [1])

        _, stats = calculate_early_mover_candidates(
            self.prices, as_of='2026-09-04', release_date_cache_path=self.cache_path,
            return_scope_stats=True)
        self.assertEqual(stats['release_scope'], RELEASE_SCOPE_ALL_YEARS)
        self.assertEqual(stats['excluded_pre_scope'], 0)

    def test_all_years_includes_unknown_dates_but_reports_them(self):
        self.momentum_reading(1, 'UncachedSet')
        self.make_relevant('UncachedSet', 900)
        self.write_cache([])  # nothing cached for this set at all

        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=self.cache_path)
        self.assertEqual(movers.product_id.tolist(), [1])  # included despite unknown date

        _, stats = calculate_early_mover_candidates(
            self.prices, as_of='2026-09-04', release_date_cache_path=self.cache_path,
            return_scope_stats=True)
        self.assertEqual(stats['excluded_unknown'], 0)
        self.assertEqual(stats['eligible_unknown_date'], 1)
        self.assertEqual(stats['unknown_set_names'], [])  # nothing was excluded for being unknown

    def test_2007_onward_is_an_optional_filter(self):
        self.momentum_reading(1, 'OldSet2006')
        self.momentum_reading(2, 'NewSet2007')
        self.make_relevant('OldSet2006', 900)
        self.make_relevant('NewSet2007', 950)
        self.write_cache([
            cache_record('1', 'OldSet2006', '2006-12-31'),
            cache_record('2', 'NewSet2007', '2007-01-01'),
        ])

        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=self.cache_path,
                                         release_scope=RELEASE_SCOPE_2007_ONWARD)
        self.assertEqual(movers.product_id.tolist(), [2])

        candidates, stats = calculate_early_mover_candidates(
            self.prices, as_of='2026-09-04', release_date_cache_path=self.cache_path,
            release_scope=RELEASE_SCOPE_2007_ONWARD, return_scope_stats=True)
        self.assertEqual(stats['excluded_pre_scope'], 1)  # only the OldSet2006 momentum candidate
        self.assertNotIn(1, candidates.product_id.tolist())
        self.assertIn(2, candidates.product_id.tolist())

    def test_2007_onward_excludes_unknown_release_dates(self):
        self.momentum_reading(1, 'UncachedSet')
        self.make_relevant('UncachedSet', 900)
        self.write_cache([])  # nothing cached for this set at all

        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=self.cache_path,
                                         release_scope=RELEASE_SCOPE_2007_ONWARD)
        self.assertTrue(movers.empty)

        _, stats = calculate_early_mover_candidates(
            self.prices, as_of='2026-09-04', release_date_cache_path=self.cache_path,
            release_scope=RELEASE_SCOPE_2007_ONWARD, return_scope_stats=True)
        self.assertEqual(stats['excluded_unknown'], 1)
        self.assertEqual(stats['unknown_set_names'], ['UncachedSet'])
        self.assertEqual(stats['eligible'], 0)

    def test_newer_reprint_of_older_card_is_included_under_2007_onward(self):
        # Same card name, printed in a set released well after 2007 -- the
        # printing's set release date governs, not the card's debut year.
        self.momentum_reading(1, 'Vintage Reprint Tin 2024', card_name='Blue-Eyes White Dragon')
        self.make_relevant('Vintage Reprint Tin 2024', 900)
        self.write_cache([cache_record('1', 'Vintage Reprint Tin 2024', '2024-03-01')])

        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=self.cache_path,
                                         release_scope=RELEASE_SCOPE_2007_ONWARD)
        self.assertEqual(movers.product_id.tolist(), [1])

    def test_ambiguous_set_name_always_excluded_regardless_of_scope(self):
        # Two different group ids sharing one set name must never be
        # silently joined to either group's release date, in either scope.
        self.momentum_reading(1, 'DuplicateName')
        self.make_relevant('DuplicateName', 900)
        self.write_cache([
            cache_record('1', 'DuplicateName', '2010-01-01'),
            cache_record('2', 'DuplicateName', '2020-01-01'),
        ])

        for scope in (RELEASE_SCOPE_ALL_YEARS, RELEASE_SCOPE_2007_ONWARD):
            _, stats = calculate_early_mover_candidates(
                self.prices, as_of='2026-09-04', release_date_cache_path=self.cache_path,
                release_scope=scope, return_scope_stats=True)
            self.assertEqual(stats['excluded_ambiguous'], 1, scope)
            self.assertEqual(stats['ambiguous_set_names'], ['DuplicateName'], scope)

    def test_future_release_date_excluded_regardless_of_scope(self):
        # A set "released" after the replay's as_of date must be excluded
        # during replay in either scope -- no look-ahead.
        self.momentum_reading(1, 'NotYetReleased')
        self.make_relevant('NotYetReleased', 900)
        self.write_cache([cache_record('1', 'NotYetReleased', '2026-09-05')])  # one day after as_of

        for scope in (RELEASE_SCOPE_ALL_YEARS, RELEASE_SCOPE_2007_ONWARD):
            movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                             release_date_cache_path=self.cache_path,
                                             release_scope=scope)
            self.assertTrue(movers.empty, scope)

            _, stats = calculate_early_mover_candidates(
                self.prices, as_of='2026-09-04', release_date_cache_path=self.cache_path,
                release_scope=scope, return_scope_stats=True)
            self.assertEqual(stats['excluded_future'], 1, scope)

    def test_missing_cache_file_2007_onward_is_explicit_unknown_not_guessed(self):
        self.momentum_reading(1, 'AnySet')
        self.make_relevant('AnySet', 900)
        missing_cache_path = str(Path(self.tmp.name) / 'does_not_exist.json')

        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=missing_cache_path,
                                         release_scope=RELEASE_SCOPE_2007_ONWARD)
        self.assertTrue(movers.empty)

        _, stats = calculate_early_mover_candidates(
            self.prices, as_of='2026-09-04', release_date_cache_path=missing_cache_path,
            release_scope=RELEASE_SCOPE_2007_ONWARD, return_scope_stats=True)
        self.assertFalse(stats['cache_present'])
        self.assertEqual(stats['excluded_unknown'], 1)

    def test_missing_cache_file_all_years_still_includes_products(self):
        self.momentum_reading(1, 'AnySet')
        self.make_relevant('AnySet', 900)
        missing_cache_path = str(Path(self.tmp.name) / 'does_not_exist.json')

        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=missing_cache_path)
        self.assertEqual(movers.product_id.tolist(), [1])

    def test_scope_passed_consistently_to_alerts_and_controls(self):
        # The alert and its control-group pool must be built from the exact
        # same post-scope candidate set (requirement: apply scope BEFORE
        # ranking/selecting either group), under the selected scope.
        self.momentum_reading(1, 'InScopeSet')
        self.insert(10, 'InScopeSet', [('2026-09-01', 10), ('2026-09-02', 10),
                                        ('2026-09-03', 10), ('2026-09-04', 10)])  # in-band control candidate
        self.insert(11, 'OutOfScopeSet', [('2026-09-01', 10), ('2026-09-02', 10),
                                           ('2026-09-03', 10), ('2026-09-04', 10)])
        self.make_relevant('InScopeSet', 900)
        self.make_relevant('OutOfScopeSet', 950)
        self.write_cache([
            cache_record('1', 'InScopeSet', '2010-01-01'),
            cache_record('2', 'OutOfScopeSet', '2001-01-01'),
        ])

        movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                         release_date_cache_path=self.cache_path,
                                         release_scope=RELEASE_SCOPE_2007_ONWARD)
        candidates = calculate_early_mover_candidates(
            self.prices, as_of='2026-09-04', release_date_cache_path=self.cache_path,
            release_scope=RELEASE_SCOPE_2007_ONWARD)
        matches, _ = build_control_groups(candidates, movers)

        self.assertEqual(movers.product_id.tolist(), [1])
        # Control pool must never include the out-of-scope set's filler cards.
        self.assertNotIn('OutOfScopeSet', candidates.set_name.tolist())
        control_ids = [c['product_id'] for c in matches[0]['controls']]
        self.assertEqual(control_ids, [10])

    # --- labeled, separate display caches ---

    def test_early_movers_cache_paths_are_distinct_and_labeled(self):
        all_years_path = early_movers_cache_path(RELEASE_SCOPE_ALL_YEARS)
        scoped_path = early_movers_cache_path(RELEASE_SCOPE_2007_ONWARD)
        self.assertNotEqual(all_years_path, scoped_path)
        self.assertIn('all_years', all_years_path)
        self.assertIn('2007_onward', scoped_path)
        with self.assertRaises(ValueError):
            early_movers_cache_path('not_a_real_scope')


if __name__ == '__main__':
    unittest.main()

