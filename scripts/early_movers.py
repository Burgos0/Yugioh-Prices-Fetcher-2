import json
import os
import sqlite3
from app.analysis import (
    calculate_early_movers,
    calculate_early_mover_candidates,
    early_movers_cache_path,
    RELEASE_SCOPE_ALL_YEARS,
    RELEASE_SCOPE_2007_ONWARD,
    SUBTYPE_MODE_VERIFIED,
)

# Production alerts must never mix unverified legacy prices with newly
# subtype-tracked prices (see app.analysis.calculate_early_mover_candidates).
# "all_years" is the default, live signal-generating scope; "2007_onward" is
# only an optional display filter and is never fed into signal history.
PRODUCTION_SCOPE = RELEASE_SCOPE_ALL_YEARS

os.makedirs("data", exist_ok=True)

movers = calculate_early_movers("data/prices.db", limit=50, release_scope=PRODUCTION_SCOPE,
                                 subtype_mode=SUBTYPE_MODE_VERIFIED)
movers_2007_onward = calculate_early_movers("data/prices.db", limit=50, release_scope=RELEASE_SCOPE_2007_ONWARD,
                                             subtype_mode=SUBTYPE_MODE_VERIFIED)

# Save both scope-labeled display caches (always, even if empty) so a page
# can never load results generated under the other scope by mistake. Each
# cache also records whether ANY product currently satisfies the full
# verified-subtype history requirement, so the empty-state UI can tell
# "still collecting verified history" apart from "history is available, but
# nothing currently qualifies as a momentum alert".
for scope, scope_movers in [(RELEASE_SCOPE_ALL_YEARS, movers), (RELEASE_SCOPE_2007_ONWARD, movers_2007_onward)]:
    candidates = calculate_early_mover_candidates(
        "data/prices.db", release_scope=scope, subtype_mode=SUBTYPE_MODE_VERIFIED)
    scope_path = early_movers_cache_path(scope)
    scope_list = [] if scope_movers.empty else scope_movers.to_dict('records')
    with open(scope_path, 'w') as f:
        json.dump({
            "scope": scope,
            "subtype_mode": SUBTYPE_MODE_VERIFIED,
            "candidates_available": not candidates.empty,
            "movers": scope_list,
        }, f, indent=2)
    print(f"Saved {len(scope_list)} {scope} Early Movers to {scope_path} "
          f"(candidates_available={not candidates.empty})")

if movers.empty:
    print("No early movers found under the production (all_years, verified-subtype) scope. "
          "Data may not be available yet -- this is expected until enough subtype-tracked "
          "price history accumulates (see app.subtype_policy).")
    exit(0)

movers_list = movers.to_dict('records')

# ===== SAVE SIGNAL HISTORY =====
# Use the latest date present in the price data, not the system/UTC date
prices_conn = sqlite3.connect("data/prices.db")
signal_date = prices_conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
prices_conn.close()


print(f"Using signal_date from prices.db: {signal_date}")

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
print(f"\nResults saved to: {early_movers_cache_path(RELEASE_SCOPE_ALL_YEARS)} and "
      f"{early_movers_cache_path(RELEASE_SCOPE_2007_ONWARD)}")
