"""
Backtest Early Mover signals: for each saved signal, check what the card's
market price actually did 3/7/14 days later (using only exact-date matches).
"""
from app.analysis import calculate_early_movers_backtest

PRICES_DB = "data/prices.db"
SIGNALS_DB = "data/signals.db"
HORIZONS = (3, 7, 14)

result = calculate_early_movers_backtest(PRICES_DB, SIGNALS_DB, HORIZONS)

print("="*130)
print("EARLY MOVERS BACKTEST")
print("="*130)

for signal in result["signals"]:
    print(f"\nCard Name: {signal['card_name']}")
    print(f"Signal Date: {signal['signal_date']}")
    print(f"Signal Price: ${signal['signal_price']:.2f}")

    for horizon in HORIZONS:
        status = signal[f"status_{horizon}d"]

        if status != "ok":
            print(f"{horizon} Day Price: {status.capitalize()}")
            print(f"{horizon} Day Return: {status.capitalize()}")
            continue

        print(f"{horizon} Day Price: ${signal[f'price_{horizon}d']:.2f}")
        print(f"{horizon} Day Return: {signal[f'return_{horizon}d']:.2f}%")

# ===== SUMMARY STATISTICS =====
print("\n" + "="*130)
print("SUMMARY (completed observations only)")
print("="*130)

for horizon in HORIZONS:
    stats = result["summary"][horizon]

    if stats["count"] == 0:
        print(f"{horizon}-day: no completed signals yet")
        continue

    print(
        f"{horizon}-day: {stats['count']} completed signals | "
        f"avg return {stats['avg_return']:.2f}% | "
        f"{stats['percent_positive']:.2f}% positive"
    )

