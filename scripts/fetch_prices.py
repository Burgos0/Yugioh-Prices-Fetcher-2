import requests
import json

CATEGORY_ID = 2

BASE = "https://tcgcsv.com/tcgplayer"

def get_groups():
    url = f"{BASE}/{CATEGORY_ID}/groups"
    r = requests.get(url)
    data = r.json()
    return data["results"]

def get_prices(group_id):
    url = f"{BASE}/{CATEGORY_ID}/{group_id}/prices"
    r = requests.get(url)
    data = r.json()
    return data["results"]

groups = get_groups()

print("Total sets:", len(groups))

all_prices = []

for g in groups[:10]:  # limit first 10 sets for now
    gid = g["groupId"]
    name = g["name"]

    print("Fetching:", name)

    prices = get_prices(gid)

    for p in prices:
        all_prices.append({
            "productId": p["productId"],
            "marketPrice": p["marketPrice"]
        })

print("Total cards fetched:", len(all_prices))
