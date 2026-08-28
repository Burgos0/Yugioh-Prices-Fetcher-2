import sqlite3
import pandas as pd
from datetime import timedelta

def calculate_relevant_sets(df):
    """
    Calculate which sets are relevant based on card value distribution.
    
    A set is relevant if:
    - At least 5 cards worth $3 or more, OR
    - At least 2 cards worth $10 or more, OR
    - At least 1 card worth $25 or more
    
    Args:
        df: DataFrame with all price data
    
    Returns:
        set of relevant set_name values
    """
    if df.empty:
        return set()
    
    df["date"] = pd.to_datetime(df["date"])
    latest_date = df["date"].max()
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
    
    card_prices = {}
    for product_id in df["product_id"].unique():
        card_prices[product_id] = get_card_price(product_id)
    
    relevant_sets = set()
    for set_name in df["set_name"].unique():
        set_cards = df[df["set_name"] == set_name]["product_id"].unique()
        set_prices = [card_prices.get(pid) for pid in set_cards if card_prices.get(pid) is not None]
        
        cards_over_3 = sum(1 for p in set_prices if p >= 3)
        cards_over_10 = sum(1 for p in set_prices if p >= 10)
        cards_over_25 = sum(1 for p in set_prices if p >= 25)
        
        if (cards_over_3 >= 5) or (cards_over_10 >= 2) or (cards_over_25 >= 1):
            relevant_sets.add(set_name)
    
    return relevant_sets


def detect_spike(product_id, baseline_value, current_value, df, latest_date):
    """
    Detect if a price increase is CONFIRMED or a suspicious UNCONFIRMED spike.
    
    Returns "UNCONFIRMED" if:
    - Latest raw price is >50% above the recent 3-day median, OR
    - Fewer than 2 of the last 3 daily prices are elevated (>=10% above baseline)
    
    Otherwise returns "CONFIRMED".
    
    Args:
        product_id: Card ID
        baseline_value: 3-day median from ~7 days ago
        current_value: 3-day median from recent days
        df: DataFrame with all price data
        latest_date: Most recent date in dataset
    
    Returns:
        "CONFIRMED" or "UNCONFIRMED"
    """
    recent_3day_start = latest_date - timedelta(days=2)
    
    # RULE 1: Latest raw price more than 50% above recent 3-day median?
    latest_price_row = df[
        (df["product_id"] == product_id) &
        (df["date"] == latest_date) &
        (df["market_price"].notna())
    ]
    
    if not latest_price_row.empty:
        latest_raw_price = latest_price_row.iloc[0]["market_price"]
        if latest_raw_price > current_value * 1.5:
            return "UNCONFIRMED"
    
    # RULE 2: Persistence check - require 2+ of last 3 prices elevated (>= 10% above baseline)
    recent_prices = df[
        (df["product_id"] == product_id) &
        (df["date"] >= recent_3day_start) &
        (df["market_price"].notna())
    ].sort_values("date")["market_price"].values
    
    if len(recent_prices) >= 2:
        elevated_count = sum(1 for p in recent_prices if p >= baseline_value * 1.1)
        if elevated_count < 2:
            return "UNCONFIRMED"
    
    return "CONFIRMED"


def calculate_top_gainers(db_path, limit=50):
    """
    Calculate top gainers with all analysis logic.
    
    Returns DataFrame with columns:
    [rank, card_name, set_name, baseline_value, current_value, 
     dollar_gain, percent_gain, status]
    
    Args:
        db_path: Path to prices.db
        limit: Number of top gainers to return
    
    Returns:
        DataFrame sorted by percent_gain descending
    """
    # Load data
    conn = sqlite3.connect(db_path)
    df = pd.read_sql("SELECT * FROM prices", conn)
    conn.close()
    
    if df.empty:
        return pd.DataFrame()
    
    df["date"] = pd.to_datetime(df["date"])
    latest_date = df["date"].max()
    
    # Calculate relevant sets
    relevant_sets = calculate_relevant_sets(df)
    
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
    
    # Build results
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
        status = detect_spike(product_id, baseline_value, current_value, df, latest_date)
        
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
    
    # Sort and limit
    results_df = pd.DataFrame(results)
    if results_df.empty:
        return results_df
    
    results_df = results_df.sort_values('percent_gain', ascending=False).head(limit)
    results_df.insert(0, 'rank', range(1, len(results_df) + 1))
    
    return results_df
