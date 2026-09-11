import requests
import sqlite3
from datetime import date
import os
import time

os.makedirs("data", exist_ok=True)

CATEGORY_ID = 2
BASE = "https://tcgcsv.com/tcgplayer"
MAX_RETRIES = 5
RETRY_DELAY = 1  # seconds, doubled each retry (exponential backoff)
SET_DELAY = 0.5  # seconds to wait between sets, to reduce rate-limit/server pressure
MIN_EXPECTED_DAILY_ROWS = 40000  # normal daily count is ~47,000
MAX_FAILED_SET_RATIO = 0.10  # fail the run if more than 10% of sets failed

conn = sqlite3.connect("data/prices.db")
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

def fetch_json(url):
    """Fetch JSON from URL with retry logic and exponential backoff.
    
    Retries on: request timeouts, connection errors, HTTP 429, and HTTP 5xx.
    Does NOT retry other HTTP errors (e.g. 404) since those won't succeed on retry.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 yugioh-price-fetcher/1.0"
    }
    
    last_error = None
    
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
        else:
            if r.status_code == 200:
                try:
                    data = r.json()
                    if "results" not in data:
                        raise ValueError("Missing 'results' in response")
                    return data["results"]
                except ValueError as e:
                    last_error = e
            elif r.status_code == 429 or r.status_code >= 500:
                # Rate-limited or server error - retryable
                last_error = requests.HTTPError(f"HTTP {r.status_code} (retryable)")
            else:
                # Non-retryable client error - fail immediately
                r.raise_for_status()
        
        if attempt < MAX_RETRIES - 1:
            sleep_time = RETRY_DELAY * (2 ** attempt)
            print(f"  {last_error}. Retrying in {sleep_time}s...")
            time.sleep(sleep_time)
    
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {last_error}")


def get_groups():
    return fetch_json(f"{BASE}/{CATEGORY_ID}/groups")


def get_products(group_id):
    return fetch_json(f"{BASE}/{CATEGORY_ID}/{group_id}/products")


def get_prices(group_id):
    return fetch_json(f"{BASE}/{CATEGORY_ID}/{group_id}/prices")


def get_record_count():
    """Get current row count in prices table."""
    cur.execute("SELECT COUNT(*) FROM prices")
    return cur.fetchone()[0]


def get_daily_row_count(day):
    """Get row count in prices table for a specific date."""
    cur.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (day,))
    return cur.fetchone()[0]



# ===== MAIN EXECUTION =====
start_time = time.time()

# Track statistics
stats = {
    'attempted': 0,
    'succeeded': 0,
    'failed': 0,
    'records_inserted': 0,
    'failed_sets': []
}

print("Starting Yu-Gi-Oh price fetch...\n")

try:
    groups = get_groups()
    print(f"Found {len(groups)} card sets\n")
except Exception as e:
    print(f"ERROR: Failed to fetch groups: {e}")
    conn.close()
    exit(1)

today = str(date.today())

for g in groups:
    try:
        stats['attempted'] += 1
        gid = g["groupId"]
        set_name = g["name"]
        
        print(f"[{stats['attempted']}/{len(groups)}] Fetching: {set_name}...", end=" ")
        
        # Get products and prices for this set
        products = get_products(gid)
        product_lookup = {}
        
        for prod in products:
            product_lookup[prod["productId"]] = prod.get("name")
        
        prices = get_prices(gid)
        
        # Count records before insert
        records_before = get_record_count()
        
        # Insert price records
        for p in prices:
            product_id = p["productId"]
            card_name = product_lookup.get(product_id)
            
            low_price = p.get("lowPrice")
            mid_price = p.get("midPrice")
            high_price = p.get("highPrice")
            market_price = p.get("marketPrice")
            direct_low_price = p.get("directLowPrice")
            
            values = [v for v in [low_price, mid_price, high_price, market_price, direct_low_price] if v is not None]
            
            if not values:
                continue
                
            cur.execute(
                """
                INSERT OR REPLACE INTO prices(
                    product_id,
                    card_name,
                    set_name,
                    low_price,
                    mid_price,
                    high_price,
                    market_price,
                    direct_low_price,
                    date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    card_name,
                    set_name,
                    low_price,
                    mid_price,
                    high_price,
                    market_price,
                    direct_low_price,
                    today
                )
            )
        
        conn.commit()
        
        # Count records after insert
        records_after = get_record_count()
        records_added = records_after - records_before
        stats['records_inserted'] += records_added
        stats['succeeded'] += 1
        
        print(f"✓ ({records_added} records)")
        
    except Exception as e:
        stats['failed'] += 1
        stats['failed_sets'].append((set_name, str(e)))
        print(f"✗ FAILED: {e}")
    
    finally:
        # Small delay between sets to reduce rate-limit/server pressure
        time.sleep(SET_DELAY)

conn.commit()

# Final row count for today, queried before closing the connection
daily_rows = get_daily_row_count(today)
conn.close()

# ===== SUMMARY REPORT =====
elapsed = time.time() - start_time

print("\n" + "="*50)
print("DAILY FETCH SUMMARY")
print("="*50)
print(f"Date: {today}")
print(f"Sets attempted:  {stats['attempted']}")
print(f"Sets succeeded:  {stats['succeeded']}")
print(f"Sets failed:     {stats['failed']}")
print(f"Records inserted: {stats['records_inserted']}")
print(f"Runtime:         {elapsed:.2f} seconds")
print(f"Rows for {today}: {daily_rows}")

if stats['failed_sets']:
    print(f"\n⚠️  Failed sets ({len(stats['failed_sets'])}):")
    for set_name, error in stats['failed_sets']:
        print(f"  • {set_name}")
        print(f"    └─ {error[:100]}")

print("="*50)

# ===== COMPLETENESS CHECK =====
# GitHub Actions must FAIL if the dataset is incomplete, instead of looking green.
failed_ratio = (stats['failed'] / stats['attempted']) if stats['attempted'] else 0
incomplete = False

if daily_rows < MIN_EXPECTED_DAILY_ROWS:
    print(f"\nERROR: Daily fetch is INCOMPLETE. Rows for {today}: {daily_rows} "
          f"(expected at least {MIN_EXPECTED_DAILY_ROWS})")
    print(f"Failed sets: {stats['failed']} / {stats['attempted']}")
    incomplete = True

if failed_ratio > MAX_FAILED_SET_RATIO:
    print(f"\nERROR: Too many sets failed ({stats['failed']} / {stats['attempted']} "
          f"= {failed_ratio * 100:.1f}%, max allowed is {MAX_FAILED_SET_RATIO * 100:.0f}%)")
    incomplete = True

if incomplete:
    exit(1)
