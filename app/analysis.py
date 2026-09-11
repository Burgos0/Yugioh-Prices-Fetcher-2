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


def detect_drop(product_id, baseline_value, current_value, df, latest_date):
    """
    Detect if a price decrease is CONFIRMED or a suspicious UNCONFIRMED dip.
    
    Mirrors detect_spike() but for downward moves. Returns "UNCONFIRMED" if:
    - Latest raw price is >50% below the recent 3-day median, OR
    - Fewer than 2 of the last 3 daily prices are depressed (<=90% of baseline)
    
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
    
    # RULE 1: Latest raw price more than 50% below recent 3-day median?
    latest_price_row = df[
        (df["product_id"] == product_id) &
        (df["date"] == latest_date) &
        (df["market_price"].notna())
    ]
    
    if not latest_price_row.empty:
        latest_raw_price = latest_price_row.iloc[0]["market_price"]
        if latest_raw_price < current_value * 0.5:
            return "UNCONFIRMED"
    
    # RULE 2: Persistence check - require 2+ of last 3 prices depressed (<= 90% of baseline)
    recent_prices = df[
        (df["product_id"] == product_id) &
        (df["date"] >= recent_3day_start) &
        (df["market_price"].notna())
    ].sort_values("date")["market_price"].values
    
    if len(recent_prices) >= 2:
        depressed_count = sum(1 for p in recent_prices if p <= baseline_value * 0.9)
        if depressed_count < 2:
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
            'product_id': product_id,
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


def calculate_top_losers(db_path, limit=50):
    """
    Calculate top losers with the same structure/safeguards as calculate_top_gainers.
    
    Returns DataFrame with columns:
    [rank, product_id, card_name, set_name, baseline_value, current_value, 
     dollar_change, percent_change, status]
    
    Args:
        db_path: Path to prices.db
        limit: Number of top losers to return
    
    Returns:
        DataFrame sorted by percent_change ascending (biggest loss first)
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
        
        # Skip if baseline value below $3 (same price-floor safeguard as top gainers)
        if baseline_value < 3.0:
            continue
        
        # Skip if no loss or a gain
        if current_value >= baseline_value:
            continue
        
        # Calculate loss
        dollar_change = current_value - baseline_value
        percent_change = (dollar_change / baseline_value) * 100
        
        # Detect drop
        status = detect_drop(product_id, baseline_value, current_value, df, latest_date)
        
        # Get card name
        card_name = df[df["product_id"] == product_id]["card_name"].iloc[0]
        
        results.append({
            'product_id': product_id,
            'card_name': card_name,
            'set_name': set_name,
            'baseline_value': baseline_value,
            'current_value': current_value,
            'dollar_change': dollar_change,
            'percent_change': percent_change,
            'status': status
        })
    
    # Sort (largest percentage loss first) and limit
    results_df = pd.DataFrame(results)
    if results_df.empty:
        return results_df
    
    results_df = results_df.sort_values('percent_change', ascending=True).head(limit)
    results_df.insert(0, 'rank', range(1, len(results_df) + 1))
    
    return results_df


def calculate_penny_movers(db_path, limit=50):
    """
    Calculate "Penny Movers" - cheap cards ($0.25-$5.00 baseline) with strong gains.
    
    Reuses the same baseline/current median logic as top gainers:
    - baseline = median of days -8, -7, -6
    - current = median of today, -1, -2
    
    Filters:
    - baseline price between $0.25 and $5.00
    - current price > baseline price
    - dollar_gain >= $0.25
    - percent_gain >= 20%
    
    Returns DataFrame with columns:
    [rank, card_name, set_name, baseline_value, current_value, 
     dollar_gain, percent_gain, status]
    
    Args:
        db_path: Path to prices.db
        limit: Number of penny movers to return
    
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
        # Skip if missing baseline or current value
        if product_id not in baseline_medians.index or product_id not in current_medians.index:
            continue
        
        baseline_value = baseline_medians[product_id]
        current_value = current_medians[product_id]
        
        # Only cheap cards
        if not (0.25 <= baseline_value <= 5.00):
            continue
        
        # Skip if no gain or negative gain
        if current_value <= baseline_value:
            continue
        
        # Calculate gain
        dollar_gain = current_value - baseline_value
        percent_gain = (dollar_gain / baseline_value) * 100
        
        # Anti-junk filtering
        if dollar_gain < 0.25 or percent_gain < 20:
            continue
        
        # Detect spike
        status = detect_spike(product_id, baseline_value, current_value, df, latest_date)
        
        # Get card name and set
        set_name = df[df["product_id"] == product_id]["set_name"].iloc[0]
        card_name = df[df["product_id"] == product_id]["card_name"].iloc[0]
        
        results.append({
            'product_id': product_id,
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


def calculate_early_movers(db_path, limit=50):
    """
    Calculate "Early Movers" - cards showing the start of upward momentum,
    before they become major Top Gainers.
    
    V1 logic (simple and explainable):
    - Look at each card's last 3 valid (non-null, non-zero) price readings:
      price_2_days_ago, previous_price, latest_price
    - Require latest_price > previous_price >= price_2_days_ago (building momentum)
    - percent_gain = (latest_price - price_2_days_ago) / price_2_days_ago * 100
    - Keep only 10% <= percent_gain <= 50% (bigger moves belong in Top Gainers)
    - Require at least $0.25 of dollar movement to reduce penny-price noise
    
    Returns DataFrame with columns:
    [rank, product_id, card_name, set_name, price_2_days_ago, previous_price,
     latest_price, dollar_gain, percent_gain]
    
    Args:
        db_path: Path to prices.db
        limit: Number of early movers to return
    
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
    
    # Calculate relevant sets
    relevant_sets = calculate_relevant_sets(df)
    
    # Ignore null or zero market prices
    df_valid = df[df["market_price"].notna() & (df["market_price"] > 0)].sort_values(["product_id", "date"])
    
    # Need enough recent data to evaluate at least the last 4 days
    valid_counts = df_valid.groupby("product_id").size()
    products_with_history = valid_counts[valid_counts >= 4].index
    
    # Take each card's last 3 valid readings: price_2_days_ago, previous_price, latest_price
    last_3 = df_valid[df_valid["product_id"].isin(products_with_history)].groupby("product_id").tail(3)
    last_3 = last_3.copy()
    last_3["position"] = last_3.groupby("product_id").cumcount()
    
    prices = last_3.pivot(index="product_id", columns="position", values="market_price")
    prices.columns = ["price_2_days_ago", "previous_price", "latest_price"]
    
    # Look for recent upward momentum
    momentum = (prices["latest_price"] > prices["previous_price"]) & \
               (prices["previous_price"] >= prices["price_2_days_ago"])
    prices = prices[momentum]
    
    if prices.empty:
        return pd.DataFrame()
    
    # Calculate gain
    prices["dollar_gain"] = prices["latest_price"] - prices["price_2_days_ago"]
    prices["percent_gain"] = (prices["dollar_gain"] / prices["price_2_days_ago"]) * 100
    
    # Anti-junk filtering
    prices = prices[
        (prices["dollar_gain"] >= 0.25) &
        (prices["percent_gain"] >= 10) &
        (prices["percent_gain"] <= 50)
    ]
    
    if prices.empty:
        return pd.DataFrame()
    
    # Attach card_name/set_name (one row per product_id) and apply relevant-set filtering
    card_info = df.drop_duplicates("product_id").set_index("product_id")[["card_name", "set_name"]]
    results_df = prices.join(card_info, how="left")
    results_df = results_df[results_df["set_name"].isin(relevant_sets)]
    
    if results_df.empty:
        return pd.DataFrame()
    
    results_df = results_df.reset_index()
    
    # Sort (highest recent percent gain first) and limit
    results_df = results_df.sort_values('percent_gain', ascending=False).head(limit)
    results_df.insert(0, 'rank', range(1, len(results_df) + 1))
    
    return results_df


def get_product_history_info(db_path, product_id):
    """
    Get first/last seen dates and days of history for a product_id.
    
    Args:
        db_path: Path to prices.db
        product_id: Card ID
    
    Returns:
        dict with first_seen_date, last_seen_date, days_of_history
        (values are None / 0 if the product has no rows)
    """
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        '''
        SELECT MIN(date), MAX(date), COUNT(DISTINCT date)
        FROM prices
        WHERE product_id = ?
        ''',
        (product_id,)
    ).fetchone()
    conn.close()

    first_seen_date, last_seen_date, days_of_history = row

    return {
        "first_seen_date": first_seen_date,
        "last_seen_date": last_seen_date,
        "days_of_history": days_of_history or 0
    }
