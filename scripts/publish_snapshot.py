"""
Publish a new DB snapshot (prices.db + signals.db) after a successful
daily fetch/analysis run. Used by the daily workflow's persist step.

Stages a consistent copy of both databases, uploads them as a brand-new
immutable release, downloads that release back down to verify it is
complete and uncorrupted, and only then advances the "latest" pointer.
The previous good snapshot is never overwritten or deleted. Any failure
raises a hard error (nonzero exit) rather than silently leaving the
pointer on a stale or partially-uploaded snapshot.

Usage:
    python -m scripts.publish_snapshot --source-commit <sha>
"""
import argparse
import sys

from scripts.snapshot_storage import (
    GhReleaseSnapshotBackend,
    SnapshotError,
    create_and_publish_snapshot,
)

PRICES_DB = "data/prices.db"
SIGNALS_DB = "data/signals.db"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", default=None,
                         help="Commit SHA the snapshot was produced from (recorded in the manifest).")
    args = parser.parse_args()

    backend = GhReleaseSnapshotBackend()
    try:
        manifest = create_and_publish_snapshot(backend, PRICES_DB, SIGNALS_DB, args.source_commit)
    except SnapshotError as e:
        print(f"ERROR: Failed to publish database snapshot: {e}")
        sys.exit(1)

    print("Snapshot published and verified.")
    print(f"  snapshot_id:       {manifest['snapshot_id']}")
    print(f"  source_commit:     {manifest['source_commit']}")
    print(f"  latest_price_date: {manifest['latest_price_date']}")
    for name, info in manifest["files"].items():
        print(f"  {name}: {info['size_bytes']:,} bytes, sha256={info['sha256'][:12]}...")


if __name__ == "__main__":
    main()
