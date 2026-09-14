import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.snapshot_storage import (
    LocalDirSnapshotBackend,
    SnapshotError,
    build_manifest,
    compute_sha256,
    create_and_publish_snapshot,
    restore_latest_snapshot,
    verify_local_snapshot,
)


def make_databases(dest_dir, price_rows=None):
    prices_path = os.path.join(dest_dir, "prices.db")
    signals_path = os.path.join(dest_dir, "signals.db")
    with sqlite3.connect(prices_path) as conn:
        conn.execute("CREATE TABLE prices (product_id INTEGER, card_name TEXT, "
                     "set_name TEXT, date TEXT, market_price REAL)")
        for row in (price_rows or [(1, "Card", "Set", "2026-09-01", 10.0)]):
            conn.execute("INSERT INTO prices VALUES (?, ?, ?, ?, ?)", row)
    with sqlite3.connect(signals_path) as conn:
        conn.execute("CREATE TABLE early_mover_signals (product_id INTEGER, card_name TEXT, "
                     "set_name TEXT, signal_date TEXT, signal_price REAL)")
    return prices_path, signals_path


class SnapshotStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.backend_root = os.path.join(self.root, "remote_releases")
        self.backend = LocalDirSnapshotBackend(self.backend_root)
        self.prices_db, self.signals_db = make_databases(self.root)

    def test_round_trip_restoration(self):
        published = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "abc123")

        dest_dir = os.path.join(self.root, "restored")
        os.makedirs(dest_dir)
        dest_prices = os.path.join(dest_dir, "prices.db")
        dest_signals = os.path.join(dest_dir, "signals.db")
        restored = restore_latest_snapshot(self.backend, dest_prices, dest_signals)

        self.assertEqual(restored["snapshot_id"], published["snapshot_id"])
        self.assertEqual(restored["source_commit"], "abc123")
        self.assertEqual(restored["latest_price_date"], "2026-09-01")
        # SQLite's backup API is a logical page copy, not guaranteed
        # byte-identical to the pre-backup source file -- so compare data
        # content, not raw bytes against the original.
        with sqlite3.connect(dest_prices) as conn:
            self.assertEqual(conn.execute("SELECT product_id, card_name, set_name, date, market_price "
                                           "FROM prices").fetchall(),
                              [(1, "Card", "Set", "2026-09-01", 10.0)])

    def test_missing_snapshot_never_restores_empty_db(self):
        # No snapshot has ever been published -- no pointer exists.
        dest_prices = os.path.join(self.root, "dest_prices.db")
        dest_signals = os.path.join(self.root, "dest_signals.db")
        with self.assertRaises(SnapshotError):
            restore_latest_snapshot(self.backend, dest_prices, dest_signals)
        self.assertFalse(os.path.exists(dest_prices))
        self.assertFalse(os.path.exists(dest_signals))

    def test_invalid_download_checksum_mismatch_never_installs(self):
        create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "abc123")

        # Corrupt the uploaded snapshot after the fact (simulates a bad/incomplete download).
        tag = self.backend.get_pointer()
        corrupted_path = os.path.join(self.backend_root, tag, "prices.db")
        with open(corrupted_path, "r+b") as f:
            f.seek(0)
            f.write(b"\x00\x00\x00\x00")

        dest_prices = os.path.join(self.root, "dest_prices.db")
        dest_signals = os.path.join(self.root, "dest_signals.db")
        with self.assertRaises(SnapshotError):
            restore_latest_snapshot(self.backend, dest_prices, dest_signals)
        self.assertFalse(os.path.exists(dest_prices))
        self.assertFalse(os.path.exists(dest_signals))

    def test_interrupted_upload_never_advances_pointer(self):
        # First, a real good snapshot exists.
        first = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-1")

        # Second publish attempt "uploads" successfully but the artifact is
        # corrupted in transit -- simulate by monkeypatching upload to write
        # a truncated prices.db, so post-upload verification must catch it.
        real_upload = self.backend.upload

        def flaky_upload(local_dir, tag):
            real_upload(local_dir, tag)
            # Simulate an interrupted/corrupted transfer after upload "succeeded".
            with open(os.path.join(self.backend_root, tag, "prices.db"), "wb") as f:
                f.write(b"not a real db")

        self.backend.upload = flaky_upload
        with self.assertRaises(SnapshotError):
            create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-2")

        # Pointer must still reference the first, good snapshot.
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])
        # The first snapshot's files must still be intact (never overwritten/deleted).
        ok, errors, _ = verify_local_snapshot(os.path.join(self.backend_root, first["snapshot_id"]))
        self.assertTrue(ok, errors)

    def test_never_overwrites_or_deletes_previous_good_snapshot(self):
        first = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-1")

        # Change the source data and publish again.
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute("INSERT INTO prices VALUES (2, 'Card2', 'Set', '2026-09-02', 5.0)")
        second = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-2")

        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertTrue(self.backend.release_exists(first["snapshot_id"]))
        self.assertTrue(self.backend.release_exists(second["snapshot_id"]))
        self.assertEqual(self.backend.get_pointer(), second["snapshot_id"])

        # Republishing under an already-used tag must be refused, never silently overwritten.
        with self.assertRaises(SnapshotError):
            self.backend.upload(self.root, first["snapshot_id"])

    def test_pointer_failure_after_creation_before_asset_upload_leaves_draft_ignored(self):
        # A good, published pointer already exists.
        first = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-1")

        # Simulate: the draft pointer release gets created successfully, but
        # the asset upload that would make it complete fails.
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute("INSERT INTO prices VALUES (2, 'Card2', 'Set', '2026-09-02', 5.0)")

        with patch.object(self.backend, "_upload_pointer_asset",
                          side_effect=SnapshotError("simulated upload failure")):
            with self.assertRaises(SnapshotError):
                create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-2")

        # A draft pointer now exists but must be invisible to restore.
        all_tags = self.backend.list_pointer_tags()
        self.assertGreaterEqual(len(all_tags), 2)  # first (published) + the new incomplete draft
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])

        dest_prices = os.path.join(self.root, "dest_prices.db")
        dest_signals = os.path.join(self.root, "dest_signals.db")
        restored = restore_latest_snapshot(self.backend, dest_prices, dest_signals)
        self.assertEqual(restored["snapshot_id"], first["snapshot_id"])

    def test_draft_pointer_that_never_gets_published_is_ignored_forever(self):
        first = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-1")

        # Directly create a draft pointer (as if _publish_pointer were never reached).
        self.backend._create_draft_pointer("data-pointer-99999999T999999Z")
        with self.assertRaises(SnapshotError):
            # It exists but is missing its asset entirely -- still must not
            # be treated as the current pointer.
            self.backend._download_and_check_pointer("data-pointer-99999999T999999Z", "anything")

        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])

    def test_bootstrap_then_independent_restore_round_trip(self):
        # Mirrors scripts.bootstrap_snapshot_storage's flow.
        manifest = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "bootstrap-commit")
        scratch_prices = os.path.join(self.root, "scratch_prices.db")
        scratch_signals = os.path.join(self.root, "scratch_signals.db")
        restored = restore_latest_snapshot(self.backend, scratch_prices, scratch_signals)
        self.assertEqual(restored["snapshot_id"], manifest["snapshot_id"])
        self.assertTrue(os.path.exists(scratch_prices))
        self.assertTrue(os.path.exists(scratch_signals))

    def test_pointer_update_failure_preserves_previous_pointer(self):
        # A good snapshot + pointer already exist.
        first = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-1")
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])

        # The next publish's snapshot upload succeeds and verifies fine, but
        # the pointer update itself fails (e.g. a transient API error).
        real_set_pointer = self.backend.set_pointer
        self.backend.set_pointer = lambda tag: (_ for _ in ()).throw(SnapshotError("simulated pointer API failure"))

        with sqlite3.connect(self.prices_db) as conn:
            conn.execute("INSERT INTO prices VALUES (2, 'Card2', 'Set', '2026-09-02', 5.0)")
        with self.assertRaises(SnapshotError):
            create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-2")

        # The previous pointer must still be intact and resolvable -- a
        # failed pointer update must never destroy access to it.
        self.assertEqual(self.backend.get_pointer(), first["snapshot_id"])
        self.backend.set_pointer = real_set_pointer

        # The new snapshot release itself was still uploaded (it just isn't
        # pointed at yet); restoring still gets the last GOOD, pointed-to snapshot.
        dest_prices = os.path.join(self.root, "dest_prices.db")
        dest_signals = os.path.join(self.root, "dest_signals.db")
        restored = restore_latest_snapshot(self.backend, dest_prices, dest_signals)
        self.assertEqual(restored["snapshot_id"], first["snapshot_id"])

    def test_pointer_history_is_append_only_never_mutated_in_place(self):
        first = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-1")
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute("INSERT INTO prices VALUES (2, 'Card2', 'Set', '2026-09-02', 5.0)")
        second = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-2")

        pointer_tags = self.backend.list_pointer_tags()
        self.assertGreaterEqual(len(pointer_tags), 2)  # one pointer release per publish, never reused
        self.assertEqual(self.backend.get_pointer(), second["snapshot_id"])

    def test_failure_between_installing_prices_and_signals_never_leaves_mixed_pair(self):
        first = create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-1")

        dest_dir = os.path.join(self.root, "installed")
        os.makedirs(dest_dir)
        dest_prices = os.path.join(dest_dir, "prices.db")
        dest_signals = os.path.join(dest_dir, "signals.db")
        restore_latest_snapshot(self.backend, dest_prices, dest_signals)  # establish the "old" pair
        old_prices_bytes = Path(dest_prices).read_bytes()
        old_signals_bytes = Path(dest_signals).read_bytes()

        # Publish a new (different) snapshot to restore next.
        with sqlite3.connect(self.prices_db) as conn:
            conn.execute("INSERT INTO prices VALUES (2, 'Card2', 'Set', '2026-09-02', 5.0)")
        create_and_publish_snapshot(self.backend, self.prices_db, self.signals_db, "commit-2")

        # Simulate a failure installing the second file of the pair.
        real_move = shutil.move
        call_count = {"n": 0}

        def flaky_move(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated disk failure during install")
            return real_move(src, dst)

        with patch("scripts.snapshot_storage.shutil.move", side_effect=flaky_move):
            with self.assertRaises(SnapshotError):
                restore_latest_snapshot(self.backend, dest_prices, dest_signals)

        # Must be rolled back to the OLD pair -- never a mix of old+new.
        self.assertEqual(Path(dest_prices).read_bytes(), old_prices_bytes)
        self.assertEqual(Path(dest_signals).read_bytes(), old_signals_bytes)

        # A recoverable backup of the pre-restore pair must have been preserved.
        backups = list(Path(dest_dir).glob("prices.db.bak.*")) + list(Path(dest_dir).glob("signals.db.bak.*"))
        self.assertTrue(backups)


class VerifyLocalSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.staging = os.path.join(self.tmp.name, "staging")
        os.makedirs(self.staging)
        self.prices_db, self.signals_db = make_databases(self.tmp.name)
        for name, src in (("prices.db", self.prices_db), ("signals.db", self.signals_db)):
            with open(src, "rb") as fsrc, open(os.path.join(self.staging, name), "wb") as fdst:
                fdst.write(fsrc.read())
        manifest = build_manifest("data-test-1", "commit-x", self.staging)
        with open(os.path.join(self.staging, "manifest.json"), "w") as f:
            json.dump(manifest, f)

    def test_valid_snapshot_passes(self):
        ok, errors, manifest = verify_local_snapshot(self.staging)
        self.assertTrue(ok, errors)
        self.assertEqual(manifest["latest_price_date"], "2026-09-01")

    def test_missing_manifest_fails(self):
        os.remove(os.path.join(self.staging, "manifest.json"))
        ok, errors, manifest = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertIsNone(manifest)

    def test_checksum_mismatch_detected(self):
        with open(os.path.join(self.staging, "prices.db"), "ab") as f:
            f.write(b"extra-bytes")
        ok, errors, _ = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertTrue(any("mismatch" in e for e in errors))

    def test_corrupt_sqlite_file_fails_integrity_check(self):
        with open(os.path.join(self.staging, "signals.db"), "wb") as f:
            f.write(os.urandom(2048))
        # Rebuild manifest to match the new (corrupt) file's checksum/size,
        # isolating the integrity-check failure from a checksum failure.
        manifest = build_manifest("data-test-2", "commit-x", self.staging)
        with open(os.path.join(self.staging, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        ok, errors, _ = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertTrue(any("integrity_check" in e for e in errors))

    def test_missing_required_table_detected(self):
        os.remove(os.path.join(self.staging, "signals.db"))
        with sqlite3.connect(os.path.join(self.staging, "signals.db")) as conn:
            conn.execute("CREATE TABLE some_other_table (x INTEGER)")
        manifest = build_manifest("data-test-3", "commit-x", self.staging)
        with open(os.path.join(self.staging, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        ok, errors, _ = verify_local_snapshot(self.staging)
        self.assertFalse(ok)
        self.assertTrue(any("required table" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
