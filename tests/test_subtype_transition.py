import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.analysis import (
    calculate_early_movers,
    calculate_early_mover_candidates,
    SUBTYPE_MODE_LEGACY_UNVERIFIED,
    SUBTYPE_MODE_VERIFIED,
)


class SubtypeTransitionTests(unittest.TestCase):
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

    def set_established(self, product_id, established_date):
        with sqlite3.connect(self.prices) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS product_subtypes '
                         '(product_id INTEGER PRIMARY KEY, subtype TEXT, established_date TEXT)')
            conn.execute('INSERT OR REPLACE INTO product_subtypes VALUES (?, ?, ?)',
                         (product_id, '1st Edition', established_date))

    def test_no_product_subtypes_table_excludes_everything_in_verified_mode(self):
        # Purely legacy database: subtype tracking never ran at all.
        self.insert(1, [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)])
        self.make_relevant()

        legacy_movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                                subtype_mode=SUBTYPE_MODE_LEGACY_UNVERIFIED)
        verified_movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                                  subtype_mode=SUBTYPE_MODE_VERIFIED)

        self.assertEqual(legacy_movers.product_id.tolist(), [1])
        self.assertTrue(verified_movers.empty)

    def test_mixed_legacy_and_verified_window_excluded_from_production_alerts(self):
        # Subtype tracking started on 2026-09-03 -- the window's earliest
        # required date (2026-09-02) is legacy, so this must never be
        # compared against the newly-tracked 2026-09-03/04 prices.
        self.insert(1, [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)])
        self.make_relevant()
        self.set_established(1, '2026-09-03')

        verified_movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                                  subtype_mode=SUBTYPE_MODE_VERIFIED)
        self.assertTrue(verified_movers.empty)

    def test_fully_verified_window_is_included_in_production_alerts(self):
        # Subtype tracking established on/before the earliest of the 4 valid
        # readings the detector requires -- all of them are consistently
        # from the same verified subtype.
        self.insert(1, [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)])
        self.make_relevant()
        self.set_established(1, '2026-09-01')  # on/before the earliest of the 4 required readings

        verified_movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                                  subtype_mode=SUBTYPE_MODE_VERIFIED)
        self.assertEqual(verified_movers.product_id.tolist(), [1])

    def test_full_four_reading_history_required_not_just_three(self):
        # The original detector requires >= 4 valid readings (not just the 3
        # used in the momentum comparison) before a product even qualifies.
        # Verified mode must apply that same 4-reading requirement to the
        # tracked subtype, not just the 3-day comparison window.
        self.insert(1, [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)])
        self.make_relevant()
        # Established the day AFTER the 4th-from-last reading: the 3-day
        # comparison window (09-02..04) is fully verified, but the detector's
        # full 4-reading gate (09-01..04) is not -- must still be excluded.
        self.set_established(1, '2026-09-02')

        verified_movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                                  subtype_mode=SUBTYPE_MODE_VERIFIED)
        self.assertTrue(verified_movers.empty)

    def test_verified_mode_only_admits_products_with_tracked_subtype(self):
        # Two identical momentum candidates; only one has ever been subtype-tracked.
        self.insert(1, [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)])
        self.insert(2, [('2026-09-01', 8), ('2026-09-02', 8), ('2026-09-03', 9), ('2026-09-04', 10)])
        self.make_relevant()
        self.set_established(1, '2026-09-01')

        verified_movers = calculate_early_movers(self.prices, as_of='2026-09-04',
                                                  subtype_mode=SUBTYPE_MODE_VERIFIED)
        self.assertEqual(verified_movers.product_id.tolist(), [1])

        candidates = calculate_early_mover_candidates(self.prices, as_of='2026-09-04',
                                                       subtype_mode=SUBTYPE_MODE_VERIFIED)
        self.assertNotIn(2, candidates.product_id.tolist())


if __name__ == '__main__':
    unittest.main()
