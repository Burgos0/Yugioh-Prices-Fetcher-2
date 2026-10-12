"""
LOCAL-ONLY follow-up experiment: why did the PROVISIONAL candidate lists lose
to production Top Gainers, and does any variant in a fixed grid beat it?

Only as-of dates with >= 9 snapshots of history AND >= --horizon later
snapshots are evaluated; every list is rebuilt from data on or before the
as-of date. Reads prices.db once (never writes it); writes
comparison_followup.md/.json to --out, which must be outside data/.

Sections: A confirmation x band split, B floor grid, C benchmarks (all
eligible + random 20), D flat-aware forward returns at 3 and 7 snapshots,
E fresh-jump outcomes, F first-appearance-only metrics and date-level
bootstrap confidence intervals.

Usage:
    python -m scripts.compare_candidate_gainers_followup --db PATH --out DIR
"""
import argparse
import json
import math
import time
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from app import analysis
from app.analysis import (
    _load_printing_prices,
    calculate_candidate_gainers,
    calculate_top_gainers,
    detect_spike,
)
from scripts.compare_candidate_gainers import IDENTITY, REPO_DATA_DIR, band_of, md_table

HORIZONS = (3, 7)
PRICE_GRID = (1.0, 2.0, 3.0, 5.0)
DOLLAR_GRID = (0.5, 1.0, 1.5, 3.0)
PCT_GRID = (15.0, 25.0, 50.0)
DEFAULT_CELL = (5.0, 3.0, 25.0)
# Loosest corner of the grid; every cell is a filter of this ranked list.
LOOSE_CONFIG = {"min_current_price": min(PRICE_GRID), "min_dollar_gain": min(DOLLAR_GRID),
                "min_percent_gain": min(PCT_GRID)}
SPLIT_EDGES = (2.0, 5.0, 20.0, 100.0)
FLAT_TOL = 0.005   # same threshold production uses for "price changed"
UNIVERSE_FLOOR = 2.0
TOP_EVAL, TOP_SPLIT = 20, 50
EPS = 1e-9


def filter_cell(ranked, min_price, min_dollar, min_pct, confirmed_only=False):
    """Apply one grid cell's floors to an already-ranked candidate list.
    Order is preserved, so the result equals a direct run at those floors."""
    keep = ((ranked["current_value"] >= min_price - EPS)
            & (ranked["dollar_gain"] >= min_dollar - EPS)
            & (ranked["percent_gain"] >= min_pct - EPS))
    if confirmed_only:
        keep &= ranked["status"] == "CONFIRMED"
    return ranked[keep]


def forward_stats(ret, chg):
    """Forward-return summary that separates flat (no sale) from moved."""
    ok = ret.notna() & chg.notna()
    ret, chg = ret[ok], chg[ok]
    n = int(len(ret))
    if n == 0:
        return {"n": 0, "median": None, "mean": None, "pct_rose": None, "pct_flat": None,
                "pct_fell": None, "pct_changed": None, "n_changed": 0, "median_if_changed": None}
    moved = chg.abs() >= FLAT_TOL
    return {
        "n": n, "median": round(float(ret.median()), 2), "mean": round(float(ret.mean()), 2),
        "pct_rose": round(100 * float((chg >= FLAT_TOL).mean()), 1),
        "pct_flat": round(100 * float((~moved).mean()), 1),
        "pct_fell": round(100 * float((chg <= -FLAT_TOL).mean()), 1),
        "pct_changed": round(100 * float(moved.mean()), 1), "n_changed": int(moved.sum()),
        "median_if_changed": round(float(ret[moved].median()), 2) if moved.any() else None,
    }


def classify_jump_outcome(baseline, jump_price, end_price):
    """Where a fresh jump ended up: CONTINUED above the jump price, REVERTED
    to within 10% of baseline, HELD >= 50% of the jump, else PARTIAL."""
    if not all(v is not None and math.isfinite(v) and v > 0 for v in (baseline, jump_price, end_price)):
        return "MISSING"
    if end_price >= jump_price + FLAT_TOL:
        return "CONTINUED"
    if end_price <= baseline * 1.10 + EPS:
        return "REVERTED"
    if end_price - baseline >= 0.5 * (jump_price - baseline) - EPS:
        return "HELD"
    return "PARTIAL"


def first_appearance(rows):
    """Keep only each card printing's earliest as-of date (rows sorted by date)."""
    return rows.sort_values("date", kind="mergesort").drop_duplicates(IDENTITY, keep="first")


def bootstrap_ci(stat, n_dates, n_boot=1000, seed=0):
    """Date-level bootstrap: resample as-of dates with replacement.
    `stat(date_indices, rng)` returns one number (or None to skip)."""
    rng = np.random.default_rng(seed)
    vals = [stat(rng.integers(0, n_dates, n_dates), rng) for _ in range(n_boot)]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return [round(float(lo), 2), round(float(hi), 2)]


def pooled_median(per_date, idx):
    vals = np.concatenate([per_date[i] for i in idx]) if len(idx) else np.array([])
    return float(np.median(vals)) if len(vals) else None


def median_diff(a, b, idx):
    left, right = pooled_median(a, idx), pooled_median(b, idx)
    return None if left is None or right is None else left - right


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/prices.db")
    parser.add_argument("--out", required=True)
    parser.add_argument("--horizon", type=int, default=7, help="Snapshots after as-of required to evaluate.")
    parser.add_argument("--random-reps", type=int, default=200)
    parser.add_argument("--boot", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261012)
    args = parser.parse_args()
    horizons = tuple(sorted(set(HORIZONS) | {args.horizon}))

    out_dir = Path(args.out).resolve()
    if out_dir == REPO_DATA_DIR or REPO_DATA_DIR in out_dir.parents:
        parser.error(f"--out must be outside production caches ({REPO_DATA_DIR})")
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    df = _load_printing_prices(args.db)
    if df.empty:
        raise SystemExit(f"No printing_prices data readable at {args.db}")
    df["date"] = pd.to_datetime(df["date"])
    snapshots = sorted(df["date"].unique())
    market = df.pivot_table(index=IDENTITY, columns="date", values="market_price", aggfunc="first")
    evaluable = [d for i, d in enumerate(snapshots) if i >= 8 and i + args.horizon < len(snapshots)]
    if not evaluable:
        raise SystemExit("No as-of date has both 9 snapshots of history and enough later snapshots.")
    t_loaded = time.perf_counter()

    def attach_forward(frame, pos):
        frame = frame.copy()
        keys = pd.MultiIndex.from_arrays([frame["product_id"], frame["printing"]])
        prices = market.reindex(keys)
        start = prices[snapshots[pos]].to_numpy(dtype=float)
        valid_start = np.isfinite(start) & (start > 0)
        for h in horizons:
            end = prices[snapshots[pos + h]].to_numpy(dtype=float)
            ok = valid_start & np.isfinite(end) & (end > 0)
            frame[f"chg{h}"] = np.where(ok, end - start, np.nan)
            frame[f"ret{h}"] = np.where(ok, 100 * (end - start) / np.where(ok, start, 1), np.nan)
            frame[f"end{h}"] = np.where(ok, end, np.nan)
        frame["entry"] = np.where(valid_start, start, np.nan)
        return frame

    parts = {"old": [], "weekly": [], "fresh": [], "universe": []}
    qualifying, timings = [], []
    for as_of in evaluable:
        t_a = time.perf_counter()
        pos = snapshots.index(as_of)
        truncated = df[df["date"] <= as_of]
        with mock.patch.object(analysis, "_load_printing_prices", lambda _path: truncated.copy()):
            old = calculate_top_gainers(None, limit=10 ** 9)
        cand = calculate_candidate_gainers(None, df=truncated, config=LOOSE_CONFIG)
        ev, dates = cand["evaluated"], cand["dates"]
        recent = [pd.Timestamp(d) for d in dates["recent"]]
        baseline = [pd.Timestamp(d) for d in dates["baseline"]]

        # Fresh-jump cards get the production status of their weekly move.
        fresh = filter_cell(cand["fresh"], *DEFAULT_CELL).copy()
        weekly_vals = ev.set_index(IDENTITY)[["baseline_value", "current_value"]]
        keys = pd.MultiIndex.from_frame(fresh[IDENTITY])
        hist = truncated[truncated["date"] >= baseline[0]]
        hist = hist[pd.MultiIndex.from_frame(hist[IDENTITY]).isin(keys)].groupby(IDENTITY, sort=False)
        statuses = []
        for pid, pr in zip(fresh["product_id"], fresh["printing"]):
            b, c = weekly_vals.loc[(pid, pr)]
            statuses.append("NO_WEEKLY_GAIN" if c <= b else detect_spike(
                pid, b, c, hist.get_group((pid, pr)), recent[-1], recent, pr, baseline))
        fresh["status"] = statuses

        universe = ev[~ev["weekly_reason"].isin(["incomplete_window", "invalid_price"])
                      & ev["relevant"] & (ev["current_value"] >= UNIVERSE_FLOOR - EPS)]
        universe = universe[IDENTITY + ["card_name", "set_name", "baseline_value", "current_value"]]
        as_of_str = str(pd.Timestamp(as_of).date())
        for name, frame in (("old", old), ("weekly", cand["weekly"]), ("fresh", fresh),
                            ("universe", universe)):
            frame = attach_forward(frame.reset_index(drop=True), pos)
            frame["date"] = as_of_str
            frame["pos"] = range(len(frame))
            frame["split_band"] = [band_of(v, list(SPLIT_EDGES)) for v in frame["current_value"]]
            parts[name].append(frame)
        qualifying.append({"as_of": as_of_str, "recent": dates["recent"], "baseline": dates["baseline"],
                           "exit_3": str(pd.Timestamp(snapshots[pos + 3]).date()),
                           f"exit_{args.horizon}": str(pd.Timestamp(snapshots[pos + args.horizon]).date()),
                           "old": len(old), "default_weekly": len(filter_cell(cand["weekly"], *DEFAULT_CELL)),
                           "fresh": len(fresh), "universe": len(universe)})
        timings.append(time.perf_counter() - t_a)

    rows = {k: pd.concat(v, ignore_index=True) for k, v in parts.items()}
    date_list = [q["as_of"] for q in qualifying]
    H = args.horizon

    def per_date_top(frame, k=TOP_EVAL):
        return frame.groupby("date", sort=False).head(k)

    def stats_both(frame, h=H):
        """Stats on all rows and on first appearances only."""
        return {"all": forward_stats(frame[f"ret{h}"], frame[f"chg{h}"]),
                "first": forward_stats(first_appearance(frame)[f"ret{h}"], first_appearance(frame)[f"chg{h}"])}

    def by_date(frame, h=H):
        return [frame.loc[frame["date"] == d, f"ret{h}"].dropna().to_numpy() for d in date_list]

    # ---- A: confirmation x band split, top 50 per date --------------------
    section_a = []
    weekly_default = filter_cell(rows["weekly"], *DEFAULT_CELL)
    # Old top-50 is all CONFIRMED (confirmed-first ranking), so also show its
    # best 50 UNCONFIRMED cards per date as the comparison group.
    for name, top in (("old", per_date_top(rows["old"], TOP_SPLIT)),
                      ("old UNCONFIRMED-only top-50",
                       per_date_top(rows["old"][rows["old"]["status"] == "UNCONFIRMED"], TOP_SPLIT)),
                      ("candidate weekly", per_date_top(weekly_default, TOP_SPLIT))):
        for status in ("CONFIRMED", "UNCONFIRMED"):
            for band in [band_of(1.0, list(SPLIT_EDGES))] + [band_of(v, list(SPLIT_EDGES)) for v in SPLIT_EDGES]:
                sub = top[(top["status"] == status) & (top["split_band"] == band)]
                if sub.empty:
                    continue
                s = stats_both(sub)
                section_a.append({"list": name, "status": status, "band": band, **s["all"],
                                  "first_n": s["first"]["n"], "first_median": s["first"]["median"],
                                  "first_pct_rose": s["first"]["pct_rose"]})
            s = stats_both(top[top["status"] == status])
            section_a.append({"list": name, "status": status, "band": "all", **s["all"],
                              "first_n": s["first"]["n"], "first_median": s["first"]["median"],
                              "first_pct_rose": s["first"]["pct_rose"]})

    # ---- B: floor grid ------------------------------------------------------
    def cell_result(price, dollar, pct, confirmed_only):
        frame = filter_cell(rows["weekly"], price, dollar, pct, confirmed_only)
        top = per_date_top(frame)
        out = {"price": price, "dollar": dollar, "pct": pct,
               "confirmed_only": confirmed_only,
               "avg_qualifying": round(len(frame) / len(date_list), 1)}
        for h in horizons:
            s = forward_stats(top[f"ret{h}"], top[f"chg{h}"])
            out.update({f"h{h}_{k}": v for k, v in s.items()})
        f = first_appearance(top)
        out.update({"first_n": int(f[f"ret{H}"].notna().sum()),
                    "first_median": forward_stats(f[f"ret{H}"], f[f"chg{H}"])["median"]})
        out["_top"] = top
        return out

    grid = [cell_result(p, d, 25.0, c) for c in (False, True) for p in PRICE_GRID for d in DOLLAR_GRID]

    def score(r):
        return (r[f"h{H}_median"] if r[f"h{H}_median"] is not None else -math.inf,
                r[f"h{H}_mean"] if r[f"h{H}_mean"] is not None else -math.inf)

    best_two = sorted([r for r in grid if not r["confirmed_only"]], key=score, reverse=True)[:2]
    pct_pass = [cell_result(r["price"], r["dollar"], pct, c)
                for r in best_two for pct in PCT_GRID for c in (False, True)]
    all_cells = grid + pct_pass
    best_cell = max(all_cells, key=score)

    # ---- C: benchmarks ------------------------------------------------------
    rng = np.random.default_rng(args.seed)
    universe_by_date = {d: rows["universe"][rows["universe"]["date"] == d] for d in date_list}

    def random_draw(local_rng):
        return pd.concat([g.sample(n=min(TOP_EVAL, len(g)), random_state=int(local_rng.integers(2 ** 31)))
                          for g in universe_by_date.values()], ignore_index=True)

    random_reps = [random_draw(rng) for _ in range(args.random_reps)]

    def random_summary(h):
        stats = [forward_stats(r[f"ret{h}"], r[f"chg{h}"]) for r in random_reps]
        return {k: (round(float(np.median([s[k] for s in stats if s[k] is not None])), 2)
                    if any(s[k] is not None for s in stats) else None) for k in stats[0]}

    random_per_date = []
    for d in date_list:
        meds = [np.nanmedian(r.loc[r["date"] == d, f"ret{H}"]) for r in random_reps]
        random_per_date.append({"as_of": d, "median_of_medians": round(float(np.median(meds)), 2),
                                "p2.5": round(float(np.percentile(meds, 2.5)), 2),
                                "p97.5": round(float(np.percentile(meds, 97.5)), 2),
                                "universe_size": int(len(universe_by_date[d]))})

    # ---- D: flat-aware returns at 3 and 7 snapshots for every list ---------
    named_lists = {
        "old top-20": per_date_top(rows["old"]),
        "candidate weekly top-20 (default $5/$3/25%)": per_date_top(weekly_default),
        "fresh top-20 (default floors)": per_date_top(rows["fresh"]),
        f"best grid cell top-20 ({best_cell['price']:g}/{best_cell['dollar']:g}/{best_cell['pct']:g}%"
        f"{', confirmed only' if best_cell['confirmed_only'] else ''})": best_cell["_top"],
        "benchmark: all relevant >= $2": rows["universe"],
    }
    section_d = []
    for name, frame in named_lists.items():
        for h in horizons:
            s = stats_both(frame, h)
            section_d.append({"list": name, "horizon": h, **s["all"],
                              "first_n": s["first"]["n"], "first_median": s["first"]["median"],
                              "first_median_if_changed": s["first"]["median_if_changed"]})
    for h in horizons:
        section_d.append({"list": f"benchmark: random 20 (median of {args.random_reps})", "horizon": h,
                          **random_summary(h)})

    # ---- E: fresh-jump outcomes -------------------------------------------
    fresh = rows["fresh"].copy()
    fresh["outcome"] = [classify_jump_outcome(b, j, e) for b, j, e in
                        zip(fresh["baseline_value"], fresh["current_value"], fresh[f"end{H}"])]
    fresh["unchanged"] = fresh[f"chg{H}"].abs() < FLAT_TOL
    outcomes = ["CONTINUED", "HELD", "PARTIAL", "REVERTED", "MISSING"]
    section_e = []
    for label, frame in (("all rows", fresh), ("first appearance", first_appearance(fresh))):
        for split in ("all", "badge", "recent_above_baseline_count", "status"):
            groups = [("all", frame)] if split == "all" else sorted(frame.groupby(split), key=lambda g: str(g[0]))
            for key, g in groups:
                counts = g["outcome"].value_counts()
                section_e.append({"rows": label, "split": split, "group": str(key), "n": int(len(g)),
                                  **{o: int(counts.get(o, 0)) for o in outcomes},
                                  "pct_reverted": round(100 * counts.get("REVERTED", 0) / len(g), 1),
                                  "pct_held_or_cont": round(100 * (counts.get("HELD", 0) + counts.get("CONTINUED", 0))
                                                            / len(g), 1),
                                  "held_unchanged": int((g["unchanged"] & (g["outcome"] == "HELD")).sum())})

    # ---- F: bootstrap CIs (date-level) -------------------------------------
    old_pd = by_date(named_lists["old top-20"])
    universe_pd = by_date(rows["universe"])

    def random_pd(local_rng):
        return by_date(random_draw(local_rng))

    variants = {name: by_date(frame) for name, frame in named_lists.items()}
    section_f = []
    for name, pdates in variants.items():
        point = pooled_median(pdates, range(len(date_list)))
        ci = bootstrap_ci(lambda idx, _r, p=pdates: pooled_median(p, idx), len(date_list), args.boot, args.seed)
        vs_old = bootstrap_ci(lambda idx, _r, p=pdates: median_diff(p, old_pd, idx),
                              len(date_list), args.boot, args.seed)
        vs_random = bootstrap_ci(lambda idx, r, p=pdates: median_diff(p, random_pd(r), idx),
                                 len(date_list), args.boot, args.seed)
        vs_universe = bootstrap_ci(lambda idx, _r, p=pdates: median_diff(p, universe_pd, idx),
                                   len(date_list), args.boot, args.seed)
        mean_ci = bootstrap_ci(lambda idx, _r, p=pdates: (lambda v: float(np.mean(v)) if len(v) else None)(
                                   np.concatenate([p[i] for i in idx])),
                               len(date_list), args.boot, args.seed)
        section_f.append({"list": name, "median": round(point, 2) if point is not None else None,
                          "median_ci": ci, "mean_ci": mean_ci, "diff_vs_old_ci": vs_old,
                          "diff_vs_random_ci": vs_random, "diff_vs_universe_ci": vs_universe})
    for cell in all_cells:
        cell["diff_vs_old_ci"] = bootstrap_ci(
            lambda idx, _r, p=by_date(cell["_top"]): median_diff(p, old_pd, idx),
            len(date_list), args.boot, args.seed)
    random_point = float(np.median([pooled_median(by_date(r), range(len(date_list))) for r in random_reps]))
    section_f.append({"list": f"benchmark: random 20 (median of {args.random_reps})",
                      "median": round(random_point, 2),
                      "median_ci": bootstrap_ci(lambda idx, r: pooled_median(random_pd(r), idx),
                                                len(date_list), args.boot, args.seed)})
    t_done = time.perf_counter()

    runtime = {"load_and_pivot_s": round(t_loaded - t0, 2),
               "per_date_build_s": round(sum(timings) / len(timings), 2),
               "total_s": round(t_done - t0, 2)}
    strip = lambda r: {k: v for k, v in r.items() if k != "_top"}
    report = {
        "label": "PROVISIONAL local research experiment; not production output",
        "db": str(Path(args.db).resolve()), "horizon": H, "horizons": list(horizons),
        "evaluable_as_of_dates": qualifying, "loose_config": LOOSE_CONFIG,
        "flat_tolerance": FLAT_TOL, "seed": args.seed, "random_reps": args.random_reps, "boot": args.boot,
        "A": section_a, "B_grid": [strip(r) for r in grid], "B_best_two": [strip(r) for r in best_two],
        "B_pct_pass": [strip(r) for r in pct_pass], "best_cell": strip(best_cell),
        "C_random_per_date": random_per_date, "D": section_d, "E": section_e, "F": section_f,
        "runtime": runtime,
    }
    with open(out_dir / "comparison_followup.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    cell_cols = ["price", "dollar", "pct", "confirmed_only", "avg_qualifying", f"h{H}_n", f"h{H}_median",
                 f"h{H}_mean", f"h{H}_pct_rose", f"h{H}_pct_flat", f"h{H}_pct_fell",
                 f"h{H}_median_if_changed", "h3_median", "h3_pct_rose", "first_n", "first_median",
                 "diff_vs_old_ci"]
    stat_cols = ["n", "median", "mean", "pct_rose", "pct_flat", "pct_fell", "pct_changed", "median_if_changed"]
    md = ["# Candidate gainers follow-up (PROVISIONAL, local only)\n",
          f"DB: `{report['db']}`  \nForward horizon for judging: {H} snapshots (also 3). "
          f"Flat = |price change| < ${FLAT_TOL}. Returns are market price from the as-of snapshot.\n",
          "## Evaluable as-of dates\n",
          md_table(qualifying, ["as_of", "baseline", "recent", "exit_3", f"exit_{H}", "old",
                                "default_weekly", "fresh", "universe"]),
          f"\n## A. Confirmation x band, top-{TOP_SPLIT} per date ({H}-snapshot)\n",
          md_table(section_a, ["list", "status", "band"] + stat_cols + ["first_n", "first_median",
                                                                        "first_pct_rose"]),
          f"\n## B. Floor grid (pct >= 25%), top-{TOP_EVAL} per date, confirmed-first ranking\n",
          md_table(grid, cell_cols),
          "\nBest two cells (all statuses, by pooled median then mean): "
          + ", ".join(f"${r['price']:g}/${r['dollar']:g}" for r in best_two) + "\n",
          "\n### B2. Percent-floor pass at the best two cells\n",
          md_table(pct_pass, cell_cols),
          f"\nCells whose diff-vs-old CI excludes zero: "
          f"{sum(1 for c in all_cells if c['diff_vs_old_ci'] and c['diff_vs_old_ci'][0] > 0)} "
          f"of {len(all_cells)} evaluated (all on the same {len(date_list)} dates).\n",
          f"\nOverall best variant (selected after the fact): ${best_cell['price']:g}/${best_cell['dollar']:g}/"
          f"{best_cell['pct']:g}%{' confirmed only' if best_cell['confirmed_only'] else ''}\n",
          f"\n## C. Random-20 benchmark per date ({args.random_reps} draws, seed {args.seed})\n",
          md_table(random_per_date, ["as_of", "universe_size", "median_of_medians", "p2.5", "p97.5"]),
          "\n## D. Flat-aware forward returns, every list\n",
          md_table(section_d, ["list", "horizon"] + stat_cols + ["first_n", "first_median",
                                                                 "first_median_if_changed"]),
          f"\n## E. Fresh-jump outcomes after {H} snapshots\n",
          "CONTINUED >= jump price; REVERTED <= 1.10x baseline; HELD >= 50% of jump kept; else PARTIAL. "
          "`held_unchanged` = HELD only because no sale moved the price.\n",
          md_table(section_e, ["rows", "split", "group", "n"] + outcomes
                   + ["pct_reverted", "pct_held_or_cont", "held_unchanged"]),
          f"\n## F. Date-level bootstrap ({args.boot} resamples, 95% CI) of pooled top-20 median {H}-snapshot return\n",
          md_table(section_f, ["list", "median", "median_ci", "mean_ci", "diff_vs_old_ci",
                               "diff_vs_random_ci", "diff_vs_universe_ci"]),
          "\n## Runtime\n", md_table([runtime], list(runtime))]
    text = "\n".join(md) + "\n"
    with open(out_dir / "comparison_followup.md", "w") as f:
        f.write(text)
    print(text)
    print(f"Wrote {out_dir / 'comparison_followup.md'} and comparison_followup.json")


if __name__ == "__main__":
    main()
