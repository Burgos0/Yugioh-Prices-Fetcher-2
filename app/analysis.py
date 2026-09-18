import sqlite3
import pandas as pd
import math
import os
import json
from statistics import median
from datetime import timedelta, datetime

DEFAULT_RELEASE_DATE_CACHE_PATH = "data/set_release_dates.json"
RELEASE_SCOPE_START_DATE = "2007-01-01"

RELEASE_SCOPE_ALL_YEARS = "all_years"
RELEASE_SCOPE_2007_ONWARD = "2007_onward"
VALID_RELEASE_SCOPES = (RELEASE_SCOPE_ALL_YEARS, RELEASE_SCOPE_2007_ONWARD)
DEFAULT_RELEASE_SCOPE = RELEASE_SCOPE_ALL_YEARS

# Labeled, separate display-cache paths per scope so a page can never load
# results generated under the other scope by accident.
EARLY_MOVERS_CACHE_PATHS = {
    RELEASE_SCOPE_ALL_YEARS: "data/early_movers_all_years.json",
    RELEASE_SCOPE_2007_ONWARD: "data/early_movers_2007_onward.json",
}


def early_movers_cache_path(release_scope):
    """Return the labeled cache file path for a given release scope. Raises on an unknown scope."""
    if release_scope not in EARLY_MOVERS_CACHE_PATHS:
        raise ValueError(f"Unknown release_scope: {release_scope!r}")
    return EARLY_MOVERS_CACHE_PATHS[release_scope]


SUBTYPE_MODE_LEGACY_UNVERIFIED = "legacy_unverified"
SUBTYPE_MODE_VERIFIED = "verified"
VALID_SUBTYPE_MODES = (SUBTYPE_MODE_LEGACY_UNVERIFIED, SUBTYPE_MODE_VERIFIED)


def fetch_group_release_dates(category_id=2, base_url="https://tcgcsv.com/tcgplayer", timeout=30):
    """
    Fetch genuine set (group) release dates from the live TCGCSV groups API.

    Uses each group's `publishedOn` field, which TCGCSV documents as the
    set's real release date. Deliberately does NOT use `modifiedOn`, which
    is only an API/catalog record-update timestamp, not a release date.

    Returns a list of dicts keyed by the stable `group_id`:
    [{"group_id": str, "name": str, "release_date": "YYYY-MM-DD" or None,
      "source": "tcgcsv_groups_api.publishedOn", "fetched_at": iso8601}, ...]
    """
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 yugioh-price-fetcher/2.0"}
    resp = requests.get(f"{base_url}/{category_id}/groups", timeout=timeout, headers=headers)
    resp.raise_for_status()
    payload = resp.json()
    groups = payload.get("results", payload) if isinstance(payload, dict) else payload
    fetched_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    records = []
    for g in groups:
        group_id = g.get("groupId")
        if group_id is None:
            continue
        published_on = g.get("publishedOn")
        release_date = None
        if published_on:
            try:
                release_date = datetime.strptime(published_on[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                release_date = None
        records.append({
            "group_id": str(group_id),
            "name": g.get("name"),
            "release_date": release_date,
            "source": "tcgcsv_groups_api.publishedOn",
            "fetched_at": fetched_at,
        })
    return records


def save_release_date_cache(records, cache_path=DEFAULT_RELEASE_DATE_CACHE_PATH):
    """Persist verified release-date records, keyed by stable group_id, with their source."""
    directory = os.path.dirname(cache_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump({r["group_id"]: r for r in records}, f, indent=2, sort_keys=True)


def load_release_date_cache(cache_path=DEFAULT_RELEASE_DATE_CACHE_PATH):
    """Load the cached release dates. Returns {} (explicit unknown state) if no cache exists yet."""
    if not os.path.exists(cache_path):
        return {}
    with open(cache_path) as f:
        return json.load(f)


def _build_set_name_index(cache):
    """
    Group cached group-id records by set name, since prices.db only stores
    set_name (not group_id) per row. A name is "known" only if it maps to
    exactly one cached group id with a parseable release date; a name
    shared by more than one group id is "ambiguous" and a name with no
    cached/parseable date is "unknown" -- neither is ever guessed.
    """
    by_name = {}
    for record in cache.values():
        name = record.get("name")
        if not name:
            continue
        by_name.setdefault(name, []).append(record)

    index = {}
    for name, records in by_name.items():
        if len(records) > 1:
            index[name] = {"status": "ambiguous", "release_date": None}
            continue
        release_date = records[0].get("release_date")
        if not release_date:
            index[name] = {"status": "unknown", "release_date": None}
            continue
        index[name] = {"status": "known", "release_date": release_date}
    return index


def apply_release_scope(candidates_df, as_of, release_scope=DEFAULT_RELEASE_SCOPE,
                         cache_path=DEFAULT_RELEASE_DATE_CACHE_PATH,
                         scope_start_date=RELEASE_SCOPE_START_DATE):
    """
    Restrict candidates by release date, per the selected scope:

    - "all_years" (default): does NOT require a known release date to
      include a product; a set with no verified release date is still
      included, but counted separately as a research limitation (never
      silently excluded just for being unverified). A KNOWN future release
      date (after `as_of`) is still excluded -- that protection against
      look-ahead applies regardless of scope.
    - "2007_onward": additionally requires a verified release date on or
      after `scope_start_date`; sets with no cached entry, an unparseable
      date, or an ambiguous (duplicate) name are excluded and counted.

    A name shared by more than one cached group id ("ambiguous") is always
    excluded in both scopes -- it is never guessed or silently joined.

    Returns (scoped_df, stats). stats reports per-row counts: total,
    eligible (= eligible_known_date + eligible_unknown_date), excluded_pre_scope,
    excluded_future, excluded_unknown, excluded_ambiguous, plus the distinct
    set names behind the unknown/ambiguous buckets for follow-up.
    """
    if release_scope not in VALID_RELEASE_SCOPES:
        raise ValueError(f"Unknown release_scope: {release_scope!r}")

    cache = load_release_date_cache(cache_path)
    name_index = _build_set_name_index(cache)

    stats = {
        "release_scope": release_scope,
        "scope_start_date": scope_start_date,
        "as_of": as_of,
        "cache_path": cache_path,
        "cache_present": os.path.exists(cache_path),
        "total_candidates": int(len(candidates_df)),
        "eligible": 0,
        "eligible_known_date": 0,
        "eligible_unknown_date": 0,
        "excluded_pre_scope": 0,
        "excluded_future": 0,
        "excluded_unknown": 0,
        "excluded_ambiguous": 0,
        "unknown_set_names": [],
        "ambiguous_set_names": [],
    }

    if candidates_df.empty:
        return candidates_df, stats

    def classify(set_name):
        entry = name_index.get(set_name)
        if entry is None or entry["status"] == "unknown":
            return "eligible_unknown_date" if release_scope == RELEASE_SCOPE_ALL_YEARS else "excluded_unknown"
        if entry["status"] == "ambiguous":
            return "excluded_ambiguous"
        release_date = entry["release_date"]
        # A known future release date is never included, in either scope.
        if release_date > as_of:
            return "excluded_future"
        if release_scope == RELEASE_SCOPE_2007_ONWARD and release_date < scope_start_date:
            return "excluded_pre_scope"
        return "eligible_known_date"

    classifications = candidates_df["set_name"].map(classify)
    for bucket in ("eligible_known_date", "eligible_unknown_date", "excluded_pre_scope",
                   "excluded_future", "excluded_unknown", "excluded_ambiguous"):
        stats[bucket] = int((classifications == bucket).sum())
    stats["eligible"] = stats["eligible_known_date"] + stats["eligible_unknown_date"]

    stats["unknown_set_names"] = sorted(set(candidates_df.loc[classifications == "excluded_unknown", "set_name"]))
    stats["ambiguous_set_names"] = sorted(set(candidates_df.loc[classifications == "excluded_ambiguous", "set_name"]))

    eligible_mask = classifications.isin(["eligible_known_date", "eligible_unknown_date"])
    scoped_df = candidates_df[eligible_mask].reset_index(drop=True)
    return scoped_df, stats


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
    
    # Aggregate once instead of scanning the full history for every product.
    valid = df_7day[df_7day["market_price"].notna()].sort_values("date")
    stats = valid.groupby("product_id")["market_price"].agg(["count", "median", "last"])
    card_prices = stats["median"].where(stats["count"] >= 7, stats["last"])
    cards = df[["set_name", "product_id"]].drop_duplicates().copy()
    cards["price"] = cards["product_id"].map(card_prices)
    for threshold in (3, 10, 25):
        cards[f"over_{threshold}"] = cards["price"] >= threshold
    counts = cards.groupby("set_name")[["over_3", "over_10", "over_25"]].sum()
    return set(counts.index[(counts.over_3 >= 5) | (counts.over_10 >= 2) | (counts.over_25 >= 1)])


def _select_weekly_mover_windows(df):
    """Return the recent and baseline snapshot dates for weekly movers."""
    snapshot_dates = sorted(df["date"].dropna().unique())
    if len(snapshot_dates) < 9:
        return None
    return snapshot_dates[-3:], snapshot_dates[-9:-6]


def detect_spike(product_id, baseline_value, current_value, df, latest_date, recent_dates=None):
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
    recent_dates = recent_dates if recent_dates is not None else [
        latest_date - timedelta(days=2), latest_date - timedelta(days=1), latest_date
    ]
    
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
        (df["date"].isin(recent_dates)) &
        (df["market_price"].notna())
    ].sort_values("date")["market_price"].values
    
    if len(recent_prices) >= 2:
        elevated_count = sum(1 for p in recent_prices if p >= baseline_value * 1.1)
        if elevated_count < 2:
            return "UNCONFIRMED"
    
    return "CONFIRMED"


def detect_drop(product_id, baseline_value, current_value, df, latest_date, recent_dates=None):
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
    recent_dates = recent_dates if recent_dates is not None else [
        latest_date - timedelta(days=2), latest_date - timedelta(days=1), latest_date
    ]
    
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
        (df["date"].isin(recent_dates)) &
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
    
    weekly_windows = _select_weekly_mover_windows(df)
    if weekly_windows is None:
        return pd.DataFrame()
    recent_dates, baseline_dates = weekly_windows
    
    # Calculate medians for each card
    df_recent_3day = df[
        (df["date"].isin(recent_dates)) &
        (df["market_price"].notna())
    ]
    current_medians = df_recent_3day.groupby("product_id")["market_price"].median()
    
    df_baseline = df[
        (df["date"].isin(baseline_dates)) &
        (df["market_price"].notna())
    ]
    baseline_medians = df_baseline.groupby("product_id")["market_price"].median()
    complete_recent = df_recent_3day.groupby("product_id")["date"].nunique() == len(recent_dates)
    complete_baseline = df_baseline.groupby("product_id")["date"].nunique() == len(baseline_dates)
    complete_products = complete_recent[complete_recent].index.intersection(
        complete_baseline[complete_baseline].index
    )
    
    # Build results
    results = []
    
    for product_id in df["product_id"].unique():
        # Skip if not in relevant set
        set_name = df[df["product_id"] == product_id]["set_name"].iloc[0]
        if set_name not in relevant_sets:
            continue
        
        # Skip if missing baseline or current value
        if product_id not in complete_products:
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
        status = detect_spike(product_id, baseline_value, current_value, df, latest_date, recent_dates)
        
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
    
    weekly_windows = _select_weekly_mover_windows(df)
    if weekly_windows is None:
        return pd.DataFrame()
    recent_dates, baseline_dates = weekly_windows
    
    # Calculate medians for each card
    df_recent_3day = df[
        (df["date"].isin(recent_dates)) &
        (df["market_price"].notna())
    ]
    current_medians = df_recent_3day.groupby("product_id")["market_price"].median()
    
    df_baseline = df[
        (df["date"].isin(baseline_dates)) &
        (df["market_price"].notna())
    ]
    baseline_medians = df_baseline.groupby("product_id")["market_price"].median()
    complete_recent = df_recent_3day.groupby("product_id")["date"].nunique() == len(recent_dates)
    complete_baseline = df_baseline.groupby("product_id")["date"].nunique() == len(baseline_dates)
    complete_products = complete_recent[complete_recent].index.intersection(
        complete_baseline[complete_baseline].index
    )
    
    # Build results
    results = []
    
    for product_id in df["product_id"].unique():
        # Skip if not in relevant set
        set_name = df[df["product_id"] == product_id]["set_name"].iloc[0]
        if set_name not in relevant_sets:
            continue
        
        # Skip if missing baseline or current value
        if product_id not in complete_products:
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
        status = detect_drop(product_id, baseline_value, current_value, df, latest_date, recent_dates)
        
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
    
    weekly_windows = _select_weekly_mover_windows(df)
    if weekly_windows is None:
        return pd.DataFrame()
    recent_dates, baseline_dates = weekly_windows
    
    # Calculate medians for each card
    df_recent_3day = df[
        (df["date"].isin(recent_dates)) &
        (df["market_price"].notna())
    ]
    current_medians = df_recent_3day.groupby("product_id")["market_price"].median()
    
    df_baseline = df[
        (df["date"].isin(baseline_dates)) &
        (df["market_price"].notna())
    ]
    baseline_medians = df_baseline.groupby("product_id")["market_price"].median()
    complete_recent = df_recent_3day.groupby("product_id")["date"].nunique() == len(recent_dates)
    complete_baseline = df_baseline.groupby("product_id")["date"].nunique() == len(baseline_dates)
    complete_products = complete_recent[complete_recent].index.intersection(
        complete_baseline[complete_baseline].index
    )
    
    # Build results
    results = []
    
    for product_id in df["product_id"].unique():
        # Skip if missing baseline or current value
        if product_id not in complete_products:
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
        status = detect_spike(product_id, baseline_value, current_value, df, latest_date, recent_dates)
        
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


def _load_subtype_established_dates(db_path):
    """
    Load each product's tracked-subtype establishment date from
    `product_subtypes` (see scripts/fetch_prices.py), keyed by product_id.
    Returns {} if the table doesn't exist (older/pre-tracking database) --
    an explicit "nothing verified yet" state, never guessed.
    """
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='product_subtypes'"
        ).fetchone()
        if not exists:
            return {}
        return dict(conn.execute("SELECT product_id, established_date FROM product_subtypes"))


def calculate_early_mover_candidates(db_path, as_of=None, release_date_cache_path=DEFAULT_RELEASE_DATE_CACHE_PATH,
                                      release_scope=DEFAULT_RELEASE_SCOPE, scope_start_date=RELEASE_SCOPE_START_DATE,
                                      subtype_mode=SUBTYPE_MODE_LEGACY_UNVERIFIED, return_scope_stats=False):
    """
    Build the full pool of products eligible for Early Mover evaluation on a
    given date: relevant-set membership, 3 valid consecutive calendar dates
    ending on `as_of` (the same history requirement the detector uses), the
    selected release-date scope (see apply_release_scope), and the selected
    subtype-provenance mode, regardless of whether a candidate ends up
    passing the momentum/gain rule.

    subtype_mode:
    - "legacy_unverified" (default here; used for historical research/replay):
      uses whatever price is stored for each date, with no requirement that
      it come from a subtype-tracked import. Necessary because most existing
      history predates subtype tracking; results using this mode must be
      labeled unverified by the caller.
    - "verified": requires the detector's FULL minimum-history requirement
      (all 4 of the most recent valid readings used to qualify a product,
      not just the 3 used in the momentum comparison) to come from on/after
      the date a stable subtype was first established for that product (see
      scripts/fetch_prices.py's product_subtypes table). This guarantees the
      comparison never mixes an unverified legacy price with a newly
      subtype-tracked price, and never admits a product with less verified
      history than the original detector requires; a product with no
      tracked subtype at all is excluded outright. This is the mode
      production alert generation must use.

    This is the shared base used by both calculate_early_movers() (which
    additionally requires momentum + gain thresholds) and historical replay
    control-group selection, so both draw from an identical, leak-free,
    identically-scoped pool.

    Returns DataFrame with columns:
    [product_id, card_name, set_name, price_2_days_ago, previous_price,
     latest_price, dollar_gain, percent_gain, is_alert]
    (empty DataFrame if no data / no eligible products), or, if
    return_scope_stats=True, a (DataFrame, stats) tuple -- see
    apply_release_scope for the stats fields.
    """
    if subtype_mode not in VALID_SUBTYPE_MODES:
        raise ValueError(f"Unknown subtype_mode: {subtype_mode!r}")

    empty_result = (pd.DataFrame(), None) if return_scope_stats else pd.DataFrame()

    # Cut off data BEFORE any filters to prevent future data entering a replay.
    with sqlite3.connect(db_path) as conn:
        if as_of is None:
            as_of = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
        if as_of is None:
            return empty_result
        as_of = datetime.strptime(as_of, "%Y-%m-%d").strftime("%Y-%m-%d")
        df = pd.read_sql("SELECT * FROM prices WHERE date <= ?", conn, params=(as_of,))
    if df.empty:
        return empty_result
    df["date"] = pd.to_datetime(df["date"])

    # Calculate relevant sets
    relevant_sets = calculate_relevant_sets(df)

    # Ignore null or zero market prices
    df_valid = df[df["market_price"].map(lambda p: pd.notna(p) and math.isfinite(p) and p > 0)].sort_values(["product_id", "date"])

    # Need enough recent data to evaluate at least the last 4 days
    valid_counts = df_valid.groupby("product_id").size()
    products_with_history = valid_counts[valid_counts >= 4].index

    if subtype_mode == SUBTYPE_MODE_VERIFIED:
        # The detector's real minimum-history gate is 4 valid readings, not
        # the 3 used in the momentum comparison. Require ALL 4 of the most
        # recent valid readings to be on/after the date a stable subtype was
        # established for that product, so a product can never qualify on a
        # mix of unverified legacy readings and newly-tracked ones, and never
        # with less verified history than the original detector requires.
        established = _load_subtype_established_dates(db_path)
        last_4 = df_valid[df_valid["product_id"].isin(products_with_history)].groupby("product_id").tail(4)
        earliest_of_4 = last_4.groupby("product_id")["date"].min()
        verified_product_ids = {
            pid for pid, earliest in earliest_of_4.items()
            if established.get(pid) is not None and earliest.strftime("%Y-%m-%d") >= established[pid]
        }
        products_with_history = products_with_history[products_with_history.isin(verified_product_ids)]

    # Take each card's last 3 valid readings: price_2_days_ago, previous_price, latest_price
    last_3 = df_valid[df_valid["product_id"].isin(products_with_history)].groupby("product_id").tail(3)
    last_3 = last_3.copy()
    # Require three consecutive calendar dates ending on the analysis date.
    expected = pd.date_range(end=as_of, periods=3)
    last_3 = last_3[last_3["date"].isin(expected)]
    counts = last_3.groupby("product_id")["date"].nunique()
    last_3 = last_3[last_3["product_id"].isin(counts[counts == 3].index)]
    if last_3.empty:
        return empty_result
    last_3["position"] = last_3.groupby("product_id").cumcount()


    prices = last_3.pivot(index="product_id", columns="position", values="market_price")
    prices.columns = ["price_2_days_ago", "previous_price", "latest_price"]

    prices["dollar_gain"] = prices["latest_price"] - prices["price_2_days_ago"]
    prices["percent_gain"] = (prices["dollar_gain"] / prices["price_2_days_ago"]) * 100

    momentum = (prices["latest_price"] > prices["previous_price"]) & \
               (prices["previous_price"] >= prices["price_2_days_ago"])
    prices["is_alert"] = momentum & \
        (prices["dollar_gain"] >= 0.25) & \
        (prices["percent_gain"] >= 10) & \
        (prices["percent_gain"] <= 50)

    # Attach card_name/set_name (one row per product_id) and apply relevant-set filtering
    card_info = df.drop_duplicates("product_id").set_index("product_id")[["card_name", "set_name"]]
    candidates = prices.join(card_info, how="left")
    candidates = candidates[candidates["set_name"].isin(relevant_sets)]

    if candidates.empty:
        return empty_result

    candidates = candidates.reset_index()

    # Apply the selected release-date scope BEFORE any ranking/selection
    # happens downstream, so Early Movers and its control pool see the
    # identical eligible population.
    candidates, scope_stats = apply_release_scope(
        candidates, as_of, release_scope=release_scope, cache_path=release_date_cache_path,
        scope_start_date=scope_start_date)

    if return_scope_stats:
        return candidates, scope_stats
    return candidates


def calculate_early_movers(db_path, limit=50, as_of=None, release_date_cache_path=DEFAULT_RELEASE_DATE_CACHE_PATH,
                            release_scope=DEFAULT_RELEASE_SCOPE, scope_start_date=RELEASE_SCOPE_START_DATE,
                            subtype_mode=SUBTYPE_MODE_LEGACY_UNVERIFIED):
    """
    Calculate "Early Movers" - cards showing the start of upward momentum,
    before they become major Top Gainers.
    
    V1 logic (simple and explainable):
    - Require 3 valid consecutive calendar dates ending on the analysis date:
      price_2_days_ago, previous_price, latest_price
    - Require latest_price > previous_price >= price_2_days_ago (building momentum)
    - percent_gain = (latest_price - price_2_days_ago) / price_2_days_ago * 100
    - Keep only 10% <= percent_gain <= 50% (bigger moves belong in Top Gainers)
    - Require at least $0.25 of dollar movement to reduce penny-price noise
    - release_scope defaults to "all_years" (2007_onward is an optional
      filter); see apply_release_scope. subtype_mode defaults to
      "legacy_unverified"; PRODUCTION alert generation must explicitly pass
      subtype_mode="verified" so it never mixes unverified legacy prices
      with newly subtype-tracked prices (see calculate_early_mover_candidates).
    
    Returns DataFrame with columns:
    [rank, product_id, card_name, set_name, price_2_days_ago, previous_price,
     latest_price, dollar_gain, percent_gain]
    
    Args:
        db_path: Path to prices.db
        limit: Number of early movers to return
    
    Returns:
        DataFrame sorted by percent_gain descending
    """
    candidates = calculate_early_mover_candidates(
        db_path, as_of=as_of, release_date_cache_path=release_date_cache_path,
        release_scope=release_scope, scope_start_date=scope_start_date, subtype_mode=subtype_mode)
    if candidates.empty:
        return pd.DataFrame()

    results_df = candidates[candidates["is_alert"]].drop(columns=["is_alert"])
    if results_df.empty:
        return pd.DataFrame()

    # Sort (highest recent percent gain first) and limit
    results_df = results_df.sort_values('percent_gain', ascending=False).head(limit)
    results_df = results_df.reset_index(drop=True)
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


def evaluate_horizon_outcome(prices_conn, product_id, start_date, start_price, horizon,
                              max_date, jump_percent, jump_dollars):
    """
    Look up the exact-date outcome for one product/start_price/horizon combination.

    Args:
        prices_conn: open sqlite3 connection to a prices.db-shaped table
        product_id: card id to look up
        start_date: datetime of the starting observation (signal or control date)
        start_price: price at start_date
        horizon: number of days ahead to evaluate
        max_date: datetime of the latest date present in prices (or None)
        jump_percent: percent-gain threshold for a "hit"
        jump_dollars: dollar-gain threshold for a "hit"

    Returns:
        (status, price, pct_return, hit) where status is "ok", "pending", or
        "unavailable"; price/pct_return/hit are None unless status == "ok".
    """
    target_date = start_date + timedelta(days=horizon)
    target_date_str = target_date.strftime("%Y-%m-%d")

    if not start_price or not math.isfinite(start_price) or start_price <= 0:
        return "unavailable", None, None, None
    if max_date is None or target_date > max_date:
        return "pending", None, None, None

    row = prices_conn.execute(
        "SELECT market_price FROM prices WHERE product_id = ? AND date = ?",
        (product_id, target_date_str)
    ).fetchone()
    price = row[0] if row and row[0] is not None else None

    if price is None or not math.isfinite(price) or price <= 0:
        return "unavailable", None, None, None

    pct_return = ((price - start_price) / start_price) * 100
    hit = pct_return >= jump_percent and price - start_price >= jump_dollars
    return "ok", price, pct_return, hit


def calculate_early_movers_backtest(prices_db_path, signals_db_path, horizons=(3, 7, 14),
                                   jump_percent=20.0, jump_dollars=1.0):
    """
    Backtest saved Early Mover signals using future prices already in the price DB.
    
    For each row in early_mover_signals, looks up the exact-date market_price
    at signal_date + N days for each horizon (no substitution of nearby dates).
    A horizon is "pending" if that date hasn't happened yet (beyond the latest
    date in prices), or "unavailable" if the date has passed but has no price row.
    
    Args:
        prices_db_path: Path to prices.db
        signals_db_path: Path to signals.db
        horizons: day offsets to evaluate (default 3, 7, 14)
    
    Returns:
        dict with:
          "signals": list of dicts, one per signal, each containing
              product_id, card_name, set_name, signal_date, signal_price,
              and for every horizon N: f"price_{N}d", f"return_{N}d", f"status_{N}d"
              (status is "ok", "pending", or "unavailable")
          "summary": dict keyed by horizon -> {count, avg_return, percent_positive}
              computed only over "ok" observations
    """
    if not all(math.isfinite(v) and v >= 0 for v in (jump_percent, jump_dollars)):
        raise ValueError("Jump thresholds must be finite and nonnegative")
    prices_conn = sqlite3.connect(prices_db_path)
    signals_conn = sqlite3.connect(signals_db_path)

    max_date_str = prices_conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]

    signal_rows = signals_conn.execute(
        '''
        SELECT product_id, card_name, set_name, signal_date, signal_price
        FROM early_mover_signals
        ORDER BY signal_date DESC, card_name
        '''
    ).fetchall()
    signals_conn.close()

    max_date = datetime.strptime(max_date_str, "%Y-%m-%d") if max_date_str else None
    returns_by_horizon = {h: [] for h in horizons}
    hits_by_horizon = {h: 0 for h in horizons}
    statuses = {h: {"pending": 0, "unavailable": 0} for h in horizons}

    signals = []
    for product_id, card_name, set_name, signal_date_str, signal_price in signal_rows:
        signal_date = datetime.strptime(signal_date_str, "%Y-%m-%d")

        signal_result = {
            "product_id": product_id,
            "card_name": card_name,
            "set_name": set_name,
            "signal_date": signal_date_str,
            "signal_price": signal_price
        }

        for horizon in horizons:
            status, price, pct_return, hit = evaluate_horizon_outcome(
                prices_conn, product_id, signal_date, signal_price, horizon,
                max_date, jump_percent, jump_dollars)

            if status == "ok":
                returns_by_horizon[horizon].append(pct_return)
                hits_by_horizon[horizon] += int(hit)
            else:
                statuses[horizon][status] += 1
            signal_result[f"jump_{horizon}d"] = hit

            signal_result[f"price_{horizon}d"] = price
            signal_result[f"return_{horizon}d"] = pct_return
            signal_result[f"status_{horizon}d"] = status

        signals.append(signal_result)

    prices_conn.close()

    summary = {}
    for horizon in horizons:
        returns = returns_by_horizon[horizon]
        count = len(returns)
        if count == 0:
            summary[horizon] = {"count": 0, "avg_return": None, "percent_positive": None}
        else:
            summary[horizon] = {
                "count": count,
                "avg_return": sum(returns) / count,
                "percent_positive": (sum(1 for r in returns if r > 0) / count) * 100
            }
        summary[horizon].update({
            "median_return": median(returns) if count else None,
            "jump_count": hits_by_horizon[horizon],
            "jump_rate": 100 * hits_by_horizon[horizon] / count if count else None,
            "miss_count": count - hits_by_horizon[horizon],
            **statuses[horizon],
        })

    return {"signals": signals, "summary": summary, "latest_price_date": max_date_str,
            "unique_products": len({s[0] for s in signal_rows}),
            "jump_percent": jump_percent, "jump_dollars": jump_dollars}
