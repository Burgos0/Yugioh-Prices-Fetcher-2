"""
LOCAL-ONLY research comparison: production Top Gainers vs the PROVISIONAL
candidate weekly and fresh-jump lists (app.analysis.calculate_candidate_gainers).

Reads prices.db (printing_prices) once; never writes to it. Writes reports
only to --out, which must be outside the repository's data/ directory
(production caches). Nothing here is used by the site or daily pipeline.

For each of the latest --max-dates eligible observation dates (>= 9
snapshots of history), every list is rebuilt from data on or before that
date only (relevance inputs included). For as-of dates with >= --horizon
later snapshots, each list's top-20 forward market-price return is measured
from the as-of market price to the market price --horizon snapshots later.

Usage:
    python -m scripts.compare_candidate_gainers --db data/prices.db --out /tmp/candidate_gainers
"""
import argparse
import json
import math
import os
import time
from pathlib import Path
from statistics import median
from unittest import mock

import pandas as pd

from app import analysis
from app.analysis import (
    CANDIDATE_GAINERS_CONFIG,
    _load_printing_prices,
    calculate_candidate_gainers,
    calculate_top_gainers,
)

REPO_DATA_DIR = (Path(__file__).resolve().parent.parent / "data").resolve()
IDENTITY = ["product_id", "printing"]
LIST_NAMES = ("old", "weekly", "fresh")


def band_of(value, edges):
    if not math.isfinite(value) or value < edges[0]:
        return f"<${edges[0]:g}"
    for lo, hi in zip(edges, edges[1:]):
        if value < hi:
            return f"${lo:g}-{hi:g}"
    return f"${edges[-1]:g}+"


def summarize(returns):
    if not returns:
        return {"n": 0, "median_return_pct": None, "pct_rose": None, "pct_fell": None}
    return {"n": len(returns), "median_return_pct": round(median(returns), 2),
            "pct_rose": round(100 * sum(r > 0 for r in returns) / len(returns), 1),
            "pct_fell": round(100 * sum(r < 0 for r in returns) / len(returns), 1)}


def md_table(rows, columns):
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join("" if row.get(c) is None else str(row.get(c)) for c in columns) + " |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/prices.db")
    parser.add_argument("--out", required=True, help="Report directory (must be outside data/).")
    parser.add_argument("--max-dates", type=int, default=14)
    parser.add_argument("--horizon", type=int, default=7, help="Forward snapshots for returns.")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--eval-top", type=int, default=20)
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    if out_dir == REPO_DATA_DIR or REPO_DATA_DIR in out_dir.parents:
        parser.error(f"--out must be outside production caches ({REPO_DATA_DIR})")
    out_dir.mkdir(parents=True, exist_ok=True)
    edges = list(CANDIDATE_GAINERS_CONFIG["price_band_edges"])

    t_start = time.perf_counter()
    df = _load_printing_prices(args.db)
    if df.empty:
        raise SystemExit(f"No printing_prices data readable at {args.db}")
    df["date"] = pd.to_datetime(df["date"])
    t_loaded = time.perf_counter()

    snapshot_dates = sorted(df["date"].unique())
    eligible = snapshot_dates[8:][-args.max_dates:]
    # One pivot of market prices for every forward-return lookup.
    market = df.pivot_table(index=IDENTITY, columns="date", values="market_price", aggfunc="first")
    position = {d: i for i, d in enumerate(snapshot_dates)}

    def forward_return(pid, printing, as_of):
        exit_pos = position[as_of] + args.horizon
        if exit_pos >= len(snapshot_dates) or (pid, printing) not in market.index:
            return None
        start = market.at[(pid, printing), as_of]
        end = market.at[(pid, printing), snapshot_dates[exit_pos]]
        if not all(pd.notna(v) and math.isfinite(v) and v > 0 for v in (start, end)):
            return None
        return 100 * (end - start) / start

    per_date, pooled, timings = [], {}, {"old": [], "candidate": []}
    skipped_forward, latest = [], None
    for as_of in eligible:
        truncated = df[df["date"] <= as_of]
        t0 = time.perf_counter()
        # Production list, unmodified, fed the as-of-truncated frame instead of the DB.
        with mock.patch.object(analysis, "_load_printing_prices", lambda _path: truncated.copy()):
            old_all = calculate_top_gainers(None, limit=10 ** 9)
        t1 = time.perf_counter()
        cand = calculate_candidate_gainers(None, df=truncated)
        t2 = time.perf_counter()
        timings["old"].append(t1 - t0)
        timings["candidate"].append(t2 - t1)

        lists = {"old": old_all, "weekly": cand["weekly"], "fresh": cand["fresh"]}
        for frame in lists.values():
            if not frame.empty:
                frame["band"] = [band_of(v, edges) for v in frame["current_value"]]
        as_of_str = str(pd.Timestamp(as_of).date())
        has_forward = position[as_of] + args.horizon < len(snapshot_dates)
        if not has_forward:
            skipped_forward.append(as_of_str)
        row = {"as_of": as_of_str, "recent": cand["dates"]["recent"],
               "baseline": cand["dates"]["baseline"],
               "qualifying": {k: int(len(v)) for k, v in lists.items()},
               "weekly_bands": {k: int(len(v)) for k, v in
                                cand["weekly"].groupby("band").groups.items()} if len(cand["weekly"]) else {},
               "exclusions": cand["exclusions"],
               "relevance_excluded": int(len(cand["relevance_excluded"])),
               "forward": {}}
        for name, frame in lists.items():
            top = frame.head(args.top)
            rets = [forward_return(p, pr, as_of) for p, pr in zip(top["product_id"], top["printing"])] \
                if has_forward else [None] * len(top)
            top = top.assign(fwd=rets)
            if has_forward:
                head = top.head(args.eval_top)
                vals = [r for r in head["fwd"] if pd.notna(r)]
                row["forward"][name] = {**summarize(vals), "missing_exit": int(head["fwd"].isna().sum())}
                bucket = pooled.setdefault(name, {"top": [], "badge": {}, "band": {}})
                bucket["top"] += vals
                for key, col in (("badge", "badge"), ("band", "band")):
                    if col not in top:
                        continue
                    for label, value in zip(top[col], top["fwd"]):
                        if pd.notna(value):
                            bucket[key].setdefault(label, []).append(value)
            lists[name] = top
        row["overlap_top"] = {
            "old_vs_weekly": len(set(zip(lists["old"]["product_id"], lists["old"]["printing"]))
                                 & set(zip(lists["weekly"]["product_id"], lists["weekly"]["printing"]))),
            "weekly_vs_fresh": len(set(zip(lists["weekly"]["product_id"], lists["weekly"]["printing"]))
                                   & set(zip(lists["fresh"]["product_id"], lists["fresh"]["printing"]))),
        }
        per_date.append(row)
        latest = (as_of_str, lists, cand)
    t_done = time.perf_counter()

    # Churn at the latest as-of date: production top-N vs candidate weekly top-N.
    as_of_str, lists, cand = latest
    old_keys = list(zip(lists["old"]["product_id"], lists["old"]["printing"]))
    new_keys = list(zip(lists["weekly"]["product_id"], lists["weekly"]["printing"]))
    reasons = cand["evaluated"].set_index(IDENTITY)["weekly_reason"]
    cols = ["product_id", "printing", "card_name", "set_name", "baseline_value", "current_value", "percent_gain"]

    def card_rows(frame, keys, extra):
        picked = frame[[k in keys for k in zip(frame["product_id"], frame["printing"])]].head(10)
        out = []
        for rec in picked[cols + [c for c in extra if c in picked]].to_dict("records"):
            for k in ("baseline_value", "current_value", "percent_gain"):
                rec[k] = round(float(rec[k]), 2)
            out.append(rec)
        return out

    entering = card_rows(lists["weekly"], set(new_keys) - set(old_keys), ["status", "badge", "band"])
    leaving = card_rows(lists["old"], set(old_keys) - set(new_keys), ["status", "band"])
    for rec in leaving:
        r = reasons.get((rec["product_id"], rec["printing"]))
        rec["candidate_reason"] = "ranked below top-N" if pd.isna(r) else r

    pooled_out = {name: {"top": summarize(b["top"]),
                         "by_badge": {k: summarize(v) for k, v in sorted(b["badge"].items())},
                         "by_band": {k: summarize(v) for k, v in sorted(b["band"].items())}}
                  for name, b in pooled.items()}
    runtime = {
        "load_seconds": round(t_loaded - t_start, 2),
        "old_seconds_per_date": round(sum(timings["old"]) / len(timings["old"]), 2),
        "candidate_seconds_per_date": round(sum(timings["candidate"]) / len(timings["candidate"]), 2),
        "total_seconds": round(t_done - t_start, 2),
        "rows": int(len(df)), "pairs": int(len(market)), "snapshots": len(snapshot_dates),
    }
    report = {
        "label": "PROVISIONAL local research comparison; not production output",
        "db": os.path.abspath(args.db), "config": CANDIDATE_GAINERS_CONFIG,
        "horizon_snapshots": args.horizon, "eval_top": args.eval_top, "top": args.top,
        "as_of_dates": [r["as_of"] for r in per_date],
        "skipped_forward_dates": skipped_forward,
        "per_date": per_date, "pooled_forward": pooled_out,
        "latest_as_of": as_of_str, "entering_top": entering, "leaving_top": leaving,
        "runtime": runtime,
    }
    with open(out_dir / "comparison.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    latest_lists = {
        "label": "PROVISIONAL candidate gainers; not production output",
        "dates": cand["dates"], "config": CANDIDATE_GAINERS_CONFIG,
        "weekly": cand["weekly"].to_dict("records"),
        "weekly_by_band": {k: v.to_dict("records") for k, v in cand["weekly_by_band"].items()},
        "fresh": cand["fresh"].to_dict("records"),
        "relevance_excluded": cand["relevance_excluded"][cols[:4]].to_dict("records"),
    }
    with open(out_dir / "candidate_latest.json", "w") as f:
        json.dump(latest_lists, f, indent=2, default=str)

    # Markdown summary.
    md = [f"# Candidate gainers comparison (PROVISIONAL, local only)\n",
          f"DB: `{report['db']}`  \nAs-of dates: {len(per_date)}  \n"
          f"Forward horizon: {args.horizon} snapshots, top-{args.eval_top} per list  \n"
          f"Skipped for forward returns (insufficient later data): {', '.join(skipped_forward) or 'none'}\n",
          "## Per-date selection and counts\n"]
    md.append(md_table([{
        "as_of": r["as_of"], "baseline": " ".join(d[5:] for d in r["baseline"]),
        "recent": " ".join(d[5:] for d in r["recent"]),
        "old": r["qualifying"]["old"], "weekly": r["qualifying"]["weekly"], "fresh": r["qualifying"]["fresh"],
        "bands (weekly)": ", ".join(f"{k}:{v}" for k, v in sorted(r["weekly_bands"].items())),
        "overlap old/weekly": r["overlap_top"]["old_vs_weekly"],
    } for r in per_date], ["as_of", "baseline", "recent", "old", "weekly", "fresh", "bands (weekly)",
                           "overlap old/weekly"]))
    reason_names = ["incomplete_window", "invalid_price", "price_floor", "dollar_floor", "pct_floor",
                    "relevance_filter"]
    md.append("\n## Exclusion reasons (weekly / fresh, first failing check)\n")
    md.append(md_table([{"as_of": r["as_of"], **{n: f"{r['exclusions']['weekly'].get(n, 0)} / "
                                                     f"{r['exclusions']['fresh'].get(n, 0)}"
                                                  for n in reason_names}}
                        for r in per_date], ["as_of"] + reason_names))
    md.append(f"\n## Forward {args.horizon}-snapshot market return, top-{args.eval_top} per date\n")
    md.append(md_table([{"as_of": r["as_of"], **{
        f"{n} median%": r["forward"][n]["median_return_pct"] for n in LIST_NAMES}, **{
        f"{n} %rose": r["forward"][n]["pct_rose"] for n in LIST_NAMES}, **{
        f"{n} n": r["forward"][n]["n"] for n in LIST_NAMES}}
        for r in per_date if r["forward"]],
        ["as_of"] + [f"{n} {m}" for m in ("median%", "%rose", "n") for n in LIST_NAMES]))
    md.append("\n## Pooled forward returns (all evaluated dates; same card can repeat across dates)\n")
    pooled_rows = []
    for name in LIST_NAMES:
        p = pooled_out.get(name)
        if not p:
            continue
        pooled_rows.append({"list": name, "slice": f"top-{args.eval_top}", **p["top"]})
        for key in ("by_badge", "by_band"):
            for label, s in p[key].items():
                pooled_rows.append({"list": name, "slice": f"top-{args.top} {label}", **s})
    md.append(md_table(pooled_rows, ["list", "slice", "n", "median_return_pct", "pct_rose", "pct_fell"]))
    md.append(f"\n## Entering top-{args.top} at {as_of_str} (candidate weekly, not in old)\n")
    md.append(md_table(entering, cols + ["status", "badge", "band"]))
    md.append(f"\n## Leaving top-{args.top} at {as_of_str} (in old, not in candidate weekly)\n")
    md.append(md_table(leaving, cols + ["status", "band", "candidate_reason"]))
    md.append("\n## Relevance filter exclusions at latest date (passed price floors)\n")
    md.append(md_table(latest_lists["relevance_excluded"], cols[:4]))
    md.append("\n## Runtime\n")
    md.append(md_table([runtime], list(runtime)))
    text = "\n".join(md) + "\n"
    with open(out_dir / "comparison.md", "w") as f:
        f.write(text)
    print(text)
    print(f"Wrote {out_dir / 'comparison.md'}, comparison.json, candidate_latest.json")


if __name__ == "__main__":
    main()
