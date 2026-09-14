"""Research replay; never writes to the live signal or price databases.

Usage: python -m scripts.replay_early_movers --as-of 2026-09-04

Uses only prices through the selected date to select alert candidates and
control products. Outcomes use later exact-date prices. This is historical
research, not a live prediction record, and does not change the live
detector, its thresholds, or the website.
"""
import argparse
import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from statistics import median

from app.analysis import (
    calculate_early_movers,
    calculate_early_movers_backtest,
    calculate_early_mover_candidates,
    evaluate_horizon_outcome,
    DEFAULT_RELEASE_DATE_CACHE_PATH,
    RELEASE_SCOPE_START_DATE,
    DEFAULT_RELEASE_SCOPE,
    VALID_RELEASE_SCOPES,
    SUBTYPE_MODE_LEGACY_UNVERIFIED,
    VALID_SUBTYPE_MODES,
)

PRICE_BAND = 0.20  # controls must be priced within +/-20% of the alert's starting price
MAX_CONTROLS_PER_ALERT = 5


def build_control_groups(candidates, alerts):
    """
    Deterministically match each alert to up to 5 non-alert control products.

    Controls are drawn from `candidates` (the same eligibility pool as the
    detector: relevant-set membership + valid consecutive-date history as of
    the replay date), excluding alerts themselves, restricted to the alert's
    own set_name, and priced within +/-20% of the alert's starting price
    (latest_price on the replay date). Selection order uses only information
    known on the replay date (absolute price distance, then product_id as a
    deterministic tiebreak) -- never future returns.

    Returns (matches, unmatched_alerts) where:
      matches: list of dicts {alert: <row dict>, controls: [<row dict>, ...]}
                for alerts with at least one control match
      unmatched_alerts: list of alert row dicts with zero eligible controls
    """
    if alerts.empty:
        return [], []

    alert_ids = set(alerts["product_id"])
    pool = candidates[~candidates["product_id"].isin(alert_ids)]

    matches = []
    unmatched_alerts = []
    for alert in alerts.to_dict("records"):
        start_price = alert["latest_price"]
        low, high = start_price * (1 - PRICE_BAND), start_price * (1 + PRICE_BAND)
        same_set = pool[
            (pool["set_name"] == alert["set_name"]) &
            (pool["latest_price"] >= low) &
            (pool["latest_price"] <= high)
        ].copy()

        if same_set.empty:
            unmatched_alerts.append(alert)
            continue

        same_set["price_distance"] = (same_set["latest_price"] - start_price).abs()
        same_set = same_set.sort_values(["price_distance", "product_id"])
        controls = same_set.head(MAX_CONTROLS_PER_ALERT).to_dict("records")
        matches.append({"alert": alert, "controls": controls})

    return matches, unmatched_alerts


def find_reused_controls(matches):
    """Return {product_id: reuse_count} for controls matched to >1 alert."""
    counts = {}
    for match in matches:
        for control in match["controls"]:
            pid = control["product_id"]
            counts[pid] = counts.get(pid, 0) + 1
    return {pid: count for pid, count in counts.items() if count > 1}


def compare_outcomes(prices_db, matches, as_of, horizons=(3, 7, 14),
                      jump_percent=20.0, jump_dollars=1.0):
    """
    Compare matched alerts against their control groups at each horizon.

    Each alert's control group is reduced to one equally-weighted per-alert
    summary (median return and hit-rate across that alert's own valid
    controls) before aggregating across alerts, so alerts with more matches
    do not dominate. An alert only contributes to a horizon's comparison if
    the alert itself has a valid ("ok") exact-date outcome AND at least one
    of its controls does too; all other alerts are reported as exclusions.
    """
    as_of_date = datetime.strptime(as_of, "%Y-%m-%d")
    with sqlite3.connect(prices_db) as prices_conn:
        max_date_str = prices_conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
        max_date = datetime.strptime(max_date_str, "%Y-%m-%d") if max_date_str else None

        results = {}
        for horizon in horizons:
            alert_returns, alert_hits = [], []
            control_returns, control_hits = [], []
            alert_statuses = {"ok": 0, "pending": 0, "unavailable": 0}
            control_statuses = {"ok": 0, "pending": 0, "unavailable": 0}
            excluded = 0

            for match in matches:
                alert = match["alert"]
                a_status, _, a_return, a_hit = evaluate_horizon_outcome(
                    prices_conn, alert["product_id"], as_of_date, alert["latest_price"],
                    horizon, max_date, jump_percent, jump_dollars)
                alert_statuses[a_status] += 1

                control_ok_returns, control_ok_hits = [], []
                for control in match["controls"]:
                    c_status, _, c_return, c_hit = evaluate_horizon_outcome(
                        prices_conn, control["product_id"], as_of_date, control["latest_price"],
                        horizon, max_date, jump_percent, jump_dollars)
                    control_statuses[c_status] += 1
                    if c_status == "ok":
                        control_ok_returns.append(c_return)
                        control_ok_hits.append(c_hit)

                if a_status == "ok" and control_ok_returns:
                    alert_returns.append(a_return)
                    alert_hits.append(a_hit)
                    # One equally-weighted summary per alert's control group.
                    control_returns.append(median(control_ok_returns))
                    control_hits.append(sum(control_ok_hits) / len(control_ok_hits))
                else:
                    excluded += 1

            results[horizon] = {
                "alerts": {
                    "compared": len(alert_returns),
                    "median_return": median(alert_returns) if alert_returns else None,
                    "percent_hit": 100 * sum(alert_hits) / len(alert_hits) if alert_hits else None,
                    "ok": alert_statuses["ok"],
                    "pending": alert_statuses["pending"],
                    "unavailable": alert_statuses["unavailable"],
                },
                "controls": {
                    "compared": len(control_returns),
                    "median_return": median(control_returns) if control_returns else None,
                    "percent_hit": 100 * sum(control_hits) / len(control_hits) if control_hits else None,
                    "ok": control_statuses["ok"],
                    "pending": control_statuses["pending"],
                    "unavailable": control_statuses["unavailable"],
                },
                "excluded_alerts": excluded,
            }

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--prices-db', default='data/prices.db')
    parser.add_argument('--release-date-cache', default=DEFAULT_RELEASE_DATE_CACHE_PATH,
                         help='Cached, verified set release dates keyed by group id (see '
                              'app.analysis.fetch_group_release_dates).')
    parser.add_argument('--release-scope', default=DEFAULT_RELEASE_SCOPE, choices=VALID_RELEASE_SCOPES,
                         help='"all_years" (default) includes products with no verified release '
                              'date; "2007_onward" additionally requires a verified date >= '
                              'scope-start-date and excludes unknown/ambiguous ones.')
    parser.add_argument('--scope-start-date', default=RELEASE_SCOPE_START_DATE,
                         help='Earliest set release date in scope (inclusive) when '
                              '--release-scope=2007_onward, YYYY-MM-DD.')
    parser.add_argument('--subtype-mode', default=SUBTYPE_MODE_LEGACY_UNVERIFIED, choices=VALID_SUBTYPE_MODES,
                         help='"legacy_unverified" (default) replays existing price history as-is '
                              '-- most of it predates subtype tracking, so results are labeled '
                              'unverified. "verified" requires the detector\'s full minimum-history '
                              'requirement (all 4 most recent valid readings, not just the 3 used '
                              'in the momentum comparison) to come from a subtype-tracked import '
                              '(see product_subtypes); with no tracked history yet this currently '
                              'yields zero candidates.')
    args = parser.parse_args()

    movers = calculate_early_movers(args.prices_db, as_of=args.as_of,
                                     release_date_cache_path=args.release_date_cache,
                                     release_scope=args.release_scope,
                                     scope_start_date=args.scope_start_date,
                                     subtype_mode=args.subtype_mode)
    candidates, scope_stats = calculate_early_mover_candidates(
        args.prices_db, as_of=args.as_of, release_date_cache_path=args.release_date_cache,
        release_scope=args.release_scope, scope_start_date=args.scope_start_date,
        subtype_mode=args.subtype_mode, return_scope_stats=True)

    matches, unmatched_alerts = build_control_groups(candidates, movers)
    reused_controls = find_reused_controls(matches)
    comparison = compare_outcomes(args.prices_db, matches, args.as_of)

    with tempfile.TemporaryDirectory() as directory:
        signals_path = str(Path(directory) / 'replay.db')
        with sqlite3.connect(signals_path) as conn:
            conn.execute('''CREATE TABLE early_mover_signals
                (product_id INTEGER, card_name TEXT, set_name TEXT,
                 signal_date TEXT, signal_price REAL)''')
            for row in movers.to_dict('records'):
                conn.execute('INSERT INTO early_mover_signals VALUES (?, ?, ?, ?, ?)',
                             (row['product_id'], row['card_name'], row['set_name'],
                              args.as_of, row['latest_price']))
        result = calculate_early_movers_backtest(args.prices_db, signals_path)

    print(json.dumps({
        'mode': 'historical research replay, not live predictions',
        'method': 'existing momentum rule with consecutive-date requirement',
        'as_of': args.as_of,
        'scope': {
            'label': '2007 onward printings' if args.release_scope == '2007_onward' else 'all years',
            'description': 'Known future-released sets are always excluded (no look-ahead). '
                            'Under 2007_onward, sets released before scope_start_date or with an '
                            'unverified/ambiguous release date are also excluded. Under all_years, '
                            'products with an unverified release date are still included and '
                            'reported under eligible_unknown_date -- a research limitation, not '
                            'an exclusion. The same scope is applied to both the alert pool and '
                            'the control pool before ranking.',
            **({} if scope_stats is None else scope_stats),
        },
        'subtype_provenance': {
            'mode': args.subtype_mode,
            'label': 'UNVERIFIED (legacy prices; printing/subtype not confirmed per row)'
                     if args.subtype_mode == 'legacy_unverified' else 'verified (subtype-tracked)',
        },
        'signals': len(movers),
        'target': 'at least 20% AND $1 above signal price at the exact horizon',
        'summary': result['summary'],
        'control_group': {
            'method': 'up to 5 non-alert products from the same set, priced within '
                      '+/-20% of the alert start price, chosen deterministically by '
                      'closest price then product_id (no future data used)',
            'alerts_matched': len(matches),
            'alerts_unmatched': [
                {'product_id': a['product_id'], 'card_name': a['card_name'], 'set_name': a['set_name']}
                for a in unmatched_alerts
            ],
            'controls_reused': reused_controls,
            'comparison': comparison,
        },
        'limitations': 'Small historical sample; controls are same-set/price-band matches, '
                       'not a full risk-adjusted control (no randomization, no liquidity or '
                       'volume matching); alerts and controls both use exact-date lookups so '
                       'thinly-traded cards can be excluded from a horizon; equal per-alert '
                       'weighting can still be skewed by alerts with only one valid control; '
                       'no execution costs. Sets with no verified release date are excluded, '
                       'never guessed, and are reported under scope.excluded_unknown. Prices '
                       'imported before the explicit subtype-selection policy existed have '
                       'unverified printing/subtype provenance -- historical subtype ambiguity '
                       'for those rows is not resolved by this replay. Does not establish '
                       'predictive advantage.',
    }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()

