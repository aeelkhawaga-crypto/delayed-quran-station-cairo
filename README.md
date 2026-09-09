# Quran Radio — Delayed Rebroadcast Server

[النسخة العربية](README.ar.md)

Records the Cairo Quran Radio stream 24/7 and rebroadcasts it as a live HLS
stream delayed by exactly 2 hours, so listeners in Ireland hear the broadcast
aligned to Irish local time (what aired at 7:00 AM Cairo time plays at 7:00 AM
Ireland time).

## How it works

Three containers (docker-compose):

| Service   | Role |
|-----------|------|
| `recorder` | ffmpeg ingests the stream and writes 10 s audio segments with UTC timestamp filenames into a rolling archive (default: keep 4 h). Auto-reconnects on network drops. |
| `scheduler` | Rebuilds `/live/delayed.m3u8` every few seconds from segments recorded exactly `DELAY_SECONDS` ago, and handles Adhan events (see below). Players see an ordinary live HLS stream. |
| `web` | nginx serves the delayed playlist, the archived segments, the Adhan/filler chunks, and a player page at `/` (hls.js, big play button, Irish clock). |

## Setup

1. Install Docker with the compose plugin on the server.
2. `cp .env.example .env` and set `STREAM_URL` to the Cairo channel's stream URL.
3. `docker compose up -d`
4. Open `http://<server-ip>:8080/` and press Play. External players can use
   `http://<server-ip>:8080/live/delayed.m3u8` directly.

To expose it on the standard port, change the port mapping in
`docker-compose.yml` to `"80:80"`.

The stream only appears after the recorder has run for `DELAY_SECONDS` (default
2 hours). Until then the player just waits — by design.

## Configuration (`.env`)

- `STREAM_URL` — the source stream (HLS `.m3u8`, shoutcast MP3/AAC, or any ffmpeg-readable URL)
- `DELAY_SECONDS` — default `7200` (2 hours)
- `SEGMENT_SECONDS` — segment length, default `10`
- `ARCHIVE_HOURS` — rolling archive size; must exceed `DELAY_SECONDS` (default `4`)
- `BITRATE` — rebroadcast audio bitrate, default `128k`

Disk usage is tiny: ~60 MB/hour at 128 kbps, so a 4-hour archive is ~250 MB.

## Timezone note

Cairo is 2 hours ahead of Ireland for most of the year, which is why the
default delay is exactly 2 hours. However, Egypt and Ireland change clocks on
different dates, so for a few days in late March–April the difference is 1 hour,
and for a few days in late October it is 3 hours. During those weeks, update
`DELAY_SECONDS` in `.env` and run `docker compose up -d` to apply.

## Adhan & fillers

The Cairo station broadcasts the Adhan 5 times a day at **Cairo** prayer
times. Because of the 2 h delay, each Adhan would air at the same clock time
in Ireland. The scheduler changes that:

- **Hidden Cairo Adhans** (timings from the [AlAdhan API](https://aladhan.com/prayer-times-api),
  `method=5`): when the delayed stream would air one, it plays the audio
  files from `fillers/` on rotation for the whole ~7-minute window instead,
  then resumes. If `fillers/` is empty, the Adhan is skipped entirely.
- **Irish Adhans**: at each time listed in `schedule/irish-times.txt`
  (daily `HH:MM`, weekly `Fri 18:30`, or exact `2026-09-15 18:23`, all in
  Europe/Dublin time), it plays one file from `adhans/` (round-robin), then
  resumes the delayed stream. The gap in the delayed content afterwards is
  expected.

To activate: drop mp3s into `adhans/` and `fillers/` (no restart needed —
the scheduler picks them up within seconds), and edit `schedule/irish-times.txt`.

### Harvesting the Cairo Adhan cuts

The station repeats the same Adhan recordings daily, so one capture per
prayer is enough. `harvest/harvest-adhan.sh` (run every 5 min via cron) cuts
`[prayer−3min, prayer+7min]` out of the rolling archive into
`adhans-raw/YYYY-MM-DD_<Prayer>.mp3` shortly after each Cairo prayer time.
Clean/trim those, then copy the final versions into `adhans/` (and use
`fillers/` for anything you want aired during the hidden Cairo windows).

Cron entry:

```
*/5 * * * * root /opt/quran-radio/harvest/harvest-adhan.sh >> /var/log/adhan-harvest.log 2>&1
```

## Operations

- Logs: `docker compose logs -f recorder` (or `timeshift`, `web`)
- Change config: edit `.env`, then `docker compose up -d`
- Disk check: `du -sh data/archive`

### Important

- **Always use `docker compose up -d` after editing `.env`** — `docker compose
  restart` does *not* re-read the environment file, so your changes silently
  won't apply.
- **Exactly one recorder must run.** Two recorders writing to the same archive
  record the same broadcast twice under different names, and the delayed
  playlist will play both copies slightly offset — this sounds like
  overlapping/doubled voices. A lock file guards against this on Linux
  servers; check with `docker ps` that only one `quran-recorder-1` exists
  (stale duplicates can reappear after a Docker daemon restart, thanks to
  `restart: unless-stopped` — remove them with `docker rm -f`).
- **Echo or doubled audio at the listener side** is almost always two players
  open at once (e.g. the web page in one tab and VLC in another, or two
  tabs) playing the same delayed stream out of sync. Close one.
- If you delete `data/` while the containers are running, recreate them
  afterwards (`docker compose up -d --force-recreate`) so the bind mounts
  pick up the new directory.

## How the audio path works

The recorder pulls the stream with `curl` through a pipe into ffmpeg. A byte
stream cannot seek backwards, which avoids a known ffmpeg behaviour when
reading HTTP MP3 directly: after network stalls the demuxer can resync by
re-reading buffered data, briefly duplicating audio. Segment durations in the
delayed playlist come from the HLS muxer itself (exact `#EXTINF` values), so
players don't drift or skip.

## Optional: HTTPS with Caddy

For a proper public link, put [Caddy](https://caddyserver.com) in front:

```
radio.example.com {
    reverse_proxy localhost:8080
}
```

Caddy obtains and renews the TLS certificate automatically.
