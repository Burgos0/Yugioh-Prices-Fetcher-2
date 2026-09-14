"""
Yu-Gi-Oh daily price fetcher using TCGCSV daily archive.

Downloads the official TCGCSV daily archive (prices-YYYY-MM-DD.ppmd.7z),
extracts category ID 2 (Yu-Gi-Oh) prices, matches them with set and card metadata,
and performs an atomic batch update into SQLite (data/prices.db).
"""
import sys
import os
import shutil
import subprocess
import tempfile
import json
import sqlite3
import time
from datetime import datetime, timezone, timedelta
import requests

# Allow `python scripts/fetch_prices.py` (direct script, no repo root on
# sys.path) as well as `python -m scripts.fetch_prices` to resolve the
# `app` package, since this script now imports app.subtype_policy.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.subtype_policy import select_subtype

# Constants
CATEGORY_ID = 2  # Yu-Gi-Oh on TCGplayer/TCGCSV
BASE_API = "https://tcgcsv.com/tcgplayer"
ARCHIVE_URL_TEMPLATE = "https://tcgcsv.com/archive/tcgplayer/prices-{date}.ppmd.7z"
DB_PATH = "data/prices.db"
MIN_EXPECTED_DAILY_ROWS = 40000  # Normal daily count is ~47,000+
REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_DELAY = 1  # seconds, doubled each retry (exponential backoff)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 yugioh-price-fetcher/2.0"


def check_7z_available():
    """Confirm the `7z` CLI is available on PATH."""
    if shutil.which("7z") is None:
        print("ERROR: The `7z` command is not available in this environment.")
        print("Install it with: sudo apt-get update && sudo apt-get install -y p7zip-full")
        sys.exit(1)


def parse_target_date(date_arg=None):
    """Determine target date in YYYY-MM-DD format (defaults to UTC yesterday)."""
    if date_arg:
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d").date()
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid date format '{date_arg}'. Expected YYYY-MM-DD.")
            sys.exit(1)
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def fetch_json(url):
    """Fetch JSON with retry logic and exponential backoff."""
    headers = {"User-Agent": USER_AGENT}
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
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
            else:
                last_error = requests.HTTPError(f"HTTP {r.status_code}")
                # For 404 or other 4xx, stop retrying unless 429
                if r.status_code != 429:
                    break

        if attempt < MAX_RETRIES - 1:
            sleep_time = RETRY_DELAY * (2 ** attempt)
            print(f"  Request failed ({last_error}). Retrying in {sleep_time}s...")
            time.sleep(sleep_time)

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def download_archive(target_date_str, dest_path):
    """Download daily archive for target date."""
    url = ARCHIVE_URL_TEMPLATE.format(date=target_date_str)
    print(f"Downloading daily archive from: {url}")
    headers = {"User-Agent": USER_AGENT}

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
            if r.status_code == 200:
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                download_size = os.path.getsize(dest_path)
                print(f"Archive downloaded successfully ({download_size:,} bytes).")
                return
            elif r.status_code == 404:
                last_error = f"HTTP 404 Not Found (archive not published yet for {target_date_str})"
            elif r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code} (server error / rate limit)"
            else:
                last_error = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            last_error = str(e)

        if attempt < MAX_RETRIES - 1:
            sleep_time = RETRY_DELAY * (2 ** attempt)
            print(f"  Download attempt {attempt + 1} failed ({last_error}). Retrying in {sleep_time}s...")
            time.sleep(sleep_time)

    print(f"\nERROR: Failed to download archive for {target_date_str}: {last_error}")
    sys.exit(1)


def extract_archive(archive_path, extract_dir):
    """Extract .7z archive to target directory."""
    print(f"Extracting archive with 7-Zip...")
    result = subprocess.run(
        ["7z", "x", archive_path, f"-o{extract_dir}", "-y"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print("ERROR: 7-Zip extraction failed.")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        sys.exit(1)


def find_category_dir(extract_dir, category_id=CATEGORY_ID):
    """Locate the directory for category ID (2) inside extracted archive."""
    cat_str = str(category_id)
    for root, dirs, files in os.walk(extract_dir):
        rel = os.path.relpath(root, extract_dir)
        parts = rel.split(os.sep)
        # Direct category folder or inside date subfolder (<date>/<category_id>)
        if (len(parts) == 1 and parts[0] == cat_str) or (len(parts) == 2 and parts[1] == cat_str):
            return root

    print(f"ERROR: Category {category_id} directory not found in extracted archive.")
    sys.exit(1)


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

    records = []
    newly_established_subtypes = {}
    skipped_missing_tracked_subtype = []
    tracked_subtypes = tracked_subtypes or {}
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

    # Build DB records, resolving one price row per productId per the
    # explicit subtype policy (never "last item in file order wins").
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
                # Never silently switch to a different printing.
                skipped_missing_tracked_subtype.append(pid)
                continue

            p = resolution["item"]
            low = p.get("lowPrice")
            mid = p.get("midPrice")
            high = p.get("highPrice")
            market = p.get("marketPrice")
            dlow = p.get("directLowPrice")

            # Match existing behavior: skip records where all prices are None
            if not any(v is not None for v in [low, mid, high, market, dlow]):
                continue

            # Only establish subtype provenance once the price is actually
            # going to be saved -- never for a row that gets skipped.
            if resolution["status"] == "established":
                newly_established_subtypes[pid] = resolution["subtype"]

            card_name = known_cards.get(pid)
            records.append((
                pid,
                card_name,
                set_name,
                low,
                mid,
                high,
                market,
                dlow,
                target_date_str
            ))

    if skipped_missing_tracked_subtype:
        print(f"Skipped {len(skipped_missing_tracked_subtype)} product(s) whose tracked "
              f"subtype is missing from today's data (never auto-switched printings).")

    return records, len(parsed_group_prices), newly_established_subtypes, skipped_missing_tracked_subtype


def main():
    start_time = time.time()
    target_date_str = parse_target_date(sys.argv[1] if len(sys.argv) > 1 else None)
    print("=" * 60)
    print(f"Yu-Gi-Oh Price Fetcher (Archive Mode)")
    print(f"Target Date: {target_date_str}")
    print("=" * 60)

    check_7z_available()

    # Preload metadata from existing DB
    known_cards, set_names_by_card = load_known_metadata(DB_PATH)
    print(f"Loaded {len(known_cards):,} known cards from existing database.")

    # Preload each product's previously-established tracked subtype/printing
    tracked_subtypes = load_tracked_subtypes(DB_PATH)
    print(f"Loaded {len(tracked_subtypes):,} tracked product subtypes from existing database.")

    # Fetch set names from live API (single request)
    set_names = fetch_set_names()
    print(f"Loaded {len(set_names):,} set names from API.")

    # Work in temporary directory
    tmp_dir = tempfile.mkdtemp(prefix="yugioh_fetch_")
    try:
        archive_path = os.path.join(tmp_dir, f"prices-{target_date_str}.ppmd.7z")
        download_archive(target_date_str, archive_path)

        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        extract_archive(archive_path, extract_dir)

        cat_dir = find_category_dir(extract_dir, CATEGORY_ID)
        records, sets_processed, newly_established_subtypes, skipped_subtypes = parse_and_build_records(
            cat_dir, target_date_str, known_cards, set_names, tracked_subtypes)

        print(f"Parsed {len(records):,} valid price records across {sets_processed} sets.")

        # Validation: Check minimum expected rows before touching DB
        if len(records) < MIN_EXPECTED_DAILY_ROWS:
            print(f"\nERROR: Daily dataset is INCOMPLETE. Parsed {len(records):,} rows "
                  f"(expected at least {MIN_EXPECTED_DAILY_ROWS:,}).")
            print("Refusing to commit incomplete dataset to database.")
            sys.exit(1)

        # Atomic commit to SQLite
        conn = init_db(DB_PATH)
        cur = conn.cursor()

        try:
            cur.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (target_date_str,))
            rows_before = cur.fetchone()[0]

            cur.executemany(
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
                records
            )
            save_tracked_subtypes(conn, newly_established_subtypes, target_date_str)
            conn.commit()

            cur.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (target_date_str,))
            daily_rows = cur.fetchone()[0]
        except Exception as e:
            conn.rollback()
            conn.close()
            print(f"\nERROR: Database transaction failed: {e}")
            sys.exit(1)

        conn.close()

        # Final verification
        if daily_rows < MIN_EXPECTED_DAILY_ROWS:
            print(f"\nERROR: Daily fetch validation failed. Database row count for {target_date_str}: "
                  f"{daily_rows:,} (expected at least {MIN_EXPECTED_DAILY_ROWS:,}).")
            sys.exit(1)

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

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
