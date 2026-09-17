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
  "format": "TCG_ADVANCED",             # required: TCG_ADVANCED | OCG | MASTER_DUEL | RUSH_DUEL | OTHER
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

first_seen_at and archived_at are set automatically to the import time
(UTC); incoming values are ignored. Card names should match the card's TCGPlayer-tracked name as
closely as possible; unresolved names are reported by the analysis layer
rather than guessed.

Validation rejects (and reports, without importing) any observation
missing a required field, with an unrecognized format/source_type, an
unparseable date, or malformed deck entries. Stable provider/deck ids are
preferred for identity, with (event_id, player) as the fallback. Unchanged
repeats are skipped; corrected versions are appended to ``revisions``.

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
    load_dataset,
    revision_key,
    save_dataset,
    validate_observation,
)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _comparable_observation(obs):
    comparable = {
        k: v
        for k, v in obs.items()
        if k not in ("archived_at", "first_seen_at", "main_deck", "side_deck", "extra_deck")
    }
    for zone in ("main_deck", "side_deck", "extra_deck"):
        counts = {}
        for entry in obs.get(zone) or []:
            name = entry.get("name")
            counts[name] = counts.get(name, 0) + entry.get("count", 0)
        comparable[zone] = sorted(counts.items())
    return comparable


def import_observations_payload(payload, dataset_path=DEFAULT_DATASET_PATH, dry_run=False, input_path=None):
    raw_observations = payload.get("observations", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_observations, list):
        raise ValueError("Input must be a JSON list of observations, or {'observations': [...]}")

    dataset = load_dataset(dataset_path)
    existing_by_key = {revision_key(o): o for o in dataset["observations"]}
    for revision in dataset.get("revisions") or []:
        key = revision_key(revision)
        if key in existing_by_key:
            existing_by_key[key] = revision

    added = []
    revisions_added = []
    rejected = []
    duplicate_existing = 0
    duplicate_in_batch = 0
    seen_batch_keys = set()
    archived_at = _now_iso()

    for index, raw in enumerate(raw_observations):
        candidate = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(candidate, dict):
            candidate.pop("archived_at", None)
            candidate.pop("first_seen_at", None)
        errors = validate_observation(candidate)
        if errors:
            rejected.append({"index": index, "event_id": raw.get("event_id") if isinstance(raw, dict) else None,
                              "errors": errors})
            continue

        key = revision_key(candidate)
        if key in seen_batch_keys:
            duplicate_in_batch += 1
            continue
        seen_batch_keys.add(key)

        obs = dict(candidate)
        obs.setdefault("published_at", None)
        obs.setdefault("placement", None)
        obs.setdefault("side_deck", [])
        obs.setdefault("extra_deck", [])
        obs["archived_at"] = archived_at

        existing = existing_by_key.get(key)
        if existing is None:
            obs["first_seen_at"] = archived_at
            added.append(obs)
            existing_by_key[key] = obs
            continue

        obs["first_seen_at"] = existing.get("first_seen_at") or existing.get("archived_at") or archived_at
        if _comparable_observation(existing) == _comparable_observation(obs):
            duplicate_existing += 1
            continue
        revisions_added.append(obs)
        existing_by_key[key] = obs

    if (added or revisions_added) and not dry_run:
        dataset["observations"].extend(added)
        dataset.setdefault("revisions", []).extend(revisions_added)
        save_dataset(dataset, dataset_path)

    return {
        "input_path": input_path,
        "dataset_path": dataset_path,
        "dry_run": dry_run,
        "added": len(added),
        "revisions_added": len(revisions_added),
        "duplicate_existing_skipped": duplicate_existing,
        "duplicate_in_batch_skipped": duplicate_in_batch,
        "rejected": rejected,
    }


def import_observations(input_path, dataset_path=DEFAULT_DATASET_PATH, dry_run=False):
    with open(input_path) as f:
        payload = json.load(f)
    return import_observations_payload(payload, dataset_path=dataset_path, dry_run=dry_run, input_path=input_path)


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
