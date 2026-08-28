from app.analysis import calculate_top_gainers

# Connect to database and calculate top gainers
gainers = calculate_top_gainers("data/prices.db", limit=50)

if gainers.empty:
    print("No gainers found. Data may not be available yet.")
    exit(0)

# ===== OUTPUT RESULTS =====
print("="*130)
print("TOP 50 WEEKLY GAINERS (Confirmed & Unconfirmed Spikes)")
print("="*130)
print(
    f"{'Rank':<6} {'Card Name':<40} {'Set':<35} {'Baseline':<12} {'Current':<12} "
    f"{'Gain $':<10} {'Gain %':<10} {'Status':<14}"
)
print("-"*130)

for _, row in gainers.iterrows():
    status_str = f"✓ {row['status']}" if row['status'] == "CONFIRMED" else f"⚠ {row['status']}"
    print(
        f"{row['rank']:<6} "
        f"{row['card_name']:<40} "
        f"{row['set_name']:<35} "
        f"${row['baseline_value']:<11.2f} "
        f"${row['current_value']:<11.2f} "
        f"${row['dollar_gain']:<9.2f} "
        f"{row['percent_gain']:<9.2f}% "
        f"{status_str:<14}"
    )

# ===== SUMMARY =====
confirmed_count = sum(1 for _, row in gainers.iterrows() if row['status'] == "CONFIRMED")
unconfirmed_count = sum(1 for _, row in gainers.iterrows() if row['status'] == "UNCONFIRMED")

print("\n" + "="*130)
print("SUMMARY")
print("="*130)
print(f"Analysis period: ~7 days ago vs recent 3-day average")
print(f"Top gainers shown: {len(gainers)}")
print(f"  ✓ CONFIRMED: {confirmed_count} (price increase sustained across multiple recent days)")
print(f"  ⚠ UNCONFIRMED: {unconfirmed_count} (potential spike or single-day anomaly)")
print("="*130)
