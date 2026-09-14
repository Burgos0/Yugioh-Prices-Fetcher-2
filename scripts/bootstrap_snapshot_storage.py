"""
One-time bootstrap: publish the very FIRST DB snapshot, by default from the
databases currently checked into Git at the exact repository paths
data/prices.db and data/signals.db, so restore_snapshot.py has something to
restore before any daily run has published a snapshot of its own.

--prices-db / --signals-db let you bootstrap from a recovered or repaired
copy elsewhere on disk (e.g. a copy with a backfilled historical date)
without ever writing that recovered file into the Git-tracked
data/prices.db path -- so it never risks being picked up by an ordinary
`git add`/commit.

This does NOT remove data/prices.db or data/signals.db from Git tracking
-- that is a separate, later step you take manually only after confirming
this bootstrap snapshot uploads and restores correctly (see the
round-trip check this script performs).

Usage:
    python -m scripts.bootstrap_snapshot_storage --source-commit <sha>
    python -m scripts.bootstrap_snapshot_storage --source-commit <sha> \\
        --prices-db /tmp/sept13_recovery/data/prices.db --signals-db data/signals.db
"""
import argparse
import sys
import tempfile

from scripts.snapshot_storage import (
    GhReleaseSnapshotBackend,
    SnapshotError,
    create_and_publish_snapshot,
    restore_latest_snapshot,
)

PRICES_DB = "data/prices.db"
SIGNALS_DB = "data/signals.db"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", default=None,
                         help="Commit SHA the checked-in databases came from.")
    parser.add_argument("--prices-db", default=PRICES_DB,
                         help=f"Path to the prices database to bootstrap from (default: {PRICES_DB}).")
    parser.add_argument("--signals-db", default=SIGNALS_DB,
                         help=f"Path to the signals database to bootstrap from (default: {SIGNALS_DB}).")
    args = parser.parse_args()

    backend = GhReleaseSnapshotBackend()

    try:
        manifest = create_and_publish_snapshot(backend, args.prices_db, args.signals_db, args.source_commit)
    except SnapshotError as e:
        print(f"ERROR: Bootstrap snapshot failed: {e}")
        sys.exit(1)

    print("Bootstrap snapshot published and verified.")
    print(f"  source prices_db:  {args.prices_db}")
    print(f"  source signals_db: {args.signals_db}")
    print(f"  snapshot_id:       {manifest['snapshot_id']}")
    print(f"  latest_price_date: {manifest['latest_price_date']}")

    # Extra end-to-end confirmation: actually restore it into a scratch
    # location (never over the real data/ files) before calling this done.
    with tempfile.TemporaryDirectory() as scratch:
        scratch_prices = f"{scratch}/prices.db"
        scratch_signals = f"{scratch}/signals.db"
        try:
            restore_latest_snapshot(backend, scratch_prices, scratch_signals)
        except SnapshotError as e:
            print(f"ERROR: Bootstrap snapshot was published but failed an independent restore check: {e}")
            sys.exit(1)

    print("Independent restore check into a scratch directory succeeded.")
    print("You may now (separately, manually) remove data/prices.db and "
          "data/signals.db from Git tracking once you're satisfied.")


if __name__ == "__main__":
    main()
