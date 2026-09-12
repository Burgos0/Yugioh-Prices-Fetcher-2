"""Research replay; never writes to the live signal or price databases.

Usage: python -m scripts.replay_early_movers --as-of 2026-09-04
Uses only prices through the selected date to select candidates. Outcomes use
later exact-date prices. Historical research is not a live prediction record.
"""
import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from app.analysis import calculate_early_movers, calculate_early_movers_backtest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--prices-db', default='data/prices.db')
    args = parser.parse_args()
    movers = calculate_early_movers(args.prices_db, as_of=args.as_of)
    with tempfile.TemporaryDirectory() as directory:
        signals_path = str(Path(directory) / 'replay.db')
        with sqlite3.connect(signals_path) as conn:
            conn.execute('''CREATE TABLE early_mover_signals
                (product_id INTEGER, card_name TEXT, set_name TEXT,
                 signal_date TEXT, signal_price REAL)''')
            for row in movers.to_dict('records'):
                conn.execute('INSERT INTO early_mover_signals VALUES (?, ?, ?, ?, ?)',
                             (row['product_id'], row['card_name'], row['set_name'],
                              args.as_of, row['latest_price']))
        result = calculate_early_movers_backtest(args.prices_db, signals_path)
    print(json.dumps({
        'mode': 'historical research replay, not live predictions',
        'method': 'existing momentum rule with consecutive-date requirement',
        'as_of': args.as_of,
        'signals': len(movers),
        'target': 'at least 20% AND $1 above signal price at the exact horizon',
        'summary': result['summary'],
        'limitations': 'No control group; no execution costs; small historical sample. '
                       'Does not establish predictive advantage.',
    }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
