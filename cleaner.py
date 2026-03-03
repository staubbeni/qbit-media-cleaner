#!/usr/bin/env python3
"""
qbit-media-cleaner
------------------
Two features:

1. RETROACTIVE ORPHAN SCAN  (runs once on startup if SCAN_ORPHANS=true)
   Finds video files in the media library that have a hard-link count of 1,
   meaning their qBittorrent source file was already deleted.
   Safe because: .nfo / artwork / subtitles created by Sonarr/Radarr are also
   link-count-1, so we filter by VIDEO_EXTENSIONS to only touch media files.

2. REAL-TIME MONITORING  (runs continuously)
   Polls the qBittorrent API.  Snapshots every torrent's file inodes while the
   files still exist.  When a torrent disappears from the list, looks up the
   stored inodes in the media library and deletes the matching (renamed) files.
"""

import os
import time
import logging
import signal
import sys
import shutil
from pathlib import Path

import qbittorrentapi

# ── Config ─────────────────────────────────────────────────────────────────────
QB_HOST        = os.environ.get("QB_HOST", "localhost")
QB_PORT        = int(os.environ.get("QB_PORT", "8080"))
QB_USER        = os.environ.get("QB_USER", "admin")
QB_PASS        = os.environ.get("QB_PASS", "adminadmin")

# Colon-separated list of media library roots (Sonarr/Radarr import targets)
MEDIA_DIRS     = [d.strip() for d in os.environ.get(
                    "MEDIA_DIRS",
                    "/media/tv:/media/movies"
                 ).split(":") if d.strip()]

POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "60"))   # seconds
DRY_RUN        = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
SCAN_ORPHANS   = os.environ.get("SCAN_ORPHANS", "true").lower() in ("1", "true", "yes")
LOG_LEVEL      = os.environ.get("LOG_LEVEL", "INFO").upper()

# Only these extensions are considered "media" during the orphan scan.
# Sonarr/Radarr-generated .nfo, .jpg, .png, .srt, etc. are intentionally excluded
# because they are created fresh (link count = 1 by design) and must NOT be deleted.
VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".flv", ".webm",
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("qbit-cleaner")

# ── Graceful shutdown ──────────────────────────────────────────────────────────
running = True
def _shutdown(sig, frame):
    global running
    log.info("Shutdown signal received, stopping…")
    running = False

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


# ══════════════════════════════════════════════════════════════════════════════
#  FEATURE 1 — Retroactive orphan scan
# ══════════════════════════════════════════════════════════════════════════════

def scan_orphaned_media():
    """
    Walk the media directories and find video files whose hard-link count
    has dropped to 1, meaning the qBittorrent source was already deleted.

    In DRY_RUN mode: only logs what would be deleted.
    """
    log.info("─" * 60)
    log.info("ORPHAN SCAN — searching for link-count=1 video files…")
    if DRY_RUN:
        log.warning("DRY-RUN ON — nothing will be deleted during scan.")

    found = 0
    deleted = 0

    for base in MEDIA_DIRS:
        if not os.path.isdir(base):
            log.warning("Media dir not found: %s", base)
            continue

        for root, _dirs, files in os.walk(base):
            for fname in files:
                ext = Path(fname).suffix.lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue                      # skip .nfo / artwork / subs

                fpath = os.path.join(root, fname)
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue

                if st.st_nlink == 1:
                    found += 1
                    size_gb = st.st_size / (1024 ** 3)
                    log.info("  ORPHAN  [%.2f GB]  %s", size_gb, fpath)
                    if not DRY_RUN:
                        if _delete_file(fpath):
                            deleted += 1

    if found == 0:
        log.info("No orphaned video files found.")
    else:
        if DRY_RUN:
            log.info("Scan complete — %d orphan(s) found (DRY-RUN, none deleted).", found)
        else:
            log.info("Scan complete — %d orphan(s) found, %d deleted.", found, deleted)
    log.info("─" * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  FEATURE 2 — Real-time monitoring via qBit API
# ══════════════════════════════════════════════════════════════════════════════

def _stat_inode(path: str) -> int | None:
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def _build_media_inode_map() -> dict[int, list[str]]:
    """Return {inode: [filepath, …]} for every file in the media library."""
    inode_map: dict[int, list[str]] = {}
    for base in MEDIA_DIRS:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    ino = os.stat(fpath).st_ino
                    inode_map.setdefault(ino, []).append(fpath)
                except OSError:
                    pass
    return inode_map


def _snapshot_torrent(qb: qbittorrentapi.Client, torrent) -> dict[int, str]:
    """Return {inode: filepath} for every file belonging to a torrent."""
    result: dict[int, str] = {}
    try:
        files     = qb.torrents_files(torrent_hash=torrent.hash)
        save_path = torrent.save_path.rstrip("/")
        for f in files:
            fpath = os.path.join(save_path, f.name)
            ino   = _stat_inode(fpath)
            if ino:
                result[ino] = fpath
    except Exception as exc:
        log.warning("Could not snapshot torrent '%s': %s", torrent.name, exc)
    return result


def _is_effectively_empty(path: Path) -> bool:
    """
    Returns True if 'path' contains no video files at any depth.
    Leftover .nfo / artwork / subtitles are intentionally ignored —
    if there are no video files the whole folder is considered empty.
    """
    for _root, _dirs, files in os.walk(str(path)):
        for fname in files:
            if Path(fname).suffix.lower() in VIDEO_EXTENSIONS:
                return False
    return True


def _cleanup_empty_parents(deleted_file: str) -> None:
    """
    Walk up the directory tree from the deleted file and remove every
    ancestor that is now effectively empty (no video files anywhere inside).
    Stops when it reaches a MEDIA_DIRS root so those are never deleted.
    """
    stop_dirs = {os.path.normpath(d) for d in MEDIA_DIRS}
    current = Path(deleted_file).parent

    while True:
        norm = os.path.normpath(str(current))
        if norm in stop_dirs:
            break                          # never delete the library roots
        if not current.is_dir():
            current = current.parent       # already gone, keep climbing
            continue
        if _is_effectively_empty(current):
            try:
                shutil.rmtree(str(current))
                log.info("    RMDIR    %s", current)
            except OSError as exc:
                log.warning("    Could not remove dir %s: %s", current, exc)
                break
            current = current.parent       # check the grandparent too
        else:
            break                          # still has video content, stop


def _delete_file(path: str) -> bool:
    """Delete a file, then recursively clean up any now-empty parent dirs."""
    try:
        os.remove(path)
        log.info("    DELETED  %s", path)
        _cleanup_empty_parents(path)
        return True
    except OSError as exc:
        log.error("    FAILED   %s  (%s)", path, exc)
        return False


def _connect(retries: int = 10, delay: int = 10) -> qbittorrentapi.Client:
    for attempt in range(1, retries + 1):
        try:
            qb = qbittorrentapi.Client(
                host=QB_HOST, port=QB_PORT,
                username=QB_USER, password=QB_PASS,
            )
            qb.auth_log_in()
            log.info("Connected to qBittorrent at %s:%s", QB_HOST, QB_PORT)
            return qb
        except Exception as exc:
            log.warning("Connection attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(delay)
    log.error("Could not connect after %d attempts. Exiting.", retries)
    sys.exit(1)


def monitor_loop():
    qb = _connect()

    # known_torrents: hash → {inode: download_filepath}
    # Built proactively so we have inodes BEFORE qBit deletes the files.
    known_torrents: dict[str, dict[int, str]] = {}

    log.info("Taking initial snapshot of all torrents…")
    for t in qb.torrents_info():
        known_torrents[t.hash] = _snapshot_torrent(qb, t)
    log.info("Tracking %d torrent(s). Polling every %ds.", len(known_torrents), POLL_INTERVAL)

    while running:
        time.sleep(POLL_INTERVAL)
        if not running:
            break

        try:
            try:
                current_list = list(qb.torrents_info())
            except qbittorrentapi.exceptions.NotLoggedInError:
                log.warning("Session expired, reconnecting…")
                qb = _connect()
                current_list = list(qb.torrents_info())

            current_hashes: set[str] = set()

            for t in current_list:
                current_hashes.add(t.hash)
                if t.hash not in known_torrents:
                    known_torrents[t.hash] = _snapshot_torrent(qb, t)
                    log.info("New torrent tracked: %s", t.name)
                else:
                    # Refresh snapshot (handles moves / re-checks)
                    fresh = _snapshot_torrent(qb, t)
                    if fresh:
                        known_torrents[t.hash] = fresh

            removed = set(known_torrents.keys()) - current_hashes
            if not removed:
                continue

            log.info("Detected %d removed torrent(s).", len(removed))
            media_map = _build_media_inode_map()

            for h in removed:
                stored = known_torrents.pop(h)
                log.info("Processing removed torrent [%s…] (%d tracked file(s))",
                         h[:8], len(stored))

                matched = 0
                for inode, dl_path in stored.items():
                    if inode not in media_map:
                        log.debug("  No media hardlink for inode %d (%s)", inode, dl_path)
                        continue
                    for media_path in media_map[inode]:
                        matched += 1
                        log.info("  ↳ hardlink: %s", media_path)
                        if DRY_RUN:
                            log.info("    [DRY-RUN] would delete: %s", media_path)
                        else:
                            _delete_file(media_path)

                if matched == 0:
                    log.info("  No media hardlinks found (never imported or already cleaned).")

        except Exception as exc:
            log.error("Unexpected error in poll loop: %s", exc, exc_info=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("qbit-media-cleaner starting")
    log.info("  Media dirs    : %s", MEDIA_DIRS)
    log.info("  Poll interval : %ds", POLL_INTERVAL)
    log.info("  Dry-run       : %s", DRY_RUN)
    log.info("  Orphan scan   : %s", SCAN_ORPHANS)
    log.info("=" * 60)

    if DRY_RUN:
        log.warning("DRY-RUN is ON — no files will actually be deleted.")

    # Feature 1: clean up files already orphaned before this service started
    if SCAN_ORPHANS:
        scan_orphaned_media()

    # Feature 2: watch for future deletions
    monitor_loop()


if __name__ == "__main__":
    main()
