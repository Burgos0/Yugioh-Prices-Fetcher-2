"""
Restore the latest published DB snapshot into data/prices.db and
data/signals.db. Used by the daily workflow's restore step and by
Codespaces (see --backup-existing).

Downloads into a temporary directory first and validates checksums,
SQLite integrity, and required tables BEFORE installing either database.
A missing pointer, a failed download, or a failed validation is a hard
failure (nonzero exit) -- this never silently leaves an empty database.

Usage:
    python -m scripts.restore_snapshot
    python -m scripts.restore_snapshot --backup-existing   # Codespaces
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.snapshot_storage import (
    GhReleaseSnapshotBackend,
    SnapshotError,
    restore_latest_snapshot,
)

PRICES_DB = "data/prices.db"
SIGNALS_DB = "data/signals.db"


def backup_existing_databases():
    """Back up any current local databases via SQLite's backup API before
    they get overwritten by the restored snapshot."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for path in (PRICES_DB, SIGNALS_DB):
        if not Path(path).exists():
            continue
        backup_path = f"{path}.bak.{stamp}"
        src = sqlite3.connect(path)
        try:
            dst = sqlite3.connect(backup_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        print(f"Backed up existing {path} -> {backup_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-existing", action="store_true",
                         help="Back up any current local databases before restoring (for Codespaces).")
    args = parser.parse_args()

    if args.backup_existing:
        backup_existing_databases()

    backend = GhReleaseSnapshotBackend()
    try:
        manifest = restore_latest_snapshot(backend, PRICES_DB, SIGNALS_DB)
    except SnapshotError as e:
        print(f"ERROR: Failed to restore database snapshot: {e}")
        print("Refusing to continue with an empty/missing database.")
        sys.exit(1)

    print("Restore succeeded.")
    print(f"  snapshot_id:       {manifest['snapshot_id']}")
    print(f"  source_commit:     {manifest['source_commit']}")
    print(f"  latest_price_date: {manifest['latest_price_date']}")
    print(f"  created_at:        {manifest['created_at']}")


if __name__ == "__main__":
    main()
