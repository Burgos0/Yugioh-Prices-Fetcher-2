import sqlite3
import pandas as pd

# connect to database
conn = sqlite3.connect("data/prices.db")

# load all data
df = pd.read_sql("SELECT * FROM prices", conn)

# find newest date
latest_date = df["date"].max()

print("Latest date:", latest_date)

# today's prices
today = df[df["date"] == latest_date]

# prices from previous days
previous = df[df["date"] < latest_date].groupby("product_id").last()

# combine datasets
merged = today.merge(previous, on="product_id", suffixes=("_now", "_old"))

# calculate gain
merged["gain"] = (merged["price_now"] - merged["price_old"]) / merged["price_old"]

# sort
top = merged.sort_values("gain", ascending=False).head(20)

print("\nTop Weekly Gainers\n")

for _, row in top.iterrows():
    print(row["product_id"], round(row["gain"] * 100, 2), "%")
