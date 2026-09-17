"""Read-only per-product price-feature layer for the Yu-Gi-Oh price model.

This module reads ``data/prices.db`` and, for each ``product_id`` present up to
an optional ``as_of`` cutoff (inclusive), computes a compact set of features
that a downstream ranker can consume. It does **not** modify the database and
does **not** pull external data.

The baseline-median convention is deliberately aligned with the existing
top-gainers/top-losers analysis in :mod:`app.analysis`: the "baseline" is the
median of ``market_price`` observations in the window
``[latest_date - 8 days, latest_date - 6 days]`` (a 3-day window centered
roughly one week before the latest observation used), and the "recent" median
is the median of the last 3 dated observations at or before ``as_of``.

All feature values are plain JSON-serializable Python scalars (or ``None`` for
missing values) so the CLI output is directly consumable.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from statistics import median, pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence


DATE_FMT = "%Y-%m-%d"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_as_of(as_of: Optional[str]) -> Optional[str]:
    """Normalize ``as_of`` to ``YYYY-MM-DD`` or return ``None``.

    Raises ``ValueError`` if the supplied string cannot be parsed.
    """
    if as_of is None:
        return None
    # Accept datetime objects for convenience.
    if isinstance(as_of, datetime):
        return as_of.strftime(DATE_FMT)
    return datetime.strptime(str(as_of), DATE_FMT).strftime(DATE_FMT)


def load_price_rows(
    db_path: str,
    as_of: Optional[str] = None,
    product_ids: Optional[Sequence[int]] = None,
) -> List[Dict[str, Any]]:
    """Load raw price rows from ``prices.db`` with an optional ``as_of`` cutoff.

    Rows are returned as a list of dicts with keys
    ``product_id``, ``card_name``, ``date`` (str, ``YYYY-MM-DD``), and
    ``market_price`` (float or ``None``). Rows are sorted by ``product_id`` and
    then ``date`` ascending so downstream consumers can treat each product's
    slice as a chronological series.

    Only rows with ``date <= as_of`` are returned when ``as_of`` is provided;
    this guarantees no future leakage.
    """
    normalized_as_of = _parse_as_of(as_of)

    if not os.path.exists(db_path):
        return []

    query = (
        "SELECT product_id, card_name, date, market_price FROM prices"
    )
    params: List[Any] = []
    where: List[str] = []
    if normalized_as_of is not None:
        where.append("date <= ?")
        params.append(normalized_as_of)
    if product_ids:
        placeholders = ",".join("?" for _ in product_ids)
        where.append(f"product_id IN ({placeholders})")
        params.extend(int(pid) for pid in product_ids)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY product_id ASC, date ASC"

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        try:
            cur.execute(query, params)
        except sqlite3.OperationalError:
            # Missing ``prices`` table -- treat as empty history.
            return []
        rows = cur.fetchall()
    finally:
        conn.close()

    result: List[Dict[str, Any]] = []
    for product_id, card_name, date_str, market_price in rows:
        result.append(
            {
                "product_id": int(product_id) if product_id is not None else None,
                "card_name": card_name,
                "date": date_str,
                "market_price": (
                    float(market_price) if market_price is not None else None
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# Feature helpers
# ---------------------------------------------------------------------------


def _consecutive_rising(prices: Sequence[float]) -> int:
    """Return the count of trailing strictly-rising observations.

    A single observation counts as ``1`` (there is one non-decreasing tail
    observation). Two ascending observations count as ``2``, and so on. Returns
    ``0`` when the series is empty.
    """
    if not prices:
        return 0
    count = 1
    for i in range(len(prices) - 1, 0, -1):
        if prices[i] > prices[i - 1]:
            count += 1
        else:
            break
    return count


def _volatility(prices: Sequence[float]) -> Optional[float]:
    """Population stdev of daily percentage changes, or ``None`` if undefined.

    Percentage change is only defined between consecutive observations with a
    positive prior value; volatility requires at least two such changes so the
    stdev is well-defined and not trivially zero from a single point.
    """
    if len(prices) < 3:
        return None
    changes: List[float] = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        if prev is None or prev <= 0:
            continue
        changes.append((prices[i] - prev) / prev)
    if len(changes) < 2:
        return None
    return float(pstdev(changes))


def _baseline_median(
    rows_for_product: Sequence[Dict[str, Any]], latest_date: datetime
) -> Optional[float]:
    """Median of ``market_price`` in the project's baseline window.

    Window: ``[latest_date - 8 days, latest_date - 6 days]`` inclusive, matching
    :func:`app.analysis.calculate_top_gainers`. ``None`` if the window has no
    valid observations.
    """
    start = latest_date - timedelta(days=8)
    end = latest_date - timedelta(days=6)
    baseline_prices: List[float] = []
    for row in rows_for_product:
        price = row.get("market_price")
        if price is None:
            continue
        row_date = datetime.strptime(row["date"], DATE_FMT)
        if start <= row_date <= end:
            baseline_prices.append(float(price))
    if not baseline_prices:
        return None
    return float(median(baseline_prices))


# ---------------------------------------------------------------------------
# Per-product feature record
# ---------------------------------------------------------------------------


def compute_product_features(
    rows_for_product: Sequence[Dict[str, Any]],
    as_of: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Compute features for a single product's chronologically-sorted rows.

    ``rows_for_product`` must already respect the ``as_of`` cutoff. Returns
    ``None`` when there are no rows at all (nothing to identify the product
    with). Otherwise returns a dict with all feature keys; keys that cannot be
    computed from the available history are set to ``None`` and flagged in
    ``data_quality``.
    """
    if not rows_for_product:
        return None

    # Preserve identity from the first row (all rows share product_id/card_name).
    product_id = rows_for_product[0].get("product_id")
    card_name = rows_for_product[0].get("card_name")

    valid_rows = [
        r for r in rows_for_product if r.get("market_price") is not None
    ]
    history_count = len(valid_rows)

    normalized_as_of = _parse_as_of(as_of)

    features: Dict[str, Any] = {
        "product_id": product_id,
        "card_name": card_name,
        "as_of": normalized_as_of,
        "latest_price": None,
        "latest_date": None,
        "recent_median": None,
        "baseline_median": None,
        "percent_change": None,
        "consecutive_rising": 0,
        "volatility": None,
        "history_count": history_count,
        "data_quality": {
            "no_valid_prices": history_count == 0,
            "sparse_history": history_count < 7,
            "missing_baseline": True,
            "missing_recent": True,
            "stale_latest": False,
        },
    }

    if history_count == 0:
        return features

    latest_row = valid_rows[-1]
    latest_price = float(latest_row["market_price"])
    latest_date_str = latest_row["date"]
    latest_date = datetime.strptime(latest_date_str, DATE_FMT)

    features["latest_price"] = latest_price
    features["latest_date"] = latest_date_str

    prices_in_order = [float(r["market_price"]) for r in valid_rows]

    # Recent 3-observation median.
    recent_slice = prices_in_order[-3:]
    if recent_slice:
        features["recent_median"] = float(median(recent_slice))
        features["data_quality"]["missing_recent"] = len(recent_slice) < 3

    # Baseline median using the project convention.
    baseline = _baseline_median(valid_rows, latest_date)
    features["baseline_median"] = baseline
    features["data_quality"]["missing_baseline"] = baseline is None

    # Percentage change vs baseline (only when both sides are usable).
    if (
        baseline is not None
        and baseline > 0
        and features["recent_median"] is not None
    ):
        features["percent_change"] = (
            (features["recent_median"] - baseline) / baseline
        ) * 100.0

    # Consecutive rising observations (trailing run).
    features["consecutive_rising"] = _consecutive_rising(prices_in_order)

    # Volatility.
    features["volatility"] = _volatility(prices_in_order)

    # Staleness: latest observation is more than 3 days before as_of.
    if normalized_as_of is not None:
        as_of_dt = datetime.strptime(normalized_as_of, DATE_FMT)
        features["data_quality"]["stale_latest"] = (
            (as_of_dt - latest_date).days > 3
        )

    return features


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def compute_features(
    db_path: str,
    as_of: Optional[str] = None,
    product_ids: Optional[Sequence[int]] = None,
) -> List[Dict[str, Any]]:
    """Compute features for every product with history at or before ``as_of``.

    Results are sorted by ``product_id`` ascending for stable output.
    """
    rows = load_price_rows(db_path, as_of=as_of, product_ids=product_ids)
    if not rows:
        return []

    # Group by product_id preserving chronological order per group.
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        pid = row["product_id"]
        if pid is None:
            continue
        grouped.setdefault(pid, []).append(row)

    features: List[Dict[str, Any]] = []
    for pid in sorted(grouped):
        record = compute_product_features(grouped[pid], as_of=as_of)
        if record is not None:
            features.append(record)
    return features


def features_to_json_payload(
    features: Iterable[Dict[str, Any]],
    db_path: str,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrap feature records in a stable envelope for CLI/JSON consumption."""
    features_list = list(features)
    return {
        "schema_version": 1,
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "db_path": db_path,
        "as_of": _parse_as_of(as_of),
        "product_count": len(features_list),
        "features": features_list,
    }
