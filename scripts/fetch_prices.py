import requests
import sqlite3
from datetime import date
import os

os.makedirs("data", exist_ok=True)

CATEGORY_ID = 2
BASE = "https://tcgcsv.com/tcgplayer"

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

def get_groups():
    r = requests.get(f"{BASE}/{CATEGORY_ID}/groups")
    return r.json()["results"]
    
def get_products(group_id):
    r = requests.get(f"{BASE}/{CATEGORY_ID}/{group_id}/products")
    return r.json()["results"]

def get_prices(group_id):
    r = requests.get(f"{BASE}/{CATEGORY_ID}/{group_id}/prices")
    return r.json()["results"]

groups = get_groups()

today = str(date.today())

for g in groups[:20]:
    gid = g["groupId"]
    set_name = g["name"]

    print("Fetching:", set_name)

    products = get_products(gid)

    product_lookup = {}

    for prod in products:
        product_lookup[prod["productId"]] = prod.get("name")

    prices = get_prices(gid)

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
conn.close()

print("Saved prices for", today)
