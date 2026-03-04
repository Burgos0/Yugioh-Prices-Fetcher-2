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
price REAL,
date TEXT
)
""")

def get_groups():
    r = requests.get(f"{BASE}/{CATEGORY_ID}/groups")
    return r.json()["results"]

def get_prices(group_id):
    r = requests.get(f"{BASE}/{CATEGORY_ID}/{group_id}/prices")
    return r.json()["results"]

groups = get_groups()

today = str(date.today())

for g in groups[:20]:   # first 20 sets for now
    gid = g["groupId"]
    name = g["name"]

    print("Fetching:", name)

    prices = get_prices(gid)

    for p in prices:

        price = p.get("marketPrice")

        if price is None:
            continue

        cur.execute(
            "INSERT INTO prices VALUES (?, ?, ?)",
            (p["productId"], price, today)
        )

conn.commit()
conn.close()

print("Saved prices for", today)
