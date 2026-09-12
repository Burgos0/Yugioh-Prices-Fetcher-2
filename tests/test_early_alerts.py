import sqlite3
import tempfile
import unittest
import pandas as pd
from pathlib import Path
from unittest.mock import patch

from app.analysis import calculate_early_movers, calculate_early_movers_backtest, calculate_relevant_sets


class EarlyAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices = str(Path(self.tmp.name) / 'prices.db')
        self.signals = str(Path(self.tmp.name) / 'signals.db')
        with sqlite3.connect(self.prices) as conn:
            conn.execute('CREATE TABLE prices (product_id INTEGER, card_name TEXT, '
                         'set_name TEXT, date TEXT, market_price REAL)')
        with sqlite3.connect(self.signals) as conn:
            conn.execute('CREATE TABLE early_mover_signals (product_id INTEGER, '
                         'card_name TEXT, set_name TEXT, signal_date TEXT, signal_price REAL)')

    def prices_for(self, product, readings):
        with sqlite3.connect(self.prices) as conn:
            conn.executemany('INSERT INTO prices VALUES (?, ?, ?, ?, ?)',
                             [(product, 'Card', 'Test', d, p) for d, p in readings])

    def test_replay_excludes_future_and_stale_readings(self):
        readings = [('2026-09-01', 10), ('2026-09-02', 10),
                    ('2026-09-03', 11), ('2026-09-04', 12)]
        self.prices_for(1, readings + [('2026-09-05', 100)])
        self.prices_for(2, [('2026-08-30', 9)] + readings[:3])
        self.prices_for(3, [('2026-08-30', 9), readings[0], readings[1], readings[3]])
        def relevant(df):
            self.assertLessEqual(str(df.date.max().date()), '2026-09-04')
            return {'Test'}
        with patch('app.analysis.calculate_relevant_sets', side_effect=relevant):
            result = calculate_early_movers(self.prices, as_of='2026-09-04')
        self.assertEqual(result.product_id.tolist(), [1])
        self.assertEqual(result.latest_price.tolist(), [12])

    def test_outcomes_require_dollars_and_percent_and_exact_dates(self):
        with sqlite3.connect(self.signals) as conn:
            conn.executemany('INSERT INTO early_mover_signals VALUES (?, ?, ?, ?, ?)',
                             [(i, 'Card', 'Test', '2026-09-01', p)
                              for i, p in [(1, 10), (2, 1), (3, 10), (4, 10), (5, 0)]])
        self.prices_for(1, [('2026-09-04', 12)])
        self.prices_for(2, [('2026-09-04', 1.5)])
        self.prices_for(3, [('2026-09-05', 20)])
        self.prices_for(4, [('2026-09-04', 0)])
        self.prices_for(5, [('2026-09-04', 10)])
        result = calculate_early_movers_backtest(self.prices, self.signals)
        summary = result['summary'][3]
        self.assertEqual(summary['count'], 2)
        self.assertEqual(summary['jump_count'], 1)
        self.assertEqual(summary['unavailable'], 3)
        self.assertEqual(summary['median_return'], 35)
        self.assertEqual(result['summary'][7]['pending'], 4)

    def test_empty_prices(self):
        result = calculate_early_movers_backtest(self.prices, self.signals)
        self.assertEqual(result['summary'][3]['count'], 0)
        self.assertEqual(result['summary'][3]['pending'], 0)
        self.assertTrue(calculate_early_movers(self.prices).empty)

    def test_relevance_uses_median_for_dense_and_latest_for_sparse(self):
        rows = []
        # A final spike alone must not qualify a card with dense history.
        for day in range(1, 9):
            rows.append((1, 'Dense', f'2026-09-{day:02}', 30 if day == 8 else 1))
        # Sparse history uses its latest valid observation.
        rows += [(2, 'Sparse', '2026-09-07', 1), (2, 'Sparse', '2026-09-08', 25),
                 (3, 'Old', '2026-08-01', 50), (4, 'Null', '2026-09-08', None)]
        frame = pd.DataFrame(rows, columns=['product_id', 'set_name', 'date', 'market_price'])
        self.assertEqual(calculate_relevant_sets(frame), {'Sparse'})


if __name__ == '__main__':
    unittest.main()
