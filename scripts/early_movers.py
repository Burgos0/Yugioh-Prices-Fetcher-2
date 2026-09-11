import json
import os
import sqlite3
from datetime import date
from app.analysis import calculate_early_movers

# Connect to database and calculate early movers
movers = calculate_early_movers("data/prices.db", limit=50)

# Save results to JSON (always, even if empty)
os.makedirs("data", exist_ok=True)
json_path = "data/early_movers.json"

if movers.empty:
    # Save empty list if no early movers found
    with open(json_path, 'w') as f:
        json.dump([], f, indent=2)
    print("No early movers found. Data may not be available yet.")
    print(f"Saved empty results to {json_path}")
    exit(0)

# Convert DataFrame to list of dicts and save
movers_list = movers.to_dict('records')
with open(json_path, 'w') as f:
    json.dump(movers_list, f, indent=2)

# ===== SAVE SIGNAL HISTORY =====
signal_date = date.today().isoformat()

signals_conn = sqlite3.connect("data/signals.db")
signals_conn.execute(
    '''
    CREATE TABLE IF NOT EXISTS early_mover_signals (
        product_id INTEGER,
        card_name TEXT,
        set_name TEXT,
        signal_date TEXT,
        signal_price REAL,
        percent_gain REAL,
        dollar_gain REAL,
        PRIMARY KEY (product_id, signal_date)
    )
    '''
)

signals_saved = 0
for row in movers_list:
    cursor = signals_conn.execute(
        '''
        INSERT OR IGNORE INTO early_mover_signals
        (product_id, card_name, set_name, signal_date, signal_price, percent_gain, dollar_gain)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            row['product_id'],
            row['card_name'],
            row['set_name'],
            signal_date,
            row['latest_price'],
            row['percent_gain'],
            row['dollar_gain']
        )
    )
    signals_saved += cursor.rowcount

signals_conn.commit()
signals_conn.close()

print(f"Saved {signals_saved} Early Mover signals for {signal_date}")

# ===== OUTPUT RESULTS =====
print("="*130)
print("EARLY MOVERS (Cards Showing Early Upward Momentum)")
print("="*130)
print(
    f"{'Rank':<6} {'Card Name':<40} {'Set':<35} {'2 Days Ago':<12} {'Previous':<12} "
    f"{'Current':<12} {'Gain $':<10} {'Gain %':<10}"
)
print("-"*130)

for _, row in movers.iterrows():
    print(
        f"{row['rank']:<6} "
        f"{row['card_name']:<40} "
        f"{row['set_name']:<35} "
        f"${row['price_2_days_ago']:<11.2f} "
        f"${row['previous_price']:<11.2f} "
        f"${row['latest_price']:<11.2f} "
        f"${row['dollar_gain']:<9.2f} "
        f"{row['percent_gain']:<9.2f}%"
    )

# ===== SUMMARY =====
print("\n" + "="*130)
print("SUMMARY")
print("="*130)
print(f"Analysis: latest price vs price 2 days ago, requiring building momentum")
print(f"Early movers shown: {len(movers)}")
print("="*130)
print(f"\nResults saved to: {json_path}")
