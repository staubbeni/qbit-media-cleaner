# qbit-media-cleaner

> Automatically deletes hardlinked media files from your Radarr/Sonarr library when you remove a torrent and its data from qBittorrent — even though the files have been renamed.

## The Problem

When Radarr/Sonarr import a file using **hardlinks**, they:
1. Create a renamed copy in your media library (e.g. `Brooklyn Nine-Nine - S01E01 - Pilot WEBDL-1080p.mkv`)
2. Keep the original file in qBittorrent's download folder for continued seeding

Both entries point to the **same data** on disk (inode). When you delete the torrent + files in qBittorrent, the download-folder entry disappears but the **renamed library copy remains** with a link count of 1 — forever, unless you clean it up manually.

Standard "orphan cleaner" tools compare filenames, which doesn't work here because Radarr/Sonarr renamed the file on import.

## The Solution

`qbit-media-cleaner` works at the **filesystem level**, using **inodes** instead of filenames:

```
[qBit has file]  →  Service records inode  →  You delete torrent in qBit UI
     ↓                                                 ↓
 download/qbittorrent/tv/                    qBit deletes source file
   Brooklyn.Nine.Nine.S01.../               link count: 2 → 1
   brooklyn.nine.nine.s01e01...mkv
   inode = 1234567                                     ↓
                                    Service polls API, sees torrent gone
                                                       ↓
                                    Scans media/ for inode 1234567
                                    Finds Brooklyn Nine-Nine/Season 1/...mkv
                                                       ↓
                                              Deletes it ✓
                                    Season 1/ now empty → deleted ✓
```

### Two Features

| Feature | When it runs | What it does |
|---|---|---|
| **Orphan scan** | Once on startup | Finds video files with link count = 1 (qBit copy already gone) |
| **Real-time monitor** | Continuously | Watches the qBit API and deletes media when a torrent is removed |

---

## Requirements

- Docker + Docker Compose
- qBittorrent with the **WebUI enabled**
- Radarr/Sonarr configured to use **hardlinks** (not copies) when importing
- Media and download folders on the **same filesystem** (required for hardlinks to work)

---

## Quick Start

### 1. Clone / download the files

```bash
git clone https://github.com/yourname/qbit-media-cleaner
cd qbit-media-cleaner
```

### 2. Configure `docker-compose.yml`

Copy the template and fill in your values:

```bash
cp docker-compose.yml docker-compose.local.yml
nano docker-compose.local.yml
```

The values you **must** change:

| Variable | Description | Example |
|---|---|---|
| `QB_HOST` | Hostname or IP of your qBittorrent instance | `192.168.1.100` or `qbittorrent` |
| `QB_PORT` | qBittorrent WebUI port | `8080` |
| `QB_USER` | WebUI username | `admin` |
| `QB_PASS` | WebUI password | `your_password` |
| `MEDIA_DIRS` | Colon-separated media paths **inside the container** | `/media/tv:/media/movies` |
| `volumes` | Map your host paths to the container paths | See below |

#### Volume mapping example

```yaml
volumes:
  - /mnt/data/media/tv:/media/tv:rw
  - /mnt/data/media/movies:/media/movies:rw
```

The left side is the path **on your host**. The right side is what the container sees, and must match `MEDIA_DIRS`.

### 3. Start in dry-run mode (safe — nothing is deleted)

```bash
docker compose -f docker-compose.local.yml up --build
```

Watch the logs. On startup you'll see the orphan scan:

```
2026-03-03 19:50:00  INFO      ORPHAN SCAN — searching for link-count=1 video files…
2026-03-03 19:50:01  INFO        ORPHAN  [4.32 GB]  /media/tv/Brooklyn Nine-Nine/Season 1/Brooklyn Nine-Nine - S01E01 - Pilot WEBDL-1080p.mkv
2026-03-03 19:50:01  INFO        ORPHAN  [4.11 GB]  /media/tv/Brooklyn Nine-Nine/Season 1/Brooklyn Nine-Nine - S01E02 - The Tagger WEBDL-1080p.mkv
...
2026-03-03 19:50:05  INFO      Scan complete — 47 orphan(s) found (DRY-RUN, none deleted).
```

Then delete a torrent from qBittorrent and wait up to 60 seconds:

```
2026-03-03 19:51:02  INFO      Detected 1 removed torrent(s).
2026-03-03 19:51:02  INFO      Processing removed torrent [a3f82c1d…] (22 tracked file(s))
2026-03-03 19:51:02  INFO        ↳ hardlink: /media/tv/Brooklyn Nine-Nine/Season 1/Brooklyn Nine-Nine - S01E01 - Pilot WEBDL-1080p.mkv
2026-03-03 19:51:02  INFO          [DRY-RUN] would delete: /media/tv/Brooklyn Nine-Nine/Season 1/...mkv
```

### 4. Go live

Once you're happy with what the logs show, set `DRY_RUN: "false"` and restart:

```bash
docker compose -f docker-compose.local.yml up -d
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `QB_HOST` | `localhost` | qBittorrent WebUI host |
| `QB_PORT` | `8080` | qBittorrent WebUI port |
| `QB_USER` | `admin` | qBittorrent username |
| `QB_PASS` | `adminadmin` | qBittorrent password |
| `MEDIA_DIRS` | `/media` | Colon-separated media library paths (inside container) |
| `POLL_INTERVAL` | `60` | Seconds between qBit API polls |
| `SCAN_ORPHANS` | `true` | Run orphan scan on startup |
| `DRY_RUN` | `true` | Log only, don't delete anything |
| `LOG_LEVEL` | `INFO` | `DEBUG` for inode-level detail |

---

## Folder Cleanup

After deleting a video file, the service walks up the directory tree and removes any parent folder that contains **no remaining video files** — including leftover `.nfo`, artwork, and subtitles that Radarr/Sonarr created.

Example — deleting an entire season's torrent:

```
DELETED  /media/tv/Brooklyn Nine-Nine/Season 1/Brooklyn Nine-Nine - S01E01 - Pilot WEBDL-1080p.mkv
DELETED  /media/tv/Brooklyn Nine-Nine/Season 1/Brooklyn Nine-Nine - S01E02 - The Tagger WEBDL-1080p.mkv
... (all 22 episodes)
RMDIR    /media/tv/Brooklyn Nine-Nine/Season 1     ← no video files left, gone
# Season 2, 3, 4 still have content → /media/tv/Brooklyn Nine-Nine/ is kept ✓
# /media/tv/ is a configured root → never deleted ✓
```

---

## Networking

### Option A — Host networking (simplest)
Use `network_mode: host`. The container shares the host's network stack so it can reach qBittorrent by IP or `localhost`.

### Option B — Docker bridge network
If both this service and qBittorrent are Docker containers on the same network:

```yaml
services:
  qbit-media-cleaner:
    # remove network_mode: host
    networks:
      - medianet
    environment:
      QB_HOST: "qbittorrent"   # ← use the container name

networks:
  medianet:
    external: true
```

---

## Troubleshooting

**Service says "No media hardlinks found" for a torrent I just deleted**
- The inode snapshot happens on the *previous* poll cycle. If the container just started and you delete immediately, the torrent may not have been snapshotted yet. Wait one poll interval after starting.

**Orphan scan finds nothing but I know there are orphans**
- Set `LOG_LEVEL: DEBUG` and check that `MEDIA_DIRS` paths are correctly mounted and visible inside the container:
  ```bash
  docker exec qbit-media-cleaner ls /media/tv
  ```

**Permission denied when deleting**
- Make sure volumes are mounted `:rw` (read-write) and that the container user has write access to the media directory.

**I want to test a single file before going fully live**
- Set `DRY_RUN: false` and `SCAN_ORPHANS: false`, start the service, then delete one torrent from qBittorrent and observe the logs.

---

## How Hardlinks Work (Visual)

```
Before deletion:                     After qBit deletes its copy:

Disk inode 1234567                   Disk inode 1234567
    ↑           ↑                             ↑
[qBit file]  [media file]               [media file only]
link count = 2                          link count = 1  ← orphan
```

qbit-media-cleaner detects the drop to link count 1 (orphan scan) or the disappearance of the torrent from the API (real-time monitor), then deletes the surviving media copy.
