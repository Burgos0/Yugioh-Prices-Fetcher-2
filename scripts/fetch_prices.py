import requests
import sqlite3
from datetime import date
import os
import time

os.makedirs("data", exist_ok=True)

CATEGORY_ID = 2
BASE = "https://tcgcsv.com/tcgplayer"
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds

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
    """Fetch JSON from URL with retry logic and exponential backoff."""
    headers = {
        "User-Agent": "Mozilla/5.0 yugioh-price-fetcher/1.0"
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            
            if r.status_code != 200:
                if attempt < MAX_RETRIES - 1:
                    sleep_time = RETRY_DELAY * (2 ** attempt)
                    print(f"  HTTP {r.status_code}. Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue
                r.raise_for_status()
            
            try:
                data = r.json()
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    sleep_time = RETRY_DELAY * (2 ** attempt)
                    print(f"  JSON parse error. Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue
                raise ValueError(f"Failed to parse JSON: {e}")
            
            if "results" not in data:
                raise ValueError("Missing 'results' in response")
            
            return data["results"]
            
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
    
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries")


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
        continue

conn.commit()
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

if stats['failed_sets']:
    print(f"\n⚠️  Failed sets ({len(stats['failed_sets'])}):")
    for set_name, error in stats['failed_sets']:
        print(f"  • {set_name}")
        print(f"    └─ {error[:100]}")

print("="*50)
