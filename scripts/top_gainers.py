import sqlite3
import pandas as pd
from datetime import timedelta

# connect to database
conn = sqlite3.connect("data/prices.db")

# load all data
df = pd.read_sql("SELECT * FROM prices", conn)

# convert dates to datetime
df["date"] = pd.to_datetime(df["date"])

# find newest date
latest_date = df["date"].max()

print("Latest date:", latest_date.date())

# today's prices
today = df[df["date"] == latest_date]

# target date 7 days earlier
week_ago = latest_date - timedelta(days=7)

# prices from closest available date at or before 7 days ago
previous = df[df["date"] <= week_ago].sort_values("date").groupby("product_id").last()

if previous.empty:
    print("Not enough history yet to calculate 7-day gainers.")
    conn.close()
    exit()

# combine datasets
merged = today.merge(previous, on="product_id", suffixes=("_now", "_old"))

# use market price for gain calculation
merged = merged[
    merged["market_price_now"].notna() &
    merged["market_price_old"].notna() &
    (merged["market_price_old"] > 0)
]

if merged.empty:
    print("No valid market price pairs found for gain calculation.")
    conn.close()
    exit()

# calculate gain
merged["gain"] = (
    merged["market_price_now"] - merged["market_price_old"]
) / merged["market_price_old"]

# sort
top = merged.sort_values("gain", ascending=False).head(20)

print("\nTop Weekly Gainers\n")

for _, row in top.iterrows():
    print(
        row["card_name_now"],
        "-",
        round(row["gain"] * 100, 2),
        "%",
        "| Set:",
        row["set_name_now"],
        "| Market:",
        row["market_price_old"],
        "->",
        row["market_price_now"]
    )

conn.close()
