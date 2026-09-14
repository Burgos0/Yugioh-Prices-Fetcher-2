import sqlite3
import tempfile
import unittest
import json
from pathlib import Path

from app.analysis import calculate_early_mover_candidates, calculate_early_movers
from scripts.replay_early_movers import build_control_groups, compare_outcomes, find_reused_controls


class ReplayControlGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices = str(Path(self.tmp.name) / 'prices.db')
        with sqlite3.connect(self.prices) as conn:
            conn.execute('CREATE TABLE prices (product_id INTEGER, card_name TEXT, '
                         'set_name TEXT, date TEXT, market_price REAL)')
        # Isolated release-date cache: these tests use fake set names, not
        # real TCGCSV groups, so give each one a valid in-scope release date
        # rather than depend on (or pollute) the real production cache.
        self.release_cache = str(Path(self.tmp.name) / 'set_release_dates.json')
        with open(self.release_cache, 'w') as f:
            json.dump({
                str(i): {'group_id': str(i), 'name': name, 'release_date': '2007-01-01',
                          'source': 'test-fixture', 'fetched_at': '2026-01-01T00:00:00Z'}
                for i, name in enumerate(['SetA', 'SetB', 'Lonely'], start=1)
            }, f)

    def insert(self, product, set_name, readings):
        with sqlite3.connect(self.prices) as conn:
            conn.executemany('INSERT INTO prices VALUES (?, ?, ?, ?, ?)',
                             [(product, f'Card{product}', set_name, d, p) for d, p in readings])

    def make_relevant(self, set_name, id_base):
        """Pad a set with cheap filler cards so calculate_relevant_sets marks it relevant
        (>=5 cards worth $3+) without affecting the momentum candidates under test."""
        for i in range(5):
            pid = id_base + i
            self.insert(pid, set_name, [('2026-08-28', 5.0), ('2026-08-29', 5.0),
                                        ('2026-08-30', 5.0), ('2026-08-31', 5.0),
                                        ('2026-09-01', 5.0)])

    def test_future_data_never_drives_control_selection(self):
        # Alert: momentum-qualifying card at $10 on 2026-09-04.
        self.insert(1, 'SetA', [('2026-09-01', 8), ('2026-09-02', 8),
                                 ('2026-09-03', 9), ('2026-09-04', 10)])
        # Candidate A: starting price further from $10 but a huge future return.
        self.insert(2, 'SetA', [('2026-09-01', 12), ('2026-09-02', 12),
                                 ('2026-09-03', 11.6), ('2026-09-04', 11.6),
                                 ('2026-09-07', 100)])
        # Candidate B: starting price closer to $10, but a flat/negative future return.
        self.insert(3, 'SetA', [('2026-09-01', 10.2), ('2026-09-02', 10.2),
                                 ('2026-09-03', 10.1), ('2026-09-04', 10.1),
                                 ('2026-09-07', 1)])
        self.make_relevant('SetA', 900)

        movers = calculate_early_movers(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        candidates = calculate_early_mover_candidates(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        self.assertEqual(movers.product_id.tolist(), [1])

        matches, unmatched = build_control_groups(candidates, movers)
        self.assertEqual(unmatched, [])
        self.assertEqual(len(matches), 1)
        # Candidate 3 is closer in *starting* price and must be picked first,
        # even though candidate 2 has the much bigger future return.
        control_ids = [c['product_id'] for c in matches[0]['controls']]
        self.assertEqual(control_ids[0], 3)
        self.assertIn(2, control_ids)

    def test_matching_same_set_price_band_and_deterministic_order(self):
        self.insert(1, 'SetA', [('2026-09-01', 8), ('2026-09-02', 8),
                                 ('2026-09-03', 9), ('2026-09-04', 10)])
        # In-band candidates (within +/-20% of $10, i.e. [8, 12]).
        for pid, price in [(9, 9.8), (10, 8.5), (11, 9.5), (12, 10.5), (13, 11.5), (14, 12.0)]:
            self.insert(pid, 'SetA', [('2026-09-01', price), ('2026-09-02', price),
                                       ('2026-09-03', price), ('2026-09-04', price)])
        # Out-of-band candidate (>20% away) must never be selected.
        self.insert(15, 'SetA', [('2026-09-01', 20), ('2026-09-02', 20),
                                  ('2026-09-03', 20), ('2026-09-04', 20)])
        # Different set: same price, must not be selected ("same set" requirement).
        self.insert(16, 'SetB', [('2026-09-01', 10), ('2026-09-02', 10),
                                  ('2026-09-03', 10), ('2026-09-04', 10)])
        self.make_relevant('SetA', 900)
        self.make_relevant('SetB', 950)

        movers = calculate_early_movers(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        candidates = calculate_early_mover_candidates(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        matches, unmatched = build_control_groups(candidates, movers)

        self.assertEqual(unmatched, [])
        controls = matches[0]['controls']
        self.assertEqual(len(controls), 5)  # capped at 5 even though 6 candidates are in-band
        control_ids = [c['product_id'] for c in controls]
        self.assertNotIn(15, control_ids)
        self.assertNotIn(16, control_ids)
        # Distances from $10: 9->0.2, 11->0.5, 12->0.5, 10->1.5, 13->1.5, 14->2.0.
        # Closest-price-first, ties broken by product_id; 14 is dropped by the cap.
        self.assertEqual(control_ids, [9, 11, 12, 10, 13])

    def test_unmatched_alert_and_reused_controls_reported(self):
        # Alert with no in-set/in-band candidates at all.
        self.insert(1, 'Lonely', [('2026-09-01', 8), ('2026-09-02', 8),
                                   ('2026-09-03', 9), ('2026-09-04', 10)])
        self.make_relevant('Lonely', 900)
        # Two alerts in SetA sharing the only eligible control.
        self.insert(2, 'SetA', [('2026-09-01', 8), ('2026-09-02', 8),
                                 ('2026-09-03', 9), ('2026-09-04', 10)])
        self.insert(3, 'SetA', [('2026-09-01', 8), ('2026-09-02', 8),
                                 ('2026-09-03', 9), ('2026-09-04', 10)])
        self.insert(20, 'SetA', [('2026-09-01', 10), ('2026-09-02', 10),
                                  ('2026-09-03', 10), ('2026-09-04', 10)])
        self.make_relevant('SetA', 950)

        movers = calculate_early_movers(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        candidates = calculate_early_mover_candidates(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        matches, unmatched = build_control_groups(candidates, movers)

        self.assertEqual([a['product_id'] for a in unmatched], [1])
        reused = find_reused_controls(matches)
        self.assertEqual(reused, {20: 2})

    def test_missing_and_pending_outcomes_excluded_from_comparison(self):
        self.insert(1, 'SetA', [('2026-09-01', 8), ('2026-09-02', 8),
                                 ('2026-09-03', 9), ('2026-09-04', 10)])
        self.insert(2, 'SetA', [('2026-09-01', 10), ('2026-09-02', 10),
                                 ('2026-09-03', 10), ('2026-09-04', 10)])
        self.make_relevant('SetA', 900)
        # Alert has a valid 3-day return; control has no price at day 3 (unavailable).
        self.insert(1, 'SetA', [('2026-09-07', 12)])

        movers = calculate_early_movers(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        candidates = calculate_early_mover_candidates(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        matches, _ = build_control_groups(candidates, movers)

        comparison = compare_outcomes(self.prices, matches, '2026-09-04')
        # Horizon 3 (2026-09-07): alert ok, control has no row -> unavailable -> excluded.
        self.assertEqual(comparison[3]['alerts']['ok'], 1)
        self.assertEqual(comparison[3]['controls']['unavailable'], 1)
        self.assertEqual(comparison[3]['excluded_alerts'], 1)
        self.assertIsNone(comparison[3]['alerts']['median_return'])
        # Horizon 14 is beyond the latest price date -> pending for both.
        self.assertEqual(comparison[14]['alerts']['pending'], 1)
        self.assertEqual(comparison[14]['controls']['pending'], 1)
        self.assertEqual(comparison[14]['excluded_alerts'], 1)

    def test_equal_weighting_across_control_group_sizes(self):
        # Alert A has 1 valid control returning +10%.
        self.insert(1, 'SetA', [('2026-09-01', 8), ('2026-09-02', 8),
                                 ('2026-09-03', 9), ('2026-09-04', 10)])
        self.insert(10, 'SetA', [('2026-09-01', 10), ('2026-09-02', 10),
                                  ('2026-09-03', 10), ('2026-09-04', 10),
                                  ('2026-09-07', 11)])
        # Alert B has 3 valid controls all returning +90% (median 90%).
        self.insert(2, 'SetB', [('2026-09-01', 8), ('2026-09-02', 8),
                                 ('2026-09-03', 9), ('2026-09-04', 10)])
        for pid in (20, 21, 22):
            self.insert(pid, 'SetB', [('2026-09-01', 10), ('2026-09-02', 10),
                                       ('2026-09-03', 10), ('2026-09-04', 10),
                                       ('2026-09-07', 19)])
        self.insert(1, 'SetA', [('2026-09-07', 11)])
        self.insert(2, 'SetB', [('2026-09-07', 11)])
        self.make_relevant('SetA', 900)
        self.make_relevant('SetB', 950)

        movers = calculate_early_movers(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        candidates = calculate_early_mover_candidates(self.prices, as_of='2026-09-04', release_date_cache_path=self.release_cache)
        matches, _ = build_control_groups(candidates, movers)
        comparison = compare_outcomes(self.prices, matches, '2026-09-04')

        # Equal weight per alert: median of [10%, 90%] = 50%, not the 3-vs-1
        # pooled median that would favor alert B's larger control group.
        self.assertEqual(comparison[3]['controls']['compared'], 2)
        self.assertEqual(comparison[3]['controls']['median_return'], 50.0)


if __name__ == '__main__':
    unittest.main()
