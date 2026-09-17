"""
Idempotent, dry-run capable backfill CLI for existing YGOPRODeck
observations in the Meta Watch dataset.

Usage:
    python -m scripts.backfill_ygoprodeck_card_names \
        --dataset data/meta_watch_lists.json \
        --report data/meta_watch_ygoprodeck_backfill_report.json \
        --cache-dir data/cache/ygoprodeck \
        [--dry-run] \
        [--prices-db data/prices.db]

Behaviour (see PR problem statement):

- Iterates only observations with ``source_provider == "ygoprodeck"``.
- Skips observations that no longer contain any numeric-passcode
  ``name`` field -- so re-running the CLI on an already-backfilled
  dataset is a no-op (idempotent).
- Loads the YGOPRODeck cardinfo catalogue at most once per run via
  ``scripts.ygoprodeck_card_catalogue.load_passcode_map`` (HTTPS + cert
  verification + timeout + retry pacing + local cache).
- Rewrites each qualifying observation's ``main_deck`` / ``side_deck``
  / ``extra_deck`` to canonical card names, preserving copy counts.
- If any observation has passcodes we cannot resolve, the observation
  is left unchanged and the unresolved passcodes/counts are reported
  honestly. Never substitutes another card or produces a partial deck.
- Legacy / non-YGOPRODeck observations are left byte-identical.
- Atomic writes: the dataset is written via a ``*.tmp`` + ``os.replace``
  swap.
- On catalogue endpoint or local cache failure the dataset is left
  byte-identical and the failure is reported. The report file is still
  written so operators have a record.
- Optional ``--prices-db`` performs a read-only coverage validation:
  exact case-insensitive canonical-name matching against the
  ``card_name`` column of ``data/prices.db``, reporting matched vs
  unmatched card names and, for matches, the number of tracked
  product_id printings. It never selects/invents a printing. If the DB
  file is missing the report says so honestly rather than being silently
  omitted.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from app.meta_watch import DEFAULT_DATASET_PATH  # noqa: E402
from scripts.ygoprodeck_card_catalogue import (  # noqa: E402
    CatalogueError,
    load_passcode_map,
    observation_has_numeric_passcodes,
    resolve_observation_cards,
)

DEFAULT_CACHE_DIR = "data/cache/ygoprodeck"
DEFAULT_REPORT_PATH = "data/meta_watch_ygoprodeck_backfill_report.json"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _iter_canonical_names(observations):
    """Yield every canonical card name mentioned in the (already-resolved)
    YGOPRODeck observations. Numeric-passcode names are excluded so the
    coverage check only reports on canonical names we actually intend to
    match against the prices DB."""
    for obs in observations:
        if obs.get("source_provider") != "ygoprodeck":
            continue
        for zone in ("main_deck", "side_deck", "extra_deck"):
            for entry in obs.get(zone) or []:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name.strip() and not name.strip().isdigit():
                    yield name.strip()


def check_prices_db_coverage(prices_db_path, canonical_names):
    """
    Read-only coverage check against ``data/prices.db``.

    Returns a dict with ``present``, ``prices_db_path``, and either
    ``matched``/``unmatched`` details, or a ``reason`` if the DB is
    missing / unreadable. Matching is exact case-insensitive on the
    ``card_name`` column; we never choose a specific printing.
    """
    result = {"prices_db_path": prices_db_path, "present": False}
    if not prices_db_path:
        result["reason"] = "no prices-db path supplied"
        return result
    if not os.path.exists(prices_db_path):
        result["reason"] = "prices-db file not found"
        return result

    unique_names = sorted({n for n in canonical_names})
    matched = []
    unmatched = []
    try:
        conn = sqlite3.connect(f"file:{prices_db_path}?mode=ro", uri=True)
        try:
            cursor = conn.cursor()
            # Confirm the expected schema up front so we fail honestly
            # if the DB has been reshaped rather than silently reporting
            # everything as unmatched.
            cursor.execute("PRAGMA table_info(prices)")
            columns = {row[1] for row in cursor.fetchall()}
            if "card_name" not in columns or "product_id" not in columns:
                result["reason"] = (
                    "prices-db does not expose expected 'card_name'/'product_id' columns"
                )
                return result
            for name in unique_names:
                cursor.execute(
                    "SELECT COUNT(DISTINCT product_id) FROM prices "
                    "WHERE LOWER(card_name) = LOWER(?)",
                    (name,),
                )
                count = cursor.fetchone()[0] or 0
                if count > 0:
                    matched.append({"card_name": name, "product_id_printings": int(count)})
                else:
                    unmatched.append(name)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["reason"] = f"prices-db read failed: {exc}"
        return result

    result["present"] = True
    result["unique_canonical_names"] = len(unique_names)
    result["matched"] = matched
    result["unmatched"] = unmatched
    return result


def backfill(
    dataset_path=DEFAULT_DATASET_PATH,
    report_path=DEFAULT_REPORT_PATH,
    cache_dir=DEFAULT_CACHE_DIR,
    dry_run=False,
    prices_db_path=None,
    session=None,
    now=None,
    passcode_map=None,
    catalogue_source_override=None,
):
    """
    Return a report dict. ``passcode_map`` may be supplied by tests to
    skip the catalogue fetch entirely and stay deterministic.
    """
    now_iso = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = {
        "backfilled_at": now_iso,
        "dataset_path": dataset_path,
        "dry_run": dry_run,
        "cache_dir": cache_dir,
        "card_catalogue": {"source": None, "size": 0},
        "endpoint_failure": None,
        "observations_total": 0,
        "observations_ygoprodeck": 0,
        "observations_scanned": 0,
        "observations_already_canonical_skipped": 0,
        "observations_resolved": 0,
        "observations_unresolved": [],
        "prices_db_coverage": None,
    }

    if not os.path.exists(dataset_path):
        report["endpoint_failure"] = {
            "endpoint": dataset_path,
            "error": "dataset file does not exist",
        }
        _atomic_write_json(report_path, report)
        return report

    # Read the raw file so we can compare byte-for-byte on failure.
    with open(dataset_path, "rb") as f:
        original_bytes = f.read()
    dataset = json.loads(original_bytes.decode("utf-8"))
    observations = dataset.get("observations", [])
    report["observations_total"] = len(observations)
    report["observations_ygoprodeck"] = sum(
        1 for o in observations if o.get("source_provider") == "ygoprodeck"
    )

    if passcode_map is None:
        try:
            passcode_map, catalogue_source = load_passcode_map(
                cache_dir=cache_dir,
                session=session,
            )
        except CatalogueError as exc:
            report["endpoint_failure"] = {
                "endpoint": "ygoprodeck cardinfo",
                "error": str(exc),
            }
            # Dataset stays byte-identical on catalogue/cache failure.
            _atomic_write_json(report_path, report)
            return report
    else:
        catalogue_source = catalogue_source_override or "provided"
    report["card_catalogue"] = {"source": catalogue_source, "size": len(passcode_map)}

    new_observations = []
    any_change = False
    for obs in observations:
        if obs.get("source_provider") != "ygoprodeck":
            new_observations.append(obs)
            continue
        if not observation_has_numeric_passcodes(obs):
            # Idempotent: already-canonical YGOPRODeck rows are skipped.
            report["observations_already_canonical_skipped"] += 1
            new_observations.append(obs)
            continue
        report["observations_scanned"] += 1
        resolved, unresolved_by_zone = resolve_observation_cards(obs, passcode_map)
        if resolved is None:
            report["observations_unresolved"].append(
                {
                    "event_id": obs.get("event_id"),
                    "source_deck_id": obs.get("source_deck_id"),
                    "unresolved": unresolved_by_zone,
                }
            )
            # Leave this observation unchanged -- never substitute a
            # card or write a partial deck.
            new_observations.append(obs)
            continue
        report["observations_resolved"] += 1
        any_change = True
        new_observations.append(resolved)

    if any_change and not dry_run:
        new_dataset = dict(dataset)
        new_dataset["observations"] = new_observations
        _atomic_write_json(dataset_path, new_dataset)

    # Coverage check runs over the *post-backfill* view of the dataset
    # (or the current view if dry-run) so it reflects what would end up
    # persisted. It never chooses a specific printing.
    if prices_db_path is not None:
        canonical_names = list(_iter_canonical_names(new_observations))
        report["prices_db_coverage"] = check_prices_db_coverage(
            prices_db_path, canonical_names
        )

    _atomic_write_json(report_path, report)
    return report


def build_stdout_summary(result):
    """
    Construct an independently-built, allowlisted stdout summary from a
    backfill report.

    Only safe scalar counts and booleans are copied out one field at a
    time -- never derived by shallow-copying or spreading the full
    report. Paths, endpoint URLs, cache directories, canonical card
    names, passcodes, exception messages, and any prices-db content are
    intentionally excluded so no potentially sensitive value can flow
    from the report dict into stdout.
    """
    endpoint_failure = result.get("endpoint_failure")
    prices = result.get("prices_db_coverage") or {}
    matched = prices.get("matched") or []
    unmatched = prices.get("unmatched") or []
    unresolved_records = result.get("observations_unresolved") or []
    unresolved_passcode_count = 0
    for record in unresolved_records:
        zones = record.get("unresolved") or {}
        for entries in zones.values():
            for entry in entries or []:
                count = entry.get("count")
                if isinstance(count, int) and count > 0:
                    unresolved_passcode_count += count

    summary = {
        "status": "ok" if endpoint_failure is None else "endpoint_failure",
        "success": endpoint_failure is None,
        "dry_run": bool(result.get("dry_run")),
        "observations_scanned": int(result.get("observations_scanned") or 0),
        "observations_changed": int(result.get("observations_resolved") or 0),
        "observations_already_canonical_skipped": int(
            result.get("observations_already_canonical_skipped") or 0
        ),
        "records_rejected": len(unresolved_records),
        "resolved_passcode_count": int(result.get("observations_resolved") or 0),
        "unresolved_passcode_count": unresolved_passcode_count,
        "matched_card_name_count": len(matched),
        "unmatched_card_name_count": len(unmatched),
        "prices_db_checked": bool(prices) and bool(prices.get("present")),
    }
    return summary


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Backfill YGOPRODeck-sourced Meta Watch observations by "
            "resolving passcode 'name' fields to canonical card names "
            "via the YGOPRODeck cardinfo catalogue."
        )
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET_PATH)
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prices-db",
        default=None,
        help="Optional path to data/prices.db for read-only coverage validation",
    )
    args = parser.parse_args()

    result = backfill(
        dataset_path=args.dataset,
        report_path=args.report,
        cache_dir=args.cache_dir,
        dry_run=args.dry_run,
        prices_db_path=args.prices_db,
    )
    # Full report is persisted to args.report by backfill(). Stdout only
    # receives an independently-constructed, allowlisted summary of safe
    # scalar counts -- never the report dict itself.
    summary = build_stdout_summary(result)
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if not summary["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
