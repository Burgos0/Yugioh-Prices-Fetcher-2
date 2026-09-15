"""
Import validated tournament decklist observations into the Meta Watch
dataset (data/meta_watch_lists.json by default).

This importer never fetches anything itself -- it only validates and
appends locally supplied JSON. It exists because automated retrieval from
official Konami tournament coverage was not available/reliable enough to
build a trustworthy scraper against (see META_WATCH.md for details); this
is the documented fallback the feature ships with instead.

Input file format: a JSON array of observation objects, or
{"observations": [...]}, where each observation is:

{
  "event_id": "2026-ycs-atlanta",       # required, stable id for the event
  "event_name": "YCS Atlanta 2026",     # required
  "event_date": "2026-08-30",           # required, YYYY-MM-DD
  "region": "NA",                       # required
  "format": "TCG_ADVANCED",             # required: TCG_ADVANCED | OCG | MASTER_DUEL
  "banlist_id": "2026-04",              # required, the banlist in effect
  "player": "Jane Doe",                 # required
  "placement": "1st",                   # optional
  "archetype": "Kashtira",              # required, as published (never guessed)
  "source_url": "https://...",          # required, the coverage page/article
  "source_type": "tournament",          # required: "tournament" | "casual"
  "published_at": "2026-08-31T12:00:00Z", # optional ISO8601 UTC; null/omitted if unknown
  "main_deck": [{"name": "Ash Blossom & Joyous Spring", "count": 3}, ...],
  "side_deck": [...],   # optional, defaults to []
  "extra_deck": [...]   # optional, defaults to []
}

first_seen_at is set automatically to the import time (UTC) if not
supplied. Card names should match the card's TCGPlayer-tracked name as
closely as possible; unresolved names are reported by the analysis layer
rather than guessed.

Validation rejects (and reports, without importing) any observation
missing a required field, with an unrecognized format/source_type, an
unparseable date, or malformed deck entries. Duplicate (event_id, player)
pairs -- against the existing dataset or within the same input file -- are
skipped and counted, never double-counted.

Usage:
    python -m scripts.import_meta_watch_lists path/to/input.json [--dataset data/meta_watch_lists.json] [--dry-run]
"""
import argparse
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from app.meta_watch import (  # noqa: E402
    DEFAULT_DATASET_PATH,
    dedupe_key,
    load_dataset,
    save_dataset,
    validate_observation,
)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def import_observations(input_path, dataset_path=DEFAULT_DATASET_PATH, dry_run=False):
    with open(input_path) as f:
        payload = json.load(f)
    raw_observations = payload.get("observations", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_observations, list):
        raise ValueError("Input must be a JSON list of observations, or {'observations': [...]}")

    dataset = load_dataset(dataset_path)
    existing_keys = {dedupe_key(o) for o in dataset["observations"]}

    added = []
    rejected = []
    duplicate_existing = 0
    duplicate_in_batch = 0
    seen_batch_keys = set()

    for index, raw in enumerate(raw_observations):
        errors = validate_observation(raw)
        if errors:
            rejected.append({"index": index, "event_id": raw.get("event_id") if isinstance(raw, dict) else None,
                              "errors": errors})
            continue

        key = dedupe_key(raw)
        if key in existing_keys:
            duplicate_existing += 1
            continue
        if key in seen_batch_keys:
            duplicate_in_batch += 1
            continue
        seen_batch_keys.add(key)

        obs = dict(raw)
        obs.setdefault("published_at", None)
        obs.setdefault("placement", None)
        obs.setdefault("side_deck", [])
        obs.setdefault("extra_deck", [])
        obs.setdefault("first_seen_at", _now_iso())
        added.append(obs)

    if added and not dry_run:
        dataset["observations"].extend(added)
        save_dataset(dataset, dataset_path)

    return {
        "input_path": input_path,
        "dataset_path": dataset_path,
        "dry_run": dry_run,
        "added": len(added),
        "duplicate_existing_skipped": duplicate_existing,
        "duplicate_in_batch_skipped": duplicate_in_batch,
        "rejected": rejected,
    }


def main():
    parser = argparse.ArgumentParser(description="Import validated Meta Watch tournament decklist observations.")
    parser.add_argument("input_path", help="Path to a JSON file of observations to import")
    parser.add_argument("--dataset", default=DEFAULT_DATASET_PATH, help="Path to the Meta Watch dataset JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Validate/report without writing the dataset")
    args = parser.parse_args()

    result = import_observations(args.input_path, dataset_path=args.dataset, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    if result["rejected"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
