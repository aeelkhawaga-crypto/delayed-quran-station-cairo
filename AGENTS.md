# AGENTS.md — Quran Radio (Delayed Rebroadcast Server)

Context for AI agents working in this repo. Read this first; it replaces a project walkthrough.

## What this project is

Records the **Cairo Quran Radio** stream 24/7 and rebroadcasts it as a live HLS
stream delayed by `DELAY_SECONDS` (default 7200 s = 2 h), so listeners in
Ireland hear it aligned to Irish local time (7:00 AM Cairo airs at 7:00 AM
Ireland). On top of the delay, the system **splices content into the delayed
window**: Irish Adhans are inserted at Irish prayer times, and the Cairo-broadcast
Adhans are suppressed (replaced with fillers).

Human-facing docs: `README.md` (English), `README.ar.md` (Arabic). Note: README
is partly stale — it describes an older "timeshift" 3-container design. The
current design (below) is 4 services with a **splicer that overwrites archive
segment files**; trust the code and this file over the README.

## Runtime architecture (docker-compose, 4 services)

| Service | Image/build | Role |
|---|---|---|
| `recorder` | `./recorder` (ffmpeg:6.1-alpine + python3) | `feed.py` pulls `STREAM_URL` and pipes de-duplicated MP3 frames into one long-lived ffmpeg; writes 10 s AAC `.ts` segments named by UTC timestamp (`%Y%m%d%H%M%S.ts`) + `index.m3u8` into `/archive` (= `./data/archive`). Rolling window `ARCHIVE_HOURS`. Auto-reconnects. flock-guarded so only one recorder writes. |
| `scheduler` | `./scheduler` | Rebuilds `/live/delayed.m3u8` every `LOOP_SECONDS` (2.5 s) from archive segments recorded exactly `DELAY_SECONDS` ago. Also pre-chunks adhan/filler/starter audio into whole-slot HLS chunksets under `/live-static` (in `data/static/`). Only emits a continuous archive sequence — no discontinuities. |
| `splicer` | `./scheduler` image, entrypoint `python3 /splice.py` | **The content-injection mechanism.** ~60 s before spliced content airs, it *overwrites the archive `.ts` segment files* inside the delayed window with adhan/filler/starter chunks (byte-for-byte, same filenames), PTS-aligned so timestamps stay continuous. Playlist never changes. |
| `web` | nginx:1.27-alpine | Serves `/live/delayed.m3u8` (no-store, CORS `*`), `/archive/*.ts` and `/live-static/*` (immutable cache), and the player page `/` (hls.js + Irish clock) on port **8080**. |

All containers run `TZ: UTC` — everything internally is UTC epochs; Dublin/Cairo
offsets are applied in code.

## Data flow

```
STREAM_URL ──curl──> ffmpeg ──> data/archive/YYYYMMDDHHMMSS.ts (+ index.m3u8)
                                       │
                 ┌─────────────────────┼──────────────────────┐
                 │ (ro)                │ (rw — splicer rewrites │
                 │                     │  segment files in the  │
                 │                     │  delayed window)       │
                 ▼                     ▼                        ▼
        scheduler.py builds     splice.py overwrites      web (nginx) serves
        data/live/delayed.m3u8  slots with adhan/filler   /live/delayed.m3u8
        (pure playlist,         /starter chunks from      + /archive/*.ts
        archive URIs only)      data/static/<kind>-N/     to players
```

## Splicing model (understand this before touching scheduler/splice)

- Segments sit on a 10 s UTC grid (filenames are grid timestamps). The delayed
  window is `[now − DELAY − 30s, now − DELAY]`.
- An event (Irish Adhan) or suppression window (Cairo Adhan) maps to archive
  slots; `splice.py:replace_slots` copies pre-chunked segments over those files
  `LEAD=60 s` before they air (players only fetch a slot in the ~30 s before it
  airs, so no client holds stale bytes).
- Chunksets are retimed with `atempo` to exact multiples of `SEG` so every
  replaced slot is exactly one full segment; true durations are shared with the
  scheduler via `schedule/.splice-durs.json`.
- Spliced chunks are remuxed (`-output_ts_offset`) so PTS continues seamlessly
  from the previous archive segment — timestamp resets stall players.
- If an Irish Adhan and a Cairo window are within `MIN_GAP=240 s`, **neither**
  is spliced (normal stream plays).
- Accepted tradeoff: after an Irish Adhan, the delayed stream resumes from where
  the adhan ended — a content gap.

## Content sources

- `adhans/` — Irish Adhan audio (round-robin on each Irish event). Upload via
  the monitor's Media library (or drop files in); picked up within seconds.
- `fillers/` — played during Cairo Adhan suppression windows (round-robin per
  window). If empty, the Cairo Adhan stays audible.
- `adhan-prefixes/<Prayer>.mp3` — short per-prayer "starter" occupying slots
  *ending* exactly at the Irish adhan anchor, so the adhan starts on time.
  Filenames must match prayer names (Fajr/Dhuhr/Asr/Maghrib/Isha, case-insensitive).
- `schedule/irish-times.txt` — manual Irish Adhan timetable: `HH:MM` (daily),
  `Fri 18:30` (weekly), `2026-09-15 18:23` (one-off); optional trailing prayer
  name attaches a starter. All Europe/Dublin.
- `schedule/dublin-prayer-times.json` — Islamic Foundation of Ireland yearly
  timetable, `MM-DD` keys, reapplied annually with the current year's Dublin DST
  offset (`dublin_offset()` implements EU rules; no tzdata in the image).
- `schedule/cairo-times.json` — auto-generated cache of AlAdhan API timings
  (Cairo, method=5). Safe to delete; it refetches.

## Config (`.env`, created from `.env.example`)

`STREAM_URL`, `DELAY_SECONDS` (7200), `SEGMENT_SECONDS` (10), `ARCHIVE_HOURS`
(4, must exceed delay + headroom), `BITRATE`. Env vars consumed in code:
`PRAYER_METHOD` (5), `LOOP_SECONDS` (2.5), `WINDOW_PRE_SECONDS` (90),
`WINDOW_POST_SECONDS` (360), `MIN_GAP_SECONDS` (240).

## Gotchas / invariants

- **Always `docker compose up -d` after editing `.env`** — `restart` does not
  re-read it.
- **Exactly one recorder** may run (flock guards it, but stale duplicate
  containers can reappear after a Docker daemon restart due to
  `restart: unless-stopped` — `docker rm -f` extras; doubled audio is the symptom).
- Recorder uses `feed.py | ffmpeg` on purpose. The radio replays the last
  ~5.5 s (byte-identical MP3 frames) on every connect; `feed.py` finds the new
  connection's first 4 KB in the tail it already sent and skips the overlap,
  so reconnects neither repeat audio nor restart ffmpeg (continuous PTS and
  segment cadence). During outages it feeds real-time silent MP3 frames after
  an 8 s grace (a shorter drop is fully recovered from the replay), so
  segments keep coming. The healer skips holes under 30 s (HEAL_MIN_GAP_SECONDS).
- `data/` is gitignored. If deleted while containers run, recreate them with
  `docker compose up -d --force-recreate` (bind mounts).
- The stream only exists after the recorder has run for `DELAY_SECONDS`.
- DST: Egypt and Ireland shift on different dates; for a few weeks each March/
  October the offset is 1 h or 3 h — update `DELAY_SECONDS` then.
- Media folders (`adhans/`, `adhans-fajr/`, `fillers/`, `adhan-prefixes/`) are
  server-managed and gitignored (only `.gitkeep` is tracked). Manage them from
  the monitor's Media library (upload / trash / restore; deleted files go to
  `<folder>/.trash/`). The scheduler re-chunks changes in a background thread
  into a staging dir and swaps it in, so the splicer never sees a half pool.
  `adhans-raw/` (raw harvested cuts) is gitignored too.
- `EXT-X-MEDIA-SEQUENCE` must step by exactly 1 per segment (iOS AVPlayer /
  ExoPlayer track position by it). `scheduler.media_sequence` assigns numbers
  when names first enter the playlist, persisted in `live/.media-seq.json`.
  Never derive it from timestamps.
- Splicer finds slots by listing the archive dir, never `index.m3u8` (the
  index restarts empty with the recorder). The healer only fills holes that
  have not aired yet, never overlapping the next real segment; the splicer
  prunes archive files by *name* timestamp (`ARCHIVE_HOURS` + 30 min) because
  rewritten files have fresh mtimes.
- Hidden state files in `schedule/`: `.splice-runs.json`, `.splice-durs.json`,
  `.splicer-state.json`, `.scheduler-state.json`, `cairo-times.json`.
- **ffprobe csv quirk**: `-of csv=p=0 -show_entries packet=pts` emits trailing
  commas on some lines (first line always; every line on remuxed files). Parse
  robustly (strip commas) — a strict `int()` silently drops packets, which
  broke PTS alignment at adhan boundaries (fixed in `splice.py:_pts_list`).
  Also: `-output_ts_offset` rebases-to-zero rather than shifts on the
  jrottenberg ffmpeg 6.1 build; `_write_aligned` probes its output and
  self-corrects the offset, so never "simplify" that loop away.

## Host-side helpers (run on the server, not in compose)

- `scripts/watchdog.sh` — host cron every minute; if the newest `.ts` is older
  than 90 s, restarts the recorder (curl|ffmpeg can hang).
- `harvest/harvest-adhan.sh` — host cron every 5 min; cuts `[prayer−3min,
  prayer+7min]` from the archive into `adhans-raw/YYYY-MM-DD_<Prayer>.mp3` using
  a throwaway `docker run` of the recorder image. Hardcoded base `/opt/quran-radio`.

## Key files

- `scheduler/scheduler.py` — playlist builder + chunkset builder (single
  `tick()` loop). Imports shared helpers.
- `scheduler/splice.py` — `import scheduler as sch` (path hack
  `sys.path.insert(0, "/")`); reads events/windows from scheduler, does the
  file overwrites. `LEAD=60`, `EXPOSURE=3*SEG`.
- `recorder/record.sh` — ingest loop; lavfi: URLs are a test source.
- `web/index.html` — hls.js player, `liveSyncDurationCount: 3`.
- `nginx/nginx.conf` — caching/CORS rules per location.

## Monitor (read-only, password-protected)

`/monitor/` on the web port: nginx `auth_basic` (`monitor.htpasswd` in the repo
root on the server, gitignored — create it before `docker compose up`, or
Docker mounts an empty directory) proxied to `scheduler/monitor.py`, a
thread in the scheduler container (port 8001). Health cards (source feed via
`archive/.feeder.json`, recorder, playlist, splicer heartbeat, watchdog via
`schedule/.watchdog.json`, disk), a 4.5 h archive timeline and a "coming up"
list of the saved-but-not-aired content (adhan / starter / Cairo filler /
healed), each listenable via `/monitor/api/listen.m3u8`. Healed segments are
detected by mtime lag (> 90 s after their name time), not by the recorder index.

## Common operations

```sh
docker compose logs -f recorder scheduler splicer   # logs
docker compose up -d                                 # apply .env/config changes
docker compose up -d --build                         # after code changes
du -sh data/archive                                  # disk check
```
