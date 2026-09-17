"""
TCG Advanced tournament adoption feature (first model input).

Read-only adapter over the already-imported Meta Watch dataset
(``data/meta_watch_lists.json``). Produces a per-canonical-card summary
of how frequently that card appeared in recently published tournament
decklists that came from the YGOPRODeck source_provider under the
TCG_ADVANCED format, and optionally fans that summary out over every
tracked printing whose ``prices.card_name`` matches (identity-level:
exact name, or ``"<name> (<printing detail>)"`` — the same boundary
rule used by :func:`app.meta_watch.resolve_card_printings`).

Design invariants (mirrors ``PROJECT_STATUS.md`` / ``app/evidence.py``):

* **Identity-level.** Adoption is a property of a *canonical card name*,
  never of a specific printing. Fan-out to ``product_id``\\ s in the
  tracked catalog is a downstream join for display / candidate
  ranking; the tournament decklist did NOT identify the printing.

* **Missing is unknown, not zero.** An observation whose
  ``source_provider`` is not ``ygoprodeck`` is excluded (not
  attributed). Placement strings that don't map to a known rank
  contribute ``0`` to the placement-weighted numerator/denominator
  (they still count toward ``unique_decks`` / ``total_copies`` /
  ``deck_penetration_rate``, but they never inflate weighted
  adoption).

* **``as_of`` cutoff excludes the future.** Observations whose
  ``event_date`` is strictly after the cutoff are dropped before
  aggregation; this lets time-ordered evaluation replay the feature
  as it would have appeared on a prior date without leaking future
  tournaments backward. When ``as_of`` is ``None`` the full dataset
  is used.

* **Read-only, no writes.** This module never mutates
  ``prices.db``, ``meta_watch_lists.json``, or any generated cache.
  It does not touch the UI, routes, or existing PR-#7/#8 flows.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from app.meta_watch import (
    DEFAULT_DATASET_PATH,
    FORMAT_TCG_ADVANCED,
    SOURCE_TOURNAMENT,
    ZONES,
    dedupe_observations,
    get_zone_entries,
    load_dataset,
    resolve_card_printings,
)

SOURCE_PROVIDER_YGOPRODECK = "ygoprodeck"

# Placement -> weight table. Higher-finishing decks contribute more to
# the weighted adoption score, but every deck (including unknown
# placements) still counts toward the unweighted deck_penetration_rate.
# Weights are deliberately deterministic and small so the interpretation
# is transparent: a Winner deck weighs the same as four Top-8 decks.
_PLACEMENT_WEIGHTS: Dict[str, float] = {
    "winner": 4.0,
    "1st": 4.0,
    "first": 4.0,
    "1": 4.0,
    "runner-up": 3.0,
    "runner up": 3.0,
    "2nd": 3.0,
    "second": 3.0,
    "2": 3.0,
    "top 4": 2.0,
    "top4": 2.0,
    "semifinalist": 2.0,
    "3rd": 2.0,
    "third": 2.0,
    "3": 2.0,
    "4th": 2.0,
    "fourth": 2.0,
    "4": 2.0,
    "top 8": 1.0,
    "top8": 1.0,
    "quarterfinalist": 1.0,
    "5": 1.0,
    "6": 1.0,
    "7": 1.0,
    "8": 1.0,
}


def placement_weight(placement: Any) -> float:
    """Return the weight for a placement string / int. Unknown → 0.0.

    Unknown placements never contribute to the placement-weighted
    adoption numerator or denominator; they still count as one deck
    for the unweighted deck-penetration rate. This is intentional:
    "we don't know how well this deck finished" is treated as unknown,
    not as a top finish.
    """
    if placement is None:
        return 0.0
    key = str(placement).strip().lower()
    return _PLACEMENT_WEIGHTS.get(key, 0.0)


def _parse_event_date(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def filter_source(observations: Iterable[Mapping[str, Any]],
                   source_provider: str = SOURCE_PROVIDER_YGOPRODECK,
                   fmt: str = FORMAT_TCG_ADVANCED,
                   source_type: str = SOURCE_TOURNAMENT) -> List[Mapping[str, Any]]:
    """Keep only observations from the requested provider, format, and source type.

    An observation with a missing ``source_provider`` is excluded (we
    can't attribute it to YGOPRODeck without lying about provenance).
    """
    kept: List[Mapping[str, Any]] = []
    for obs in observations:
        if obs.get("source_provider") != source_provider:
            continue
        if obs.get("format") != fmt:
            continue
        if obs.get("source_type") != source_type:
            continue
        kept.append(obs)
    return kept


def apply_as_of_cutoff(observations: Iterable[Mapping[str, Any]],
                        as_of: Optional[str]) -> List[Mapping[str, Any]]:
    """Drop observations whose ``event_date`` is strictly after ``as_of``.

    Observations with an unparseable ``event_date`` are excluded when a
    cutoff is requested (we can't prove they are in the past). When
    ``as_of`` is ``None`` all observations are kept.
    """
    if as_of is None:
        return list(observations)
    cutoff = _parse_event_date(as_of)
    if cutoff is None:
        raise ValueError(f"invalid as_of date: {as_of!r} (expected YYYY-MM-DD)")
    kept: List[Mapping[str, Any]] = []
    for obs in observations:
        ed = _parse_event_date(obs.get("event_date"))
        if ed is None:
            continue
        if ed <= cutoff:
            kept.append(obs)
    return kept


def _deck_card_copies(obs: Mapping[str, Any]) -> Dict[str, int]:
    """Total copies of each canonical card name across every zone of one deck.

    Multiple entries with the same name inside the same zone are
    combined by :func:`app.meta_watch.get_zone_entries`. Copies in
    main / side / extra are summed here so a card that appears in both
    the main deck and side deck counts once toward ``unique_decks``
    and its full main+side copy count toward ``total_copies``.
    """
    combined: Dict[str, int] = defaultdict(int)
    for zone in ZONES:
        for name, count in get_zone_entries(obs, zone):
            combined[name] += count
    return dict(combined)


def aggregate_adoption(observations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate deduplicated, filtered observations into per-card features.

    Returns a dict with:

    * ``total_decks``: number of qualifying decks that fed the aggregation.
    * ``total_placement_weight``: sum of placement weights across those decks.
    * ``cards``: list of per-card feature dicts sorted by (unique_decks desc,
      card_name asc). Each entry has:

      - ``card_name``: canonical card name as it appears in the decklist.
      - ``unique_decks``: number of distinct decks containing the card.
      - ``total_copies``: sum of copies across those decks.
      - ``deck_penetration_rate``: ``unique_decks / total_decks``
        (0.0 when ``total_decks == 0``).
      - ``placement_weighted_adoption``: ``sum(weight(deck) for deck with card) /
        sum(weight(deck) for all decks)`` (0.0 when the total weight is 0,
        so a dataset with only unknown placements still produces a
        defined score rather than raising).
      - ``latest_event_date``: max ``event_date`` (YYYY-MM-DD string)
        among decks containing the card, or ``None`` if no event date
        was parseable.
    """
    total_decks = len(observations)
    total_placement_weight = 0.0
    per_deck_weight: List[float] = []
    for obs in observations:
        w = placement_weight(obs.get("placement"))
        per_deck_weight.append(w)
        total_placement_weight += w

    per_card: Dict[str, Dict[str, Any]] = {}
    for idx, obs in enumerate(observations):
        weight = per_deck_weight[idx]
        event_date = obs.get("event_date")
        copies_by_name = _deck_card_copies(obs)
        for name, copies in copies_by_name.items():
            card = per_card.setdefault(name, {
                "card_name": name,
                "unique_decks": 0,
                "total_copies": 0,
                "_weight_sum": 0.0,
                "latest_event_date": None,
            })
            card["unique_decks"] += 1
            card["total_copies"] += copies
            card["_weight_sum"] += weight
            if isinstance(event_date, str) and event_date:
                if card["latest_event_date"] is None or event_date > card["latest_event_date"]:
                    card["latest_event_date"] = event_date

    cards: List[Dict[str, Any]] = []
    for card in per_card.values():
        weight_sum = card.pop("_weight_sum")
        card["deck_penetration_rate"] = (
            card["unique_decks"] / total_decks if total_decks else 0.0
        )
        card["placement_weighted_adoption"] = (
            weight_sum / total_placement_weight if total_placement_weight else 0.0
        )
        cards.append(card)
    cards.sort(key=lambda c: (-c["unique_decks"], c["card_name"]))

    return {
        "total_decks": total_decks,
        "total_placement_weight": total_placement_weight,
        "cards": cards,
    }


def build_adoption_features(dataset_path: str = DEFAULT_DATASET_PATH,
                              as_of: Optional[str] = None,
                              source_provider: str = SOURCE_PROVIDER_YGOPRODECK,
                              fmt: str = FORMAT_TCG_ADVANCED) -> Dict[str, Any]:
    """End-to-end feature build: load → filter → dedupe → cutoff → aggregate.

    Handles a missing or empty dataset file gracefully by returning an
    empty (but well-formed) feature report; downstream consumers can
    check ``total_decks == 0`` rather than special-casing IO failures.
    """
    dataset = load_dataset(dataset_path)
    raw = dataset.get("observations") or []
    filtered = filter_source(raw, source_provider=source_provider, fmt=fmt)
    deduped, duplicate_count = dedupe_observations(filtered)
    windowed = apply_as_of_cutoff(deduped, as_of)
    agg = aggregate_adoption(windowed)
    return {
        "as_of": as_of,
        "source_provider": source_provider,
        "format": fmt,
        "dataset_path": dataset_path,
        "raw_observation_count": len(raw),
        "filtered_observation_count": len(filtered),
        "duplicate_dropped_count": duplicate_count,
        "total_decks": agg["total_decks"],
        "total_placement_weight": agg["total_placement_weight"],
        "cards": agg["cards"],
    }


def join_printings(conn: sqlite3.Connection, cards: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Read-only join of adoption features to matching tracked printings.

    Fans one canonical card out over every ``prices`` row whose
    ``card_name`` matches exactly, or matches the ``"<name> (…"``
    prefix pattern used by :func:`app.meta_watch.resolve_card_printings`.
    Cards with no matching tracked printing are still returned, with
    ``printings=[]`` and ``printing_count=0`` — the adoption feature
    itself is card-level and remains valid even without a price join.

    The connection is used read-only: only ``SELECT`` statements are
    issued. This never claims the decklist identified the specific
    printing; it only surfaces "here are the tracked printings this
    card could refer to" for downstream ranking / display.
    """
    joined: List[Dict[str, Any]] = []
    for card in cards:
        printings = resolve_card_printings(conn, card["card_name"])
        entry = dict(card)
        entry["printings"] = printings
        entry["printing_count"] = len(printings)
        joined.append(entry)
    return joined


def build_report(dataset_path: str = DEFAULT_DATASET_PATH,
                   as_of: Optional[str] = None,
                   prices_db_path: Optional[str] = None,
                   top: Optional[int] = None) -> Dict[str, Any]:
    """Build the concise JSON report used by the CLI and by tests.

    When ``prices_db_path`` is provided AND the file exists, adoption
    features are joined read-only to matching printings. When the file
    is absent, the report still contains the full identity-level
    adoption feature set (with ``printings_joined=False``) so the
    feature is useful even before a snapshot is restored.
    """
    features = build_adoption_features(dataset_path=dataset_path, as_of=as_of)
    all_cards = features["cards"]
    if top is not None and top >= 0:
        cards_slice = all_cards[:top]
    else:
        cards_slice = all_cards

    printings_joined = False
    if prices_db_path and os.path.exists(prices_db_path):
        with sqlite3.connect(f"file:{prices_db_path}?mode=ro", uri=True) as conn:
            cards_slice = join_printings(conn, cards_slice)
        printings_joined = True
    else:
        # Preserve the identity-level view even without a price join.
        cards_slice = [dict(c, printings=[], printing_count=0) for c in cards_slice]

    return {
        "as_of": features["as_of"],
        "source_provider": features["source_provider"],
        "format": features["format"],
        "dataset_path": features["dataset_path"],
        "prices_db_path": prices_db_path,
        "printings_joined": printings_joined,
        "raw_observation_count": features["raw_observation_count"],
        "filtered_observation_count": features["filtered_observation_count"],
        "duplicate_dropped_count": features["duplicate_dropped_count"],
        "total_decks": features["total_decks"],
        "total_placement_weight": features["total_placement_weight"],
        "returned_card_count": len(cards_slice),
        "total_card_count": len(all_cards),
        "cards": cards_slice,
    }


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print a concise TCG_ADVANCED tournament-adoption report "
            "over the imported YGOPRODeck decklists. Read-only."
        )
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET_PATH,
                        help=f"Meta Watch dataset path (default: {DEFAULT_DATASET_PATH}).")
    parser.add_argument("--as-of", default=None,
                        help="Exclude observations whose event_date is after this YYYY-MM-DD cutoff.")
    parser.add_argument("--prices-db", default=None,
                        help="Optional path to prices.db for read-only printing fan-out.")
    parser.add_argument("--top", type=int, default=25,
                        help="Return only the top-N cards by unique_decks (default: 25; use 0 for all).")
    parser.add_argument("--output", default=None,
                        help="Write JSON to this path instead of stdout.")
    args = parser.parse_args(argv)

    top = None if args.top is None or args.top <= 0 else args.top
    report = build_report(
        dataset_path=args.dataset,
        as_of=args.as_of,
        prices_db_path=args.prices_db,
        top=top,
    )
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        directory = os.path.dirname(args.output)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.output, "w") as f:
            f.write(payload)
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
