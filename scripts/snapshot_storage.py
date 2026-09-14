"""
Release-based durable storage for data/prices.db and data/signals.db.

A "snapshot" is an immutable, uniquely-tagged bundle of:
  - prices.db, signals.db (consistent point-in-time copies)
  - manifest.json: {snapshot_id, created_at, source_commit, latest_price_date,
                     files: {name: {sha256, size_bytes}}}

Snapshots are never overwritten or deleted once published. A separate,
mutable "pointer" record is the only thing that changes day to day; it is
only advanced after a new snapshot has been uploaded AND verified by
downloading it back and re-checking checksums/integrity. Restoring always
downloads into a temporary directory and validates before installing.

Two backends implement the same interface:
  - GhReleaseSnapshotBackend: real backend, GitHub Releases via the `gh` CLI.
    Used in production (the daily workflow, Codespaces bootstrap/restore).
  - LocalDirSnapshotBackend: a local-filesystem test double used only by the
    test suite, so the round-trip/failure-mode logic can be exercised
    quickly and offline, without publishing anything to GitHub.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

SNAPSHOT_FILES = ("prices.db", "signals.db")
MANIFEST_FILENAME = "manifest.json"
POINTER_TAG_PREFIX = "data-pointer-"
POINTER_FILENAME = "pointer.json"
REQUIRED_TABLES = {
    "prices.db": ["prices"],
    "signals.db": ["early_mover_signals"],
}
MIN_EXPECTED_BYTES = {
    "prices.db": 1024,   # a real prices.db is tens of MB; guard against a near-empty file
    "signals.db": 1024,
}


class SnapshotError(Exception):
    """Raised for any condition that must hard-fail the caller (never a silent empty DB)."""


def compute_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_integrity_ok(path):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            return result is not None and result[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _sqlite_has_tables(path, required_tables):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            existing = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            return set(required_tables).issubset(existing)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def get_latest_price_date(prices_db_path):
    conn = sqlite3.connect(f"file:{prices_db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    finally:
        conn.close()


def make_snapshot_id(source_commit):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_commit = (source_commit or "unknown")[:12]
    return f"data-{stamp}-{short_commit}"


def make_pointer_tag():
    """
    A monotonically-increasing (lexically-sortable) tag for a new,
    immutable pointer release. Never reused: each pointer update creates a
    brand-new release rather than mutating an existing one, so a failed
    update can never destroy access to the previously-verified pointer.
    """
    return f"{POINTER_TAG_PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}Z"


def backup_databases(prices_db_path, signals_db_path, dest_dir):
    """
    Copy both databases into dest_dir using SQLite's backup API, so the
    snapshot reflects a consistent point-in-time copy even if something
    else happens to be reading the source concurrently. This is the point
    where "writers are stopped": the caller must not be running the
    fetcher (or anything else that writes) while this executes.
    """
    for name, src_path in ((SNAPSHOT_FILES[0], prices_db_path), (SNAPSHOT_FILES[1], signals_db_path)):
        if not os.path.exists(src_path):
            raise SnapshotError(f"Cannot snapshot missing source database: {src_path}")
        src = sqlite3.connect(src_path)
        try:
            dst = sqlite3.connect(os.path.join(dest_dir, name))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()


def build_manifest(snapshot_id, source_commit, staging_dir):
    latest_price_date = get_latest_price_date(os.path.join(staging_dir, "prices.db"))
    files = {}
    for name in SNAPSHOT_FILES:
        path = os.path.join(staging_dir, name)
        files[name] = {
            "sha256": compute_sha256(path),
            "size_bytes": os.path.getsize(path),
        }
    return {
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": source_commit,
        "latest_price_date": latest_price_date,
        "files": files,
    }


def verify_local_snapshot(dir_path):
    """
    Validate a downloaded (or freshly-staged) snapshot directory: manifest
    present and parseable, each file's checksum/size matches the manifest,
    each database passes PRAGMA integrity_check, and each database has its
    required tables. Returns (ok, errors, manifest_or_None).
    """
    errors = []
    manifest_path = os.path.join(dir_path, MANIFEST_FILENAME)
    if not os.path.exists(manifest_path):
        return False, [f"missing {MANIFEST_FILENAME}"], None

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        return False, [f"unreadable/invalid {MANIFEST_FILENAME}: {e}"], None

    files = manifest.get("files", {})
    for name in SNAPSHOT_FILES:
        path = os.path.join(dir_path, name)
        expected = files.get(name)
        if not expected:
            errors.append(f"manifest missing entry for {name}")
            continue
        if not os.path.exists(path):
            errors.append(f"{name} is missing from snapshot")
            continue

        size = os.path.getsize(path)
        if size < MIN_EXPECTED_BYTES.get(name, 1):
            errors.append(f"{name} is suspiciously small ({size} bytes)")
        if size != expected.get("size_bytes"):
            errors.append(f"{name} size mismatch: expected {expected.get('size_bytes')}, got {size}")

        actual_sha256 = compute_sha256(path)
        if actual_sha256 != expected.get("sha256"):
            errors.append(f"{name} checksum mismatch")
            continue  # corrupted; skip further checks on this file

        if not _sqlite_integrity_ok(path):
            errors.append(f"{name} failed PRAGMA integrity_check")
        required = REQUIRED_TABLES.get(name, [])
        if required and not _sqlite_has_tables(path, required):
            errors.append(f"{name} is missing required table(s): {required}")

    return (len(errors) == 0), errors, manifest


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class GhReleaseSnapshotBackend:
    """Real backend: GitHub Releases via the `gh` CLI. Snapshot releases are
    immutable and never overwritten. Pointer updates publish a brand-new,
    uniquely-tagged release rather than mutating one in place, and go
    through draft -> upload -> validate -> publish so a release is only
    ever visible to restore (get_pointer) once it is known-complete;
    get_pointer() also skips any draft or otherwise-incomplete pointer
    release and falls back to the newest valid one."""

    def __init__(self, repo=None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY")

    def _run(self, args, **kwargs):
        cmd = ["gh"] + args
        if self.repo:
            cmd += ["--repo", self.repo]
        return subprocess.run(cmd, capture_output=True, text=True, **kwargs)

    def release_exists(self, tag):
        result = self._run(["release", "view", tag])
        return result.returncode == 0

    def upload(self, local_dir, tag):
        if self.release_exists(tag):
            raise SnapshotError(f"Refusing to overwrite existing snapshot release {tag!r}")
        asset_paths = [os.path.join(local_dir, name)
                        for name in (*SNAPSHOT_FILES, MANIFEST_FILENAME)]
        result = self._run(["release", "create", tag, *asset_paths,
                             "--title", tag, "--notes", "Automated Yu-Gi-Oh price database snapshot"])
        if result.returncode != 0:
            raise SnapshotError(f"gh release create failed for {tag!r}: {result.stderr.strip()}")

    def download(self, tag, dest_dir):
        result = self._run(["release", "download", tag, "--dir", dest_dir, "--clobber"])
        if result.returncode != 0:
            raise SnapshotError(f"gh release download failed for {tag!r}: {result.stderr.strip()}")

    def _list_pointer_releases(self):
        """All pointer releases (including drafts), as [{tag, is_draft}], tag-sorted."""
        result = self._run(["release", "list", "--json", "tagName,isDraft", "-L", "1000"])
        if result.returncode != 0:
            raise SnapshotError(f"Failed to list releases: {result.stderr.strip()}")
        try:
            entries = json.loads(result.stdout)
        except ValueError as e:
            raise SnapshotError(f"Could not parse release list: {e}")
        pointer_entries = [
            {"tag": e["tagName"], "is_draft": bool(e.get("isDraft"))}
            for e in entries if e.get("tagName", "").startswith(POINTER_TAG_PREFIX)
        ]
        return sorted(pointer_entries, key=lambda e: e["tag"])

    def list_pointer_tags(self):
        """All pointer tags, including drafts/incomplete ones -- diagnostics/tests only."""
        return [e["tag"] for e in self._list_pointer_releases()]

    def _create_draft_pointer(self, pointer_tag):
        result = self._run(["release", "create", pointer_tag, "--draft",
                             "--title", pointer_tag, "--notes", "Pointer to a verified DB snapshot"])
        if result.returncode != 0:
            raise SnapshotError(f"Failed to create draft pointer release {pointer_tag!r}: {result.stderr.strip()}. "
                                 "The previous published pointer (if any) is untouched and still restorable.")

    def _upload_pointer_asset(self, pointer_tag, pointer_path):
        result = self._run(["release", "upload", pointer_tag, pointer_path])
        if result.returncode != 0:
            raise SnapshotError(
                f"Failed to upload pointer.json to draft release {pointer_tag!r}: {result.stderr.strip()}. "
                "Left as an incomplete draft (ignored by restore); the previous published pointer is untouched.")

    def _download_and_check_pointer(self, pointer_tag, expected_snapshot_tag):
        with tempfile.TemporaryDirectory() as verify_tmp:
            result = self._run(["release", "download", pointer_tag, "--dir", verify_tmp, "--clobber"])
            if result.returncode != 0:
                raise SnapshotError(
                    f"Failed to verify draft pointer release {pointer_tag!r} after upload: {result.stderr.strip()}. "
                    "Left as an incomplete draft (ignored by restore).")
            try:
                with open(os.path.join(verify_tmp, POINTER_FILENAME)) as f:
                    downloaded = json.load(f)
            except (OSError, ValueError) as e:
                raise SnapshotError(f"Draft pointer release {pointer_tag!r} asset is invalid after upload: {e}. "
                                     "Left as an incomplete draft (ignored by restore).")
        if downloaded.get("latest_snapshot_tag") != expected_snapshot_tag:
            raise SnapshotError(f"Draft pointer release {pointer_tag!r} content mismatch after upload. "
                                 "Left as an incomplete draft (ignored by restore).")

    def _publish_pointer(self, pointer_tag):
        result = self._run(["release", "edit", pointer_tag, "--draft=false"])
        if result.returncode != 0:
            raise SnapshotError(
                f"Uploaded and validated pointer release {pointer_tag!r} but failed to publish it "
                f"(remains a draft, ignored by restore): {result.stderr.strip()}")

    def set_pointer(self, tag):
        """
        Publish a brand-new pointer release through draft -> upload ->
        validate -> publish, so it only becomes visible to restore
        (get_pointer) once fully complete. Never touches any previous
        pointer release -- if any phase fails, the release is left as an
        (ignored) draft or simply never created, and the previously
        published pointer remains completely intact and resolvable.
        """
        new_pointer_tag = make_pointer_tag()
        with tempfile.TemporaryDirectory() as tmp:
            pointer_path = os.path.join(tmp, POINTER_FILENAME)
            with open(pointer_path, "w") as f:
                json.dump({"latest_snapshot_tag": tag,
                           "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)

            self._create_draft_pointer(new_pointer_tag)
            self._upload_pointer_asset(new_pointer_tag, pointer_path)
            self._download_and_check_pointer(new_pointer_tag, tag)
            self._publish_pointer(new_pointer_tag)

    def get_pointer(self):
        """
        Return the snapshot tag of the newest PUBLISHED (non-draft) pointer
        release whose asset is actually readable. Drafts and incomplete/
        unreadable pointer releases are ignored, falling back to older
        published ones rather than failing outright.
        """
        entries = self._list_pointer_releases()
        published_tags = sorted((e["tag"] for e in entries if not e["is_draft"]))
        if not published_tags:
            raise SnapshotError("No published pointer release found -- no snapshot has ever been published.")

        errors = []
        for pointer_tag in reversed(published_tags):  # newest first
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    result = self._run(["release", "download", pointer_tag, "--dir", tmp, "--clobber"])
                    if result.returncode != 0:
                        errors.append(f"{pointer_tag}: download failed: {result.stderr.strip()}")
                        continue
                    pointer_path = os.path.join(tmp, POINTER_FILENAME)
                    with open(pointer_path) as f:
                        data = json.load(f)
                snapshot_tag = data.get("latest_snapshot_tag")
                if snapshot_tag:
                    return snapshot_tag
                errors.append(f"{pointer_tag}: missing latest_snapshot_tag")
            except (OSError, ValueError) as e:
                errors.append(f"{pointer_tag}: unreadable ({e})")

        raise SnapshotError(f"All published pointer releases were incomplete or unreadable: {errors}")


class LocalDirSnapshotBackend:
    """Test-only double: stores 'releases' as directories under a local
    root. Never used in production -- production always goes through
    GhReleaseSnapshotBackend so snapshots are actually durable outside Git."""

    def __init__(self, root_dir):
        self.root_dir = root_dir
        os.makedirs(root_dir, exist_ok=True)

    def _tag_dir(self, tag):
        return os.path.join(self.root_dir, tag)

    def release_exists(self, tag):
        return os.path.isdir(self._tag_dir(tag))

    def upload(self, local_dir, tag):
        if self.release_exists(tag):
            raise SnapshotError(f"Refusing to overwrite existing snapshot release {tag!r}")
        tag_dir = self._tag_dir(tag)
        os.makedirs(tag_dir)
        for name in (*SNAPSHOT_FILES, MANIFEST_FILENAME):
            shutil.copy2(os.path.join(local_dir, name), os.path.join(tag_dir, name))

    def download(self, tag, dest_dir):
        tag_dir = self._tag_dir(tag)
        if not os.path.isdir(tag_dir):
            raise SnapshotError(f"Snapshot release {tag!r} not found")
        for name in (*SNAPSHOT_FILES, MANIFEST_FILENAME):
            src = os.path.join(tag_dir, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dest_dir, name))

    def _draft_marker(self, tag):
        return os.path.join(self._tag_dir(tag), ".draft")

    def _create_draft_pointer(self, pointer_tag):
        pointer_dir = self._tag_dir(pointer_tag)
        os.makedirs(pointer_dir)
        open(self._draft_marker(pointer_tag), "w").close()

    def _upload_pointer_asset(self, pointer_tag, pointer_path):
        shutil.copy2(pointer_path, os.path.join(self._tag_dir(pointer_tag), POINTER_FILENAME))

    def _download_and_check_pointer(self, pointer_tag, expected_snapshot_tag):
        path = os.path.join(self._tag_dir(pointer_tag), POINTER_FILENAME)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise SnapshotError(f"Draft pointer {pointer_tag!r} asset is invalid after upload: {e}")
        if data.get("latest_snapshot_tag") != expected_snapshot_tag:
            raise SnapshotError(f"Draft pointer {pointer_tag!r} content mismatch after upload.")

    def _publish_pointer(self, pointer_tag):
        os.remove(self._draft_marker(pointer_tag))

    def set_pointer(self, tag):
        """Publish a brand-new pointer through the same draft -> upload ->
        validate -> publish phases as GhReleaseSnapshotBackend, so tests can
        exercise failures at each phase offline."""
        new_pointer_tag = make_pointer_tag()
        with tempfile.TemporaryDirectory() as tmp:
            pointer_path = os.path.join(tmp, POINTER_FILENAME)
            with open(pointer_path, "w") as f:
                json.dump({"latest_snapshot_tag": tag,
                           "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)

            self._create_draft_pointer(new_pointer_tag)
            self._upload_pointer_asset(new_pointer_tag, pointer_path)
            self._download_and_check_pointer(new_pointer_tag, tag)
            self._publish_pointer(new_pointer_tag)

    def list_pointer_tags(self):
        """All pointer tags, including drafts/incomplete ones -- diagnostics/tests only."""
        return sorted(
            name for name in os.listdir(self.root_dir)
            if name.startswith(POINTER_TAG_PREFIX) and os.path.isdir(self._tag_dir(name))
        )

    def get_pointer(self):
        published_tags = sorted(t for t in self.list_pointer_tags() if not os.path.exists(self._draft_marker(t)))
        if not published_tags:
            raise SnapshotError("No published pointer found (no snapshot has ever been published, "
                                 "or all pointer releases are still draft/incomplete).")

        errors = []
        for pointer_tag in reversed(published_tags):  # newest first
            path = os.path.join(self._tag_dir(pointer_tag), POINTER_FILENAME)
            try:
                with open(path) as f:
                    data = json.load(f)
                snapshot_tag = data.get("latest_snapshot_tag")
                if snapshot_tag:
                    return snapshot_tag
                errors.append(f"{pointer_tag}: missing latest_snapshot_tag")
            except (OSError, ValueError) as e:
                errors.append(f"{pointer_tag}: unreadable ({e})")
        raise SnapshotError(f"All published pointer releases were incomplete or unreadable: {errors}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def create_and_publish_snapshot(backend, prices_db_path, signals_db_path, source_commit):
    """
    Stage a consistent copy of both databases, upload it as a brand-new,
    uniquely-tagged snapshot, download it back to verify the upload was
    complete and uncorrupted, and only then advance the pointer. Raises
    SnapshotError on any failure; never partially advances the pointer.
    """
    with tempfile.TemporaryDirectory(prefix="snapshot_stage_") as staging_dir:
        backup_databases(prices_db_path, signals_db_path, staging_dir)
        snapshot_id = make_snapshot_id(source_commit)
        manifest = build_manifest(snapshot_id, source_commit, staging_dir)
        with open(os.path.join(staging_dir, MANIFEST_FILENAME), "w") as f:
            json.dump(manifest, f, indent=2)

        backend.upload(staging_dir, snapshot_id)

        with tempfile.TemporaryDirectory(prefix="snapshot_verify_") as verify_dir:
            backend.download(snapshot_id, verify_dir)
            ok, errors, _ = verify_local_snapshot(verify_dir)
            if not ok:
                raise SnapshotError(
                    f"Snapshot {snapshot_id!r} uploaded but failed post-upload verification: {errors}. "
                    "Pointer was NOT advanced; the previous good snapshot remains current.")

        backend.set_pointer(snapshot_id)
        return manifest


def _install_verified_pair(temp_dir, prices_db_path, signals_db_path):
    """
    Install both databases from temp_dir over the real destination paths as
    an all-or-nothing pair: either both files end up as the new verified
    pair, or both remain the old pair -- never a mix of one new and one
    old. Any pre-existing destination file is preserved first as a
    timestamped, recoverable backup (never deleted).
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_paths = (prices_db_path, signals_db_path)
    backup_paths = {}

    for dest_path in dest_paths:
        dest_dir = os.path.dirname(dest_path)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)
        if os.path.exists(dest_path):
            backup_path = f"{dest_path}.bak.{stamp}"
            shutil.copy2(dest_path, backup_path)
            backup_paths[dest_path] = backup_path

    installed = []
    try:
        for name, dest_path in zip(SNAPSHOT_FILES, dest_paths):
            shutil.move(os.path.join(temp_dir, name), dest_path)
            installed.append(dest_path)
    except Exception as e:
        # Roll back any file we did manage to install, so callers (and any
        # concurrent readers) never observe a mixed old/new pair.
        for dest_path in installed:
            if dest_path in backup_paths:
                shutil.copy2(backup_paths[dest_path], dest_path)
            else:
                os.remove(dest_path)  # nothing existed before this restore attempt
        raise SnapshotError(
            f"Failed installing verified snapshot pair ({e}); rolled back to the previous pair. "
            f"Pre-restore backups preserved at: {list(backup_paths.values())}")


def restore_latest_snapshot(backend, prices_db_path, signals_db_path):
    """
    Restore the pointer's snapshot into a temporary directory, validate it
    completely, and only then install it over prices_db_path/signals_db_path
    as an atomic pair (see _install_verified_pair). Raises SnapshotError
    (never falls back to an empty database, and never leaves a mixed
    old/new pair) if the pointer is missing, the download fails, validation
    fails, or the pair installation fails partway.
    """
    tag = backend.get_pointer()

    with tempfile.TemporaryDirectory(prefix="snapshot_restore_") as temp_dir:
        backend.download(tag, temp_dir)
        ok, errors, manifest = verify_local_snapshot(temp_dir)
        if not ok:
            raise SnapshotError(f"Downloaded snapshot {tag!r} failed validation: {errors}")

        _install_verified_pair(temp_dir, prices_db_path, signals_db_path)

        return manifest
