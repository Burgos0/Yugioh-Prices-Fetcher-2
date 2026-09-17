"""
Yu-Gi-Oh daily price fetcher using the live TCGCSV per-set API.

TCGCSV's live responses do not expose a price-effective date. Each complete
fetch is therefore stored under the UTC date on which it was observed. The
fetch is rejected if it crosses a UTC date boundary, so one database date
never contains observations from two retrieval dates.
"""
import sys
import os
import json
import sqlite3
import time
from datetime import datetime, timezone
import requests

# Allow `python scripts/fetch_prices.py` (direct script, no repo root on
# sys.path) as well as `python -m scripts.fetch_prices` to resolve the
# `app` package, since this script now imports app.subtype_policy.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.subtype_policy import select_subtype

# Constants
CATEGORY_ID = 2  # Yu-Gi-Oh on TCGplayer/TCGCSV
BASE_API = "https://tcgcsv.com/tcgplayer"
DB_PATH = "data/prices.db"
MIN_EXPECTED_DAILY_ROWS = 40000  # Normal daily count is ~47,000+
REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_DELAY = 1  # seconds, doubled each retry (exponential backoff)
TCGCSV_REQUEST_INTERVAL = 0.5
TCGCSV_401_COOLDOWN = 60
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 yugioh-price-fetcher/2.0"
_last_tcgcsv_request_at = None


def current_utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_target_date(date_arg=None):
    """Return today's UTC observation date, optionally validating a supplied date."""
    observation_date = current_utc_date()
    if date_arg:
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: Invalid date format '{date_arg}'. Expected YYYY-MM-DD.")
            sys.exit(1)
        parsed_date = parsed.strftime("%Y-%m-%d")
        if parsed_date != observation_date:
            print(f"ERROR: Live observations must use today's UTC date ({observation_date}), "
                  f"not {parsed_date}.")
            sys.exit(1)
    return observation_date


def is_tcgcsv_live_url(url):
    return url == BASE_API or url.startswith(f"{BASE_API}/")


def pace_tcgcsv_request(url):
    """Keep live TCGCSV request starts at least 0.5 seconds apart."""
    global _last_tcgcsv_request_at
    if not is_tcgcsv_live_url(url):
        return

    now = time.monotonic()
    if _last_tcgcsv_request_at is not None:
        wait = TCGCSV_REQUEST_INTERVAL - (now - _last_tcgcsv_request_at)
        if wait > 0:
            time.sleep(wait)
            now += wait
    _last_tcgcsv_request_at = now


def fetch_json(url):
    """Fetch JSON with retry logic and exponential backoff."""
    headers = {"User-Agent": USER_AGENT}
    last_error = None
    is_tcgcsv = is_tcgcsv_live_url(url)

    for attempt in range(MAX_RETRIES):
        retry_delay = RETRY_DELAY * (2 ** attempt)
        try:
            pace_tcgcsv_request(url)
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.RequestException) as e:
            last_error = e
        else:
            if r.status_code == 200:
                try:
                    data = r.json()
                    if "results" in data:
                        return data["results"]
                    return data
                except ValueError as e:
                    last_error = e
            elif r.status_code == 429 or r.status_code >= 500:
                last_error = requests.HTTPError(f"HTTP {r.status_code} (retryable)")
            elif r.status_code == 401 and is_tcgcsv:
                last_error = requests.HTTPError("HTTP 401 (retryable for TCGCSV)")
                retry_delay = TCGCSV_401_COOLDOWN
            else:
                last_error = requests.HTTPError(f"HTTP {r.status_code}")
                # For 404 or other 4xx, stop retrying unless 429
                if r.status_code != 429:
                    break
        if attempt < MAX_RETRIES - 1:
            print(f"  Request failed ({last_error}). Retrying in {retry_delay}s...")
            time.sleep(retry_delay)

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def init_db(db_path=DB_PATH):
    """Ensure database directory, prices table, and subtype-tracking table exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prices (
        product_id INTEGER,
        card_name TEXT,
        set_name TEXT,
        low_price REAL,
        mid_price REAL,
        high_price REAL,
        market_price REAL,
        direct_low_price REAL,
        date TEXT,
        PRIMARY KEY (product_id, date)
    )
    """)
    # Tracks which printing/subtype (e.g. "1st Edition" vs "Unlimited") each
    # product's price row is sourced from, per the explicit subtype policy
    # in app.subtype_policy. Rows imported before this table existed have no
    # entry here -- their subtype provenance is unverified, not "none".
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_subtypes (
        product_id INTEGER PRIMARY KEY,
        subtype TEXT,
        established_date TEXT
    )
    """)
    conn.commit()
    return conn


def load_tracked_subtypes(db_path=DB_PATH):
    """Load each product's previously-established tracked subtype, if any."""
    tracked = {}
    if not os.path.exists(db_path):
        return tracked
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_subtypes'")
        if cur.fetchone():
            for pid, subtype in cur.execute("SELECT product_id, subtype FROM product_subtypes"):
                tracked[pid] = subtype
        conn.close()
    except Exception as e:
        print(f"Warning: Could not read tracked subtypes from {db_path}: {e}")
    return tracked


def save_tracked_subtypes(conn, newly_established, target_date_str):
    """Persist newly-established product->subtype assignments (first-seen policy)."""
    if not newly_established:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO product_subtypes (product_id, subtype, established_date) VALUES (?, ?, ?)",
        [(pid, subtype, target_date_str) for pid, subtype in newly_established.items()]
    )


def load_known_metadata(db_path=DB_PATH):
    """Load known card names and set names from existing database."""
    card_names = {}
    set_names_by_card = {}

    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT product_id, card_name, set_name FROM prices WHERE card_name IS NOT NULL GROUP BY product_id")
            for pid, cname, sname in cur.fetchall():
                card_names[pid] = cname
                if sname:
                    set_names_by_card[pid] = sname
            conn.close()
        except Exception as e:
            print(f"Warning: Could not read existing metadata from {db_path}: {e}")

    return card_names, set_names_by_card


def fetch_set_names():
    """Fetch current set/group mappings from TCGCSV API."""
    try:
        groups = fetch_json(f"{BASE_API}/{CATEGORY_ID}/groups")
        return {str(g["groupId"]): g.get("name") for g in groups if "groupId" in g}
    except Exception as e:
        print(f"Warning: Failed to fetch live groups list: {e}")
        return {}


def fetch_live_dataset(known_cards):
    """Fetch every Yu-Gi-Oh group's products and prices; fail on any missing group."""
    groups = fetch_json(f"{BASE_API}/{CATEGORY_ID}/groups")
    if not isinstance(groups, list) or not groups:
        raise RuntimeError("Live groups response was empty or invalid")

    parsed_group_prices = {}
    set_names = {}
    for index, group in enumerate(groups, 1):
        gid = group.get("groupId")
        if gid is None:
            raise RuntimeError("Live groups response contained a group without groupId")
        gid = str(gid)
        set_names[gid] = group.get("name")
        products = fetch_json(f"{BASE_API}/{CATEGORY_ID}/{gid}/products")
        prices = fetch_json(f"{BASE_API}/{CATEGORY_ID}/{gid}/prices")
        if not isinstance(products, list) or not isinstance(prices, list):
            raise RuntimeError(f"Group {gid} returned an invalid products or prices response")
        for product in products:
            pid = product.get("productId")
            name = product.get("name")
            if pid is not None and name:
                known_cards[pid] = name
        parsed_group_prices[gid] = prices
        if index % 100 == 0:
            print(f"Fetched {index:,}/{len(groups):,} sets...")

    return parsed_group_prices, set_names


def build_records(parsed_group_prices, target_date_str, known_cards, set_names,
                  tracked_subtypes=None):
    """Build one validated price record per product using the subtype policy."""
    records = []
    newly_established_subtypes = {}
    skipped_missing_tracked_subtype = []
    tracked_subtypes = tracked_subtypes or {}

    for gid, items in parsed_group_prices.items():
        set_name = set_names.get(str(gid))
        items_by_product = {}
        for p in items:
            pid = p.get("productId")
            if pid is None:
                continue
            items_by_product.setdefault(pid, []).append(p)

        for pid, product_items in items_by_product.items():
            resolution = select_subtype(product_items, tracked_subtypes.get(pid))
            if resolution["status"] == "missing_tracked":
                skipped_missing_tracked_subtype.append(pid)
                continue

            p = resolution["item"]
            prices = (
                p.get("lowPrice"),
                p.get("midPrice"),
                p.get("highPrice"),
                p.get("marketPrice"),
                p.get("directLowPrice"),
            )
            if not any(value is not None for value in prices):
                continue
            if resolution["status"] == "established":
                newly_established_subtypes[pid] = resolution["subtype"]

            records.append((
                pid, known_cards.get(pid), set_name, *prices, target_date_str
            ))

    if skipped_missing_tracked_subtype:
        print(f"Skipped {len(skipped_missing_tracked_subtype)} product(s) whose tracked "
              f"subtype is missing from today's data (never auto-switched printings).")

    return (records, len(parsed_group_prices), newly_established_subtypes,
            skipped_missing_tracked_subtype)


def parse_and_build_records(cat_dir, target_date_str, known_cards, set_names, tracked_subtypes=None):
    """
    Parse price files for all groups under the category directory.
    Fetches missing product metadata for new cards when needed.

    When a productId has multiple same-day printing rows (subtypes), the
    explicit policy in app.subtype_policy.select_subtype decides which one
    to keep -- never "whichever came last in the file". `tracked_subtypes`
    (product_id -> subtype) records subtypes already established for a
    product on a prior run; newly established choices are returned so the
    caller can persist them.
    """

    group_dirs = [d for d in os.listdir(cat_dir) if os.path.isdir(os.path.join(cat_dir, d))]
    print(f"Found {len(group_dirs)} Yu-Gi-Oh set directories in archive.")

    # Identify groups that might have unknown products
    groups_with_unknown_products = {}
    parsed_group_prices = {}

    for gid in group_dirs:
        gpath = os.path.join(cat_dir, gid)
        pfile = os.path.join(gpath, "prices")
        if not os.path.isfile(pfile):
            pfile = os.path.join(gpath, "prices.json")
        if not os.path.isfile(pfile):
            continue

        try:
            with open(pfile, "r") as f:
                data = json.load(f)
            items = data.get("results", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            parsed_group_prices[gid] = items

            # Check for unknown product IDs in this group
            for p in items:
                pid = p.get("productId")
                if pid and pid not in known_cards:
                    groups_with_unknown_products.setdefault(gid, []).append(pid)
        except Exception as e:
            print(f"Warning: Failed to parse price file in group {gid}: {e}")

    # For the few groups with unknown products, fetch product names
    if groups_with_unknown_products:
        print(f"Resolving names for {sum(len(v) for v in groups_with_unknown_products.values())} new products across {len(groups_with_unknown_products)} sets...")
        for gid in groups_with_unknown_products:
            try:
                products = fetch_json(f"{BASE_API}/{CATEGORY_ID}/{gid}/products")
                for prod in products:
                    pid = prod.get("productId")
                    pname = prod.get("name")
                    if pid and pname:
                        known_cards[pid] = pname
            except Exception as e:
                print(f"  Warning: Could not fetch product names for group {gid}: {e}")

    return build_records(
        parsed_group_prices, target_date_str, known_cards, set_names, tracked_subtypes)


def write_records_atomically(db_path, target_date_str, records, newly_established_subtypes):
    """Write and validate the complete observation in one SQLite transaction."""
    conn = init_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (target_date_str,))
        rows_before = cur.fetchone()[0]
        cur.execute("DELETE FROM prices WHERE date = ?", (target_date_str,))
        cur.executemany(
            """
            INSERT OR REPLACE INTO prices(
                product_id, card_name, set_name, low_price, mid_price, high_price,
                market_price, direct_low_price, date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records
        )
        save_tracked_subtypes(conn, newly_established_subtypes, target_date_str)
        cur.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (target_date_str,))
        daily_rows = cur.fetchone()[0]
        if daily_rows < MIN_EXPECTED_DAILY_ROWS:
            raise RuntimeError(
                f"Database row count for {target_date_str} is {daily_rows:,}; "
                f"expected at least {MIN_EXPECTED_DAILY_ROWS:,}")
        conn.commit()
        return rows_before, daily_rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    start_time = time.time()
    target_date_str = parse_target_date(sys.argv[1] if len(sys.argv) > 1 else None)
    print("=" * 60)
    print(f"Yu-Gi-Oh Price Fetcher (Live API Mode)")
    print(f"UTC observation date: {target_date_str}")
    print("=" * 60)

    # Preload metadata from existing DB
    known_cards, _ = load_known_metadata(DB_PATH)
    print(f"Loaded {len(known_cards):,} known cards from existing database.")

    # Preload each product's previously-established tracked subtype/printing
    tracked_subtypes = load_tracked_subtypes(DB_PATH)
    print(f"Loaded {len(tracked_subtypes):,} tracked product subtypes from existing database.")

    try:
        parsed_group_prices, set_names = fetch_live_dataset(known_cards)
        if current_utc_date() != target_date_str:
            raise RuntimeError(
                "Fetch crossed a UTC date boundary; refusing mixed-date observations")
        records, sets_processed, newly_established_subtypes, _ = build_records(
            parsed_group_prices, target_date_str, known_cards, set_names, tracked_subtypes)

        print(f"Parsed {len(records):,} valid price records across {sets_processed} sets.")

        # Validation: Check minimum expected rows before touching DB
        if len(records) < MIN_EXPECTED_DAILY_ROWS:
            print(f"\nERROR: Daily dataset is INCOMPLETE. Parsed {len(records):,} rows "
                  f"(expected at least {MIN_EXPECTED_DAILY_ROWS:,}).")
            print("Refusing to commit incomplete dataset to database.")
            return 1

        rows_before, daily_rows = write_records_atomically(
            DB_PATH, target_date_str, records, newly_established_subtypes)

        elapsed = time.time() - start_time
        print("\n" + "=" * 60)
        print("DAILY FETCH SUMMARY")
        print("=" * 60)
        print(f"Date:                {target_date_str}")
        print(f"Sets processed:      {sets_processed}")
        print(f"Records parsed:      {len(records):,}")
        print(f"Rows before update:  {rows_before:,}")
        print(f"Rows for date in DB: {daily_rows:,}")
        print(f"Runtime:             {elapsed:.2f} seconds")
        print(f"Status:              SUCCESS (>= {MIN_EXPECTED_DAILY_ROWS:,} rows)")
        print("=" * 60)
        return 0
    except Exception as e:
        print(f"\nERROR: Live price fetch failed: {e}")
        print("No price observations were written.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
