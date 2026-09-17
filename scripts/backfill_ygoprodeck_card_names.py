"""
Idempotent backfill CLI: rewrite existing YGOPRODeck observations so
their deck arrays store canonical card names instead of numeric card_ids.

Scope and invariants:

- **YGOPRODeck-only.** The backfill only touches observations whose
  ``source_provider`` is ``"ygoprodeck"``. Every other observation --
  including legacy Konami imports, hand-curated entries, and anything
  else that predates this feature -- is left byte-for-byte unchanged.
- **Idempotent.** A second run against the already-backfilled dataset
  produces zero changes. That includes: entries whose ``name`` is
  already a canonical (non-numeric) name are passed through untouched;
  duplicate card_ids that both map to the same canonical name have
  their counts summed on the first run and stay stable on subsequent
  runs.
- **Honest rejection.** If any card_id in an observation cannot be
  resolved by the card bridge, that whole observation is left untouched
  and reported. We never write a partial rewrite.
- **Dry-run by default.** ``--apply`` is required to actually persist
  changes; without it the dataset file is not modified. Either way, a
  detailed JSON report is printed.
- **No network unless needed.** The bridge fetches the cardinfo
  endpoint at most once, and only if the on-disk cache cannot resolve
  every card_id already. Rerunning after a successful apply is a pure
  cache-hit, no-op operation.
"""
import argparse
import json
import sys

sys.path.insert(0, ".")

from app.meta_watch import DEFAULT_DATASET_PATH, load_dataset, save_dataset  # noqa: E402
from scripts.ygoprodeck_card_bridge import (  # noqa: E402
    DEFAULT_CACHE_PATH as DEFAULT_BRIDGE_CACHE_PATH,
    CardBridgeError,
    _normalize_card_id,
    rebuild_cards_with_canonical_names,
    resolve_card_ids,
)


DECK_FIELDS = ("main_deck", "side_deck", "extra_deck")


def _collect_card_ids(observation):
    card_ids = set()
    for field in DECK_FIELDS:
        for entry in observation.get(field, []) or []:
            card_id = _normalize_card_id(entry.get("name") if isinstance(entry, dict) else None)
            if card_id is not None:
                card_ids.add(card_id)
    return card_ids


def _observation_needs_backfill(observation):
    """True if any deck entry still uses a numeric card_id as its name."""
    return bool(_collect_card_ids(observation))


def _rewrite_observation(observation, resolved):
    """Return (new_observation, unresolved_ids, changed)."""
    unresolved = set()
    changed = False
    new_obs = dict(observation)
    for field in DECK_FIELDS:
        original = observation.get(field, []) or []
        rebuilt, missing = rebuild_cards_with_canonical_names(original, resolved)
        for m in missing:
            unresolved.add(m)
        if missing:
            # Do not partially rewrite -- keep the original entries so the
            # observation stays honest on rollback.
            new_obs[field] = list(original)
            continue
        if rebuilt != list(original):
            changed = True
        new_obs[field] = rebuilt
    return new_obs, sorted(unresolved), changed


def backfill(
    dataset_path=DEFAULT_DATASET_PATH,
    card_cache_path=DEFAULT_BRIDGE_CACHE_PATH,
    apply=False,
    resolve_card_ids_fn=resolve_card_ids,
    session=None,
    timeout=None,
):
    """
    Rewrite YGOPRODeck observations in ``dataset_path`` so their deck
    arrays hold canonical card names.

    Args:
        dataset_path: path to ``meta_watch_lists.json``.
        card_cache_path: on-disk card_id -> name cache.
        apply: when False (default) the dataset file is left untouched
            and only a report is produced.
        resolve_card_ids_fn: dependency-injection hook (tests use this
            to skip the real bridge fetch).
        session, timeout: forwarded to the bridge fetcher when needed.

    Returns:
        A JSON-serialisable report dict.
    """
    dataset = load_dataset(dataset_path)
    observations = dataset.get("observations", [])

    report = {
        "dataset_path": dataset_path,
        "cache_path": card_cache_path,
        "dry_run": not apply,
        "observations_scanned": len(observations),
        "ygoprodeck_observations": 0,
        "observations_needing_backfill": 0,
        "observations_rewritten": 0,
        "observations_unchanged": 0,
        "observations_with_unresolved_ids": [],
        "source_limitation_ids": [],
        "unresolved_id_count": 0,
        "card_bridge_failure": None,
        "fetched_from_endpoint": False,
    }

    needs_backfill = []
    all_card_ids = set()
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            continue
        if observation.get("source_provider") != "ygoprodeck":
            continue
        report["ygoprodeck_observations"] += 1
        card_ids = _collect_card_ids(observation)
        if not card_ids:
            # Already canonical -- idempotency guarantees this row is
            # untouched on subsequent runs.
            continue
        needs_backfill.append((index, observation, card_ids))
        all_card_ids.update(card_ids)

    report["observations_needing_backfill"] = len(needs_backfill)

    if not needs_backfill:
        # Nothing to do -- do NOT hit the endpoint or rewrite the file.
        return report

    try:
        resolved, _unresolved_global, fetched = resolve_card_ids_fn(
            sorted(all_card_ids),
            cache_path=card_cache_path,
            session=session,
            timeout=timeout,
        )
    except CardBridgeError as exc:
        report["card_bridge_failure"] = {"error": str(exc)}
        return report

    report["fetched_from_endpoint"] = bool(fetched)

    mutated = False
    new_observations = list(observations)
    for index, observation, _card_ids in needs_backfill:
        new_obs, unresolved, changed = _rewrite_observation(observation, resolved)
        if unresolved:
            report["observations_with_unresolved_ids"].append(
                {
                    "index": index,
                    "event_id": observation.get("event_id"),
                    "source_deck_id": observation.get("source_deck_id"),
                    "unresolved_ids": unresolved,
                }
            )
            report["observations_unchanged"] += 1
            continue
        if not changed:
            report["observations_unchanged"] += 1
            continue
        new_observations[index] = new_obs
        report["observations_rewritten"] += 1
        mutated = True

    if apply and mutated:
        dataset["observations"] = new_observations
        save_dataset(dataset, dataset_path)

    # Union of every id the resolver could not resolve, from both the
    # full-catalogue pass and the bounded single-card fallback. These
    # represent YGOPRODeck's own source limitation for this run.
    source_limitation_ids = sorted(
        {cid for entry in report["observations_with_unresolved_ids"]
         for cid in entry["unresolved_ids"]}
    )
    report["source_limitation_ids"] = source_limitation_ids
    report["unresolved_id_count"] = len(source_limitation_ids)

    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Backfill canonical card names into existing YGOPRODeck "
            "observations. Dry-run by default; use --apply to persist."
        )
    )
    parser.add_argument(
        "--dataset", default=DEFAULT_DATASET_PATH, help="Path to meta_watch_lists.json"
    )
    parser.add_argument(
        "--card-cache",
        default=DEFAULT_BRIDGE_CACHE_PATH,
        help="Path to the card_id -> canonical name cache",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist changes. Without this flag the dataset is left untouched.",
    )
    args = parser.parse_args(argv)

    report = backfill(
        dataset_path=args.dataset,
        card_cache_path=args.card_cache,
        apply=args.apply,
    )
    print(json.dumps(report, indent=2))
    if report.get("card_bridge_failure") or report.get("observations_with_unresolved_ids"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
