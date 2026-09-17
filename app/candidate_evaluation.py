"""Offline retrospective evaluation for the combined candidate ranker.

The evaluator is deliberately read-only and deterministic. At every historical
cutoff it rebuilds the production candidate ranking with deck versions archived
by that day and price observations dated on or before that day. Outcomes are
then measured from the last known cutoff price to an exact later horizon date.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.candidate_score import build_candidates


DATE_FMT = "%Y-%m-%d"
DEFAULT_HORIZONS = (7, 14)
RETROSPECTIVE_NOTICE = (
    "Retrospective evaluation only. Historical association does not establish "
    "causation or guarantee future price performance."
)


def _parse_day(value: str) -> datetime:
    return datetime.strptime(value, DATE_FMT)


def load_price_history(
    db_path: str,
) -> Tuple[Dict[int, Dict[str, float]], List[str]]:
    """Load dated market prices without opening the database for writes."""
    if not os.path.exists(db_path):
        return {}, []

    try:
        with sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT product_id, date, market_price FROM prices "
                "WHERE product_id IS NOT NULL AND date IS NOT NULL "
                "AND market_price IS NOT NULL ORDER BY product_id, date"
            ).fetchall()
    except sqlite3.OperationalError:
        return {}, []

    history: Dict[int, Dict[str, float]] = {}
    valid_dates = set()
    for product_id, date_value, market_price in rows:
        try:
            date_text = _parse_day(str(date_value)).strftime(DATE_FMT)
            price = float(market_price)
        except (TypeError, ValueError):
            continue
        history.setdefault(int(product_id), {})[date_text] = price
        valid_dates.add(date_text)
    return history, sorted(valid_dates)


def generate_cutoffs(
    price_dates: Sequence[str],
    horizons: Sequence[int],
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    step_days: int = 7,
) -> List[str]:
    """Generate inclusive rolling cutoffs with room for the longest horizon."""
    if step_days <= 0:
        raise ValueError("step_days must be positive")
    if not horizons or any(int(horizon) <= 0 for horizon in horizons):
        raise ValueError("horizons must contain positive day counts")
    if not price_dates:
        return []

    first = _parse_day(start) if start else _parse_day(min(price_dates))
    latest_allowed = _parse_day(max(price_dates)) - timedelta(days=max(horizons))
    last = _parse_day(end) if end else latest_allowed
    last = min(last, latest_allowed)
    if first > last:
        return []

    cutoffs = []
    current = first
    while current <= last:
        cutoffs.append(current.strftime(DATE_FMT))
        current += timedelta(days=step_days)
    return cutoffs


def calculate_forward_return(
    cutoff_price: Optional[float], future_price: Optional[float]
) -> Optional[float]:
    """Return percentage movement, or ``None`` when either price is unusable."""
    if cutoff_price is None or future_price is None or cutoff_price <= 0:
        return None
    return ((float(future_price) - float(cutoff_price)) / float(cutoff_price)) * 100.0


def _price_only_order(candidates: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Rank the same evidence-backed universe by cutoff-safe price momentum."""
    return sorted(
        candidates,
        key=lambda row: (
            -(
                row.get("price", {}).get("percent_change")
                if row.get("price", {}).get("percent_change") is not None
                else float("-inf")
            ),
            row.get("product_id") if row.get("product_id") is not None else 0,
        ),
    )


def _evaluate_rows(
    rows: Sequence[Mapping[str, Any]],
    cutoff: str,
    horizon: int,
    price_history: Mapping[int, Mapping[str, float]],
    hit_threshold_pct: float,
) -> Tuple[List[Dict[str, Any]], List[float]]:
    target_date = (_parse_day(cutoff) + timedelta(days=horizon)).strftime(DATE_FMT)
    evaluated = []
    returns = []
    for row in rows:
        product_id = row.get("product_id")
        price = row.get("price") or {}
        cutoff_price = price.get("latest_price")
        latest_date = price.get("latest_date")
        if product_id is None or latest_date is None or latest_date > cutoff:
            continue
        future_price = price_history.get(int(product_id), {}).get(target_date)
        forward_return = calculate_forward_return(cutoff_price, future_price)
        if forward_return is None:
            continue
        rounded_return = round(forward_return, 6)
        returns.append(rounded_return)
        evaluated.append(
            {
                "product_id": int(product_id),
                "cutoff_price_date": latest_date,
                "target_date": target_date,
                "forward_return_pct": rounded_return,
                "hit": rounded_return > hit_threshold_pct,
            }
        )
    return evaluated, returns


def _metrics(returns: Sequence[float], hit_threshold_pct: float) -> Dict[str, Any]:
    return {
        "evaluated_candidate_count": len(returns),
        "top_k_hit_rate": (
            round(sum(value > hit_threshold_pct for value in returns) / len(returns), 6)
            if returns
            else None
        ),
        "median_forward_return_pct": (
            round(float(median(returns)), 6) if returns else None
        ),
    }


def evaluate_candidate_ranker(
    dataset_path: str,
    prices_db_path: str,
    *,
    cutoffs: Optional[Sequence[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    step_days: int = 7,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    top_k: int = 10,
    hit_threshold_pct: float = 0.0,
) -> Dict[str, Any]:
    """Evaluate the combined ranker against a price-momentum-only baseline."""
    normalized_horizons = tuple(sorted({int(value) for value in horizons}))
    if not normalized_horizons or any(value <= 0 for value in normalized_horizons):
        raise ValueError("horizons must contain positive day counts")
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    price_history, price_dates = load_price_history(prices_db_path)
    attempted_cutoffs = (
        sorted({_parse_day(value).strftime(DATE_FMT) for value in cutoffs})
        if cutoffs is not None
        else generate_cutoffs(
            price_dates,
            normalized_horizons,
            start=start,
            end=end,
            step_days=step_days,
        )
    )

    reasons: Counter[str] = Counter()
    if not price_dates:
        reasons["no_price_data"] += 1
    if not attempted_cutoffs:
        reasons["no_rolling_cutoffs_with_complete_horizon"] += 1

    all_returns: Dict[int, Dict[str, List[float]]] = {
        horizon: {"combined_model": [], "price_only_baseline": []}
        for horizon in normalized_horizons
    }
    eligible_by_horizon = {horizon: 0 for horizon in normalized_horizons}
    eligible_cutoff_count = 0
    ranked_candidate_count = 0
    cutoff_results = []

    for cutoff in attempted_cutoffs:
        ranking = build_candidates(
            dataset_path=dataset_path,
            prices_db_path=prices_db_path,
            as_of=cutoff,
        )
        candidates = ranking["candidates"]
        ranked_candidate_count += len(candidates)
        if not candidates:
            reasons["no_combined_candidates"] += 1

        combined_top = candidates[:top_k]
        baseline_top = _price_only_order(candidates)[:top_k]
        horizon_results: Dict[str, Any] = {}
        cutoff_complete = bool(combined_top)
        for horizon in normalized_horizons:
            combined_rows, combined_returns = _evaluate_rows(
                combined_top,
                cutoff,
                horizon,
                price_history,
                hit_threshold_pct,
            )
            baseline_rows, baseline_returns = _evaluate_rows(
                baseline_top,
                cutoff,
                horizon,
                price_history,
                hit_threshold_pct,
            )
            all_returns[horizon]["combined_model"].extend(combined_returns)
            all_returns[horizon]["price_only_baseline"].extend(baseline_returns)
            if combined_returns:
                eligible_by_horizon[horizon] += 1
            else:
                cutoff_complete = False
                reasons[f"no_evaluable_combined_return_{horizon}d"] += 1
            if not baseline_returns:
                reasons[f"no_evaluable_baseline_return_{horizon}d"] += 1
            horizon_results[str(horizon)] = {
                "combined_model": combined_rows,
                "price_only_baseline": baseline_rows,
            }
        if cutoff_complete:
            eligible_cutoff_count += 1
        cutoff_results.append(
            {
                "cutoff": cutoff,
                "ranked_candidate_count": len(candidates),
                "combined_top_product_ids": [
                    row["product_id"] for row in combined_top
                ],
                "price_only_baseline_top_product_ids": [
                    row["product_id"] for row in baseline_top
                ],
                "horizons": horizon_results,
            }
        )

    horizon_summaries: Dict[str, Any] = {}
    for horizon in normalized_horizons:
        combined = _metrics(
            all_returns[horizon]["combined_model"], hit_threshold_pct
        )
        baseline = _metrics(
            all_returns[horizon]["price_only_baseline"], hit_threshold_pct
        )
        horizon_summaries[str(horizon)] = {
            "eligible_cutoff_count": eligible_by_horizon[horizon],
            "combined_model": combined,
            "price_only_baseline": baseline,
            "comparison": {
                "hit_rate_delta": (
                    round(
                        combined["top_k_hit_rate"] - baseline["top_k_hit_rate"], 6
                    )
                    if combined["top_k_hit_rate"] is not None
                    and baseline["top_k_hit_rate"] is not None
                    else None
                ),
                "median_forward_return_delta_pct": (
                    round(
                        combined["median_forward_return_pct"]
                        - baseline["median_forward_return_pct"],
                        6,
                    )
                    if combined["median_forward_return_pct"] is not None
                    and baseline["median_forward_return_pct"] is not None
                    else None
                ),
            },
        }

    return {
        "schema_version": 1,
        "evaluation_type": "retrospective_offline_evaluation",
        "notice": RETROSPECTIVE_NOTICE,
        "dataset_path": dataset_path,
        "prices_db_path": prices_db_path,
        "configuration": {
            "horizons_days": list(normalized_horizons),
            "top_k": top_k,
            "hit_threshold_pct": hit_threshold_pct,
            "step_days": step_days,
            "forward_price_policy": "exact cutoff+horizon date",
            "cutoff_price_policy": "latest market price dated on or before cutoff",
            "baseline": (
                "same evidence-backed candidate universe ranked by cutoff-safe "
                "percent_change only"
            ),
            "eligible_cutoff_policy": (
                "at least one combined top-k forward return at every horizon"
            ),
        },
        "attempted_cutoff_count": len(attempted_cutoffs),
        "eligible_cutoff_count": eligible_cutoff_count,
        "ranked_candidate_count": ranked_candidate_count,
        "horizons": horizon_summaries,
        "insufficient_data_reasons": dict(sorted(reasons.items())),
        "cutoffs": cutoff_results,
    }
