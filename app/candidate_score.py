"""Explainable combined candidate score.

This module fuses two already-shipped, read-only feature layers into a
single ranked list of candidate printings:

* :mod:`app.tournament_adoption` — TCG_ADVANCED tournament-adoption
  features at the *identity* (canonical card name) level. Adoption is a
  property of the card, not of a specific printing: the decklist did
  not identify which printing the player sleeved.
* :mod:`app.price_features` — read-only per-``product_id`` price
  features (momentum, trend, volatility, quality flags) computed from
  ``data/prices.db``.

Fusion rules
------------

* **Fan-out, don't fabricate.** One adopted canonical card fans out to
  *every* tracked printing whose ``prices.card_name`` matches (via
  :func:`app.meta_watch.resolve_card_printings`). Each printing
  receives the same identity-level adoption metrics. We never claim an
  exact printing was used.
* **Product identity is the ranking key.** Output is one row per
  ``product_id`` — the price-history unit — even though adoption is
  identity-level. Rows include the ``card_name`` and both feature
  blocks side-by-side.
* **``as_of`` cutoff flows through both layers.** The same cutoff is
  passed to the adoption adapter (drops tournaments after the cutoff)
  and to the price loader (drops price observations after the cutoff),
  so replaying at a past date does not leak future evidence.
* **Deterministic, documented weights.** No machine learning. The
  weights below are constants and each component's contribution is
  reported alongside the final score so a reviewer can reproduce it by
  hand.
* **Missing is unknown, not zero.** A missing component is *dropped
  from the weighted average* (the denominator shrinks) rather than
  contributing 0. Every dropped component appears in
  ``missing_flags`` and lowers ``confidence``. This means a row with
  only adoption evidence does not silently look identical to a row
  with strong bearish price evidence.
* **Evidence-backed candidates are ranked separately from price-only
  movers.** Because missing components are dropped from the weighted
  average, a card with strong price momentum but *zero* tournament
  adoption could otherwise renormalize to ``final_score == 1.0`` and
  outrank a card that has real deck-adoption evidence. To prevent
  that, the primary ranked list (``candidates``) contains only rows
  that have **both** adoption and price evidence. Price-only movers
  (price evidence but no adoption) are surfaced in a separate
  ``price_only_movers`` list, and adoption-only cards with no
  matching tracked printing are surfaced in
  ``unresolved_adoption_cards``. All three lists preserve honest
  missing-data flags and deterministic ordering; only the *routing*
  changes.

The module is read-only: no writes to ``prices.db``, no cached
datasets, no changes to the UI or existing PR-#7/#8 flows.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.meta_watch import DEFAULT_DATASET_PATH, resolve_card_printings
from app.price_features import compute_features
from app.tournament_adoption import build_adoption_features


# ---------------------------------------------------------------------------
# Deterministic weights and normalization thresholds
# ---------------------------------------------------------------------------
#
# Weights sum to 1.0 across the four base components. When a component is
# missing for a given row it is *excluded* from the weighted average (the
# remaining weights are renormalized), which is not the same as treating
# the missing component as a zero contribution — see ``compute_row_score``.

WEIGHT_DECK_PENETRATION = 0.30
WEIGHT_PLACEMENT_WEIGHTED = 0.30
WEIGHT_PRICE_MOMENTUM = 0.25
WEIGHT_PRICE_TREND = 0.15

_ALL_WEIGHTS: Dict[str, float] = {
    "deck_penetration": WEIGHT_DECK_PENETRATION,
    "placement_weighted": WEIGHT_PLACEMENT_WEIGHTED,
    "price_momentum": WEIGHT_PRICE_MOMENTUM,
    "price_trend": WEIGHT_PRICE_TREND,
}

# Documented normalization thresholds. A positive percent change of
# ``MOMENTUM_CAP_PCT`` or greater maps to 1.0; anything at or below zero
# maps to 0.0; the interior is linear. This is deliberately linear and
# saturating so a single freak print run cannot dominate the score.
MOMENTUM_CAP_PCT = 50.0

# A trailing run of ``TREND_CAP`` or more consecutive strictly-rising
# observations maps to 1.0. A single reading (which
# :func:`app.price_features._consecutive_rising` reports as 1) does not
# count as evidence of a rise, so we subtract one before normalizing.
TREND_CAP = 5

# Confidence thresholds (documented and deterministic).
MIN_TOTAL_DECKS_FOR_HIGH = 5
MIN_HISTORY_COUNT_FOR_HIGH = 7


# ---------------------------------------------------------------------------
# Safe normalization helpers
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return float(x)


def normalize_penetration(value: Optional[float]) -> Optional[float]:
    """``deck_penetration_rate`` is already in ``[0, 1]``; clamp defensively."""
    if value is None:
        return None
    return _clamp01(float(value))


def normalize_placement_weighted(value: Optional[float]) -> Optional[float]:
    """``placement_weighted_adoption`` is already in ``[0, 1]``; clamp."""
    if value is None:
        return None
    return _clamp01(float(value))


def normalize_momentum(percent_change: Optional[float]) -> Optional[float]:
    """Map percent-change to ``[0, 1]``.

    A price *drop* is not evidence of a coming rise, so negative
    percent-changes clamp to ``0.0`` rather than pushing the row below
    a hypothetical neutral midpoint. Values at or above
    :data:`MOMENTUM_CAP_PCT` clamp to ``1.0``.
    """
    if percent_change is None:
        return None
    if percent_change <= 0.0:
        return 0.0
    return _clamp01(float(percent_change) / MOMENTUM_CAP_PCT)


def normalize_trend(consecutive_rising: Optional[int]) -> Optional[float]:
    """Map trailing rising-run length to ``[0, 1]``.

    ``consecutive_rising`` counts the current observation plus each
    previous strictly-rising step (so ``1`` means "no rising evidence,
    just one point"). We subtract that starting point before dividing
    by :data:`TREND_CAP` so a single point contributes nothing. ``None``
    (undefined trend, e.g. no price history) propagates.
    """
    if consecutive_rising is None:
        return None
    effective = max(0, int(consecutive_rising) - 1)
    denom = max(1, TREND_CAP - 1)
    return _clamp01(effective / denom)


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------


def _adoption_metrics(entry: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Extract the adoption feature block for a card, or a None-filled block."""
    if entry is None:
        return {
            "unique_decks": None,
            "total_copies": None,
            "deck_penetration_rate": None,
            "placement_weighted_adoption": None,
            "latest_event_date": None,
        }
    return {
        "unique_decks": entry.get("unique_decks"),
        "total_copies": entry.get("total_copies"),
        "deck_penetration_rate": entry.get("deck_penetration_rate"),
        "placement_weighted_adoption": entry.get("placement_weighted_adoption"),
        "latest_event_date": entry.get("latest_event_date"),
    }


def _price_metrics(entry: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Extract the price feature block, or a None-filled block."""
    if entry is None:
        return {
            "latest_price": None,
            "latest_date": None,
            "recent_median": None,
            "baseline_median": None,
            "percent_change": None,
            "consecutive_rising": None,
            "volatility": None,
            "history_count": 0,
        }
    return {
        "latest_price": entry.get("latest_price"),
        "latest_date": entry.get("latest_date"),
        "recent_median": entry.get("recent_median"),
        "baseline_median": entry.get("baseline_median"),
        "percent_change": entry.get("percent_change"),
        "consecutive_rising": entry.get("consecutive_rising"),
        "volatility": entry.get("volatility"),
        "history_count": entry.get("history_count", 0),
    }


def compute_row_score(
    adoption_entry: Optional[Mapping[str, Any]],
    price_entry: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Optional[float]], List[str], float, int]:
    """Return per-component scores, missing flags, final score, and available count.

    The final score is the weighted average of the *available* component
    scores, using :data:`_ALL_WEIGHTS`. Missing components are excluded
    (renormalized) rather than counted as zero — a printing with no
    price history should not be penalized for its absent history.
    """
    ap = _adoption_metrics(adoption_entry)
    pp = _price_metrics(price_entry)

    components: Dict[str, Optional[float]] = {
        "deck_penetration": normalize_penetration(ap["deck_penetration_rate"]),
        "placement_weighted": normalize_placement_weighted(
            ap["placement_weighted_adoption"]
        ),
        "price_momentum": normalize_momentum(pp["percent_change"]),
        "price_trend": normalize_trend(pp["consecutive_rising"]),
    }

    missing_flags: List[str] = []
    weighted_sum = 0.0
    weight_sum = 0.0
    for name, score in components.items():
        if score is None:
            missing_flags.append(name)
            continue
        w = _ALL_WEIGHTS[name]
        weighted_sum += w * score
        weight_sum += w

    final_score = weighted_sum / weight_sum if weight_sum > 0 else 0.0
    available_count = len(_ALL_WEIGHTS) - len(missing_flags)
    return components, missing_flags, final_score, available_count


def _classify_confidence(
    adoption_entry: Optional[Mapping[str, Any]],
    price_entry: Optional[Mapping[str, Any]],
    adoption_total_decks: int,
    available_count: int,
) -> str:
    """Return ``"high"``, ``"medium"``, or ``"low"``.

    * ``"high"`` — all four base components available AND the adoption
      layer saw at least :data:`MIN_TOTAL_DECKS_FOR_HIGH` decks AND the
      price layer has at least :data:`MIN_HISTORY_COUNT_FOR_HIGH` valid
      observations AND the latest price is not stale.
    * ``"medium"`` — at least one adoption component AND one price
      component is available, but the "high" thresholds are not met.
    * ``"low"`` — one side (adoption or price) is entirely absent.
    """
    has_adoption = adoption_entry is not None
    has_price = price_entry is not None and (price_entry.get("history_count") or 0) > 0
    if not has_adoption or not has_price:
        return "low"

    stale = False
    dq = price_entry.get("data_quality") if isinstance(price_entry, Mapping) else None
    if isinstance(dq, Mapping) and dq.get("stale_latest"):
        stale = True

    if (
        available_count == len(_ALL_WEIGHTS)
        and adoption_total_decks >= MIN_TOTAL_DECKS_FOR_HIGH
        and (price_entry.get("history_count") or 0) >= MIN_HISTORY_COUNT_FOR_HIGH
        and not stale
    ):
        return "high"
    return "medium"


def _build_reasons(
    adoption_entry: Optional[Mapping[str, Any]],
    price_entry: Optional[Mapping[str, Any]],
    components: Mapping[str, Optional[float]],
    missing_flags: Sequence[str],
    total_printings_for_card: int,
) -> List[str]:
    """Human-readable justifications, deterministic in ordering and wording."""
    reasons: List[str] = []

    if adoption_entry is None:
        reasons.append(
            "No TCG_ADVANCED tournament adoption observed for this card in the window."
        )
    else:
        pen = adoption_entry.get("deck_penetration_rate") or 0.0
        pwa = adoption_entry.get("placement_weighted_adoption") or 0.0
        reasons.append(
            f"Deck penetration {pen * 100:.1f}% "
            f"({adoption_entry.get('unique_decks', 0)} of qualifying decks)."
        )
        reasons.append(
            f"Placement-weighted adoption {pwa * 100:.1f}% "
            "(higher finishers count more)."
        )
        if total_printings_for_card > 1:
            reasons.append(
                f"Identity-level adoption fanned out to {total_printings_for_card} "
                "tracked printings; the decklist did not identify a specific printing."
            )

    if price_entry is None or (price_entry.get("history_count") or 0) == 0:
        reasons.append("No price history for this product_id at the cutoff.")
    else:
        pc = price_entry.get("percent_change")
        if pc is not None:
            reasons.append(
                f"Recent vs baseline price change {pc:+.1f}%."
            )
        cr = price_entry.get("consecutive_rising")
        if cr is not None and cr >= 2:
            reasons.append(f"Trailing rising run of {int(cr)} observations.")
        dq = price_entry.get("data_quality") or {}
        if isinstance(dq, Mapping):
            if dq.get("sparse_history"):
                reasons.append(
                    f"Sparse price history ({price_entry.get('history_count', 0)} "
                    "valid observations)."
                )
            if dq.get("stale_latest"):
                reasons.append("Latest price observation is stale relative to the cutoff.")

    for flag in missing_flags:
        reasons.append(f"Missing component: {flag} (excluded from weighted average).")

    return reasons


# ---------------------------------------------------------------------------
# End-to-end build
# ---------------------------------------------------------------------------


def _adoption_by_product_id(
    conn: sqlite3.Connection,
    adoption_cards: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[int, Tuple[str, Mapping[str, Any], int]], Dict[str, int]]:
    """Fan each adoption card out to matching product_ids.

    Returns:

    * A mapping ``product_id -> (card_name, adoption_entry, printing_count)``
      where ``printing_count`` is how many printings the *card* fanned
      out to (so a downstream reason can note the fan-out). When two
      distinct cards fan to the same ``product_id`` (very rare — implies
      an ambiguous name), the card with the higher unique-deck count
      wins, ties broken by card_name ascending for determinism.
    * A mapping ``card_name -> printing_count`` for every adoption card,
      including those with zero matching printings (used to surface
      card-level rows without a price join).
    """
    by_pid: Dict[int, Tuple[str, Mapping[str, Any], int]] = {}
    printings_per_card: Dict[str, int] = {}
    for entry in adoption_cards:
        name = entry["card_name"]
        printings = resolve_card_printings(conn, name)
        printings_per_card[name] = len(printings)
        for p in printings:
            pid = p.get("product_id")
            if pid is None:
                continue
            existing = by_pid.get(int(pid))
            if existing is None:
                by_pid[int(pid)] = (name, entry, len(printings))
                continue
            # Deterministic tiebreak on collision.
            e_name, e_entry, _ = existing
            new_key = (-(entry.get("unique_decks") or 0), name)
            old_key = (-(e_entry.get("unique_decks") or 0), e_name)
            if new_key < old_key:
                by_pid[int(pid)] = (name, entry, len(printings))
    return by_pid, printings_per_card


def _has_adoption_evidence(entry: Optional[Mapping[str, Any]]) -> bool:
    """A row has adoption evidence iff at least one qualifying deck adopted it.

    We check ``unique_decks`` rather than the presence of the entry so a
    zero-decks entry (should not happen in practice, but be defensive)
    is not treated as evidence.
    """
    if entry is None:
        return False
    unique = entry.get("unique_decks")
    try:
        return int(unique or 0) > 0
    except (TypeError, ValueError):
        return False


def _has_price_evidence(entry: Optional[Mapping[str, Any]]) -> bool:
    """A row has price evidence iff we have at least one valid observation."""
    if entry is None:
        return False
    try:
        return int(entry.get("history_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def build_candidates(
    dataset_path: str = DEFAULT_DATASET_PATH,
    prices_db_path: Optional[str] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """End-to-end build of the ranked candidate list.

    Returns a report dict with:

    * ``as_of``: the cutoff string as supplied (or ``None``).
    * ``dataset_path`` / ``prices_db_path``: inputs, for provenance.
    * ``weights``: the documented deterministic weight table.
    * ``adoption_total_decks``: how many qualifying decks fed the
      adoption layer (used by callers to reason about confidence).
    * ``candidates``: **primary** ranked list — rows with BOTH
      adoption and price evidence, sorted by ``final_score`` desc
      then ``product_id`` asc.
    * ``price_only_movers``: rows with price evidence but no
      adoption. Deterministically ordered but *never* mixed into
      ``candidates``, so a strong price-only mover cannot outrank an
      evidence-backed candidate.
    * ``unresolved_adoption_cards``: adoption cards with zero
      matching tracked printings (product_id is ``None``). Ordered
      by card_name asc.
    """
    adoption = build_adoption_features(dataset_path=dataset_path, as_of=as_of)
    adoption_cards = adoption["cards"]

    price_features: List[Dict[str, Any]] = []
    price_by_pid: Dict[int, Dict[str, Any]] = {}
    if prices_db_path and os.path.exists(prices_db_path):
        price_features = compute_features(prices_db_path, as_of=as_of)
        price_by_pid = {int(p["product_id"]): p for p in price_features if p.get("product_id") is not None}

    by_pid: Dict[int, Tuple[str, Mapping[str, Any], int]] = {}
    printings_per_card: Dict[str, int] = {}
    if prices_db_path and os.path.exists(prices_db_path):
        with sqlite3.connect(f"file:{prices_db_path}?mode=ro", uri=True) as conn:
            by_pid, printings_per_card = _adoption_by_product_id(conn, adoption_cards)

    all_pids = sorted(set(price_by_pid) | set(by_pid))

    evidence_backed: List[Dict[str, Any]] = []
    price_only: List[Dict[str, Any]] = []
    adoption_only_product_rows: List[Dict[str, Any]] = []
    for pid in all_pids:
        pf = price_by_pid.get(pid)
        adoption_hit = by_pid.get(pid)
        if adoption_hit is not None:
            card_name, adoption_entry, printing_count = adoption_hit
        else:
            card_name = pf["card_name"] if pf else None
            adoption_entry = None
            printing_count = 0
        row = _build_row(
            product_id=pid,
            card_name=card_name,
            adoption_entry=adoption_entry,
            price_entry=pf,
            adoption_total_decks=adoption["total_decks"],
            printing_count_for_card=printing_count,
        )
        # Route rows. Only rows with BOTH adoption AND price evidence go
        # to the primary ranked list. This is what prevents a strong
        # price-only mover from renormalizing its weighted average to
        # 1.0 and outranking a real, adoption-backed candidate.
        has_adopt = _has_adoption_evidence(adoption_entry)
        has_price = _has_price_evidence(pf)
        if has_adopt and has_price:
            evidence_backed.append(row)
        elif has_price and not has_adopt:
            price_only.append(row)
        else:
            # Adoption-only product row (adoption entry present but no
            # price history). Not a recommendation, but surfaced so the
            # identity-level evidence is not silently dropped.
            adoption_only_product_rows.append(row)

    evidence_backed.sort(
        key=lambda r: (-r["final_score"], r["product_id"] if r["product_id"] is not None else 0)
    )
    price_only.sort(
        key=lambda r: (-r["final_score"], r["product_id"] if r["product_id"] is not None else 0)
    )
    adoption_only_product_rows.sort(
        key=lambda r: (r["card_name"] or "", r["product_id"] if r["product_id"] is not None else 0)
    )

    # Adoption cards with zero tracked printings — pure identity-level
    # evidence with no product to attach to. Sort by card_name asc.
    unresolved_cards = [
        c for c in adoption_cards if printings_per_card.get(c["card_name"], 0) == 0
    ]
    unresolved_rows: List[Dict[str, Any]] = [
        _build_row(
            product_id=None,
            card_name=c["card_name"],
            adoption_entry=c,
            price_entry=None,
            adoption_total_decks=adoption["total_decks"],
            printing_count_for_card=0,
        )
        for c in sorted(unresolved_cards, key=lambda x: x["card_name"])
    ]

    return {
        "as_of": as_of,
        "dataset_path": dataset_path,
        "prices_db_path": prices_db_path,
        "weights": dict(_ALL_WEIGHTS),
        "momentum_cap_pct": MOMENTUM_CAP_PCT,
        "trend_cap": TREND_CAP,
        "adoption_total_decks": adoption["total_decks"],
        "adoption_revision_count": adoption["revision_count"],
        "adoption_unknown_archive_timestamp_excluded": adoption[
            "unknown_archive_timestamp_excluded"
        ],
        "adoption_invalid_event_date_excluded": adoption["invalid_event_date_excluded"],
        "adoption_future_event_date_excluded": adoption["future_event_date_excluded"],
        "adoption_source_provider": adoption["source_provider"],
        "adoption_format": adoption["format"],
        "price_product_count": len(price_features),
        "candidate_count": len(evidence_backed),
        "candidates": evidence_backed,
        "price_only_mover_count": len(price_only),
        "price_only_movers": price_only,
        "adoption_only_product_count": len(adoption_only_product_rows),
        "adoption_only_product_rows": adoption_only_product_rows,
        "unresolved_adoption_card_count": len(unresolved_rows),
        "unresolved_adoption_cards": unresolved_rows,
    }


def _build_row(
    product_id: Optional[int],
    card_name: Optional[str],
    adoption_entry: Optional[Mapping[str, Any]],
    price_entry: Optional[Mapping[str, Any]],
    adoption_total_decks: int,
    printing_count_for_card: int,
) -> Dict[str, Any]:
    components, missing_flags, final_score, available_count = compute_row_score(
        adoption_entry, price_entry
    )
    confidence = _classify_confidence(
        adoption_entry, price_entry, adoption_total_decks, available_count
    )
    reasons = _build_reasons(
        adoption_entry, price_entry, components, missing_flags, printing_count_for_card
    )
    return {
        "product_id": product_id,
        "card_name": card_name,
        "adoption": _adoption_metrics(adoption_entry),
        "price": _price_metrics(price_entry),
        "components": components,
        "component_weights": dict(_ALL_WEIGHTS),
        "available_component_count": available_count,
        "final_score": final_score,
        "confidence": confidence,
        "missing_flags": list(missing_flags),
        "reasons": reasons,
        "printing_count_for_card": printing_count_for_card,
    }


def rank_and_limit(
    candidates: Iterable[Mapping[str, Any]],
    top: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return a stable-ordered, optionally-truncated copy of ``candidates``.

    ``build_candidates`` already sorts; this helper is provided so a
    caller that wants only the top-N without re-sorting can call it
    cheaply.
    """
    ordered = list(candidates)
    if top is not None and top >= 0:
        ordered = ordered[:top]
    return [dict(r) for r in ordered]
