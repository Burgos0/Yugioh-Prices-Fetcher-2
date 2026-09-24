"""Yu-Gi-Oh daily price fetcher using the TCGCSV live API."""
import sys
import os
import shutil
import json
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
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
MAX_RETRIES = 3
RETRY_DELAY = 1
RATE_LIMIT_RETRY_DELAY = 5
TCGCSV_REQUEST_INTERVAL = 0.75
MAX_REQUEST_DELAY = 8.0
USER_AGENT = "YugiohPriceTracker/2.0"


def current_utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_target_date(date_arg=None):
    """Return an explicit date or yesterday's UTC date for archive fetching."""
    if date_arg:
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: Invalid date format '{date_arg}'. Expected YYYY-MM-DD.")
            sys.exit(1)
        return parsed.strftime("%Y-%m-%d")
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def parse_observation_date(date_arg=None):
    """Capture the live observation date once, at the beginning of a run."""
    if date_arg:
        try:
            return datetime.strptime(date_arg, "%Y-%m-%d").date().strftime("%Y-%m-%d")
        except ValueError:
            print(f"ERROR: Invalid date format '{date_arg}'. Expected YYYY-MM-DD.")
            sys.exit(1)
    return current_utc_date()


class TCGCSVClient:
    """Per-run client with pacing, bounded retries, and group-price caching."""

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.request_delay = TCGCSV_REQUEST_INTERVAL
        self._last_request_at = None
        self._group_price_cache = {}

    def _pace(self):
        now = time.monotonic()
        if self._last_request_at is not None:
            wait_time = self.request_delay - (now - self._last_request_at)
            if wait_time > 0:
                time.sleep(wait_time)
                now += wait_time
        self._last_request_at = now

    def _increase_delay(self):
        self.request_delay = min(MAX_REQUEST_DELAY, max(
            self.request_delay * 2, TCGCSV_REQUEST_INTERVAL))

    def fetch_json(self, url):
        """Fetch JSON with bounded rate-limit and transient-error retries."""
        last_error = None
        rate_limit_retry_used = False

        for attempt in range(MAX_RETRIES):
            self._pace()
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as error:
                last_error = error
                retry_delay = RETRY_DELAY * (2 ** attempt)
            else:
                if response.status_code == 200:
                    try:
                        data = response.json()
                        return data["results"] if isinstance(data, dict) and "results" in data else data
                    except ValueError as error:
                        last_error = error
                        retry_delay = RETRY_DELAY * (2 ** attempt)
                elif response.status_code in (401, 429):
                    last_error = requests.HTTPError(f"HTTP {response.status_code} (rate limited)")
                    self._increase_delay()
                    if rate_limit_retry_used:
                        break
                    rate_limit_retry_used = True
                    retry_delay = RATE_LIMIT_RETRY_DELAY
                elif response.status_code >= 500:
                    last_error = requests.HTTPError(f"HTTP {response.status_code} (retryable)")
                    retry_delay = RETRY_DELAY * (2 ** attempt)
                else:
                    last_error = requests.HTTPError(f"HTTP {response.status_code}")
                    break

            if attempt >= MAX_RETRIES - 1:
                break
            print(f"  Request failed ({last_error}). Retrying in {retry_delay}s...")
            time.sleep(retry_delay)

        raise RuntimeError(f"Failed to fetch {url}: {last_error}")

    def fetch_group_prices(self, group_id):
        """Fetch one group's prices at most once during this run."""
        group_id = str(group_id)
        if group_id not in self._group_price_cache:
            data = self.fetch_json(f"{BASE_API}/{CATEGORY_ID}/{group_id}/prices")
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected prices response for group {group_id}")
            self._group_price_cache[group_id] = data
        return self._group_price_cache[group_id]


def fetch_json(url, client=None):
    """Compatibility wrapper for callers that fetch one TCGCSV JSON endpoint."""
    return (client or TCGCSVClient()).fetch_json(url)


def check_7z_available():
    if shutil.which("7z") is None:
        raise RuntimeError("The `7z` command is not available in this environment")


def download_archive(target_date_str, dest_path):
    """Download exactly one fixed-date archive, without retrying HTTP 401."""
    url = ARCHIVE_URL_TEMPLATE.format(date=target_date_str)
    print(f"Downloading daily archive from: {url}")
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                url, headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT, stream=True)
            if response.status_code == 200:
                with open(dest_path, "wb") as archive_file:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        archive_file.write(chunk)
                print(f"Archive downloaded successfully ({os.path.getsize(dest_path):,} bytes).")
                return
            last_error = f"HTTP {response.status_code}"
            if response.status_code == 401 or response.status_code == 404:
                break
            if response.status_code != 429 and response.status_code < 500:
                break
        except requests.exceptions.RequestException as error:
            last_error = str(error)
        if attempt < MAX_RETRIES - 1:
            delay = RETRY_DELAY * (2 ** attempt)
            print(f"  Archive download failed ({last_error}). Retrying in {delay}s...")
            time.sleep(delay)
    raise RuntimeError(f"Failed to download archive for {target_date_str}: {last_error}")


def extract_archive(archive_path, extract_dir):
    result = subprocess.run(
        ["7z", "x", archive_path, f"-o{extract_dir}", "-y"],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"7-Zip extraction failed: {result.stderr or result.stdout}")


def find_category_dir(extract_dir, category_id=CATEGORY_ID):
    category = str(category_id)
    for root, dirs, files in os.walk(extract_dir):
        parts = os.path.relpath(root, extract_dir).split(os.sep)
        if parts == [category] or (len(parts) == 2 and parts[1] == category):
            return root
    raise RuntimeError(f"Category {category_id} directory not found in extracted archive")


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
    cur.execute("""
    CREATE TABLE IF NOT EXISTS printing_prices (
        product_id INTEGER,
        printing TEXT,
        card_name TEXT,
        set_name TEXT,
        low_price REAL,
        mid_price REAL,
        high_price REAL,
        market_price REAL,
        direct_low_price REAL,
        date TEXT,
        PRIMARY KEY (product_id, printing, date)
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


def fetch_set_names(client=None):
    """Fetch current set/group mappings from TCGCSV API."""
    groups = fetch_json(f"{BASE_API}/{CATEGORY_ID}/groups", client)
    return {str(g["groupId"]): g.get("name") for g in groups if "groupId" in g}


def fetch_live_group_prices(set_names, known_cards, client=None):
    """Fetch each group once and resolve metadata only for unknown products."""
    client = client or TCGCSVClient()
    parsed_group_prices = {}
    successful_groups = []
    failed_groups = []
    groups_with_unknown_products = {}

    for group_id in set_names:
        group_id = str(group_id)
        try:
            items = client.fetch_group_prices(group_id)
            parsed_group_prices[group_id] = items
            successful_groups.append(group_id)
            unknown_product_ids = {
                item.get("productId") for item in items
                if item.get("productId") is not None and item.get("productId") not in known_cards
            }
            if unknown_product_ids:
                groups_with_unknown_products[group_id] = unknown_product_ids
        except Exception as error:
            failed_groups.append(group_id)
            print(f"Warning: Failed to fetch prices for group {group_id}: {error}")

    for group_id in groups_with_unknown_products:
        try:
            products = fetch_json(
                f"{BASE_API}/{CATEGORY_ID}/{group_id}/products", client)
            for product in products:
                product_id = product.get("productId")
                product_name = product.get("name")
                if product_id is not None and product_name:
                    known_cards[product_id] = product_name
        except Exception as error:
            print(f"Warning: Could not fetch product names for group {group_id}: {error}")

    return parsed_group_prices, successful_groups, failed_groups


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


def build_printing_records(parsed_group_prices, target_date_str, known_cards, set_names):
    """Build one record for every valid TCGCSV row, preserving its printing."""
    records = []
    for gid, items in parsed_group_prices.items():
        set_name = set_names.get(str(gid))
        for item in items:
            product_id = item.get("productId")
            if product_id is None:
                continue
            prices = (
                item.get("lowPrice"),
                item.get("midPrice"),
                item.get("highPrice"),
                item.get("marketPrice"),
                item.get("directLowPrice"),
            )
            if not any(value is not None for value in prices):
                continue
            records.append(
                (product_id, item.get("subTypeName") or "Unspecified",
                 known_cards.get(product_id), set_name, *prices, target_date_str)
            )
    return records


def parse_archive_group_prices(cat_dir, known_cards):
    """
    Parse price files for all groups under the category directory.
    Fetches missing product metadata for new cards when needed.

    Product names are resolved only for groups containing IDs absent from the
    existing metadata cache. Name lookup is best-effort because prices remain
    valid without a newly resolved display name.
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

    return parsed_group_prices


def parse_and_build_records(cat_dir, target_date_str, known_cards, set_names,
                            tracked_subtypes=None):
    """Compatibility wrapper for callers that parse an extracted archive."""
    parsed_group_prices = parse_archive_group_prices(cat_dir, known_cards)
    return build_records(
        parsed_group_prices, target_date_str, known_cards, set_names,
        tracked_subtypes)


def write_records_atomically(db_path, target_date_str, records, newly_established_subtypes,
                             printing_records=None):
    """Write and validate the complete observation in one SQLite transaction."""
    if printing_records is None:
        printing_records = [
            (record[0], "Unspecified", record[1], record[2], *record[3:])
            for record in records
        ]
    conn = init_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (target_date_str,))
        rows_before = cur.fetchone()[0]
        cur.execute("DELETE FROM prices WHERE date = ?", (target_date_str,))
        cur.execute("DELETE FROM printing_prices WHERE date = ?", (target_date_str,))
        cur.executemany(
            """
            INSERT OR REPLACE INTO prices(
                product_id, card_name, set_name, low_price, mid_price, high_price,
                market_price, direct_low_price, date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            records
        )
        cur.executemany(
            """
            INSERT OR REPLACE INTO printing_prices(
                product_id, printing, card_name, set_name, low_price, mid_price,
                high_price, market_price, direct_low_price, date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            printing_records
        )
        save_tracked_subtypes(conn, newly_established_subtypes, target_date_str)
        cur.execute("SELECT COUNT(*) FROM prices WHERE date = ?", (target_date_str,))
        daily_rows = cur.fetchone()[0]
        if daily_rows < MIN_EXPECTED_DAILY_ROWS:
            raise RuntimeError(
                f"Database row count for {target_date_str} is {daily_rows:,}; "
                f"expected at least {MIN_EXPECTED_DAILY_ROWS:,}")
        printing_daily_rows = cur.execute(
            "SELECT COUNT(*) FROM printing_prices WHERE date = ?", (target_date_str,)
        ).fetchone()[0]
        if printing_daily_rows < MIN_EXPECTED_DAILY_ROWS:
            raise RuntimeError(
                f"Printing database row count for {target_date_str} is {printing_daily_rows:,}; "
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
    target_date_str = parse_observation_date(sys.argv[1] if len(sys.argv) > 1 else None)
    print("=" * 60)
    print("Yu-Gi-Oh Price Fetcher (Live API Mode)")
    print(f"Observation Date: {target_date_str}")
    print("=" * 60)

    # Preload metadata from existing DB
    known_cards, _ = load_known_metadata(DB_PATH)
    print(f"Loaded {len(known_cards):,} known cards from existing database.")

    # Preload each product's previously-established tracked subtype/printing
    tracked_subtypes = load_tracked_subtypes(DB_PATH)
    print(f"Loaded {len(tracked_subtypes):,} tracked product subtypes from existing database.")

    try:
        client = TCGCSVClient()
        print(f"Request delay: {client.request_delay:.2f} seconds")
        set_names = fetch_set_names(client)
        print(f"Loaded {len(set_names):,} set names from API.")

        parsed_group_prices, successful_groups, failed_groups = fetch_live_group_prices(
            set_names, known_cards, client)

        records, _, newly_established_subtypes, _ = build_records(
            parsed_group_prices, target_date_str, known_cards, set_names, tracked_subtypes)
        printing_records = build_printing_records(
            parsed_group_prices, target_date_str, known_cards, set_names)

        print(f"Groups processed: {len(successful_groups):,}")
        print(f"Groups failed: {len(failed_groups):,}")
        print(f"Legacy rows parsed: {len(records):,}")
        print(f"Printing rows parsed: {len(printing_records):,}")

        # Validation: Check minimum expected rows before touching DB
        if (len(records) < MIN_EXPECTED_DAILY_ROWS or
                len(printing_records) < MIN_EXPECTED_DAILY_ROWS):
            print(f"Final validation: FAILED (each table requires at least "
                f"{MIN_EXPECTED_DAILY_ROWS:,} rows).")
            print("Refusing to commit incomplete dataset to database.")
            return 1

        rows_before, daily_rows = write_records_atomically(
            DB_PATH, target_date_str, records, newly_established_subtypes, printing_records)

        elapsed = time.time() - start_time
        print("\n" + "=" * 60)
        print("DAILY FETCH SUMMARY")
        print("=" * 60)
        print(f"Date:                {target_date_str}")
        print(f"Groups processed:     {len(successful_groups):,}")
        print(f"Groups failed:        {len(failed_groups):,}")
        print(f"Legacy rows parsed:   {len(records):,}")
        print(f"Printing rows parsed:  {len(printing_records):,}")
        print(f"Rows before update:  {rows_before:,}")
        print(f"Rows for date in DB: {daily_rows:,}")
        print(f"Request delay:        {client.request_delay:.2f} seconds")
        print(f"Runtime:             {elapsed:.2f} seconds")
        print(f"Final validation:     PASSED (>= {MIN_EXPECTED_DAILY_ROWS:,} rows)")
        print("Status:               SUCCESS")
        print("=" * 60)
        return 0
    except Exception as e:
        print(f"\nERROR: Live API price fetch failed: {e}")
        print("No price observations were written.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
