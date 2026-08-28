import sqlite3
import pandas as pd
from datetime import timedelta

# Connect to database
conn = sqlite3.connect("data/prices.db")
df = pd.read_sql("SELECT * FROM prices", conn)
conn.close()

if df.empty:
    print("ERROR: No price data found in database")
    exit(1)

# Convert date to datetime
df["date"] = pd.to_datetime(df["date"])

# Find latest date
latest_date = df["date"].max()
print(f"Latest collection date: {latest_date.date()}\n")

# ===== STEP 1: CALCULATE RELEVANT SETS =====
# Reuse logic from set_relevance.py
seven_days_ago = latest_date - timedelta(days=7)
df_7day = df[df["date"] >= seven_days_ago]

def get_card_price(product_id):
    """Get 7-day median price for a card, or latest if sparse."""
    prices_7day = df_7day[
        (df_7day["product_id"] == product_id) & 
        (df_7day["market_price"].notna())
    ]["market_price"]
    
    if len(prices_7day) >= 7:
        return prices_7day.median()
    elif len(prices_7day) > 0:
        card_data = df[
            (df["product_id"] == product_id) & 
            (df["market_price"].notna())
        ].sort_values("date")
        return card_data.iloc[-1]["market_price"]
    else:
        return None

# Build card price lookup
card_prices = {}
for product_id in df["product_id"].unique():
    card_prices[product_id] = get_card_price(product_id)

# Find relevant sets
relevant_sets = set()
for set_name in df["set_name"].unique():
    set_cards = df[df["set_name"] == set_name]["product_id"].unique()
    set_prices = [card_prices.get(pid) for pid in set_cards if card_prices.get(pid) is not None]
    
    cards_over_3 = sum(1 for p in set_prices if p >= 3)
    cards_over_10 = sum(1 for p in set_prices if p >= 10)
    cards_over_25 = sum(1 for p in set_prices if p >= 25)
    
    if (cards_over_3 >= 5) or (cards_over_10 >= 2) or (cards_over_25 >= 1):
        relevant_sets.add(set_name)

print(f"Found {len(relevant_sets)} relevant sets out of {len(df['set_name'].unique())}")
print(f"Analyzing only cards from relevant sets...\n")

# ===== STEP 2: CALCULATE GAIN METRICS =====
# Define time windows
recent_3day_start = latest_date - timedelta(days=2)
baseline_window_start = latest_date - timedelta(days=8)
baseline_window_end = latest_date - timedelta(days=6)

# Calculate medians for each card
df_recent_3day = df[
    (df["date"] >= recent_3day_start) & 
    (df["market_price"].notna())
]
current_medians = df_recent_3day.groupby("product_id")["market_price"].median()

df_baseline = df[
    (df["date"] >= baseline_window_start) & 
    (df["date"] <= baseline_window_end) &
    (df["market_price"].notna())
]
baseline_medians = df_baseline.groupby("product_id")["market_price"].median()

# ===== STEP 3: SPIKE DETECTION =====
def detect_spike(product_id, baseline_value, current_value):
    """
    Detect if a price increase is confirmed or suspicious.
    
    Returns: "CONFIRMED" or "UNCONFIRMED"
    """
    
    # Get latest raw price
    latest_price_row = df[
        (df["product_id"] == product_id) &
        (df["date"] == latest_date) &
        (df["market_price"].notna())
    ]
    
    if latest_price_row.empty:
        return "CONFIRMED"  # No latest price, assume confirmed
    
    latest_raw_price = latest_price_row.iloc[0]["market_price"]
    
    # RULE 1: Latest raw price more than 50% above recent 3-day median?
    if latest_raw_price > current_value * 1.5:
        return "UNCONFIRMED"
    
    # RULE 2: Persistence check - require 2+ of last 3 prices elevated (>= 10% above baseline)
    recent_prices = df[
        (df["product_id"] == product_id) &
        (df["date"] >= recent_3day_start) &
        (df["market_price"].notna())
    ].sort_values("date")["market_price"].values
    
    if len(recent_prices) >= 2:  # Need at least 2 observations
        elevated_count = sum(1 for p in recent_prices if p >= baseline_value * 1.1)
        if elevated_count < 2:
            return "UNCONFIRMED"
    
    return "CONFIRMED"

# ===== STEP 4: BUILD RESULTS TABLE =====
results = []

for product_id in df["product_id"].unique():
    # Skip if not in relevant set
    set_name = df[df["product_id"] == product_id]["set_name"].iloc[0]
    if set_name not in relevant_sets:
        continue
    
    # Skip if missing baseline or current value
    if product_id not in baseline_medians.index or product_id not in current_medians.index:
        continue
    
    baseline_value = baseline_medians[product_id]
    current_value = current_medians[product_id]
    
    # Skip if current value below $3
    if current_value < 3.0:
        continue
    
    # Skip if no gain or negative gain
    if current_value <= baseline_value:
        continue
    
    # Calculate gain
    dollar_gain = current_value - baseline_value
    percent_gain = (dollar_gain / baseline_value) * 100
    
    # Detect spike
    status = detect_spike(product_id, baseline_value, current_value)
    
    # Get card name
    card_name = df[df["product_id"] == product_id]["card_name"].iloc[0]
    
    results.append({
        'card_name': card_name,
        'set_name': set_name,
        'baseline_value': baseline_value,
        'current_value': current_value,
        'dollar_gain': dollar_gain,
        'percent_gain': percent_gain,
        'status': status
    })

# ===== SORT AND FILTER =====
results_df = pd.DataFrame(results)
results_df = results_df.sort_values('percent_gain', ascending=False).head(50)

# ===== OUTPUT =====
print("="*130)
print("TOP 50 WEEKLY GAINERS (Confirmed & Unconfirmed Spikes)")
print("="*130)
print(
    f"{'Card Name':<40} {'Set':<35} {'Baseline':<12} {'Current':<12} "
    f"{'Gain $':<10} {'Gain %':<10} {'Status':<14}"
)
print("-"*130)

for _, row in results_df.iterrows():
    status_str = f"✓ {row['status']}" if row['status'] == "CONFIRMED" else f"⚠ {row['status']}"
    print(
        f"{row['card_name']:<40} "
        f"{row['set_name']:<35} "
        f"${row['baseline_value']:<11.2f} "
        f"${row['current_value']:<11.2f} "
        f"${row['dollar_gain']:<9.2f} "
        f"{row['percent_gain']:<9.2f}% "
        f"{status_str:<14}"
    )

# ===== SUMMARY =====
confirmed_count = sum(1 for _, row in results_df.iterrows() if row['status'] == "CONFIRMED")
unconfirmed_count = sum(1 for _, row in results_df.iterrows() if row['status'] == "UNCONFIRMED")

print("\n" + "="*130)
print("SUMMARY")
print("="*130)
print(f"Analysis period: ~7 days ago vs recent 3-day average")
print(f"Cards analyzed: From {len(relevant_sets)} relevant sets")
print(f"Top gainers shown: {len(results_df)}")
print(f"  ✓ CONFIRMED: {confirmed_count} (price increase sustained across multiple recent days)")
print(f"  ⚠ UNCONFIRMED: {unconfirmed_count} (potential spike or single-day anomaly)")
print("="*130)
