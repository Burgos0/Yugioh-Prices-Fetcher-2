import sqlite3
import pandas as pd
from datetime import timedelta

# Connect to database
conn = sqlite3.connect("data/prices.db")

# Load all prices
df = pd.read_sql("SELECT * FROM prices", conn)
conn.close()

if df.empty:
    print("ERROR: No price data found in database")
    exit(1)

# Convert date to datetime
df["date"] = pd.to_datetime(df["date"])

# Find latest date (most recent collection)
latest_date = df["date"].max()
print(f"Latest collection date: {latest_date.date()}\n")

# Define 7-day window
seven_days_ago = latest_date - timedelta(days=7)

# ===== CALCULATE 7-DAY MEDIAN PRICES PER CARD =====
# For each product_id, get median market price from past 7 days
# If insufficient history, fall back to latest market price

df_7day = df[df["date"] >= seven_days_ago]

def get_card_price(product_id):
    """Get 7-day median price for a card, or latest if sparse."""
    prices_7day = df_7day[
        (df_7day["product_id"] == product_id) & 
        (df_7day["market_price"].notna())
    ]["market_price"]
    
    if len(prices_7day) >= 7:
        # Enough history: use median
        return prices_7day.median()
    elif len(prices_7day) > 0:
        # Some history but not 7 days: use latest available
        card_data = df[
            (df["product_id"] == product_id) & 
            (df["market_price"].notna())
        ].sort_values("date")
        return card_data.iloc[-1]["market_price"]
    else:
        # No market price data
        return None


# Build price lookup for all unique cards
card_prices = {}
for product_id in df["product_id"].unique():
    card_prices[product_id] = get_card_price(product_id)

# ===== ANALYZE SETS =====
# For each set, count cards in each price tier

set_analysis = []

for set_name in sorted(df["set_name"].unique()):
    set_cards = df[df["set_name"] == set_name]["product_id"].unique()
    
    # Get prices for this set's cards
    set_prices = []
    for product_id in set_cards:
        price = card_prices.get(product_id)
        if price is not None:  # Only count cards with valid prices
            set_prices.append(price)
    
    # Count by threshold
    cards_over_3 = sum(1 for p in set_prices if p >= 3)
    cards_over_10 = sum(1 for p in set_prices if p >= 10)
    cards_over_25 = sum(1 for p in set_prices if p >= 25)
    
    # Determine relevance
    relevant = (cards_over_3 >= 5) or (cards_over_10 >= 2) or (cards_over_25 >= 1)
    
    set_analysis.append({
        'set_name': set_name,
        'total_cards': len(set_cards),
        'cards_with_price': len(set_prices),
        'cards_over_3': cards_over_3,
        'cards_over_10': cards_over_10,
        'cards_over_25': cards_over_25,
        'relevant': relevant
    })

# ===== OUTPUT RESULTS =====
print("="*100)
print("SET RELEVANCE ANALYSIS")
print("="*100)
print(f"{'Set Name':<50} {'Total':<8} {'Priced':<8} {'$3+':<6} {'$10+':<6} {'$25+':<6} {'Relevant':<10}")
print("-"*100)

for analysis in set_analysis:
    relevant_str = "✓ YES" if analysis['relevant'] else "✗ NO"
    print(
        f"{analysis['set_name']:<50} "
        f"{analysis['total_cards']:<8} "
        f"{analysis['cards_with_price']:<8} "
        f"{analysis['cards_over_3']:<6} "
        f"{analysis['cards_over_10']:<6} "
        f"{analysis['cards_over_25']:<6} "
        f"{relevant_str:<10}"
    )

# ===== SUMMARY =====
total_sets = len(set_analysis)
relevant_sets = sum(1 for s in set_analysis if s['relevant'])
irrelevant_sets = total_sets - relevant_sets

print("\n" + "="*100)
print("SUMMARY")
print("="*100)
print(f"Total sets analyzed:    {total_sets}")
print(f"Relevant sets:          {relevant_sets} ({100*relevant_sets/total_sets:.1f}%)")
print(f"Irrelevant sets:        {irrelevant_sets} ({100*irrelevant_sets/total_sets:.1f}%)")
print("="*100)
