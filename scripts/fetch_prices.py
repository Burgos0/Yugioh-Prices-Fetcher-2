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
    price REAL,
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
        price = p.get("marketPrice")

        if price is None:
        continue

        cur.execute(
            "INSERT OR REPLACE INTO prices(product_id, card_name, set_name, price, date) VALUES (?, ?, ?, ?, ?)",
            (product_id, card_name, set_name, price, today)
        )

        cur.execute(
            "INSERT OR REPLACE INTO prices(product_id, card_name, set_name, price, date) VALUES (?, ?, ?, ?, ?)",
            (p["productId"], card_name, set_name, price, today)
        )

conn.commit()
conn.close()

print("Saved prices for", today)
